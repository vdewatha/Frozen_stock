"""Real faults around the production job wrapper; no broker execution certification."""
import json
import os
import time
from urllib.request import Request, urlopen

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("FAULT_DRILL") != "1", reason="isolated fault stack only")
JOB = "iex_research_collection_job"


def api(path, payload=None):
    request = Request("http://toxiproxy:8474" + path,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=3) as response:
        data = response.read()
        return json.loads(data) if data else None


@pytest.fixture(scope="module")
def app():
    from app.core.config import settings
    from app.db.base import Base
    from app.db.session import engine
    from app.tasks import jobs

    assert settings.redis_url == "redis://toxiproxy:16379/0"
    assert str(engine.url) == "postgresql+psycopg://postgres@toxiproxy:15432/paper_tests"
    for attempt in range(30):
        try:
            api("/version")
            break
        except OSError:
            if attempt == 29:
                raise
            time.sleep(0.2)
    api("/populate", [
        {"name": "redis", "listen": "0.0.0.0:16379", "upstream": "redis:6379"},
        {"name": "postgres", "listen": "0.0.0.0:15432", "upstream": "postgres:5432"},
    ])
    Base.metadata.create_all(engine)
    yield jobs, engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_faults(app):
    api("/reset", {})
    yield
    api("/reset", {})


def test_duplicate_redis_lease_blocks_callback(app):
    jobs, _ = app
    calls = []
    lease = jobs._acquire_job_lock(JOB)
    assert lease is not None
    try:
        result = jobs._run_job(JOB, lambda db: calls.append("work"))
        assert result["status"] == "skipped"
        assert calls == []
    finally:
        lease.release()
    jobs._run_job(JOB, lambda db: calls.append("work"))
    assert calls == ["work"]


@pytest.mark.parametrize("service", ["redis", "postgres"])
def test_disconnection_blocks_work_and_recovers(app, service):
    from sqlalchemy.exc import SQLAlchemyError
    jobs, _ = app
    calls = []
    jobs._run_job(JOB, lambda db: calls.append("before"))
    api("/proxies/" + service, {"enabled": False})
    start = time.monotonic()
    with pytest.raises(RuntimeError if service == "redis" else SQLAlchemyError):
        jobs._run_job(JOB, lambda db: calls.append("during"))
    assert time.monotonic() - start < 15
    assert calls == ["before"]
    api("/proxies/" + service, {"enabled": True})
    jobs._run_job(JOB, lambda db: calls.append("after"))
    assert calls == ["before", "after"]


def test_delayed_redis_response_times_out_without_work(app):
    jobs, _ = app
    calls = []
    api("/proxies/redis/toxics", {"name": "slow", "type": "latency", "stream": "downstream",
                               "attributes": {"latency": 5000, "jitter": 0}})
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="Redis coordination is unavailable"):
        jobs._run_job(JOB, lambda db: calls.append("during"))
    elapsed = time.monotonic() - start
    assert 1.5 <= elapsed < 15
    assert calls == []
    api("/reset", {})
    jobs._run_job(JOB, lambda db: calls.append("after"))
    assert calls == ["after"]


def test_postgres_connection_reset_blocks_work_and_recovers(app):
    from sqlalchemy.exc import SQLAlchemyError
    jobs, _ = app
    calls = []
    api("/proxies/postgres/toxics", {"name": "reset", "type": "reset_peer",
                                  "stream": "downstream", "attributes": {"timeout": 0}})
    start = time.monotonic()
    with pytest.raises(SQLAlchemyError):
        jobs._run_job(JOB, lambda db: calls.append("during"))
    assert time.monotonic() - start < 15
    assert calls == []
    api("/reset", {})
    jobs._run_job(JOB, lambda db: calls.append("after"))
    assert calls == ["after"]


def test_failed_job_does_not_commit_partial_work(app):
    from app.models import Notification
    jobs, _ = app

    def fail(db):
        jobs.create_notification(db, category="fault_fixture", severity="critical",
                                 source="must_rollback", title="fixture", message="fixture")
        db.flush()
        raise ValueError("original fixture failure")

    with pytest.raises(ValueError, match="original fixture failure"):
        jobs._run_job(JOB, fail)
    with jobs.SessionLocal() as db:
        assert db.query(Notification).filter(Notification.source == "must_rollback").count() == 0
        assert db.query(Notification).filter(Notification.source == JOB,
                                             Notification.message == "original fixture failure").count() == 1


def test_notification_outage_preserves_original_error(app):
    jobs, _ = app

    def fail(db):
        api("/proxies/postgres", {"enabled": False})
        raise ValueError("original error survives notification outage")

    with pytest.raises(ValueError, match="original error survives notification outage"):
        jobs._run_job(JOB, fail)
