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


def test_candidate_strategies_are_observed_without_order_path():
    strategy = SimpleNamespace(id=2, strategy_type="rsi_mean_reversion", current_status="research")
    db = _Db([strategy])
    with patch.object(settings, "paper_execution_symbols", ["AAPL"]), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         patch("app.services.stock_paper_autotrader.create_stock_paper_research_signal", return_value={
             "signal_id": 8, "signal_action": "BUY", "reference_price": "100",
         }) as observe, \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order") as reserve:
        result = run_stock_paper_signal_cycle(db)

    assert result["signal_count"] == 1
    assert result["submitted_count"] == 0
    assert result["blocked_count"] == 0
    assert result["results"][0]["status"] == "observed"
    observe.assert_called_once_with(db, "AAPL", "rsi_mean_reversion")
    reserve.assert_not_called()


# --- signal conflict, pyramiding and cross-strategy exit regressions -------

class _ModelDb(_Db):
    def __init__(self, strategies, rows_by_model):
        super().__init__(strategies)
        self.rows_by_model = rows_by_model

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "Strategy":
            return _Query(self.strategies)
        return _Query(self.rows_by_model.get(name, []))


def _signals(actions):
    def signal(_db, symbol, strategy_slug):
        return {"signal_id": hash(strategy_slug) % 1000, "signal_action": actions[strategy_slug], "reference_price": "100"}
    return signal


def _run(db, actions, holdings, pending=(), attributed=True):
    order = SimpleNamespace(id=42, status="accepted")
    with patch.object(settings, "paper_execution_symbols", ["SPY"]), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         patch("app.services.stock_paper_autotrader.create_stock_paper_signal", side_effect=_signals(actions)), \
         patch("app.services.stock_paper_autotrader._strategy_holdings", return_value=(holdings, attributed)), \
         patch("app.services.stock_paper_autotrader._pending_orders", return_value=list(pending)), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order):
        result = run_stock_paper_signal_cycle(db)
    return result, reserve


_MA = SimpleNamespace(id=1, strategy_type="moving_average_crossover")
_MACD = SimpleNamespace(id=3, strategy_type="macd_momentum")


def test_opposing_signals_exit_first_and_block_same_cycle_reentry():
    db = _ModelDb([_MA, _MACD], {"StockPaperPosition": [SimpleNamespace(quantity=Decimal("5"))]})
    result, reserve = _run(db, {"moving_average_crossover": "BUY", "macd_momentum": "SELL"},
                           {"macd_momentum": Decimal("5")})
    assert reserve.call_count == 1
    assert reserve.call_args.kwargs["side"] == "sell"
    by_strategy = {row["strategy"]: row for row in result["results"]}
    assert by_strategy["macd_momentum"]["status"] == "submitted"
    assert by_strategy["moving_average_crossover"]["status"] == "skipped"
    assert "re-entry" in by_strategy["moving_average_crossover"]["reason"]


def test_persistent_buy_state_does_not_pyramid_an_owned_position():
    db = _ModelDb([_MA], {"StockPaperPosition": [SimpleNamespace(quantity=Decimal("5"))]})
    result, reserve = _run(db, {"moving_average_crossover": "BUY"}, {"moving_average_crossover": Decimal("5")})
    reserve.assert_not_called()
    assert result["results"][0]["status"] == "skipped"
    assert "pyramiding" in result["results"][0]["reason"]


def test_pending_buy_from_same_strategy_blocks_duplicate_entry():
    pending = [SimpleNamespace(id=9, side="buy", strategy_id=1)]
    db = _ModelDb([_MA], {})
    result, reserve = _run(db, {"moving_average_crossover": "BUY"}, {}, pending=pending)
    reserve.assert_not_called()
    assert result["results"][0]["status"] == "skipped"


def test_sell_from_non_owning_strategy_does_not_liquidate_other_strategy_lots():
    db = _ModelDb([_MA, _MACD], {"StockPaperPosition": [SimpleNamespace(quantity=Decimal("5"))]})
    result, reserve = _run(db, {"moving_average_crossover": "HOLD", "macd_momentum": "SELL"},
                           {"moving_average_crossover": Decimal("5")})
    reserve.assert_not_called()
    by_strategy = {row["strategy"]: row for row in result["results"]}
    assert by_strategy["macd_momentum"]["status"] == "skipped"
    assert "attributable" in by_strategy["macd_momentum"]["reason"]


