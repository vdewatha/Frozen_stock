"""Per-strategy exit policy, symbol scope, lineup seeding and readiness.

Every test uses fakes or mocks; nothing reaches a broker.
"""
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
import app.models.stock_paper  # noqa: F401
from app.core.config import settings
from app.db.base import Base
from app.models import AuditLog, Strategy
from app.services import stock_paper_signal_batch as batch
from app.services.stock_paper_autotrader import (
    _exit_plan,
    _strategy_lots,
    run_stock_paper_signal_cycle,
)
from app.services.stock_paper_ledger import EXPLORATION_SOURCE
from app.services.strategies.base import ExitPolicy
from app.services.strategies.registry import get_strategy
from app.services.strategy_execution_policy import (
    evaluate_policy_exit,
    exit_policy_for,
    in_symbol_scope,
    sessions_held,
)
from tests.test_stock_paper_autotrader import _ModelDb, _account, signal_source

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
RSI2_PARAMS = {"stop_loss_pct": 0.10, "take_profit_pct": 0, "max_holding_sessions": 10,
               "symbol_scope": ["SPY", "QQQ", "IWM", "DIA"]}


def _rsi2(**overrides):
    return SimpleNamespace(id=21, strategy_type="rsi2_pullback", current_status="paper_trading_candidate",
                           is_active=True, parameters={**RSI2_PARAMS, **overrides})


_MA = SimpleNamespace(id=1, strategy_type="moving_average_crossover", current_status="paper_trading_candidate",
                      is_active=True, parameters={"short_window": 20, "long_window": 50})


# --------------------------------------------------------------- declarations

def test_reversion_strategies_declare_policy_and_index_scope():
    for slug in ("rsi2_pullback", "ibs_mean_reversion"):
        strategy = get_strategy(slug)
        assert strategy.exit_policy() == ExitPolicy(stop_loss_pct=0.10, take_profit_pct=0.0,
                                                    max_holding_sessions=10, indicator_exit=True)
        assert strategy.symbol_scope() == {"SPY", "QQQ", "IWM", "DIA"}
    for slug in ("moving_average_crossover", "macd_momentum", "model_predictive_long"):
        assert get_strategy(slug).exit_policy() is None
        assert get_strategy(slug).symbol_scope() is None


def test_policy_overrides_and_invalid_values_never_drop_the_stop():
    policy = get_strategy("rsi2_pullback", {"stop_loss_pct": 0.08, "take_profit_pct": 0.2,
                                            "max_holding_sessions": 5}).exit_policy()
    assert (policy.stop_loss_pct, policy.take_profit_pct, policy.max_holding_sessions) == (0.08, 0.2, 5)
    bad = get_strategy("rsi2_pullback", {"stop_loss_pct": 0, "take_profit_pct": -1,
                                         "max_holding_sessions": 2.5}).exit_policy()
    assert (bad.stop_loss_pct, bad.take_profit_pct, bad.max_holding_sessions) == (0.10, 0.0, 10)
    assert get_strategy("rsi2_pullback", {"symbol_scope": "spy, qqq"}).symbol_scope() == {"SPY", "QQQ"}
    assert in_symbol_scope(_rsi2(), "spy") and not in_symbol_scope(_rsi2(), "AAPL")
    assert in_symbol_scope(_MA, "AAPL")


# ------------------------------------------------------------- exit payloads

