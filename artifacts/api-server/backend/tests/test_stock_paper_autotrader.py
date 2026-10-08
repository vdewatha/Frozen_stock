from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.core.config import settings
from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args, **kwargs):
        return self

    def filter_by(self, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class _Db:
    def __init__(self, strategies):
        self.strategies = strategies

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "Strategy":
            return _Query(self.strategies)
        return _Query([])

    def rollback(self):
        pass


def _account():
    return SimpleNamespace(
        id=1,
        status="reconciled",
        reconciliation_required=False,
        unexplained_residual=False,
        equity=Decimal("100000"),
    )


def test_cycle_is_safe_noop_when_market_is_closed():
    db = _Db([])
    with patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=False):
        result = run_stock_paper_signal_cycle(db)
    assert result["status"] == "skipped"
    assert result["live_trading"] is False


def test_cycle_fans_out_symbols_and_strategies_but_keeps_orders_gated():
    strategy = SimpleNamespace(id=1, strategy_type="moving_average_crossover")
    db = _Db([strategy])
    signal_calls = []

    def signal(_db, symbol, strategy_slug):
        signal_calls.append((symbol, strategy_slug))
        return {
            "signal_id": len(signal_calls),
            "signal_action": "BUY",
            "reference_price": "100",
        }

    with patch.object(settings, "paper_execution_symbols", ["AAPL", "SPY"]), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         patch("app.services.stock_paper_autotrader.create_stock_paper_signal", side_effect=signal), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", side_effect=RuntimeError("risk gate")):
        result = run_stock_paper_signal_cycle(db)

    assert result["signal_count"] == 2
    assert result["submitted_count"] == 0
    assert result["blocked_count"] == 2
    assert signal_calls == [("AAPL", "moving_average_crossover"), ("SPY", "moving_average_crossover")]


def test_cycle_dispatches_only_after_modern_reservation():
    strategy = SimpleNamespace(id=1, strategy_type="moving_average_crossover")
    db = _Db([strategy])
    order = SimpleNamespace(id=42, status="accepted")
    with patch.object(settings, "paper_execution_symbols", ["AAPL"]), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         patch("app.services.stock_paper_autotrader.create_stock_paper_signal", return_value={
             "signal_id": 7, "signal_action": "BUY", "reference_price": "100",
         }), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order) as dispatch:
        result = run_stock_paper_signal_cycle(db)

    assert result["submitted_count"] == 1
    reserve.assert_called_once()
    dispatch.assert_called_once_with(db, 42)