def test_sell_exits_only_the_owning_strategy_quantity():
    db = _ModelDb([_MA, _MACD], {"StockPaperPosition": [SimpleNamespace(quantity=Decimal("8"))]})
    _result, reserve = _run(db, {"moving_average_crossover": "HOLD", "macd_momentum": "SELL"},
                            {"moving_average_crossover": Decimal("5"), "macd_momentum": Decimal("3")})
    assert reserve.call_args.kwargs["quantity"] == Decimal("3")


def test_unattributed_position_keeps_full_risk_reduction_available():
    db = _ModelDb([_MACD], {"StockPaperPosition": [SimpleNamespace(quantity=Decimal("8"))]})
    _result, reserve = _run(db, {"macd_momentum": "SELL"}, {}, attributed=False)
    assert reserve.call_args.kwargs["quantity"] == Decimal("8")


def test_strategy_holdings_nets_fills_by_strategy_type():
    from app.services.stock_paper_autotrader import _strategy_holdings

    fills = [
        SimpleNamespace(order_id=1, side="buy", quantity=Decimal("5")),
        SimpleNamespace(order_id=2, side="buy", quantity=Decimal("3")),
        SimpleNamespace(order_id=3, side="sell", quantity=Decimal("1")),
    ]
    orders = [SimpleNamespace(id=1, strategy_id=1), SimpleNamespace(id=2, strategy_id=3),
              SimpleNamespace(id=3, strategy_id=3)]
    db = _ModelDb([], {"StockPaperFill": fills, "StockPaperOrder": orders})
    holdings, attributed = _strategy_holdings(db, 1, "SPY", {1: "moving_average_crossover", 3: "macd_momentum"})
    assert attributed is True
    assert holdings == {"moving_average_crossover": Decimal("5"), "macd_momentum": Decimal("2")}

    fills.append(SimpleNamespace(order_id=None, side="buy", quantity=Decimal("1")))
    _holdings, attributed = _strategy_holdings(db, 1, "SPY", {1: "moving_average_crossover", 3: "macd_momentum"})
    assert attributed is False


def _stop_position(entry, quantity="4"):
    return SimpleNamespace(quantity=Decimal(quantity), average_entry_price=Decimal(entry))


def test_protective_stop_exits_breached_long_and_blocks_reentry():
    db = _ModelDb([_MA], {"StockPaperPosition": [_stop_position("110")]})  # price 100 => -9.1%
    result, reserve = _run(db, {"moving_average_crossover": "BUY"}, {"moving_average_crossover": Decimal("4")})
    assert reserve.call_count == 1
    kwargs = reserve.call_args.kwargs
    assert kwargs["side"] == "sell" and kwargs["quantity"] == Decimal("4")
    assert kwargs["idempotency_key"].endswith(":SPY:protective_stop:sell")
    by_strategy = {row["strategy"]: row for row in result["results"]}
    assert by_strategy["protective_stop"]["status"] == "submitted"
    assert by_strategy["moving_average_crossover"]["status"] == "skipped"


def test_protective_stop_runs_even_when_no_signal_is_actionable():
    db = _ModelDb([_MA], {"StockPaperPosition": [_stop_position("110")]})
    _result, reserve = _run(db, {"moving_average_crossover": "HOLD"}, {})
    assert reserve.call_args.kwargs["side"] == "sell"


def test_protective_stop_does_not_fire_inside_budget_or_when_disabled():
    db = _ModelDb([_MA], {"StockPaperPosition": [_stop_position("102")]})  # -1.96%
    _result, reserve = _run(db, {"moving_average_crossover": "HOLD"}, {})
    reserve.assert_not_called()
    rule = SimpleNamespace(value={"position_stop_loss_pct": 0})
    db = _ModelDb([_MA], {"StockPaperPosition": [_stop_position("150")], "RiskRule": [rule]})
    _result, reserve = _run(db, {"moving_average_crossover": "HOLD"}, {})
    reserve.assert_not_called()