def test_policy_exit_plan_is_stop_only_oto_and_defaults_keep_bracket():
    rules = {"position_stop_loss_pct": "0.03", "position_take_profit_pct": "0.03"}
    oto = _exit_plan(rules, Decimal("100"), exit_policy_for(_rsi2()))
    assert oto == {"order_class": "oto", "stop_price": Decimal("90.00"), "take_profit_price": None}
    wide = _exit_plan(rules, Decimal("100"), exit_policy_for(_rsi2(take_profit_pct=0.2)))
    assert wide == {"order_class": "bracket", "stop_price": Decimal("90.00"), "take_profit_price": Decimal("120.00")}
    default = _exit_plan(rules, Decimal("100"), exit_policy_for(_MA))
    assert default == {"order_class": "bracket", "stop_price": Decimal("97.00"), "take_profit_price": Decimal("103.00")}
    # A strategy policy keeps its catastrophe stop even when the rule disables legs.
    assert _exit_plan({"position_stop_loss_pct": 0, "position_take_profit_pct": 0}, Decimal("100"),
                      exit_policy_for(_rsi2()))["stop_price"] == Decimal("90.00")


def test_oto_payload_carries_only_the_stop_leg():
    from tests.test_stock_paper_bracket_exits import BracketGateway, BracketLedgerTests
    from app.services.stock_paper_ledger import dispatch_reserved_order, reserve_stock_paper_order

    case = BracketLedgerTests("test_bracket_payload_shape_and_legs_persisted_with_strategy")
    case.setUp()
    try:
        with Session(case.engine) as db, ExitStack() as stack:
            case.gates(stack)
            _strategy, signal = case.seed(db)
            plan = _exit_plan({}, Decimal("100"), exit_policy_for(_rsi2()))
            order = reserve_stock_paper_order(
                db, symbol="SPY", side="buy", quantity=Decimal("2"), reference_price=Decimal("100"),
                idempotency_key="oto-entry", source=EXPLORATION_SOURCE, signal_id=signal.id, exit_plan=plan,
            )
            gateway = BracketGateway()
            dispatch_reserved_order(db, order.id, gateway)
            payload = gateway.payloads[0]
            assert payload["order_class"] == "oto"
            assert payload["stop_loss"] == {"stop_price": "90.00"}
            assert "take_profit" not in payload
            assert payload["time_in_force"] == "gtc" and payload["qty"] == "2"
    finally:
        case.tearDown()


# ------------------------------------------------------------ cycle harness

def _bars(closes, last_day):
    """Daily frame of completed sessions ending on ``last_day``."""
    days = []
    day = last_day
    while len(days) < len(closes):
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({"date": days, "open": closes, "high": closes * 1.01, "low": closes * 0.99,
                         "close": closes, "volume": 1_000_000, "source": "yfinance"})


def _cycle(db, signals, *, symbols=("SPY",), holdings=None, attributed=True, lots=None, frame=None,
           pending=(), exit_orders=(), cancel=None, now=None):
    """Run one cycle with every gate and the broker mocked out."""
    order = SimpleNamespace(id=42, status="accepted")
    db.rows_by_model.setdefault("RiskRule", [SimpleNamespace(value={})])
    calls = MagicMock()
    with ExitStack() as stack:
        stack.enter_context(patch.object(settings, "paper_execution_symbols", list(symbols)))
        stack.enter_context(patch.object(settings, "paper_exploration_enabled", True))
        stack.enter_context(patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()))
        stack.enter_context(patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True))
        stack.enter_context(patch("app.services.stock_paper_autotrader.cancel_stale_exit_entries", return_value=[]))
        if now is not None:
            fake_dt = MagicMock(wraps=datetime)
            fake_dt.now.return_value = now
            stack.enter_context(patch("app.services.stock_paper_autotrader.datetime", fake_dt))
        signal_mock = stack.enter_context(signal_source(True, side_effect=signals))
        stack.enter_context(patch("app.services.stock_paper_autotrader._strategy_holdings",
                                  return_value=(holdings or {}, attributed)))
        stack.enter_context(patch("app.services.stock_paper_autotrader._lot_entry_times",
                                  return_value=lots or {"opened_at": {}, "buyers": set()}))
        stack.enter_context(patch("app.services.stock_paper_autotrader._daily_frame", return_value=frame))
        stack.enter_context(patch("app.services.stock_paper_autotrader._pending_orders", return_value=list(pending)))
        stack.enter_context(patch("app.services.stock_paper_autotrader.broker_stop_coverage",
                                  return_value=Decimal("1000")))
        stack.enter_context(patch("app.services.stock_paper_autotrader.live_exit_orders",
                                  return_value=list(exit_orders)))
        stack.enter_context(patch("app.services.stock_paper_autotrader.release_unsubmitted_reservation"))
        calls.cancel.return_value = cancel or {"cancelled_order_ids": [11], "clear": True, "leg_filled": False}
        calls.reserve.return_value = order
        calls.dispatch.return_value = order
        stack.enter_context(patch("app.services.stock_paper_autotrader.cancel_broker_exit_orders",
                                  side_effect=lambda *_a, **_k: calls.cancel()))
        stack.enter_context(patch("app.services.stock_paper_autotrader.reserve_stock_paper_order",
                                  side_effect=lambda *_a, **kw: calls.reserve(**kw)))
        stack.enter_context(patch("app.services.stock_paper_autotrader.dispatch_reserved_order",
                                  side_effect=lambda *_a, **_k: calls.dispatch()))
        result = run_stock_paper_signal_cycle(db)
    return result, calls, signal_mock


