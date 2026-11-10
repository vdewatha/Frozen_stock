"""Public heartbeat for the outside uptime check (.github/workflows/uptime.yml).

/api/ready can take over 40 s on the hosted VM, so the outside check reads this
cheaper route instead: worker heartbeat, beat lease, monitor/watchdog stamps
and how long trading has been halted.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.security import AuthenticationMiddleware
from app.models import StockPaperRecoveryState
from app.services import heartbeat as hb

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)


class FakeRedis:
    def __init__(self, store):
        self.store = store

    def scan_iter(self, match, count=None):
        prefix = match.rstrip("*")
        return iter([key for key in list(self.store) if key.startswith(prefix)])

    def exists(self, key):
        return int(key in self.store)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)

    def close(self):
        pass


@pytest.fixture
def store():
    data = {"celery:worker:heartbeat:w1": "{}", "celery:beat:lease:primary": "token"}
    with patch.object(hb.redis.Redis, "from_url", side_effect=lambda *a, **k: FakeRedis(data)):
        yield data


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    StockPaperRecoveryState.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    session.add(StockPaperRecoveryState(
        id=1, status="resumable",
        last_monitor_heartbeat_at=NOW - timedelta(minutes=2),
        last_watchdog_heartbeat_at=NOW - timedelta(minutes=3),
    ))
    session.commit()
    yield session
    session.close()


def state(db):
    return db.get(StockPaperRecoveryState, 1)


def test_healthy_bot_is_ok(db, store):
    snap = hb.heartbeat_snapshot(db, now=NOW)
    assert snap["ok"] and snap["problems"] == []
    assert snap["tradable"] is True and snap["halted_for_seconds"] is None
    assert snap["monitor_age_seconds"] == 120.0


def test_dead_worker_and_beat_fail(db, store):
    store.clear()
    snap = hb.heartbeat_snapshot(db, now=NOW)
    assert not snap["ok"]
    assert set(snap["problems"]) == {"no_worker_heartbeat", "no_beat_lease"}


def test_stale_monitoring_jobs_fail_even_with_processes_up(db, store):
    state(db).last_monitor_heartbeat_at = NOW - timedelta(minutes=25)
    db.commit()
    snap = hb.heartbeat_snapshot(db, now=NOW)
    assert snap["problems"] == ["monitor_heartbeat_stale"]


def test_halt_is_reported_with_duration_but_does_not_fail(db, store):
    state(db).status = "paused"
    db.commit()
    first = hb.heartbeat_snapshot(db, now=NOW)
    later = hb.heartbeat_snapshot(db, now=NOW + timedelta(minutes=40))
    assert first["ok"] and first["tradable"] is False and first["halted_for_seconds"] == 0.0
    assert later["halted_for_seconds"] == 2400.0
    state(db).status = "armed"
    db.commit()
    resumed = hb.heartbeat_snapshot(db, now=NOW + timedelta(minutes=45))
    assert resumed["tradable"] is True and hb.HALT_SEEN_KEY not in store


def test_accounting_review_counts_as_halted(db, store):
    state(db).accounting_review_required = True
    db.commit()
    assert hb.heartbeat_snapshot(db, now=NOW)["tradable"] is False


def test_redis_down_fails_with_reason(db):
    with patch.object(hb.redis.Redis, "from_url", side_effect=ConnectionError("down")):
        snap = hb.heartbeat_snapshot(db, now=NOW)
    assert not snap["ok"] and snap["problems"] == ["redis:ConnectionError"]


def test_route_is_public_cached_and_returns_503_when_stopped(db, store):
    from app.api.routes import router
    from app.db.session import get_db

    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware, configuration=Settings(
        _env_file=None, environment="production", auth_mode="clerk_gateway",
    ))
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    # The route reads the real clock, so the stamps must be fresh against it,
    # not against the fixed NOW (this failed every run after 15:10 UTC 11-10).
    real_now = datetime.now(UTC)
    row = state(db)
    row.last_monitor_heartbeat_at = real_now - timedelta(minutes=2)
    row.last_watchdog_heartbeat_at = real_now - timedelta(minutes=3)
    db.commit()
    client = TestClient(app)
    hb.reset_cache()
    try:
        ok = client.get("/heartbeat")
        assert ok.status_code == 200 and ok.json()["ok"] is True
        store.clear()
        assert client.get("/heartbeat").status_code == 200  # cached for a few seconds
        hb.reset_cache()
        stopped = client.get("/heartbeat")
        assert stopped.status_code == 503 and "no_worker_heartbeat" in stopped.json()["problems"]
    finally:
        hb.reset_cache()
