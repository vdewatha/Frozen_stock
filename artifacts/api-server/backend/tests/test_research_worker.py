"""The research worker: its memory cap, and the record of jobs lost with their process.

The load scenario showed that a research job in flight when its worker is
killed is gone: research tasks acknowledge at start, so nothing delivers them
again, and nothing said so. They are still not delivered again (the job that
exhausted memory would exhaust it again); every loss is written down with the
scheduled run that covers it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from billiard.exceptions import WorkerLostError
from celery.schedules import crontab
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models import Notification
from app.services.notifications import (
    LOST_RESEARCH_JOB_ENTITY,
    resolve_successful_job_notifications,
    trading_blocking_critical_filter,
)
from app.tasks import catchup, research_worker
from app.tasks.celery_app import celery_app

UTC = timezone.utc
BACKEND = Path(__file__).resolve().parents[1]
SERVER = BACKEND.parent / "src" / "production-server.ts"
LAUNCHER = BACKEND / "scripts" / "run_local_stack.sh"
# Monday 2026-11-16 10:00 ET.
NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
SCHEDULE = {
    "experiment-engine": {"task": "app.tasks.jobs.experiment_engine_job",
                          "schedule": crontab(minute=0, hour=12, day_of_week="sun")},
    "nightly-backtest-job": {"task": "app.tasks.jobs.nightly_backtest_job", "schedule": crontab(minute=0, hour=19)},
    "drift-retrain": {"task": "app.tasks.jobs.drift_retrain_job", "schedule": 60 * 15},
    "loss-feedback": {"task": "app.tasks.jobs.loss_feedback_job",
                      "schedule": crontab(minute=45, hour=16, day_of_week="mon-fri")},
}


class FakeRedis:
    """The few commands the research worker uses, on plain dictionaries."""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def set(self, key, value, ex=None, nx=False):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def exists(self, key):
        return int(key in self.values)

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hdel(self, key, field):
        self.hashes.get(key, {}).pop(field, None)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def close(self):
        pass


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def worker(monkeypatch):
    """This process as the research worker's main process, on a fake Redis."""
    client = FakeRedis()
    monkeypatch.setattr(research_worker, "_client", lambda: client)
    monkeypatch.setattr(research_worker, "_worker_hostname", "paper-research@host")
    monkeypatch.setattr(research_worker, "_worker_started_at", 1_000.0)
    research_worker._current.clear()
    yield client
    research_worker._current.clear()


def _task(name="app.tasks.jobs.experiment_engine_job", *, queue="learning", acks_late=False, reject=False):
    return SimpleNamespace(
        name=name, acks_late=acks_late, reject_on_worker_lost=reject,
        request=SimpleNamespace(delivery_info={"routing_key": queue}, hostname="paper-research@host"),
    )


# ------------------------------------------------------------------ the cap

def test_the_cap_comes_from_the_environment_with_a_default():
    assert research_worker.max_memory_mb({}) == research_worker.DEFAULT_MAX_MEMORY_MB == 2048
    assert research_worker.max_memory_mb({"PAPER_RESEARCH_MAX_MEMORY_MB": "1536"}) == 1536
    assert research_worker.max_memory_mb({"PAPER_RESEARCH_MAX_MEMORY_MB": " "}) == 2048
    # 0 switches the cap off; a value that cannot be read never does.
    assert research_worker.max_memory_mb({"PAPER_RESEARCH_MAX_MEMORY_MB": "0"}) == 0
    assert research_worker.max_memory_mb({"PAPER_RESEARCH_MAX_MEMORY_MB": "two gigabytes"}) == 2048


def test_a_process_is_over_the_cap_only_above_it_and_only_with_a_reading():
    assert research_worker.over_cap(2049.0, 2048) is True
    assert research_worker.over_cap(2048.0, 2048) is False
    assert research_worker.over_cap(None, 2048) is False
    assert research_worker.over_cap(9999.0, 0) is False


def test_only_a_worker_that_consumes_learning_alone_is_the_research_worker(monkeypatch):
    started: list[int] = []
    monkeypatch.setattr(research_worker, "_die_with_parent", lambda: None)
    monkeypatch.setattr(research_worker.threading, "Thread", lambda **kwargs: SimpleNamespace(
        start=lambda: started.append(kwargs["args"][0])))
    monkeypatch.setenv("PAPER_RESEARCH_MAX_MEMORY_MB", "1024")
    monkeypatch.setenv(research_worker.RESEARCH_WORKER_ENV, "0")

    def consuming(*queues):
        return SimpleNamespace(app=SimpleNamespace(amqp=SimpleNamespace(queues=SimpleNamespace(
            consume_from={queue: object() for queue in queues}))))

    for queues, capped in ((("learning",), True), (("learning", "learning_fast"), False),
                           (("default", "market_data"), False)):
        started.clear()
        research_worker._on_worker_setup(sender="worker@host", instance=consuming(*queues))
        research_worker._on_task_process_start()
        assert started == ([1024] if capped else []), queues
    # The cap switched off: the research worker starts no watcher.
    monkeypatch.setenv("PAPER_RESEARCH_MAX_MEMORY_MB", "0")
    research_worker._on_worker_setup(sender="paper-research@host", instance=consuming("learning"))
    started.clear()
    research_worker._on_task_process_start()
    assert started == []