def _signal(action="HOLD", price="100"):
    def fake(_db, symbol, strategy_type):
        return {"signal_id": 7, "signal_action": action, "reference_price": price}
    return fake


def _leg_row(role="stop_loss"):
    return SimpleNamespace(id=11, side="sell", leg_role=role, status="held", strategy_id=21, parent_order_id=10)


def _position(quantity="2"):
    return SimpleNamespace(quantity=Decimal(quantity), average_entry_price=Decimal("100"))


def _session_days_before(today: date, sessions: int) -> date:
    day = today
    seen = 0
    while seen < sessions:
        day -= timedelta(days=1)
        if day.weekday() < 5 and sessions_held(day, day + timedelta(days=1)) == 1:
            seen += 1
    return day


NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)  # Wednesday 11:00 ET
TODAY = NOW.astimezone(ET).date()
YESTERDAY = date(2026, 10, 27)


# ------------------------------------------------------------- entries/scope

def test_rsi2_entry_uses_oto_catastrophe_stop_and_scope_limits_symbols():
    db = _ModelDb([_MA, _rsi2()], {})
    result, calls, signals = _cycle(db, _signal("BUY", "50"), symbols=("AAPL", "SPY"), frame=_calm_frame())
    # The signal double answers every pair (real batch scoping is tested
    # below); the cycle itself must ignore the out-of-scope pair.
    assert not any(row["symbol"] == "AAPL" and row["strategy"] == "rsi2_pullback" for row in result["results"])
    assert any(row["symbol"] == "AAPL" and row["strategy"] == "moving_average_crossover" for row in result["results"])
    assert not any(kw["symbol"] == "AAPL" and "rsi2" in kw["idempotency_key"] for _, kw in calls.reserve.call_args_list)
    rsi2_entry = next(kw for _, kw in calls.reserve.call_args_list
                      if kw["symbol"] == "SPY" and kw["side"] == "buy" and "rsi2" in kw["idempotency_key"])
    assert rsi2_entry["exit_plan"] == {"order_class": "oto", "stop_price": Decimal("45.00"), "take_profit_price": None}
    assert rsi2_entry["source"] == EXPLORATION_SOURCE
    assert rsi2_entry["quantity"] == Decimal("5")  # 0.25% exploration budget of 100k at $50, whole shares
    row = next(r for r in result["results"] if r["symbol"] == "SPY" and r["strategy"] == "rsi2_pullback")
    assert row["exit_plan"]["order_class"] == "oto" and row["exit_plan"]["take_profit_price"] is None
    # Calm history: target/realized vol > 1, capped at 1x (never sizes up).
    assert row["volatility_scale"]["scale"] == 1.0 and row["volatility_scale"]["status"] == "ok"
    ma_row = next(r for r in result["results"] if r["symbol"] == "SPY" and r["strategy"] == "moving_average_crossover")
    assert "volatility_scale" not in ma_row  # only daily-bar strategies are vol-scaled


