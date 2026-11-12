"""2026-11-11: the VM ran out of memory three times.

The readiness snapshot was built in two places (the API every five minutes,
the worker's deployment monitor every fifteen); each build took about 100
seconds and grew its process by 0.4 to 1.4 GiB, and the monitor's build was
running at all three freezes.  The API now shares its snapshot and the monitor
reuses it; the workers hand memory back after long tasks.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import patch

from app.core import memory
from app.services import deployment_monitor, readiness
from app.tasks import celery_app as celery_module


class FakeRedis:
    store: dict = {}

    @classmethod
    def from_url(cls, *_args, **_kwargs):
        return cls()

    def set(self, key, value, ex=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def close(self):
        pass


def _snapshot(status="warning", generated_at=None):
    return {"generated_at": generated_at or datetime.utcnow(), "overall_status": status,
            "summary": {"ready": 1, "warning": 1, "blocked": 0},
            "checks": [{"name": "Market data", "status": "ready", "message": "ok",
                        "details": {"untrusted_assets": [], "missing_assets": [], "stale_assets": []}}],
            "paper_trading_allowed": status != "blocked", "live_safety": {}}


def _fresh_redis():
    FakeRedis.store = {}
    return patch.object(readiness.redis, "Redis", FakeRedis)


def test_the_shared_snapshot_round_trips_and_expires_by_age():
    with _fresh_redis():
        assert readiness.shared_snapshot() is None                       # nothing published yet
        assert readiness.publish_shared_snapshot(_snapshot())
        shared = readiness.shared_snapshot()
        assert shared["overall_status"] == "warning" and isinstance(shared["generated_at"], datetime)
        assert shared["checks"][0]["name"] == "Market data" and shared["shared_age_seconds"] >= 0
        # Older than one API refresh cycle: the API is not keeping it fresh, so it is not used.
        later = datetime.utcnow() + timedelta(seconds=readiness.SHARED_SNAPSHOT_MAX_AGE_SECONDS + 5)
        assert readiness.shared_snapshot(now=later) is None
        FakeRedis.store[readiness.SHARED_SNAPSHOT_KEY] = "not json"
        assert readiness.shared_snapshot() is None                       # never raises


def test_a_redis_outage_falls_back_to_building():
    class Down:
        @classmethod
        def from_url(cls, *_a, **_k):
            raise ConnectionError("redis is down")

    with patch.object(readiness.redis, "Redis", Down):
        assert readiness.publish_shared_snapshot(_snapshot()) is False
        assert readiness.shared_snapshot() is None
        with patch.object(readiness, "readiness_snapshot", return_value=_snapshot()) as build:
            _snap, source = readiness.monitor_readiness(object())
    assert source == "computed" and build.call_count == 1


def test_the_monitor_reuses_the_api_snapshot_and_builds_nothing():
    night = datetime(2026, 11, 11, 11, 9)        # 06:09 ET, the first freeze: market closed, snapshot blocked
    midday = datetime(2026, 11, 11, 19, 33)      # 14:33 ET, the third freeze: market open, snapshot not blocked
    for moment, status in ((night, "blocked"), (midday, "warning")):
        with _fresh_redis(), patch.object(readiness, "readiness_snapshot") as build:
            readiness.publish_shared_snapshot(_snapshot(status, generated_at=moment - timedelta(seconds=200)))
            snapshot, source = readiness.monitor_readiness(object(), now=moment)
        assert source == "shared" and snapshot["overall_status"] == status and build.call_count == 0


def test_a_block_during_the_session_is_confirmed_with_a_fresh_build():
    """A block in market hours holds orders and raises a critical alert: it must be current evidence."""
    midday = datetime(2026, 11, 11, 19, 33)
    with _fresh_redis(), patch.object(readiness, "readiness_snapshot", return_value=_snapshot("ready")) as build:
        readiness.publish_shared_snapshot(_snapshot("blocked", generated_at=midday - timedelta(seconds=200)))
        snapshot, source = readiness.monitor_readiness(object(), now=midday)
    assert source == "computed" and snapshot["overall_status"] == "ready" and build.call_count == 1


def test_the_deployment_monitor_reports_where_its_readiness_came_from():
    with patch.object(deployment_monitor, "monitor_readiness", return_value=(_snapshot(), "shared")), \
         patch.object(deployment_monitor, "_check_database", return_value={"name": "db", "status": "ready"}), \
         patch.object(deployment_monitor, "_check_redis", return_value={"name": "redis", "status": "ready"}), \
         patch.object(deployment_monitor, "_check_celery_schedule", return_value={"name": "s", "status": "ready"}), \
         patch.object(deployment_monitor, "_check_celery_workers", return_value={"name": "w", "status": "ready"}), \
         patch.object(deployment_monitor, "_check_celery_beat", return_value={"name": "b", "status": "ready"}), \
         patch.object(deployment_monitor, "_check_live_trading_safety", return_value={"name": "l", "status": "ready"}):
        snapshot = deployment_monitor.deployment_monitor_snapshot(object())
    assert snapshot["readiness_source"] == "shared" and snapshot["deployable"] is True


def test_each_check_is_timed_and_the_log_line_names_the_slowest_and_largest():
    checks = readiness._TimedChecks()
    with patch.object(readiness, "resident_mb", side_effect=[900.0, 1700.0]), \
         patch.object(readiness.time, "monotonic", side_effect=[checks._at + 80.0, checks._at + 83.0]):
        checks._rss = 800.0
        checks.append({"name": "Intraday feed", "status": "ready"})
        checks.append({"name": "Market data", "status": "ready"})
    assert checks.timing == {"Intraday feed": {"seconds": 80.0, "rss_delta_mb": 100.0},
                             "Market data": {"seconds": 3.0, "rss_delta_mb": 800.0}}
    assert list(checks) == [{"name": "Intraday feed", "status": "ready"}, {"name": "Market data", "status": "ready"}]
    line = readiness.timing_summary({"timing": checks.timing})
    assert line == "; slowest Intraday feed 80.0s, largest growth Market data +800MiB"
    assert readiness.timing_summary({}) == ""
    json.dumps(checks.timing)                                            # the snapshot stays JSON-safe


def test_memory_is_trimmed_after_long_tasks_only_and_never_raises(monkeypatch):
    calls = []

    class Libc:
        def malloc_trim(self, pad):
            calls.append(pad)
            return 1

    monkeypatch.setattr(memory, "_libc", Libc())
    monkeypatch.setattr(memory, "_libc_checked", True)
    monkeypatch.setattr(memory, "_last_trim", 0.0)
    assert memory.trim_memory() is True and calls == [0]
    assert memory.trim_memory() is False and calls == [0]                # inside the interval
    monkeypatch.setattr(memory, "_last_trim", 0.0)
    monkeypatch.setattr(memory, "_libc", None)                           # not glibc (macOS, musl): a no-op
    assert memory.trim_memory() is False
    # The task hooks: a short task does not trim, a long one does.
    trimmed = []
    monkeypatch.setattr(memory, "trim_memory", lambda *a, **k: trimmed.append(True) or True)
    clock = iter([100.0, 101.0, 200.0, 260.0])
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    celery_module._note_task_start(task_id="short")
    celery_module._trim_after_long_task(task_id="short")
    assert trimmed == []
    celery_module._note_task_start(task_id="long")
    celery_module._trim_after_long_task(task_id="long")
    assert trimmed == [True] and celery_module._task_started == {}
    celery_module._trim_after_long_task(task_id="never-started")         # unknown task: nothing to do
    assert memory.resident_mb() is None or memory.resident_mb() > 0


def test_the_api_responses_carry_the_timing_and_the_readiness_source():
    """2026-11-12: after the publish the timing was only in the log line.

    Both routes declare a response model, and a field the model does not name
    is dropped, so the evidence the build records never reached a reader.
    """
    from app.schemas.trading import DeploymentMonitorResponse, ReadinessResponse

    timing = {"Market data": {"seconds": 41.2, "rss_delta_mb": 310.5}}
    body = ReadinessResponse.model_validate({**_snapshot(), "timing": timing}).model_dump()
    assert body["timing"] == timing
    assert ReadinessResponse.model_validate(_snapshot()).timing is None   # the placeholder has none

    monitor = DeploymentMonitorResponse.model_validate({
        "generated_at": datetime.utcnow(), "environment": "production", "deployable": True, "status": "ready",
        "paper_trading_allowed": True, "live_trading_allowed": False, "message": "ok", "checks": [],
        "blockers": [], "warnings": [], "readiness_status": "warning", "readiness_blockers": [],
        "readiness_warnings": [], "readiness_source": "shared", "readiness": _snapshot(),
    })
    assert monitor.model_dump()["readiness_source"] == "shared"
