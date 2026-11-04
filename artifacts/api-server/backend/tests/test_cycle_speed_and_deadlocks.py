"""Signal-cycle admission reuse, duplicate reconciliation skip and deadlock retries."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

from app.services import stock_paper_ledger as ledger
from app.tasks import jobs

FAILING_GATES = {"notifications": {"status": "fail", "reason": "Unresolved critical notifications"}}


class _Info:
    def __init__(self, cached=False):
        self.info = {ledger.LAUNCH_GATES_CACHE_ENABLED: True} if cached else {}

    def query(self, *_a, **_k):
        query = MagicMock()
        query.filter_by.return_value.one_or_none.return_value = None
        return query


def _reserve(db):
    with pytest.raises(ledger.StockPaperError, match="launch admission"):
        ledger.reserve_stock_paper_order(db, symbol="SPY", side="buy", quantity=Decimal("1"),
                                         reference_price=Decimal("100"), idempotency_key="k-" + str(id(db)),
                                         source=next(iter(ledger.AUTONOMOUS_ORDER_SOURCES)))


def test_a_cycle_evaluates_launch_admission_once_for_many_orders():
    db = _Info(cached=True)
    with patch("app.services.stock_learning_cycle.evaluate_launch_admission_prerequisites",
               return_value=FAILING_GATES) as evaluate:
        for _ in range(5):
            _reserve(db)
    assert evaluate.call_count == 1


def test_outside_a_cycle_every_reservation_evaluates_the_gates():
    db = _Info(cached=False)
    with patch("app.services.stock_learning_cycle.evaluate_launch_admission_prerequisites",
               return_value=FAILING_GATES) as evaluate:
        _reserve(db)
        _reserve(db)
    assert evaluate.call_count == 2
    assert ledger.LAUNCH_GATES_CACHE not in db.info


def test_cached_gates_expire_after_the_reuse_window():
    db = _Info(cached=True)
    db.info[ledger.LAUNCH_GATES_CACHE] = (datetime.now(timezone.utc) - timedelta(seconds=45), {})
    with patch("app.services.stock_learning_cycle.evaluate_launch_admission_prerequisites",
               return_value=FAILING_GATES) as evaluate:
        _reserve(db)
    assert evaluate.call_count == 1  # the stale all-pass entry was not reused


def test_signal_cycle_enables_then_clears_the_cache():
    from app.services import stock_paper_autotrader as autotrader

    db = SimpleNamespace(info={})
    seen = {}

    def inner(session):
        seen["enabled"] = session.info.get(ledger.LAUNCH_GATES_CACHE_ENABLED)
        session.info[ledger.LAUNCH_GATES_CACHE] = (datetime.now(timezone.utc), {})
        return {"status": "complete"}

    with patch.object(autotrader, "_run_signal_cycle", side_effect=inner):
        assert autotrader.run_stock_paper_signal_cycle(db)["status"] == "complete"
    assert seen["enabled"] is True and db.info == {}


def test_restart_reconciler_reuses_a_fresh_reconciliation():
    from app.services import stock_recovery

    account = SimpleNamespace(id=1, status="reconciled", reconciliation_required=False,
                              last_reconciled_at=datetime.now(timezone.utc) - timedelta(seconds=20))
    state = SimpleNamespace(status="paused", updated_by=None)
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    with patch.object(stock_recovery, "active_paper_account", return_value=account), \
         patch.object(stock_recovery, "reconcile_stock_paper_account") as reconcile, \
         patch.object(stock_recovery, "_state", return_value=state), \
         patch.object(stock_recovery, "_event"):
        result = stock_recovery.reconcile_inflight_stock_orders_on_restart(db)
        reconcile.assert_not_called()
        assert result["status"] == "reconciled" and state.status == "revalidation_required"
        account.last_reconciled_at = datetime.now(timezone.utc) - timedelta(minutes=3)
        reconcile.return_value = {"status": "reconciled"}
        stock_recovery.reconcile_inflight_stock_orders_on_restart(db)
        reconcile.assert_called_once()


class _Deadlock(Exception):
    sqlstate = "40P01"


def _deadlock():
    return OperationalError("UPDATE stock_paper_accounts", {}, _Deadlock("deadlock detected"))


def test_reconciliation_deadlock_victim_is_retried():
    calls = []

    def work(_db):
        calls.append(1)
        if len(calls) < 3:
            raise _deadlock()
        return {"status": "reconciled"}

    db = MagicMock()
    with patch.object(jobs.time, "sleep"):
        assert jobs._work_with_deadlock_retry("stock_paper_broker_reconciliation_job", work, db) == {"status": "reconciled"}
    assert len(calls) == 3 and db.rollback.call_count == 2


def test_deadlock_retries_are_bounded_and_limited_to_idempotent_jobs():
    def always(_db):
        raise _deadlock()

    with patch.object(jobs.time, "sleep"), pytest.raises(OperationalError):
        jobs._work_with_deadlock_retry("stock_forward_trial_reconcile_job", always, MagicMock())
    with pytest.raises(OperationalError):
        jobs._work_with_deadlock_retry("paper_trading_signal_job", always, MagicMock())

    def other(_db):
        raise OperationalError("SELECT 1", {}, Exception("connection reset"))

    with pytest.raises(OperationalError):
        jobs._work_with_deadlock_retry("stock_paper_broker_reconciliation_job", other, MagicMock())