def _calm_frame(rows: int = 40, step: float = 0.001):
    closes = [50.0 * (1 + (step if i % 2 else -step)) for i in range(rows)]
    return pd.DataFrame({"close": closes})


def _wild_frame(rows: int = 40, step: float = 0.05):
    return _calm_frame(rows, step)


def _rsi2_entry(calls):
    return next(kw for _, kw in calls.reserve.call_args_list
                if kw["side"] == "buy" and "rsi2" in kw["idempotency_key"])


def test_rsi2_entry_volatility_scaling_shrinks_and_fails_closed():
    # +-5% daily swings (~79% annualized) -> 0.15 / 0.79 = 0.19, floored at 0.25x:
    # 5 shares * 0.25 = 1.25 -> 1 whole share.
    result, calls, _ = _cycle(_ModelDb([_rsi2()], {}), _signal("BUY", "50"), frame=_wild_frame())
    assert _rsi2_entry(calls)["quantity"] == Decimal("1")
    row = next(r for r in result["results"] if r["strategy"] == "rsi2_pullback")
    assert row["volatility_scale"]["scale"] == 0.25 and row["volatility_scale"]["realized_vol"] > 0.7
    # No usable history: fail closed to the floor rather than full size.
    result, calls, _ = _cycle(_ModelDb([_rsi2()], {}), _signal("BUY", "50"), frame=None)
    assert _rsi2_entry(calls)["quantity"] == Decimal("1")
    row = next(r for r in result["results"] if r["strategy"] == "rsi2_pullback")
    assert row["volatility_scale"] == {"scale": 0.25, "status": "volatility_unavailable",
                                      "target_vol": 0.15, "lookback_days": 20}


def test_volatility_size_scale_bounds():
    from app.services.stock_paper_autotrader import realized_volatility, volatility_size_scale

    assert realized_volatility([1.0] * 5, 20) is None  # too short
    assert realized_volatility([1.0] * 21, 20) is None  # zero vol is not usable
    assert realized_volatility([1.0] * 20 + [float("nan")], 20) is None
    mid = _calm_frame(rows=21, step=0.0075)["close"].tolist()  # ~24% annualized
    vol = realized_volatility(mid, 20)
    assert 0.2 < vol < 0.3
    scaled = volatility_size_scale(mid)
    assert scaled["status"] == "ok" and abs(scaled["scale"] - 0.15 / vol) < 1e-6
    assert volatility_size_scale(_calm_frame()["close"])["scale"] == 1.0
    assert volatility_size_scale(None)["scale"] == 0.25
    with patch.object(settings, "paper_vol_target_annual", 0):
        assert volatility_size_scale(None) == {"scale": 1.0, "status": "disabled"}


def test_quantity_for_buy_scale_never_increases_size():
    from app.services.stock_paper_autotrader import _quantity_for_buy

    account = _account()
    base = _quantity_for_buy(account, Decimal("50"), {}, exploration=True)
    assert _quantity_for_buy(account, Decimal("50"), {}, exploration=True, size_scale=Decimal("3")) == base
    assert _quantity_for_buy(account, Decimal("50"), {}, exploration=True, size_scale=Decimal("0.5")) == base / 2


