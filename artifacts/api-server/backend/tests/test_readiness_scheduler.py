import threading
import time
from unittest.mock import patch

from app.services import readiness


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
