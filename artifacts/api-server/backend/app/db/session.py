from __future__ import annotations

import logging
import os
from collections.abc import Generator, Mapping

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)

# Every paper process tags its connections with the deploy's boot id so a new
# deploy can tell which sessions a replaced instance left behind.
APPLICATION_NAME_PREFIX = "frozen-paper:"


def application_name(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    boot_id = (env.get("PAPER_BOOT_ID") or "").strip()
    return f"{APPLICATION_NAME_PREFIX}{boot_id}"[:63] if boot_id else "frozen-paper"


connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {"connect_timeout": 10, "application_name": application_name()}
)

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_timeout=10,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _limit_idle_transactions(dbapi_connection, _record=None) -> None:
    # A worker killed mid-transaction (a replaced deployment VM) left its
    # session "idle in transaction" holding paper account and recovery row
    # locks for up to 50 minutes, freezing the new deployment. Let the server
    # end such sessions; live work never idles that long inside a transaction.
    seconds = int(settings.database_idle_in_transaction_timeout_seconds)
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"SET idle_in_transaction_session_timeout = '{seconds}s'")
    finally:
        cursor.close()
    dbapi_connection.commit()


if engine.dialect.name == "postgresql" and settings.database_idle_in_transaction_timeout_seconds > 0:
    event.listen(engine, "connect", _limit_idle_transactions)


# A replaced deploy's process can be stopped mid-transaction. Its session then
# sits "idle in transaction" holding the recovery-state row lock until the
# idle timeout above ends it; meanwhile the new deploy's watchdog and monitor
# wait on that lock and the watchdog re-pauses trading (2026-11-06: 10 and 30
# minutes after two publishes). Sessions of another boot idle this long have
# no live owner here.
PREVIOUS_BOOT_IDLE_SECONDS = 60
_PREVIOUS_BOOT_SESSIONS = text(
    "SELECT pid, application_name FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
    "AND application_name LIKE :prefix AND application_name <> :current "
    "AND state IN ('idle in transaction', 'idle in transaction (aborted)') "
    "AND state_change < now() - make_interval(secs => :idle_seconds)"
)


def release_previous_boot_transactions(bind: Engine | None = None, environ: Mapping[str, str] | None = None,
                                       idle_seconds: int = PREVIOUS_BOOT_IDLE_SECONDS) -> list[int]:
    """End another boot's idle transactions; returns the terminated backend pids.

    A no-op without ``PAPER_BOOT_ID`` or off PostgreSQL. Sessions of this boot
    and untagged clients are never touched.
    """
    bind = bind or engine
    env = os.environ if environ is None else environ
    if not (env.get("PAPER_BOOT_ID") or "").strip() or bind.dialect.name != "postgresql":
        return []
    released: list[int] = []
    with bind.connect() as connection:
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
        rows = connection.execute(_PREVIOUS_BOOT_SESSIONS, {
            "prefix": APPLICATION_NAME_PREFIX + "%", "current": application_name(env), "idle_seconds": idle_seconds,
        }).all()
        for pid, name in rows:
            if connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}).scalar():
                released.append(int(pid))
                logger.warning("Ended an idle transaction left by a previous deploy: pid=%s application=%s", pid, name)
    return released


def limit_lock_waits(seconds: int, bind: Engine | None = None) -> None:
    """Make this process's PostgreSQL sessions give up a lock wait after ``seconds``.

    For loops that retry on their next cycle (watchdog, monitor): a wait that
    never ends would otherwise hide them until the blocking session is gone.
    """
    bind = bind or engine
    if bind.dialect.name != "postgresql" or seconds <= 0:
        return

    def _set_lock_timeout(dbapi_connection, _record=None) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET lock_timeout = '{int(seconds)}s'")
        finally:
            cursor.close()
        dbapi_connection.commit()

    event.listen(bind, "connect", _set_lock_timeout)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