def test_signal_batch_skips_out_of_scope_pairs():
    from tests.test_stock_paper_signal_batch import _DataDouble

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    batch.clear_signal_cache()
    data = _DataDouble(["SPY", "AAPL"])
    try:
        with Session(engine) as db:
            db.add(Strategy(name="RSI(2) Pullback", strategy_type="rsi2_pullback", parameters=dict(RSI2_PARAMS),
                            is_active=True, current_status="paper_trading_candidate"))
            db.add(Strategy(name="MA", strategy_type="moving_average_crossover", parameters={},
                            is_active=True, current_status="paper_trading_candidate"))
            db.commit()
            stack = data.patches()
            for item in stack:
                item.start()
            try:
                outcomes, stats = batch.generate_cycle_signals(
                    db, ["SPY", "AAPL"], db.query(Strategy).order_by(Strategy.id).all())
            finally:
                for item in stack:
                    item.stop()
        assert set(outcomes) == {("SPY", "rsi2_pullback"), ("SPY", "moving_average_crossover"),
                                 ("AAPL", "moving_average_crossover")}
        assert stats["out_of_scope_pairs"] == 1
        assert stats["signals_persisted"] == 3
    finally:
        batch.clear_signal_cache()
        engine.dispose()


# ------------------------------------------------------------- policy exits

def _rising_frame():
    # Last close well above its 5-day SMA: Connors exit condition.
    return _bars([100.0] * 250 + [95, 94, 96, 99, 104], YESTERDAY)


def test_indicator_exit_cancels_live_legs_before_selling():
    entry = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)  # Friday entry, 3 sessions held
    legs = [_leg_row()]
    db = _ModelDb([_rsi2()], {"StockPaperPosition": [_position("2")]})
    result, calls, _ = _cycle(db, _signal("HOLD"), holdings={"rsi2_pullback": Decimal("2")},
                              lots={"opened_at": {"rsi2_pullback": entry}, "buyers": {"rsi2_pullback"}},
                              frame=_rising_frame(), pending=legs, exit_orders=legs, now=NOW)
    assert [name for name, *_ in calls.mock_calls] == ["reserve", "cancel", "dispatch"]
    kwargs = calls.reserve.call_args.kwargs
    assert kwargs["side"] == "sell" and kwargs["quantity"] == Decimal("2")
    assert kwargs["replacing_exit_order_ids"] == {11}
    assert kwargs["idempotency_key"] == "explore-paper:2026-10-28:SPY:rsi2_pullback:sell"
    row = next(r for r in result["results"] if r.get("exit_policy"))
    assert row["exit_policy"]["reason"] == "indicator_exit:close_above_sma5"
    assert row["exit_policy"]["sessions_held"] == 3
    assert row["status"] == "submitted"


def test_time_stop_exits_after_max_sessions_without_indicator():
    flat = _bars([100.0] * 255, YESTERDAY)  # close == SMA5: no indicator exit
    entry = datetime.combine(_session_days_before(TODAY, 10), datetime.min.time(), ET).replace(hour=11)
    db = _ModelDb([_rsi2()], {"StockPaperPosition": [_position()]})
    result, calls, _ = _cycle(db, _signal("HOLD"), holdings={"rsi2_pullback": Decimal("2")},
                              lots={"opened_at": {"rsi2_pullback": entry}, "buyers": {"rsi2_pullback"}},
                              frame=flat, now=NOW)
    assert calls.reserve.call_args.kwargs["side"] == "sell"
    row = next(r for r in result["results"] if r.get("exit_policy"))
    assert row["exit_policy"]["reason"] == "time_stop" and row["exit_policy"]["sessions_held"] == 10

    entry_9 = datetime.combine(_session_days_before(TODAY, 9), datetime.min.time(), ET).replace(hour=11)
    _result, calls, _ = _cycle(db, _signal("HOLD"), holdings={"rsi2_pullback": Decimal("2")},
                               lots={"opened_at": {"rsi2_pullback": entry_9}, "buyers": {"rsi2_pullback"}},
                               frame=flat, now=NOW)
    calls.reserve.assert_not_called()


def test_no_policy_exit_in_the_entry_session():
    entry = NOW - timedelta(hours=1)
    db = _ModelDb([_rsi2()], {"StockPaperPosition": [_position()]})
    _result, calls, _ = _cycle(db, _signal("HOLD"), holdings={"rsi2_pullback": Decimal("2")},
                               lots={"opened_at": {"rsi2_pullback": entry}, "buyers": {"rsi2_pullback"}},
                               frame=_rising_frame(), now=NOW)
    calls.reserve.assert_not_called()


