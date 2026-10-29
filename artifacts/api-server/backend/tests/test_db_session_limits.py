from unittest.mock import MagicMock, patch

from app.core.config import settings
from app.db import session


def test_connections_limit_idle_transactions():
    dbapi_connection = MagicMock()
    with patch.object(settings, "database_idle_in_transaction_timeout_seconds", 600):
        session._limit_idle_transactions(dbapi_connection)
    cursor = dbapi_connection.cursor.return_value
    cursor.execute.assert_called_once_with("SET idle_in_transaction_session_timeout = '600s'")
    cursor.close.assert_called_once()
    dbapi_connection.commit.assert_called_once()


def test_job_lock_connection_stays_outside_a_transaction():
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath("app/tasks/jobs.py").read_text()
    assert 'connect().execution_options(isolation_level="AUTOCOMMIT")' in source
