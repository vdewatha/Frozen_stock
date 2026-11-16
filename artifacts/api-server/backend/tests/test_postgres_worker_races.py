"""PostgreSQL races between workers: dispatch claims, audit writers, bar upserts, job locks.

Each test runs the production function from several real sessions at once.
SQLite serializes every writer and has no advisory locks, so the SQLite suite
cannot show any of these behaviours.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Barrier, Event, Lock
from unittest.mock import patch
from uuid import uuid4

import pytest
import redis
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models import AuditLog, IntradayBar, IntradayNoTradeInterval, StockLearningWorkerDispatch
from app.services import intraday_data
from app.services.audit import write_audit_log
from app.services.operational_hardening import _audit_chain_check
from app.tasks import jobs
from tests.postgres_support import WAIT_SECONDS, backend_pid, result_or_fail, wait_until_blocked

pytestmark = pytest.mark.postgres


def _together(count: int) -> Barrier:
    return Barrier(count, timeout=WAIT_SECONDS)


# --- (c) two workers claiming one learning dispatch ---


def _dispatch_row(engine, **values) -> int:
    with Session(engine) as db:
        row = StockLearningWorkerDispatch(batch_id="batch-1", symbol="AAPL", strategy_slug="ensemble",
                                          **{"status": "published", "attempt_count": 0, **values})
        db.add(row)
        db.commit()
        return row.id


def _claim(engine, dispatch_id, task_id, pids=None, start=None):
    with Session(engine) as db:
        if pids is not None:
            pids[task_id] = backend_pid(db)
        if start is not None:
            start.wait()
        dispatch, status = jobs._claim_learning_dispatch(
            db, dispatch_id, task_id, expected_symbol="aapl", expected_strategy="ensemble",
        )
        return status, dispatch.task_id, dispatch.attempt_count


@pytest.mark.parametrize("prior", ["published", "expired_lease"])
def test_two_workers_claiming_one_learning_dispatch_admit_one(postgres_schema, prior):
    engine = postgres_schema.engine
    expired = prior == "expired_lease"
    dispatch_id = _dispatch_row(engine, **({
        "status": "running", "task_id": "lost-worker", "attempt_count": 1,
        "lease_expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
    } if expired else {}))
    pids = {}
    with Session(engine) as holder, ThreadPoolExecutor(max_workers=2) as pool:
        # Hold the row so both deliveries are queued on it before either reads it.
        holder.execute(text("SELECT id FROM stock_learning_worker_dispatches WHERE id = :id FOR UPDATE"),
                       {"id": dispatch_id})
        claims = {task: pool.submit(_claim, engine, dispatch_id, task, pids) for task in ("task-a", "task-b")}
        try:
            for task in claims:
                deadline = time.monotonic() + WAIT_SECONDS
                while task not in pids:
                    assert time.monotonic() < deadline, f"{task} never opened its transaction"
                    time.sleep(0.01)
                # The first waits on the holder; the second queues behind the first.
                wait_until_blocked(postgres_schema.owner, pids[task])
        finally:
            holder.rollback()
        results = {task: result_or_fail(future, f"Learning worker {task}") for task, future in claims.items()}

    assert sorted(status for status, _, _ in results.values()) == ["claimed", "in_progress"]
    winner = next(task for task, (status, _, _) in results.items() if status == "claimed")
    attempts = 2 if expired else 1
    # The loser read the winner's committed claim, not the row it queued on.
    assert set(results.values()) == {("claimed", winner, attempts), ("in_progress", winner, attempts)}
    with Session(engine) as db:
        row = db.get(StockLearningWorkerDispatch, dispatch_id)
        assert (row.status, row.task_id, row.attempt_count) == ("running", winner, attempts)
        assert row.lease_expires_at > datetime.now(timezone.utc)
        assert row.error_type == ("lease_expired" if expired else None)


def test_many_duplicate_deliveries_claim_a_learning_dispatch_once(postgres_schema):
    engine = postgres_schema.engine
    dispatch_id = _dispatch_row(engine)
    workers = 8
    start = _together(workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_claim, engine, dispatch_id, f"task-{index}", None, start) for index in range(workers)]
        results = [result_or_fail(future, "Learning worker") for future in futures]
    statuses = [status for status, _, _ in results]
    assert statuses.count("claimed") == 1 and statuses.count("in_progress") == workers - 1
    with Session(engine) as db:
        row = db.get(StockLearningWorkerDispatch, dispatch_id)
        assert row.attempt_count == 1
        assert {task for _, task, _ in results} == {row.task_id}


# --- (d) concurrent audit writers keep one hash chain ---


def test_concurrent_audit_writers_leave_one_unbroken_hash_chain(postgres_schema):
    engine = postgres_schema.engine
    writers, transactions = 8, 6
    start = _together(writers)

    def write(writer: int) -> int:
        committed = 0
        start.wait()
        for number in range(transactions):
            events = 1 + (writer + number) % 3  # one to three events in a transaction
            with Session(engine) as db:
                for index in range(events):
                    write_audit_log(
                        db, event_type="postgres_concurrency", action="write", status="complete",
                        message=f"writer {writer} transaction {number} event {index}",
                        entity_type="test", entity_id=writer,
                        payload={"writer": writer, "transaction": number, "event": index},
                    )
                if (writer + number) % 4 == 0:
                    # A writer that fails after hashing must not leave a gap or a fork.
                    db.rollback()
                else:
                    db.commit()
                    committed += events
        return committed

    with ThreadPoolExecutor(max_workers=writers) as pool:
        futures = [pool.submit(write, writer) for writer in range(writers)]
        expected = sum(result_or_fail(future, "Audit writer", timeout=WAIT_SECONDS * 4) for future in futures)

    with Session(engine) as db:
        rows = db.scalars(select(AuditLog).order_by(AuditLog.id)).all()
        assert len(rows) == expected and expected > writers
        previous = None
        for row in rows:
            assert row.previous_event_sha256 == previous, f"Audit chain forks at row {row.id}"
            previous = row.event_sha256
        # No digest is the predecessor of two events, and the app's own verifier agrees.
        assert len({row.previous_event_sha256 for row in rows}) == len(rows)
        check = _audit_chain_check(db)
        assert check["status"] == "clear", check
        assert check["details"]["event_count"] == expected
        assert {row.payload["writer"] for row in rows} == set(range(writers))


# --- (e) concurrent intraday upserts ---

SESSION_OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)  # Tuesday 09:30 New York
INGESTED_AT = SESSION_OPEN + timedelta(hours=3)


def _minute(offset: int) -> datetime:
    return SESSION_OPEN + timedelta(minutes=offset)


def _bars(offsets, close: float) -> list[dict]:
    return [{"time": _minute(offset).isoformat(), "open": 100, "high": 110, "low": 90, "close": close, "volume": 1000}
            for offset in offsets]


def _hold_inserts_until_all_read(engine, table: str, parties: int):
    """Make every party finish its "what is stored?" read before any of them inserts."""
    gate = _together(parties)

    def before_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(f"INSERT INTO {table.upper()}"):
            gate.wait()

    event.listen(engine, "before_cursor_execute", before_insert)
    return lambda: event.remove(engine, "before_cursor_execute", before_insert)


def test_two_collectors_storing_the_same_new_minutes_keep_one_row_each(postgres_schema):
    engine = postgres_schema.engine
    offsets = range(30)
    release = _hold_inserts_until_all_read(engine, "intraday_bars", 2)

    def collect(close: float, verified: bool):
        with Session(engine) as db:
            return intraday_data.upsert_intraday_bars(
                db, "SPY", _bars(offsets, close), ingested_at=INGESTED_AT,
                provider="alpaca_iex", feed_class="iex", verify_entitlement=verified,
            )["rows_imported"]

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            # An authenticated collector and an unauthenticated import of the same minutes.
            verified = pool.submit(collect, 101.0, True)
            unverified = pool.submit(collect, 102.0, False)
            assert result_or_fail(verified, "Verifying collector") == len(offsets)
            assert result_or_fail(unverified, "Non-verifying import") == len(offsets)
    finally:
        release()

    with Session(engine) as db:
        rows = db.query(IntradayBar).order_by(IntradayBar.opened_at).all()
        assert [row.opened_at for row in rows] == [_minute(offset) for offset in offsets]
        # Whichever statement ran second replaced every bar: no minute mixes the two fetches.
        assert len({row.close for row in rows}) == 1
        # In either order the authenticated observation survives.
        assert {row.last_verified_at for row in rows} == {INGESTED_AT}


def test_collectors_with_overlapping_windows_never_deadlock_or_duplicate(postgres_schema, rounds=5):
    engine = postgres_schema.engine
    windows = [range(0, 40), range(20, 60), range(10, 50), range(30, 70)]

    def collect(index: int, start: Barrier):
        start.wait()
        with Session(engine) as db:
            return intraday_data.upsert_intraday_bars(
                db, "SPY", _bars(windows[index], 100.0), ingested_at=INGESTED_AT + timedelta(seconds=index),
                provider="alpaca_iex", feed_class="iex", verify_entitlement=True,
            )["rows_imported"]

    for _ in range(rounds):
        # Later rounds mix re-verifying the stored bars with the upsert of new ones.
        start = _together(len(windows))
        with ThreadPoolExecutor(max_workers=len(windows)) as pool:
            futures = [pool.submit(collect, index, start) for index in range(len(windows))]
            assert [result_or_fail(future, "Intraday collector") for future in futures] == [len(w) for w in windows]
        with Session(engine) as db:
            db.query(IntradayBar).filter(IntradayBar.opened_at >= _minute(35)).delete()
            db.commit()
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(IntradayBar)) == 35
        assert db.scalar(select(func.count(func.distinct(IntradayBar.opened_at)))) == 35


def test_a_provider_correction_seen_by_one_of_two_collectors_is_stored_without_a_deadlock(postgres_schema):
    engine = postgres_schema.engine
    with Session(engine) as db:
        intraday_data.upsert_intraday_bars(
            db, "SPY", _bars(range(10), 100.0), ingested_at=INGESTED_AT,
            provider="alpaca_iex", feed_class="iex", verify_entitlement=True,
        )
    later = INGESTED_AT + timedelta(minutes=1)
    start = _together(2)

    def collect(bars):
        start.wait()
        with Session(engine) as db:
            return intraday_data.upsert_intraday_bars(
                db, "SPY", bars, ingested_at=later, provider="alpaca_iex", feed_class="iex", verify_entitlement=True,
            )["rows_imported"]

    # Both fetch minutes 0-10; only one fetch carries the late correction to minute 9.
    corrected = _bars(range(9), 100.0) + _bars([9], 101.0) + _bars([10], 100.0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(collect, corrected), pool.submit(collect, _bars(range(11), 100.0))]
        assert [result_or_fail(future, "Intraday collector") for future in futures] == [11, 11]
    with Session(engine) as db:
        rows = db.query(IntradayBar).order_by(IntradayBar.opened_at).all()
        assert [row.opened_at for row in rows] == [_minute(offset) for offset in range(11)]
        assert {row.last_verified_at for row in rows} == {later}


def test_two_collectors_verifying_the_same_no_trade_minutes_record_them_once(postgres_schema):
    engine = postgres_schema.engine
    with patch.object(intraday_data, "MARKET_DATA_PROVIDER", "alpaca_iex"), \
         patch.object(intraday_data, "MARKET_DATA_FEED_CLASS", "iex"), \
         patch.object(settings, "intraday_no_trade_max_run_minutes", 5):
        with Session(engine) as db:
            # IEX printed minutes 0 and 3; minutes 1 and 2 had no trade there.
            intraday_data.upsert_intraday_bars(
                db, "SPY", _bars([0, 3], 100.0), ingested_at=INGESTED_AT,
                provider="alpaca_iex", feed_class="iex", verify_entitlement=True,
            )
        release = _hold_inserts_until_all_read(engine, "intraday_no_trade_intervals", 2)

        def verify():
            with Session(engine) as db:
                return intraday_data.record_verified_no_trade_minutes(
                    db, "SPY", _minute(0), _minute(4), observed_at=INGESTED_AT,
                    provider="alpaca_iex", feed_class="iex",
                )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(verify) for _ in range(2)]
                results = [result_or_fail(future, "No-trade verification") for future in futures]
        finally:
            release()
    expected = [_minute(1).isoformat(), _minute(2).isoformat()]
    assert results == [expected, expected]
    with Session(engine) as db:
        markers = db.query(IntradayNoTradeInterval).order_by(IntradayNoTradeInterval.opened_at).all()
        assert [marker.opened_at for marker in markers] == [_minute(1), _minute(2)]


# --- (f) the job advisory lock admits one runner ---


def _lock_is_free(owner, job_name: str) -> bool:
    key = int.from_bytes(sha256(f"job:{job_name}".encode()).digest()[:8], "big") % 2_147_483_647
    with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        free = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        if free:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        return bool(free)


def test_job_advisory_lock_admits_one_of_many_simultaneous_runners(postgres_schema):
    engine = postgres_schema.engine
    job_name = "postgres_one_runner_" + uuid4().hex
    runners = 6
    start = _together(runners)
    inside, release, counter = Event(), Event(), Lock()
    ran = []

    def work(db):
        with counter:
            ran.append(backend_pid(db))
        db.commit()  # returns the session's connection to the pool; the lock must outlive it
        inside.set()
        assert release.wait(WAIT_SECONDS * 4), "The test never released the admitted runner"
        db.execute(text("SELECT 1"))
        db.commit()
        return {"status": "complete", "job": job_name}

    def run():
        start.wait()
        return jobs._run_job(job_name, work)

    with patch.object(jobs, "SessionLocal", sessionmaker(bind=engine)), \
         ThreadPoolExecutor(max_workers=runners) as pool:
        futures = [pool.submit(run) for _ in range(runners)]
        try:
            assert inside.wait(WAIT_SECONDS), "No runner was admitted"
            deadline = time.monotonic() + WAIT_SECONDS
            while sum(future.done() for future in futures) < runners - 1:
                assert time.monotonic() < deadline, "A refused runner waited instead of skipping"
                time.sleep(0.01)
            assert not _lock_is_free(postgres_schema.owner, job_name)
        finally:
            release.set()
        results = [result_or_fail(future, "Scheduled job runner") for future in futures]
        assert len(ran) == 1
        assert sorted(result["status"] for result in results) == ["complete"] + ["skipped"] * (runners - 1)
        assert {result["reason"] for result in results if result["status"] == "skipped"} == {
            "duplicate worker lease is active"
        }
        # The lock left with its runner: the next scheduled run is admitted.
        assert _lock_is_free(postgres_schema.owner, job_name)
        release.set()
        assert jobs._run_job(job_name, work)["status"] == "complete"
    assert len(ran) == 2


def test_advisory_lock_still_fences_a_job_whose_redis_lease_a_new_boot_released(postgres_schema, monkeypatch):
    """A deploy overlap: the new boot drops the old boot's Redis lease while the old run is alive.

    ``_release_previous_boot_lease`` relies on the advisory lock for exactly
    this; nothing else stops the second runner.
    """
    engine = postgres_schema.engine
    job_name = "postgres_boot_overlap_" + uuid4().hex
    inside, release = Event(), Event()
    ran = []

    def work(db):
        ran.append(os.environ.get("PAPER_BOOT_ID"))
        inside.set()
        assert release.wait(WAIT_SECONDS * 4), "The test never released the old boot's run"
        return {"status": "complete", "job": job_name}

    client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    key = f"trading:scheduled-job:{job_name}"
    try:
        with patch.object(jobs, "SessionLocal", sessionmaker(bind=engine)), \
             patch.object(jobs, "REDIS_LOCKED_JOBS", jobs.REDIS_LOCKED_JOBS | {job_name}), \
             ThreadPoolExecutor(max_workers=1) as pool:
            monkeypatch.setenv("PAPER_BOOT_ID", "boot-old")
            old_run = pool.submit(jobs._run_job, job_name, work)
            try:
                assert inside.wait(WAIT_SECONDS), "The old boot's run never started"
                assert client.get(key).decode().startswith("boot-old:")

                # Same boot: the Redis lease refuses the duplicate before the database is asked.
                same_boot = jobs._run_job(job_name, work)
                assert same_boot["reason"] == "duplicate scheduled job lease is active"

                # New boot: the old lease is dropped and retaken, and the advisory lock refuses.
                monkeypatch.setenv("PAPER_BOOT_ID", "boot-new")
                new_boot = jobs._run_job(job_name, work)
                assert new_boot == {"status": "skipped", "job": job_name, "reason": "duplicate worker lease is active"}
                assert client.get(key) is None, "The refused runner kept the Redis lease"
            finally:
                release.set()
            assert result_or_fail(old_run, "The old boot's run")["status"] == "complete"
        assert ran == ["boot-old"]
        assert _lock_is_free(postgres_schema.owner, job_name)
    finally:
        client.delete(key)
        client.close()