def test_persistent_buy_is_superseded_by_the_exit():
    entry = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
    db = _ModelDb([_rsi2()], {"StockPaperPosition": [_position()]})
    result, calls, _ = _cycle(db, _signal("BUY"), holdings={"rsi2_pullback": Decimal("2")},
                              lots={"opened_at": {"rsi2_pullback": entry}, "buyers": {"rsi2_pullback"}},
                              frame=_rising_frame(), now=NOW)
    assert calls.reserve.call_count == 1 and calls.reserve.call_args.kwargs["side"] == "sell"
    row = next(r for r in result["results"] if r["strategy"] == "rsi2_pullback")
    assert row["superseded_signal"] == "BUY"


def test_pending_sell_prevents_a_double_sell():
    entry = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
    pending_sell = SimpleNamespace(id=55, side="sell", leg_role=None, status="accepted", strategy_id=None)
    db = _ModelDb([_rsi2()], {"StockPaperPosition": [_position()]})
    result, calls, _ = _cycle(db, _signal("HOLD"), holdings={"rsi2_pullback": Decimal("2")},
                              lots={"opened_at": {"rsi2_pullback": entry}, "buyers": {"rsi2_pullback"}},
                              frame=_rising_frame(), pending=[pending_sell], now=NOW)
    calls.reserve.assert_not_called()
    row = next(r for r in result["results"] if r.get("exit_policy"))
    assert row["status"] == "skipped" and "already pending" in row["reason"]


def test_own_sell_signal_and_policy_exit_produce_one_sell():
    entry = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
    db = _ModelDb([_rsi2()], {"StockPaperPosition": [_position()]})
    result, calls, _ = _cycle(db, _signal("SELL"), holdings={"rsi2_pullback": Decimal("2")},
                              lots={"opened_at": {"rsi2_pullback": entry}, "buyers": {"rsi2_pullback"}},
                              frame=_rising_frame(), now=NOW)
    assert calls.reserve.call_count == 1
    rows = [r for r in result["results"] if r["strategy"] == "rsi2_pullback"]
    assert len(rows) == 1 and rows[0]["exit_policy"]["reason"].startswith("indicator_exit")


def test_policy_exit_never_shorts_or_sells_another_strategys_lot():
    entry = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
    lots = {"opened_at": {"rsi2_pullback": entry}, "buyers": {"rsi2_pullback"}}
    # No position: nothing to exit.
    db = _ModelDb([_rsi2()], {})
    _result, calls, _ = _cycle(db, _signal("HOLD"), holdings={"rsi2_pullback": Decimal("2")}, lots=lots,
                               frame=_rising_frame(), now=NOW)
    calls.reserve.assert_not_called()
    # The position belongs to another strategy.
    db = _ModelDb([_MA, _rsi2()], {"StockPaperPosition": [_position()]})
    _result, calls, _ = _cycle(db, _signal("HOLD"), holdings={"moving_average_crossover": Decimal("2")},
                               lots={"opened_at": {"moving_average_crossover": entry},
                                     "buyers": {"moving_average_crossover"}},
                               frame=_rising_frame(), now=NOW)
    calls.reserve.assert_not_called()
    # Unknown attribution with several buyers: no policy-driven liquidation.
    _result, calls, _ = _cycle(db, _signal("HOLD"), holdings={}, attributed=False,
                               lots={"opened_at": {"rsi2_pullback": entry},
                                     "buyers": {"rsi2_pullback", "moving_average_crossover"}},
                               frame=_rising_frame(), now=NOW)
    calls.reserve.assert_not_called()


def test_sole_buyer_with_unattributed_fill_still_exits_whole_position():
    entry = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
    db = _ModelDb([_rsi2()], {"StockPaperPosition": [_position("3")]})
    _result, calls, _ = _cycle(db, _signal("HOLD"), holdings={}, attributed=False,
                               lots={"opened_at": {"rsi2_pullback": entry}, "buyers": {"rsi2_pullback"}},
                               frame=_rising_frame(), now=NOW)
    assert calls.reserve.call_args.kwargs["quantity"] == Decimal("3")


