"""Automatic resume checks never queue on the account while holding the recovery row (2026-11-10).

Broker reconciliation holds the account row for its whole broker sync (40-85 s)
and updates the recovery row at its end. The explained-halt auto-resume check
(every 30 minutes, ~8.5 s each) took the recovery row and then queued on the
account; the watchdog queued on the recovery row between them, and Postgres
picked it as the deadlock victim at 19:16 and 19:46 UTC.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from app.services import auto_revalidation as auto
from app.services import stock_recovery
from tests.test_auto_revalidation import NOW, _pause, db  # noqa: F401  (fixture)


class _LockNotAvailable(Exception):
    sqlstate = "55P03"


class _DeadlockDetected(Exception):
    sqlstate = "40P01"


def _busy_query(*_a, **_k):
    query = MagicMock()
    query.filter_by.return_value.with_for_update.return_value.one_or_none.side_effect = OperationalError(
        "SELECT ... FOR UPDATE NOWAIT", {}, _LockNotAvailable("could not obtain lock on row"))
    return query


def test_no_wait_lock_on_a_busy_account_raises_busy_and_rolls_back():
    db = MagicMock()
    db.query.side_effect = _busy_query
    with pytest.raises(stock_recovery.RecoveryAccountBusy):
        stock_recovery._account_after_state(db, wait=False)
    db.rollback.assert_called_once()


def test_other_database_errors_are_not_swallowed():
    db = MagicMock()
    query = MagicMock()
    query.filter_by.return_value.with_for_update.return_value.one_or_none.side_effect = OperationalError(
        "SELECT", {}, Exception("connection reset"))
    db.query.return_value = query
    with pytest.raises(OperationalError):
        stock_recovery._account_after_state(db, wait=False)


def test_the_waiting_form_is_unchanged_for_operator_paths():
    db = MagicMock()
    with patch.object(stock_recovery, "active_paper_account", return_value="account") as active:
        assert stock_recovery._account_after_state(db) == "account"
    active.assert_called_once_with(db, for_update=True)


def test_explained_halt_auto_resume_gives_way_to_a_running_reconciliation(db):
    with patch.object(stock_recovery, "_account_after_state",
                      side_effect=stock_recovery.RecoveryAccountBusy(stock_recovery.ACCOUNT_BUSY_REASON)):
        result = stock_recovery.attempt_explained_halt_auto_resume(db, now=NOW)
    assert result == {"status": "busy", "reason": stock_recovery.ACCOUNT_BUSY_REASON}


def test_explained_halt_auto_resume_still_runs_when_the_account_is_free(db):
    result = stock_recovery.attempt_explained_halt_auto_resume(db, now=NOW)
    assert result["status"] == "not_halted"


def test_automatic_revalidation_gives_way_and_does_not_count_as_blocked(db):
    _pause(db)
    with patch.object(auto, "_fresh_monitoring_is_clear", return_value=(True, "test monitoring")), \
         patch.object(auto, "resume_stock_paper_after_revalidation",
                      side_effect=stock_recovery.RecoveryAccountBusy(stock_recovery.ACCOUNT_BUSY_REASON)) as resume:
        result = auto.run_auto_revalidation(db, now=NOW)
    assert result == {"status": "busy", "reason": stock_recovery.ACCOUNT_BUSY_REASON}
    assert resume.call_args.kwargs["wait_for_account"] is False


def test_operator_revalidation_waits_for_the_account(db):
    _pause(db)
    with patch.object(stock_recovery, "_account_after_state", wraps=stock_recovery._account_after_state) as lock:
        try:
            stock_recovery.resume_stock_paper_after_revalidation(db, actor="operator-1", reason="checked")
        except stock_recovery.StockPaperError:
            pass  # the gates may refuse; only the lock mode matters here
    assert lock.call_args.kwargs["wait"] is True


def test_the_watchdog_retries_once_when_it_is_the_deadlock_victim(monkeypatch):
    import scripts.run_stock_watchdog as watchdog

    calls = []

    def evaluate(db):
        calls.append(db)
        if len(calls) == 1:
            raise OperationalError("SELECT ... FOR UPDATE", {}, _DeadlockDetected("deadlock detected"))
        return {"status": "clear", "reasons": []}

    session = MagicMock()
    monkeypatch.setattr(watchdog, "SessionLocal", lambda: session)
    monkeypatch.setattr(watchdog, "run_stock_watchdog", evaluate)
    monkeypatch.setattr(watchdog.time, "sleep", lambda _s: None)
    assert watchdog.run_once() == {"status": "clear", "reasons": []}
    assert len(calls) == 2
    session.rollback.assert_called_once()


def test_the_watchdog_stays_fail_closed_after_a_second_deadlock(monkeypatch):
    import scripts.run_stock_watchdog as watchdog

    def evaluate(_db):
        raise OperationalError("SELECT ... FOR UPDATE", {}, _DeadlockDetected("deadlock detected"))

    monkeypatch.setattr(watchdog, "SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(watchdog, "run_stock_watchdog", evaluate)
    monkeypatch.setattr(watchdog.time, "sleep", lambda _s: None)
    result = watchdog.run_once()
    assert result["status"] == "unknown" and result["fail_closed"] is True


def test_the_watchdog_does_not_retry_other_errors(monkeypatch):
    import scripts.run_stock_watchdog as watchdog

    calls = []

    def evaluate(_db):
        calls.append(1)
        raise RuntimeError("bad evidence")

    monkeypatch.setattr(watchdog, "SessionLocal", lambda: SimpleNamespace(rollback=lambda: None, close=lambda: None))
    monkeypatch.setattr(watchdog, "run_stock_watchdog", evaluate)
    assert watchdog.run_once()["status"] == "unknown"
    assert calls == [1]
