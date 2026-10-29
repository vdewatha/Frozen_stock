from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {"connect_timeout": 10}
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