def test_policy_exit_still_runs_for_a_paused_strategy():
    """Exits only reduce risk: a lot outlives its strategy's exploration status."""
    entry = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
    paused = _rsi2()
    paused.current_status = "paused"
    db = _ModelDb([_MA, paused], {"StockPaperPosition": [_position()]})
    _result, calls, _ = _cycle(db, _signal("HOLD"), holdings={"rsi2_pullback": Decimal("2")},
                               lots={"opened_at": {"rsi2_pullback": entry}, "buyers": {"rsi2_pullback"}},
                               frame=_rising_frame(), now=NOW)
    kwargs = calls.reserve.call_args.kwargs
    assert kwargs["side"] == "sell" and kwargs["source"] == "scheduled_paper_signal"


def test_strategies_without_policy_keep_existing_behaviour():
    db = _ModelDb([_MA], {"StockPaperPosition": [_position()]})
    _result, calls, _ = _cycle(db, _signal("HOLD"), holdings={"moving_average_crossover": Decimal("2")},
                               lots={"opened_at": {"moving_average_crossover": datetime(2026, 1, 2, tzinfo=UTC)},
                                     "buyers": {"moving_average_crossover"}},
                               frame=_rising_frame(), now=NOW)
    calls.reserve.assert_not_called()  # no time stop or indicator exit for an undeclared policy


# ------------------------------------------------------------ pure helpers

def test_sessions_held_counts_nyse_sessions_only():
    assert sessions_held(date(2026, 10, 23), date(2026, 10, 26)) == 1  # Fri -> Mon
    assert sessions_held(date(2026, 10, 26), date(2026, 10, 26)) == 0
    assert sessions_held(date(2026, 11, 25), date(2026, 11, 30)) == 2  # Wed, (Thanksgiving), Fri


def test_evaluate_policy_exit_needs_the_completed_entry_bar():
    rsi2 = _rsi2()
    frame = _bars([100.0] * 250 + [95, 94, 96, 99, 104], date(2026, 10, 26))
    # Entered on 10/27 but bars end 10/26: the entry bar is not complete yet.
    assert evaluate_policy_exit(rsi2, "SPY", frame, datetime(2026, 10, 27, 15, tzinfo=UTC), NOW) is None
    # Today's in-progress bar (if a frame ever carried one) is ignored.
    today_bar = _bars([100.0] * 254 + [130], TODAY)
    # Completed bars are flat (close == SMA5) and 6 sessions < 10: hold.
    assert evaluate_policy_exit(rsi2, "SPY", today_bar, datetime(2026, 10, 20, 15, tzinfo=UTC), NOW) is None
    rising = evaluate_policy_exit(rsi2, "SPY", _rising_frame(), datetime(2026, 10, 20, 15, tzinfo=UTC), NOW)
    assert rising["reason"] == "indicator_exit:close_above_sma5"
    assert rising["last_completed_bar"] == YESTERDAY.isoformat() and rising["sessions_held"] == 6


