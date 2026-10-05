import os
from hashlib import sha256
from uuid import uuid4
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import sessionmaker

from app.tasks import jobs


@pytest.mark.parametrize("event_name", ["before_cursor_execute", "after_cursor_execute"])
def test_uncertain_acquisition_discards_connection_without_executing_work(event_name):
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("Requires isolated PostgreSQL test database")
    engine = create_engine(url, pool_size=3)
    name = "uncertain_acquisition_" + uuid4().hex
    key = int.from_bytes(sha256(f"job:{name}".encode()).digest()[:8], "big") % 2_147_483_647
    observer = engine.connect()
    def lose_confirmation(conn, cursor, statement, parameters, context, executemany):
        if conn is not observer and "pg_try_advisory_lock(" in statement:
            raise RuntimeError("acquisition confirmation unavailable")
    event.listen(engine, event_name, lose_confirmation)
    try:
        with patch.object(jobs, "SessionLocal", sessionmaker(bind=engine)), patch.object(jobs, "create_notification"):
            with pytest.raises(RuntimeError, match="confirmation unavailable"):
                jobs._run_job(name, lambda db: pytest.fail("Work executed with uncertain lock"))
        assert observer.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}), "Uncertain acquisition leaked a lock"
        observer.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    finally:
        observer.close()
        engine.dispose()


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("cleanup", ["normal", "exception", "lost_ownership"])
def test_job_lock_stays_on_owned_connection_across_commits(fail, cleanup, caplog):
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("Requires isolated PostgreSQL test database")
    engine = create_engine(url, pool_size=5)
    name = "connection_ownership_" + uuid4().hex
    key = int.from_bytes(sha256(f"job:{name}".encode()).digest()[:8], "big") % 2_147_483_647
    borrowed = []
    observer = engine.connect()
    def interrupt_unlock(conn, cursor, statement, parameters, context, executemany):
        if conn is not observer and "pg_advisory_unlock(" in statement:
            if cleanup == "exception":
                raise RuntimeError("private-driver-error")
            if cleanup == "lost_ownership":
                return "SELECT false", {}
        return statement, parameters
    event.listen(engine, "before_cursor_execute", interrupt_unlock, retval=True)
    try:
        def available():
            acquired = observer.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            if acquired:
                observer.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            return acquired

        def work(db):
            db.execute(text("SELECT 1"))
            db.commit()
            # Force the session's just-returned pooled connection to be borrowed.
            borrowed.append(engine.connect())
            assert not available(), "Lock was lost during work"
            db.execute(text("SELECT 2"))
            db.commit()
            assert not available()
            nested = jobs._run_job(name, lambda db: pytest.fail("Duplicate job executed"))
            assert nested["status"] == "skipped"
            if fail:
                raise ValueError("expected job failure")
            return {"status": "complete"}

        with patch.object(jobs, "SessionLocal", sessionmaker(bind=engine)), patch.object(jobs, "create_notification"):
            if fail:
                with pytest.raises(ValueError, match="expected job failure"):
                    jobs._run_job(name, work)
            else:
                assert jobs._run_job(name, work)["status"] == "complete"
        assert available(), "Job leaked its advisory lock on a borrowed pooled connection"
        assert "private-driver-error" not in caplog.text
        if cleanup != "normal":
            assert "lock cleanup failed" in caplog.text
    finally:
        for connection in borrowed:
            connection.execute(text("SELECT pg_advisory_unlock_all()"))
            connection.close()
        observer.close()
        engine.dispose()
