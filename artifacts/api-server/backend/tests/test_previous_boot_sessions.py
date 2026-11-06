"""A new deploy ends the idle transactions a replaced instance left behind."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import create_engine

from app.db import session


def _postgres(rows, terminated=True):
    connection = MagicMock()
    connection.execution_options.return_value = connection
    selected = MagicMock()
    selected.all.return_value = rows
    terminate = MagicMock()
    terminate.scalar.return_value = terminated
    connection.execute.side_effect = lambda statement, params=None: (
        terminate if "pg_terminate_backend" in str(statement) else selected)
    bind = MagicMock()
    bind.dialect = SimpleNamespace(name="postgresql")
    bind.connect.return_value.__enter__.return_value = connection
    return bind, connection


def test_connections_are_tagged_with_the_boot_id():
    assert session.application_name({"PAPER_BOOT_ID": "abc123"}) == "frozen-paper:abc123"
    assert session.application_name({}) == "frozen-paper"
    assert len(session.application_name({"PAPER_BOOT_ID": "x" * 100})) == 63


def test_only_other_boots_idle_transactions_are_ended():
    bind, connection = _postgres([(4101, "frozen-paper:old-boot"), (4102, "frozen-paper:older")])
    released = session.release_previous_boot_transactions(bind, {"PAPER_BOOT_ID": "new-boot"})
    assert released == [4101, 4102]
    select_call = connection.execute.call_args_list[0]
    sql, params = str(select_call.args[0]), select_call.args[1]
    assert "idle in transaction" in sql and "state_change" in sql and "pg_backend_pid()" in sql
    assert params == {"prefix": "frozen-paper:%", "current": "frozen-paper:new-boot", "idle_seconds": 60}
    assert connection.execution_options.call_args.kwargs == {"isolation_level": "AUTOCOMMIT"}


def test_a_refused_termination_is_not_reported():
    bind, _connection = _postgres([(4101, "frozen-paper:old-boot")], terminated=False)
    assert session.release_previous_boot_transactions(bind, {"PAPER_BOOT_ID": "new-boot"}) == []


def test_nothing_happens_without_a_boot_id_or_off_postgres():
    bind, connection = _postgres([(4101, "frozen-paper:old-boot")])
    assert session.release_previous_boot_transactions(bind, {}) == []
    connection.execute.assert_not_called()
    sqlite = create_engine("sqlite://")
    assert session.release_previous_boot_transactions(sqlite, {"PAPER_BOOT_ID": "new-boot"}) == []


def test_lock_wait_limit_is_postgres_only():
    sqlite = create_engine("sqlite://")
    session.limit_lock_waits(20, sqlite)  # no listener, no error
    with sqlite.connect() as connection:
        assert connection.exec_driver_sql("select 1").scalar() == 1


def test_watchdog_and_monitor_clean_up_before_each_cycle(monkeypatch):
    import scripts.run_stock_monitor_loop as monitor
    import scripts.run_stock_watchdog as watchdog

    for module in (watchdog, monitor):
        calls = []
        monkeypatch.setattr(module, "release_previous_boot_transactions", lambda: calls.append(1) or [])
        module.release_stale_sessions()
        assert calls == [1]

        def boom():
            raise RuntimeError("no pg_stat_activity access")

        monkeypatch.setattr(module, "release_previous_boot_transactions", boom)
        module.release_stale_sessions()  # logged, never raised