def test_strategy_lots_restart_attribution_after_flat():
    t0 = datetime(2026, 10, 1, 15, tzinfo=UTC)
    fills = [
        SimpleNamespace(order_id=1, side="buy", quantity=Decimal("5"), filled_at=t0),
        SimpleNamespace(order_id=None, side="sell", quantity=Decimal("5"), filled_at=t0 + timedelta(days=1)),
        SimpleNamespace(order_id=2, side="buy", quantity=Decimal("2"), filled_at=t0 + timedelta(days=5)),
    ]
    orders = [SimpleNamespace(id=1, strategy_id=1), SimpleNamespace(id=2, strategy_id=21)]
    db = _ModelDb([], {"StockPaperFill": fills, "StockPaperOrder": orders})
    lots = _strategy_lots(db, 1, "SPY", {1: "moving_average_crossover", 21: "rsi2_pullback"})
    assert lots["attributed"] is True
    assert lots["holdings"] == {"moving_average_crossover": Decimal("0"), "rsi2_pullback": Decimal("2")}
    assert lots["opened_at"] == {"rsi2_pullback": t0 + timedelta(days=5)}
    assert lots["buyers"] == {"rsi2_pullback"}
    # An unattributed partial sell inside the open episode marks it unattributed.
    fills.append(SimpleNamespace(order_id=None, side="sell", quantity=Decimal("1"), filled_at=t0 + timedelta(days=6)))
    lots = _strategy_lots(db, 1, "SPY", {1: "moving_average_crossover", 21: "rsi2_pullback"})
    assert lots["attributed"] is False and lots["buyers"] == {"rsi2_pullback"}


# ------------------------------------------------------- seeding and demotion

@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_lineup_seeds_candidates_and_demotes_active_crossover_once(db):
    from app.services.seed import SCORECARD_DEMOTION_ACTION, ensure_strategy_lineup

    db.add(Strategy(name="Moving Average Crossover", strategy_type="moving_average_crossover",
                    parameters={}, is_active=True, current_status="paper_trading_active"))
    db.commit()
    outcome = ensure_strategy_lineup(db)
    db.commit()
    assert outcome == {"created": ["rsi2_pullback", "ibs_mean_reversion"], "demoted_moving_average_crossover": True}
    rows = {row.strategy_type: row for row in db.query(Strategy).all()}
    assert rows["moving_average_crossover"].current_status == "paper_trading_candidate"
    assert rows["rsi2_pullback"].current_status == "paper_trading_candidate"
    assert rows["ibs_mean_reversion"].parameters["entry_threshold"] == 0.1
    audit = db.query(AuditLog).filter_by(action=SCORECARD_DEMOTION_ACTION).one()
    assert audit.event_type == "strategy_governance"
    assert audit.entity_id == rows["moving_average_crossover"].id
    assert "scorecard" in audit.message.lower()
    assert audit.payload["old_status"] == "paper_trading_active"

    # Idempotent; and a later governed re-activation is not undone.
    rows["moving_average_crossover"].current_status = "paper_trading_active"
    rows["rsi2_pullback"].current_status = "paused"
    db.commit()
    assert ensure_strategy_lineup(db) == {"created": [], "demoted_moving_average_crossover": False}
    db.commit()
    assert rows["moving_average_crossover"].current_status == "paper_trading_active"
    assert rows["rsi2_pullback"].current_status == "paused"
    assert db.query(AuditLog).filter_by(action=SCORECARD_DEMOTION_ACTION).count() == 1


# -------------------------------------------------------------- readiness

def _availability(db, exploration):
    from app.services import readiness
    from tests.test_trading_universe import readiness_checks

    with patch.object(settings, "paper_exploration_enabled", exploration):
        checks = readiness_checks(readiness, db)
    return next(check for check in checks if check["name"] == "Strategy availability")


def test_readiness_with_zero_active_strategies(db):
    db.add(Strategy(name="RSI(2) Pullback", strategy_type="rsi2_pullback", parameters={},
                    is_active=True, current_status="paper_trading_candidate"))
    db.add(Strategy(name="Trial", strategy_type="not_registered", parameters={},
                    is_active=True, current_status="paper_trading_candidate"))
    db.commit()
    explore = _availability(db, True)
    assert explore["status"] == "ready"
    assert explore["details"]["mode"] == "exploration_only"
    assert explore["details"]["exploration_strategies"] == 1
    off = _availability(db, False)
    assert off["status"] == "warning"
    assert off["details"]["mode"] == "none"


def test_readiness_with_no_strategies_at_all_stays_a_warning(db):
    assert _availability(db, True)["status"] == "warning"