def test_a_process_over_the_cap_says_why_and_kills_itself(worker, monkeypatch):
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(research_worker.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(research_worker.os, "write", lambda *_args: None)
    research_worker._current.update({"id": "task-1", "name": "app.tasks.jobs.experiment_engine_job"})
    research_worker._stop_over_cap(2301.4, 2048)
    assert killed == [(research_worker.os.getpid(), research_worker.signal.SIGKILL)]
    assert json.loads(worker.get("celery:research:stopped:task-1")) == {
        "reason": "memory_cap", "resident_mb": 2301.4, "cap_mb": 2048}


# ------------------------------------------------------- the covering run

def test_a_weekly_job_is_covered_by_its_next_firing_and_the_catch_up():
    cover = research_worker.covering_run("app.tasks.jobs.experiment_engine_job", SCHEDULE, NOW, FakeRedis())
    # Sunday 2026-11-22 12:00 ET.
    assert cover == {"covered": True, "entry": "experiment-engine", "next_run_at": "2026-11-22T17:00:00+00:00",
                     "every_seconds": None, "catchup": "pending"}


def test_a_catch_up_already_queued_for_the_firing_is_not_promised_again():
    client = FakeRedis()
    fired = datetime(2026, 11, 15, 17, 0, tzinfo=UTC)  # Sunday 12:00 ET
    client.set(f"{catchup.CATCHUP_MARKER_PREFIX}experiment-engine:{fired.isoformat()}", "queued")
    cover = research_worker.covering_run("app.tasks.jobs.experiment_engine_job", SCHEDULE, NOW, client)
    assert cover["catchup"] == "used"


def test_no_catch_up_is_promised_when_the_next_run_is_close_or_the_entry_is_not_listed():
    # 15:00 ET: loss-feedback fires at 16:45 ET, inside the three-hour grace.
    close = research_worker.covering_run("app.tasks.jobs.loss_feedback_job", SCHEDULE,
                                         datetime(2026, 11, 16, 20, 0, tzinfo=UTC), FakeRedis())
    assert close["catchup"] == "next_run_close" and close["next_run_at"] == "2026-11-16T21:45:00+00:00"
    nightly = research_worker.covering_run("app.tasks.jobs.nightly_backtest_job", SCHEDULE, NOW, FakeRedis())
    assert nightly["catchup"] == "not_listed" and nightly["next_run_at"] == "2026-11-17T00:00:00+00:00"


def test_an_interval_job_is_covered_within_its_interval():
    cover = research_worker.covering_run("app.tasks.jobs.drift_retrain_job", SCHEDULE, NOW)
    assert cover["covered"] is True and cover["every_seconds"] == 900.0
    assert cover["next_run_at"] == (NOW + timedelta(minutes=15)).isoformat()


def test_a_task_with_no_beat_entry_is_not_covered():
    cover = research_worker.covering_run("app.tasks.jobs.stock_training_job", SCHEDULE, NOW)
    assert cover["covered"] is False and cover["next_run_at"] is None


def test_every_learning_task_is_either_scheduled_or_reported_as_uncovered():
    # The real schedule: covering_run answers for every task routed to the research queue.
    for task, route in celery_app.conf.task_routes.items():
        if route.get("queue") != research_worker.RESEARCH_QUEUE:
            continue
        cover = research_worker.covering_run(task, celery_app.conf.beat_schedule, NOW)
        assert cover["covered"] is (cover["next_run_at"] is not None), task


def test_the_message_names_the_cause_the_next_run_and_the_catch_up():
    cover = research_worker.covering_run("app.tasks.jobs.experiment_engine_job", SCHEDULE, NOW, FakeRedis())
    message = research_worker.loss_message("experiment_engine_job", "signal 9 (SIGKILL)", cover, 412.4)
    assert message == (
        "The process running experiment_engine_job was stopped after 412 s (signal 9 (SIGKILL)). "
        "The job was not delivered again. Its next scheduled run (experiment-engine) is "
        "Sunday 2026-11-22 12:00 ET. The catch-up pass (at boot, and weekdays after the close) "
        "queues one run before then."
    )
    uncovered = research_worker.loss_message("stock_training_job", "signal 9 (SIGKILL)", {"covered": False}, None)
    assert uncovered.endswith("No beat entry runs this task, so nothing queues it again.")


# ------------------------------------------------------------ the record

def test_a_lost_job_is_stored_as_a_warning_that_does_not_block_trading(sessions, worker):
    worker.set("celery:research:stopped:task-1",
               json.dumps({"reason": "memory_cap", "resident_mb": 2301.4, "cap_mb": 2048}))
    loss = research_worker.record_lost_job(
        "app.tasks.jobs.experiment_engine_job", "task-1", fallback_cause="signal 9 (SIGKILL)",
        started_at=NOW.timestamp() - 412, hostname="paper-research@host", now=NOW, client=worker,
        session_factory=sessions,
    )
    assert loss["redelivered"] is False and loss["covering_run"]["covered"] is True
    assert loss["cause"] == "memory cap: 2301.4 MiB resident, cap 2048 MiB (PAPER_RESEARCH_MAX_MEMORY_MB)"
    with sessions() as db:
        row = db.query(Notification).one()
        assert (row.category, row.severity, row.status, row.source, row.entity_type) == (
            "scheduled_job", "warning", "open", "experiment_engine_job", LOST_RESEARCH_JOB_ENTITY)
        assert row.title == "Research job lost: experiment_engine_job"
        assert "memory cap: 2301.4 MiB" in row.message and "was not delivered again" in row.message
        assert row.payload["task_id"] == "task-1" and row.payload["ran_seconds"] == 412.0
        # A critical scheduled-job alert holds paper orders; this record must not.
        assert db.query(Notification).filter(trading_blocking_critical_filter()).count() == 0


def test_the_next_successful_run_of_the_job_closes_the_record(sessions, worker):
    research_worker.record_lost_job("app.tasks.jobs.experiment_engine_job", "task-1", fallback_cause="lost",
                                    now=NOW, client=worker, session_factory=sessions)
    research_worker.record_lost_job("app.tasks.jobs.nightly_backtest_job", "task-2", fallback_cause="lost",
                                    now=NOW, client=worker, session_factory=sessions)
    with sessions() as db:
        assert resolve_successful_job_notifications(db, "experiment_engine_job") == 1
        db.commit()
        statuses = {row.source: row.status for row in db.query(Notification)}
    assert statuses == {"experiment_engine_job": "resolved", "nightly_backtest_job": "open"}


def test_a_store_that_is_down_never_raises(worker, caplog):
    def broken():
        raise RuntimeError("database is down")

    loss = research_worker.record_lost_job("app.tasks.jobs.experiment_engine_job", "task-1",
                                           fallback_cause="lost", now=NOW, client=worker, session_factory=broken)
    assert loss["job"] == "experiment_engine_job"
    assert "Research job lost with its process" in caplog.text
    assert "Could not store the lost research job" in caplog.text


def test_a_task_process_killed_under_a_research_task_is_recorded_by_its_worker(worker, monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(research_worker, "record_lost_job",
                        lambda name, task_id, **kwargs: recorded.append({"task": name, "id": task_id, **kwargs}))
    task = _task()
    research_worker._on_prerun(task_id="task-1", task=task)
    marker = json.loads(worker.hget("celery:research:inflight:paper-research@host", "task-1"))
    assert marker["task"] == task.name and marker["pid"] == research_worker.os.getpid()

    research_worker._on_failure(sender=task, task_id="task-1",
                                exception=WorkerLostError("Worker exited prematurely: signal 9 (SIGKILL) Job: 3."))
    assert [(item["task"], item["id"], item["fallback_cause"]) for item in recorded] == [
        (task.name, "task-1", "Worker exited prematurely: signal 9 (SIGKILL)")]
    assert recorded[0]["started_at"] == marker["started_at"]
    # The marker is gone, so the worker's next start does not record it twice.
    assert worker.hgetall("celery:research:inflight:paper-research@host") == {}


def test_ordinary_failures_and_redelivered_tasks_are_not_recorded_as_lost(worker, monkeypatch):
    recorded: list[str] = []
    monkeypatch.setattr(research_worker, "record_lost_job", lambda name, *_a, **_k: recorded.append(name))
    lost = WorkerLostError("Worker exited prematurely: signal 9 (SIGKILL) Job: 3.")
    # An exception in the job: _run_job already files that.
    research_worker._on_failure(sender=_task(), task_id="task-1", exception=ValueError("bad input"))
    # Celery delivers this one again itself (strategy_learning_scope_job).
    redelivered = _task("app.tasks.jobs.strategy_learning_scope_job", acks_late=True, reject=True)
    research_worker._on_prerun(task_id="task-2", task=redelivered)
    assert worker.hgetall("celery:research:inflight:paper-research@host") == {}
    research_worker._on_failure(sender=redelivered, task_id="task-2", exception=lost)
    # A lost task of another queue, with no marker: not research.
    research_worker._on_failure(sender=_task("app.tasks.jobs.daily_news_import", queue="market_data"),
                                task_id="task-3", exception=lost)
    assert recorded == []
    # A research task killed before its marker was written is still recorded, by its route.
    research_worker._on_failure(sender=_task("app.tasks.jobs.nightly_backtest_job"), task_id="task-4", exception=lost)
    assert recorded == ["app.tasks.jobs.nightly_backtest_job"]


def test_a_job_killed_at_its_hard_time_limit_is_recorded_too(worker, monkeypatch):
    from billiard.exceptions import TimeLimitExceeded

    recorded: list[str] = []
    monkeypatch.setattr(research_worker, "record_lost_job",
                        lambda _name, _id, **kwargs: recorded.append(kwargs["fallback_cause"]))
    task = _task("app.tasks.jobs.drift_retrain_job")
    research_worker._on_prerun(task_id="task-1", task=task)
    research_worker._on_failure(sender=task, task_id="task-1", exception=TimeLimitExceeded(150))
    assert recorded == ["its hard time limit of 150 s"]
    assert worker.hgetall("celery:research:inflight:paper-research@host") == {}


def test_two_jobs_at_once_each_clear_their_own_marker(worker):
    # A layout that still runs learning on a worker with two slots.
    first, second = _task(), _task("app.tasks.jobs.nightly_backtest_job")
    research_worker._on_prerun(task_id="task-1", task=first)
    research_worker._on_prerun(task_id="task-2", task=second)
    research_worker._on_postrun(task_id="task-1", task=first)
    assert list(worker.hgetall("celery:research:inflight:paper-research@host")) == ["task-2"]
    research_worker._on_postrun(task_id="task-2", task=second)
    assert worker.hgetall("celery:research:inflight:paper-research@host") == {}


def test_a_finished_task_leaves_no_marker(worker):
    task = _task()
    research_worker._on_prerun(task_id="task-1", task=task)
    research_worker._on_postrun(task_id="task-1", task=task)
    assert worker.hgetall("celery:research:inflight:paper-research@host") == {}
    # Tasks of other queues never get one.
    research_worker._on_prerun(task_id="task-2", task=_task("app.tasks.jobs.daily_news_import", queue="market_data"))
    assert worker.hgetall("celery:research:inflight:paper-research@host") == {}


def test_a_restarted_worker_records_what_its_killed_predecessor_was_running(worker, monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(research_worker, "record_lost_job",
                        lambda name, task_id, **kwargs: recorded.append({"task": name, "id": task_id, **kwargs}) or {})
    key = "celery:research:inflight:paper-research@host"
    worker.hset(key, "old", json.dumps({"task": "app.tasks.jobs.strategy_discovery_job", "started_at": 900.0}))
    # Started after this worker did: a task it is running now, not a loss.
    worker.hset(key, "current", json.dumps({"task": "app.tasks.jobs.drift_retrain_job", "started_at": 1_005.0}))
    research_worker._on_worker_ready(sender=SimpleNamespace(hostname="paper-research@host"))
    assert [(item["task"], item["id"], item["fallback_cause"]) for item in recorded] == [
        ("app.tasks.jobs.strategy_discovery_job", "old", "its worker was killed and restarted")]
    assert list(worker.hgetall(key)) == ["current"]


# ---------------------------------------------------------- the launchers

def _worker_args(source: str, hostname: str) -> str:
    match = re.search(rf'--hostname={re.escape(hostname)}@%h.*?(?=\], "|&\n)', source, flags=re.DOTALL)
    assert match, hostname
    return match.group(0)


def test_the_hosted_layout_runs_learning_only_in_the_research_worker():
    source = SERVER.read_text()
    research = _worker_args(source, "paper-research")
    assert '"--queues=learning"' in research
    for flag in ("--pool=prefork", "--concurrency=1", "--prefetch-multiplier=1", "--max-tasks-per-child=1"):
        assert f'"{flag}"' in research, flag
    assert '], "paper-research");' in source
    core = re.search(r'"--queues=([a-z_,]+)"', _worker_args(source, "paper-core")).group(1).split(",")
    assert "learning" not in core and "learning_fast" in core
    for hostname in ("paper-fast", "paper-intraday", "paper-broker"):
        queues = re.search(r'"--queues=([a-z_,]+)"', _worker_args(source, hostname)).group(1).split(",")
        assert "learning" not in queues, hostname


def test_the_local_launcher_mirrors_the_research_worker():
    source = LAUNCHER.read_text()
    research = _worker_args(source, "paper-research")
    assert "--queues=learning " in research
    for flag in ("--pool=prefork", "--concurrency=1", "--prefetch-multiplier=1", "--max-tasks-per-child=1"):
        assert flag in research, flag
    for general in re.findall(r'^\s*general_queues="([a-z_,]+)"$', source, flags=re.MULTILINE):
        assert "learning" not in general.split(",")
