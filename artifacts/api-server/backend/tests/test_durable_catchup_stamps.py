"""A restart must not re-run jobs that already ran (2026-11-10 boots).

The deployed Redis keeps nothing across a restart, so the catch-up's stamp
hash was empty at every boot: the 09:01, 12:09 and 16:47 ET boots re-queued
24, 24 and 18 jobs that had run on schedule. The audit log now carries a
durable copy of each catch-up entry's last success.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models.models import AuditLog
from app.tasks import catchup
from app.tasks.celery_app import celery_app
from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)
from tests.test_missed_job_catchup import FakeRedis

ET = ZoneInfo("America/New_York")
SCHEDULE = celery_app.conf.beat_schedule
# The 16:47 ET publish on Tuesday 2026-11-10 (after the close, so the
# session-deferred entries are eligible too).
BOOT = datetime(2026, 11, 10, 16, 48, tzinfo=ET).astimezone(timezone.utc)
ENGINE_TASK = SCHEDULE["experiment-engine"]["task"]


def _durable_all_ran(db, now):
    for name in catchup.CATCHUP_ENTRIES:
        catchup.record_durable_success(db, SCHEDULE[name]["task"], {"status": "complete"},
                                       now=now - timedelta(minutes=1))


def _lookup(db):
    return lambda task: catchup.durable_last_success(db, task)


def test_an_empty_redis_after_a_restart_reruns_everything_without_the_durable_stamp():
    queued = catchup.queue_missed_jobs(FakeRedis(), SCHEDULE, lambda *a, **k: None, now=BOOT)
    # The bug: a fresh Redis makes every recent firing look missed.
    assert len(queued) >= 15


def test_a_restart_reruns_nothing_that_already_ran(db):
    _durable_all_ran(db, BOOT)
    queued = catchup.queue_missed_jobs(FakeRedis(), SCHEDULE, lambda *a, **k: None, now=BOOT,
                                       durable_last_success=_lookup(db))
    assert [item["entry"] for item in queued if not item.get("one_off")] == []


def test_a_job_that_really_missed_its_slot_is_still_caught_up(db):
    _durable_all_ran(db, BOOT)
    # Discovery's last real run was the Sunday before last: this Sunday's 10:00 run was lost.
    discovery = SCHEDULE["strategy-discovery"]["task"]
    catchup.record_durable_success(db, discovery, {"status": "complete"},
                                   now=datetime(2026, 11, 1, 11, 0, tzinfo=ET))
    names = [item["entry"] for item in catchup.queue_missed_jobs(
        FakeRedis(), SCHEDULE, lambda *a, **k: None, now=BOOT, durable_last_success=_lookup(db))]
    assert "strategy-discovery" in names


def test_the_newer_of_the_two_stamps_counts(db):
    client = FakeRedis()
    for name in catchup.CATCHUP_ENTRIES:
        catchup.record_task_success(client, SCHEDULE[name]["task"], {"status": "complete"},
                                    now=BOOT - timedelta(days=9))
    _durable_all_ran(db, BOOT)
    queued = catchup.queue_missed_jobs(client, SCHEDULE, lambda *a, **k: None, now=BOOT,
                                       durable_last_success=_lookup(db))
    assert [item["entry"] for item in queued if not item.get("one_off")] == []


def test_failures_and_deferrals_are_not_written(db):
    assert not catchup.record_durable_success(db, ENGINE_TASK, {"status": "deferred"})
    assert not catchup.record_durable_success(db, ENGINE_TASK, {"status": "failed"})
    assert catchup.durable_last_success(db, ENGINE_TASK) is None
    assert catchup.record_durable_success(db, ENGINE_TASK, {"status": "complete"}, now=BOOT)
    assert catchup.durable_last_success(db, ENGINE_TASK) == BOOT
    row = db.scalar(select(AuditLog).where(AuditLog.event_type == catchup.DURABLE_EVENT_TYPE))
    assert row.action == ENGINE_TASK and row.payload["result_status"] == "complete"


def test_a_broken_durable_store_falls_back_to_redis():
    def broken(task):
        raise RuntimeError("database unavailable")

    client = FakeRedis()
    for name in catchup.CATCHUP_ENTRIES:
        catchup.record_task_success(client, SCHEDULE[name]["task"], {"status": "complete"},
                                    now=BOOT - timedelta(minutes=1))
    queued = catchup.queue_missed_jobs(client, SCHEDULE, lambda *a, **k: None, now=BOOT,
                                       durable_last_success=broken)
    assert [item["entry"] for item in queued if not item.get("one_off")] == []


def test_a_failing_catchup_is_queued_once_per_missed_firing(db):
    _durable_all_ran(db, BOOT)
    discovery = SCHEDULE["strategy-discovery"]["task"]
    catchup.record_durable_success(db, discovery, {"status": "complete"},
                                   now=datetime(2026, 11, 1, 11, 0, tzinfo=ET))
    client = FakeRedis()
    first = catchup.queue_missed_jobs(client, SCHEDULE, lambda *a, **k: None, now=BOOT,
                                      durable_last_success=_lookup(db))
    assert "strategy-discovery" in [item["entry"] for item in first]
    # The run is killed (no success recorded). The next afternoon's pass,
    # in the same Redis, does not queue the same lost firing again.
    wednesday = datetime(2026, 11, 11, 16, 20, tzinfo=ET).astimezone(timezone.utc)
    again = catchup.queue_missed_jobs(client, SCHEDULE, lambda *a, **k: None, now=wednesday,
                                      durable_last_success=_lookup(db))
    assert "strategy-discovery" not in [item["entry"] for item in again]


def test_the_one_off_stops_once_a_real_engine_run_is_recorded(db):
    client = FakeRedis()
    # The Redis stamp the one-off distrusts: the in-session skip of 09:01 ET.
    catchup.record_task_success(client, ENGINE_TASK, {"status": "complete"},
                                now=datetime(2026, 11, 10, 9, 1, tzinfo=ET))
    _durable_all_ran(db, datetime(2026, 11, 10, 8, 0, tzinfo=ET))  # before the window opened
    first = catchup.queue_missed_jobs(client, SCHEDULE, lambda *a, **k: None, now=BOOT,
                                      durable_last_success=_lookup(db))
    assert [item["entry"] for item in first if item.get("one_off")] == ["experiment-engine"]
    # A real engine run lands; after the next restart (empty Redis) the one-off stays quiet.
    catchup.record_durable_success(db, ENGINE_TASK, {"status": "complete"}, now=BOOT + timedelta(minutes=30))
    later = BOOT + timedelta(hours=2)
    after = catchup.queue_missed_jobs(FakeRedis(), SCHEDULE, lambda *a, **k: None, now=later,
                                      durable_last_success=_lookup(db))
    assert not any(item.get("one_off") for item in after)


def test_only_catchup_entries_get_a_durable_stamp():
    names = catchup.catchup_task_names(SCHEDULE)
    assert SCHEDULE["strategy-discovery"]["task"] in names
    assert SCHEDULE["paper-auto-revalidation"]["task"] not in names


def test_the_signal_writes_the_durable_stamp_for_catchup_entries(db, monkeypatch):
    from app.db import session as db_session
    from app.tasks import celery_app as celery_module

    monkeypatch.setattr(db_session, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    celery_module._record_durable_task_success(ENGINE_TASK, {"status": "complete"})
    celery_module._record_durable_task_success(SCHEDULE["paper-auto-revalidation"]["task"], {"status": "complete"})
    rows = db.scalars(select(AuditLog).where(AuditLog.event_type == catchup.DURABLE_EVENT_TYPE)).all()
    assert [row.action for row in rows] == [ENGINE_TASK]


def test_the_store_wrapper_reads_through_one_session(db):
    _durable_all_ran(db, BOOT)
    closed = []
    monkey_close = db.close
    db.close = lambda: closed.append(True)
    try:
        queued = catchup.queue_missed_jobs_from_store(FakeRedis(), SCHEDULE, lambda *a, **k: None, now=BOOT,
                                                      session_factory=lambda: db)
    finally:
        db.close = monkey_close
    assert [item["entry"] for item in queued if not item.get("one_off")] == []
    assert closed == [True]


# ------------------------------------------------- startup research refresh

IMPORT_TASK = SCHEDULE[catchup.STARTUP_REFRESH_ENTRY]["task"]
NOON_BOOT = datetime(2026, 11, 10, 12, 9, tzinfo=ET).astimezone(timezone.utc)


def test_a_mid_session_boot_skips_the_refresh_when_the_morning_import_ran(db):
    catchup.record_durable_success(db, IMPORT_TASK, {"status": "complete"},
                                   now=datetime(2026, 11, 10, 8, 20, tzinfo=ET))
    assert not catchup.startup_refresh_due(FakeRedis(), SCHEDULE, now=NOON_BOOT, durable_last_success=_lookup(db))


def test_a_boot_after_a_missed_morning_import_refreshes(db):
    catchup.record_durable_success(db, IMPORT_TASK, {"status": "complete"},
                                   now=datetime(2026, 11, 9, 17, 20, tzinfo=ET))
    assert catchup.startup_refresh_due(FakeRedis(), SCHEDULE, now=NOON_BOOT, durable_last_success=_lookup(db))


def test_a_first_boot_with_no_stamp_refreshes(db):
    assert catchup.startup_refresh_due(FakeRedis(), SCHEDULE, now=NOON_BOOT, durable_last_success=_lookup(db))


def test_the_import_gets_a_durable_stamp():
    assert IMPORT_TASK in catchup.catchup_task_names(SCHEDULE)


def test_the_launcher_refreshes_when_the_check_fails(monkeypatch):
    import scripts.run_beat_with_lease as launcher
    from app.tasks import catchup as module

    def broken(*_a, **_k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(module, "startup_refresh_due_from_store", broken)
    assert launcher._startup_refresh_due(FakeRedis()) is True
