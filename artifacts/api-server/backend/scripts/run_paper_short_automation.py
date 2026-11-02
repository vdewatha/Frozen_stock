"""Supervised autonomous paper-short loop; no per-trade user actions."""
from contextlib import contextmanager
import fcntl
from hashlib import sha256
import logging
from pathlib import Path
import signal
from threading import Event

from sqlalchemy import text

from app.core.config import settings
from app.db.schema import assert_schema_current
from app.db.session import engine, SessionLocal
from app.services.paper_short_automation import run_short_automation_cycle
from app.services.paper_short_pilot import ShortPilotGateway, _binding

logger = logging.getLogger("paper-short-automation")


@contextmanager
def leader(database_engine):
    """Hold process-lifetime leadership, never a renewable wall-clock lease."""
    dialect = database_engine.dialect.name
    if dialect == "sqlite":
        database = database_engine.url.database
        if not database or database == ":memory:":
            raise RuntimeError("Automation requires a persistent database")
        path = Path(database).resolve()
        with path.with_name(path.name + ".short-automation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield lambda: None
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    elif dialect == "postgresql":
        key = int.from_bytes(sha256(b"development-paper-short-automation-v1").digest()[:8], "big") % (2 ** 63)
        with database_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                raise RuntimeError("Another paper short automation process already owns this database")
            backend = connection.scalar(text("SELECT pg_backend_pid()"))

            def check():
                if connection.scalar(text("SELECT pg_backend_pid()")) != backend:
                    raise RuntimeError("Automation leadership connection changed")

            try:
                yield check
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    else:
        raise RuntimeError("Unsupported automation database")


def run_once():
    with SessionLocal() as db:
        return run_short_automation_cycle(db)


def run_loop(stop, check_leader, *, once=run_once, interval=60):
    while not stop.is_set():
        # Leadership failure must stop the process, not silently reconnect.
        check_leader()
        try:
            result = once()
            logger.info("status=%s action=%s", result.get("status"), result.get("action"))
        except Exception as exc:
            logger.error("cycle failed; no safety gates reset: %s", type(exc).__name__)
        stop.wait(interval)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s paper-short %(message)s")
    if not settings.dev_paper_shorts_automation_enabled:
        logger.info("Paper short automation is disabled by deployment policy")
        return
    _binding(ShortPilotGateway())
    assert_schema_current(engine)
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    with leader(engine) as check_leader:
        run_loop(stop, check_leader)


if __name__ == "__main__":
    main()
