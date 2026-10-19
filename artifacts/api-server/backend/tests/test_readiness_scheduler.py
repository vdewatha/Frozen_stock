from datetime import datetime
import threading
import time
from unittest.mock import patch

from app.services import readiness
from app.api import routes


def test_scheduler_health_retries_transient_worker_and_beat_probe():
    inspector = type("Inspector", (), {})()
    inspector.ping = lambda: {"execution@localhost": {"ok": True}}
    redis_client = type("RedisClient", (), {})()
    redis_client.scan_iter = lambda match: iter(
        [b"celery:beat:lease:primary"] if "lease" in match else []
    )
    redis_client.close = lambda: None
    with patch.object(
        readiness.celery_app.control,
        "inspect",
        side_effect=[Exception("timeout"), inspector],
    ), patch.object(readiness.redis.Redis, "from_url", return_value=redis_client):
        result = readiness._scheduler_health()

    assert result["workers"] == ["execution@localhost"]
    assert result["beat_evidence"] == ["celery:beat:lease:primary"]
    assert result["healthy"] is True


def test_scheduler_health_returns_when_worker_broker_ignores_inspect_timeout():
    release = threading.Event()

    class SlowInspector:
        def ping(self):
            release.wait(2)
            return {}

    started = time.monotonic()
    redis_client = type("RedisClient", (), {})()
    redis_client.scan_iter = lambda match: iter(())
    redis_client.close = lambda: None
    with patch.object(
        readiness.celery_app.control,
        "inspect",
        return_value=SlowInspector(),
    ), patch.object(readiness.redis.Redis, "from_url", return_value=redis_client):
        result = readiness._scheduler_health()
    release.set()

    assert time.monotonic() - started < 2
    assert result["healthy"] is False
    assert result["worker_error"] in {"RuntimeError", "probe_in_progress"}


def test_system_readiness_returns_explicit_pending_state_while_refreshing():
    routes._readiness_cache = None
    routes._readiness_refresh_active = False
    routes._readiness_refresh_error = None
    release = threading.Event()

    def slow_snapshot(_db):
        release.wait(2)
        return {
            "generated_at": datetime.utcnow(),
            "overall_status": "blocked",
            "summary": {"ready": 0, "warning": 0, "blocked": 1},
            "checks": [{
                "name": "test",
                "status": "blocked",
                "message": "blocked",
                "details": {},
            }],
            "paper_trading_allowed": False,
            "live_safety": None,
        }

    with patch.object(routes, "readiness_snapshot", side_effect=slow_snapshot):
        started = time.monotonic()
        result = routes.system_readiness()
        elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert result["overall_status"] == "blocked"
    assert result["paper_trading_allowed"] is False
    assert result["checks"][0]["name"] == "Readiness evaluation"
    release.set()
    deadline = time.monotonic() + 2
    while routes._readiness_refresh_active and time.monotonic() < deadline:
        time.sleep(0.01)
    assert routes._readiness_refresh_active is False
