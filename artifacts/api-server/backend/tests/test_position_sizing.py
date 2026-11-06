"""Position-sizing modes: arithmetic, caps, fallbacks, the backtest's rule, the cycle and the seed contract."""
from __future__ import annotations

import sys
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.core.config import settings
from app.models import AuditLog, Strategy
from app.services import position_sizing as ps
from app.services.stock_paper_autotrader import _quantity_for_buy, run_stock_paper_signal_cycle
from tests.test_loss_feedback_loop import _attribution, _strategy, db  # noqa: F401  (fixture)
from tests.test_stock_paper_autotrader import _Db, _account, signal_source

pytestmark = pytest.mark.usefixtures("legacy_technical_exits")

EQUITY = Decimal("100000")


# ------------------------------------------------------------------ parameters

def test_sizing_parameters_validate_mode_and_risk_budget():
    assert ps.sizing_parameters(None) == {"sizing_mode": "fixed_fraction", "risk_per_trade": 0.005}
    assert ps.sizing_parameters({"sizing_mode": "Volatility_Target", "risk_per_trade": "0.0025"}) == {
        "sizing_mode": "volatility_target", "risk_per_trade": 0.0025}
    assert ps.sizing_parameters({"sizing_mode": "martingale", "risk_per_trade": 0.2})["sizing_mode"] == "fixed_fraction"
    assert ps.sizing_parameters({"sizing_mode": "fractional_kelly", "risk_per_trade": 0.2})["risk_per_trade"] == 0.005
    assert ps.sizing_parameters({"risk_per_trade": "nope"})["risk_per_trade"] == 0.005


# ------------------------------------------------------------------ modes

def test_fixed_fraction_reproduces_the_live_rule_and_clamps_the_scale():
    out = ps.fixed_fraction(EQUITY, Decimal("0.01"), Decimal("0.5"))
    assert out["notional"] == Decimal("500") and out["status"] == "sized"
    assert ps.fixed_fraction(EQUITY, "0.01", 3)["notional"] == Decimal("1000")  # scale never above 1x
    assert ps.fixed_fraction(Decimal("-5"), "0.01")["notional"] == Decimal("0")
    account = SimpleNamespace(equity=EQUITY)
    live = _quantity_for_buy(account, Decimal("100"), {"max_risk_per_trade": "0.01"}, size_scale=Decimal("0.5"))
    assert live == (out["notional"] / Decimal("100")).quantize(Decimal("0.000001"))


def test_volatility_target_sizes_the_loss_at_the_stop_and_is_capped():
    # 0.5% of equity at a 10% stop = 5% notional; the cap is the fixed fraction.
    out = ps.volatility_target(EQUITY, 0.10, 0.005, cap_notional=Decimal("8000"))
    assert out["notional"] == Decimal("5000") and out["status"] == "sized" and out["capped"] is False
    capped = ps.volatility_target(EQUITY, 0.03, 0.005, cap_notional=Decimal("1000"))
    assert capped["notional"] == Decimal("1000") and capped["status"] == "capped"
    assert capped["target_notional"] == Decimal("16666.66666666")
    # No stop leg (0 or None): nothing to target, back to the fixed fraction.
    for stop in (0, None, "x", 1.5):
        fallback = ps.volatility_target(EQUITY, stop, 0.005, cap_notional=Decimal("1000"))
        assert fallback["status"] == "fallback_fixed_fraction" and fallback["notional"] == Decimal("1000")


def test_kelly_arithmetic_and_payoff_ratio():
    kelly = ps.kelly_fraction(0.6, 1.5)
    assert kelly["full_kelly"] == pytest.approx(0.6 - 0.4 / 1.5)
    assert kelly["kelly"] == pytest.approx(0.25 * (0.6 - 0.4 / 1.5))
    assert ps.kelly_fraction(0.3, 1.5)["kelly"] == 0.0  # no edge is floored, never negative
    with pytest.raises(ValueError):
        ps.kelly_fraction(0.6, 0)
    payoff = ps.payoff_ratio([0.02, 0.04, -0.01, -0.03, 0.0, "bad", float("nan")])
    assert payoff == {"trades": 5, "wins": 2, "losses": 3, "avg_win": 0.03, "avg_loss": pytest.approx(0.013333, abs=1e-6),
                      "payoff_ratio": pytest.approx(2.25, abs=1e-5)}
    assert ps.payoff_ratio([0.01, 0.02])["payoff_ratio"] is None  # needs losses too


def test_fractional_kelly_sizes_quarter_kelly_capped_at_the_fixed_fraction():
    fallback = ps.volatility_target(EQUITY, 0.03, 0.005, cap_notional=Decimal("1000"))
    payoff = {"trades": 40, "payoff_ratio": 1.5}
    # p = 0.41, b = 1.5: full Kelly 0.01667, quarter 0.004167 -> 416.67 of 100k, under the 1,000 cap.
    out = ps.fractional_kelly(EQUITY, 0.41, payoff, enforced=True, cap_notional=Decimal("1000"), fallback=fallback)
    assert out["status"] == "sized" and out["notional"] == Decimal("416.66666666")
    assert out["kelly"] == pytest.approx(0.004167, abs=1e-6) and out["closed_trades"] == 40
    capped = ps.fractional_kelly(EQUITY, 0.6, payoff, enforced=True, cap_notional=Decimal("1000"), fallback=fallback)
    assert capped["status"] == "capped" and capped["notional"] == Decimal("1000") and capped["capped"] is True


def test_fractional_kelly_falls_back_to_the_volatility_target():
    cap = Decimal("1000")
    fallback = ps.volatility_target(EQUITY, 0.10, 0.00025, cap_notional=cap)  # 0.025% at a 10% stop = 250
    payoff = {"trades": 40, "payoff_ratio": 1.5}
    cases = {
        "not in enforce": dict(probability=0.6, payoff=payoff, enforced=False),
        "no probability": dict(probability=None, payoff=payoff, enforced=True),
        "too few trades": dict(probability=0.6, payoff={"trades": 29, "payoff_ratio": 1.5}, enforced=True),
        "no payoff": dict(probability=0.6, payoff={"trades": 40, "payoff_ratio": None}, enforced=True),
        "no edge": dict(probability=0.3, payoff=payoff, enforced=True),
    }
    for name, case in cases.items():
        out = ps.fractional_kelly(EQUITY, case["probability"], case["payoff"], enforced=case["enforced"],
                                  cap_notional=cap, fallback=fallback)
        assert out["status"] == "fallback_volatility_target", name
        assert out["notional"] == Decimal("250"), name
        assert out["reason"] and out["fallback"]["status"] == "sized", name


def test_size_notional_dispatches_and_never_exceeds_the_fixed_fraction():
    common = dict(equity=EQUITY, risk_fraction=Decimal("0.01"), size_scale=Decimal("1"), stop_distance=0.03)
    fixed = ps.size_notional(mode="fixed_fraction", **common)
    assert fixed["notional"] == Decimal("1000") and fixed["requested_mode"] == "fixed_fraction"
    vol = ps.size_notional(mode="volatility_target", risk_per_trade=0.0001, **common)
    assert vol["notional"] == Decimal("333.33333333") and vol["fixed_fraction_notional"] == Decimal("1000")
    kelly = ps.size_notional(mode="fractional_kelly", risk_per_trade=0.0001, probability=0.9,
                             payoff={"trades": 50, "payoff_ratio": 3.0}, enforced=True, **common)
    assert kelly["notional"] == Decimal("1000") and kelly["status"] == "capped"
    unknown = ps.size_notional(mode="whatever", **common)
    assert unknown["mode"] == "fixed_fraction" and unknown["notional"] == Decimal("1000")
    explicit = ps.size_notional(mode="volatility_target", risk_per_trade=0.0001, cap_notional=Decimal("300"), **common)
    assert explicit["notional"] == Decimal("300") and explicit["status"] == "capped"
    assert explicit["fixed_fraction_notional"] == Decimal("300")
    features = ps.sizing_features(vol, quantity=Decimal("3.333333"), reference_price=Decimal("100"))
    assert features["notional"] == "333.33333333" and features["quantity"] == "3.333333"
    assert features["paper_only"] is True and all(not isinstance(v, Decimal) for v in features.values())


def test_strategy_payoff_reads_closed_scheduled_round_trips(db):  # noqa: F811
    _strategy(db, "mr")
    for value in (0.03, 0.01, -0.02, -0.02):
        _attribution(db, strategy_type="mr", net_return=value)
    _attribution(db, strategy_type="mr", net_return=0.5, costs_known=False)  # unknown costs never count
    _attribution(db, strategy_type="mr", net_return=0.5, source="exploration_lot")
    out = ps.strategy_payoff(db, "mr")
    assert out["trades"] == 4 and out["payoff_ratio"] == 1.0 and out["strategy_type"] == "mr"


# ------------------------------------------------------------------ the backtest's rule

@pytest.fixture()
def backtest():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import run_position_sizing_backtest as bt
        yield bt
    finally:
        sys.path.remove(str(scripts))


def _windows(total, drawdown, resized=5):
    return {w: {"trades": 100, "total_return": total[i], "max_drawdown": drawdown[i], "trades_resized": resized}
            for i, w in enumerate(("all", "2015+", "2020+"))}


def test_backtest_rule_adopts_only_profit_not_lower_and_drawdown_not_worse_everywhere(backtest):
    baseline = _windows([0.10, 0.50, 0.30], [0.50, 0.20, 0.20])
    strategies = {
        "good": {"trades": 50, "arms": {
            "volatility_target_0.25%": _windows([0.11, 0.51, 0.31], [0.49, 0.20, 0.20]),
            "fractional_kelly_p0.550": _windows([0.13, 0.52, 0.30], [0.50, 0.19, 0.20]),
        }},
        "worse_profit": {"trades": 50, "arms": {
            "volatility_target_0.25%": _windows([0.12, 0.49, 0.31], [0.40, 0.10, 0.10])}},
        "worse_drawdown": {"trades": 50, "arms": {
            "volatility_target_0.25%": _windows([0.12, 0.52, 0.31], [0.50, 0.21, 0.20])}},
        "never_fires": {"trades": 50, "arms": {
            "volatility_target_0.25%": _windows([0.10, 0.50, 0.30], [0.50, 0.20, 0.20], resized=0)}},
    }
    decision = backtest.decide(baseline, strategies)
    assert decision["good"]["adopted"] == "volatility_target_0.25%"  # largest minimum gain (+0.01 vs 0.0)
    assert decision["good"]["eligible"] == {"volatility_target_0.25%": 0.01, "fractional_kelly_p0.550": 0.0}
    assert decision["good"]["parameters"] == {"sizing_mode": "volatility_target", "risk_per_trade": 0.0025}
    for name in ("worse_profit", "worse_drawdown", "never_fires"):
        assert decision[name]["adopted"] == "fixed_fraction" and decision[name]["eligible"] == {}
        assert decision[name]["parameters"] == {"sizing_mode": "fixed_fraction"}
    assert any("total profit lower" in r for r in decision["worse_profit"]["rejected"]["volatility_target_0.25%"])
    assert any("drawdown worse" in r for r in decision["worse_drawdown"]["rejected"]["volatility_target_0.25%"])
    assert any("never changed" in r for r in decision["never_fires"]["rejected"]["volatility_target_0.25%"])
    assert backtest.adopted_parameters(decision) == {"good": {"sizing_mode": "volatility_target",
                                                              "risk_per_trade": 0.0025}}
    assert "probability" not in str(backtest.adopted_parameters(decision))


def test_backtest_sizes_are_causal_and_capped_at_the_slot(backtest):
    rows = pd.DataFrame({
        "strategy": ["rsi2_pullback"] * 35 + ["macd_momentum"],
        "entry_date": list(pd.bdate_range("2024-01-01", periods=35)) + [pd.Timestamp("2024-03-01")],
        "exit_date": list(pd.bdate_range("2024-01-02", periods=35)) + [pd.Timestamp("2024-03-05")],
        "net_return": [0.02 if i % 2 else -0.04 for i in range(35)] + [0.01],
    })
    sized = backtest.size_trades(rows)
    slot = backtest.SLOT
    # Volatility target at 0.25% with a 10% stop: 2.5% of equity, under the 5% slot; the 3% stop caps.
    assert sized["w_volatility_target_0.25%"].iloc[0] == pytest.approx(0.025)
    assert sized["w_volatility_target_0.25%"].iloc[-1] == pytest.approx(slot)
    assert (sized["w_volatility_target_1.00%"] == slot).all()
    # Kelly needs 30 closed trades before the entry: the first entries fall back (0.5% target = the slot).
    assert sized["payoff_trades"].iloc[0] == 0 and sized["payoff_trades"].iloc[34] == 33
    assert sized["w_fractional_kelly_p0.600"].iloc[0] == pytest.approx(slot)
    # By the 32nd entry 31 trades are closed: payoff 0.5, p = 0.6 -> full Kelly -0.2 -> falls back to the slot;
    # p would need to clear 1 / (1 + b) = 0.667 for a positive bet.
    assert sized["w_fractional_kelly_p0.600"].iloc[34] == pytest.approx(slot)
    assert (sized["w_fixed_fraction"] == slot).all()
    records = backtest.PayoffRecords(rows)
    before = records.payoff("rsi2_pullback", pd.Timestamp("2024-01-10"))
    assert before["trades"] == 6 and before["payoff_ratio"] == pytest.approx(0.5)
    assert records.payoff("nobody", pd.Timestamp("2024-01-10"))["trades"] == 0


def test_backtest_book_marks_trades_daily_and_prices_any_weight_vector(backtest):
    dates = pd.bdate_range("2024-01-01", periods=6)
    frames = {"AAA": pd.DataFrame({"date": dates, "close": [100.0, 102.0, 101.0, 103.0, 104.0, 105.0]})}
    rows = pd.DataFrame({"symbol": ["AAA", "AAA"], "strategy": ["s", "s"], "entry_idx": [1, 4], "exit_idx": [3, 4],
                         "entry_price": [101.0, 104.0], "exit_price": [102.5, 104.5],
                         "decision_date": [dates[0], dates[3]], "net_return": [0.0148, 0.0048]})
    book = backtest.Book(rows, frames)
    curve = book.curve(np.array([0.5, 0.5]), None)
    # Trade 1: 102/101 - 1, 101/102 - 1, 102.5/101 - 1 at half weight; trade 2: a one-bar trade.
    expected = np.cumprod(1 + 0.5 * np.array([0.0, 102 / 101 - 1, 101 / 102 - 1, 102.5 / 101 - 1, 104.5 / 104 - 1, 0.0]))
    assert curve[1:] == pytest.approx(expected)
    later = book.curve(np.array([0.5, 0.5]), dates[3])  # only trades decided from the start count
    assert later[1:] == pytest.approx(np.cumprod(1 + 0.5 * np.array([0, 0, 0, 0, 104.5 / 104 - 1, 0])))
    metrics = backtest.book_metrics(book, rows, np.array([0.5, 0.5]), None)
    assert metrics["trades"] == 2 and metrics["total_return"] == pytest.approx(expected[-1] - 1, abs=1e-4)
    assert metrics["trades_resized"] == 2 and metrics["max_drawdown"] > 0


# ------------------------------------------------------------------ the cycle

class _CycleDb(_Db):
    def begin_nested(self):
        return nullcontext()


def _cycle(strategy, signal_record, *, enforced=(), payoff=None):
    order = SimpleNamespace(id=42, status="accepted")
    tags = []
    with patch.object(settings, "paper_execution_symbols", ["AAPL"]), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         patch("app.services.meta_label_modes.enforced_strategy_types", return_value=set(enforced)), \
         patch("app.services.position_sizing.strategy_payoff", return_value=payoff or {"trades": 0, "payoff_ratio": None}), \
         patch("app.services.stock_paper_autotrader._tag_signal", side_effect=lambda _db, sid, key, value: tags.append((sid, key, value))), \
         signal_source(False, return_value=signal_record), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order):
        result = run_stock_paper_signal_cycle(_CycleDb([strategy]))
    return result, reserve, tags


def test_the_cycle_sizes_by_the_strategy_row_parameter_and_records_it():
    signal = {"signal_id": 7, "signal_action": "BUY", "reference_price": "100"}
    # Fixed fraction (the default): 1% of 100k at 100 = 10 shares, recorded as such.
    strategy = SimpleNamespace(id=1, strategy_type="moving_average_crossover", current_status="paper_trading_active",
                               parameters={})
    result, reserve, tags = _cycle(strategy, signal)
    assert reserve.call_args.kwargs["quantity"] == Decimal("10")
    [buy] = [r for r in result["results"] if r.get("signal_action") == "BUY"]
    assert buy["sizing"]["mode"] == "fixed_fraction" and Decimal(buy["sizing"]["notional"]) == 1000
    [(signal_id, _, sizing)] = [tag for tag in tags if tag[1] == "sizing"]
    assert signal_id == 7 and sizing == buy["sizing"]

    # Volatility target at 0.01% risk with the 3% rule stop: 333.33 notional -> 3.33 shares -> 3 whole shares.
    strategy.parameters = {"sizing_mode": "volatility_target", "risk_per_trade": 0.0001}
    result, reserve, tags = _cycle(strategy, signal)
    assert reserve.call_args.kwargs["quantity"] == Decimal("3")
    [buy] = [r for r in result["results"] if r.get("signal_action") == "BUY"]
    assert buy["sizing"]["mode"] == "volatility_target" and buy["sizing"]["status"] == "sized"
    assert buy["sizing"]["notional"] == "333.33333333" and Decimal(buy["sizing"]["fixed_fraction_notional"]) == 1000
    assert buy["sizing"]["stop_distance"] == 0.03 and buy["sizing"]["risk_per_trade"] == 0.0001
    assert buy["sizing"]["quantity"] == "3.333333" and buy["sizing"]["allocation_weight"] == "1"
    assert buy["allocation_weight"] == "1"


def test_the_cycle_uses_kelly_only_in_enforce_mode_with_enough_closed_trades():
    strategy = SimpleNamespace(id=1, strategy_type="moving_average_crossover", current_status="paper_trading_active",
                               parameters={"sizing_mode": "fractional_kelly", "risk_per_trade": 0.0001})
    signal = {"signal_id": 9, "signal_action": "BUY", "reference_price": "100",
              "meta_label": {"mode": "enforce", "decision": "take", "probability": 0.41}}
    # Not enforced: the Kelly mode falls back to the volatility target (0.01% at a 3% stop = 333.33).
    result, reserve, _ = _cycle(strategy, signal, payoff={"trades": 40, "payoff_ratio": 1.5})
    [buy] = [r for r in result["results"] if r.get("signal_action") == "BUY"]
    assert buy["sizing"]["status"] == "fallback_volatility_target" and "enforce" in buy["sizing"]["reason"]
    assert reserve.call_args.kwargs["quantity"] == Decimal("3")
    # Enforced with 40 closed trades at payoff 1.5 and p = 0.41: quarter Kelly 0.4167% -> 416.67 -> 4 shares.
    result, reserve, tags = _cycle(strategy, signal, enforced={"moving_average_crossover"},
                                   payoff={"trades": 40, "payoff_ratio": 1.5})
    [buy] = [r for r in result["results"] if r.get("signal_action") == "BUY"]
    assert buy["sizing"]["mode"] == "fractional_kelly" and buy["sizing"]["status"] == "sized"
    assert buy["sizing"]["probability"] == 0.41 and buy["sizing"]["payoff_ratio"] == 1.5
    assert buy["sizing"]["closed_trades"] == 40 and buy["sizing"]["notional"] == "416.66666666"
    assert reserve.call_args.kwargs["quantity"] == Decimal("4")
    assert [t for t in tags if t[1] == "sizing"][0][2]["kelly"] == pytest.approx(0.004167, abs=1e-6)
    # A strong probability wants more than the fixed fraction: capped at it, and recorded as capped.
    result, reserve, _ = _cycle(strategy, signal | {"meta_label": {"decision": "take", "probability": 0.7}},
                                enforced={"moving_average_crossover"}, payoff={"trades": 40, "payoff_ratio": 1.5})
    [buy] = [r for r in result["results"] if r.get("signal_action") == "BUY"]
    assert buy["sizing"]["status"] == "capped" and Decimal(buy["sizing"]["notional"]) == 1000
    assert reserve.call_args.kwargs["quantity"] == Decimal("10")
    # Enforced but too few closed trades: back to the volatility target.
    result, reserve, _ = _cycle(strategy, signal, enforced={"moving_average_crossover"},
                                payoff={"trades": 12, "payoff_ratio": 1.5})
    [buy] = [r for r in result["results"] if r.get("signal_action") == "BUY"]
    assert buy["sizing"]["status"] == "fallback_volatility_target" and "12 closed" in buy["sizing"]["reason"]
    assert reserve.call_args.kwargs["quantity"] == Decimal("3")


def test_exploration_probes_keep_learning_sizing():
    strategy = SimpleNamespace(id=1, strategy_type="moving_average_crossover", current_status="paper_trading_candidate",
                               parameters={"sizing_mode": "volatility_target", "risk_per_trade": 0.0001})
    signal = {"signal_id": 3, "signal_action": "BUY", "reference_price": "100"}
    order = SimpleNamespace(id=42, status="accepted")
    with patch.object(settings, "paper_execution_symbols", ["AAPL"]), \
         patch.object(settings, "paper_exploration_enabled", True), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         signal_source(True, return_value=signal), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order):
        result = run_stock_paper_signal_cycle(_CycleDb([strategy]))
    [buy] = [r for r in result["results"] if r.get("signal_action") == "BUY"]
    assert buy["mode"] == "explore" and "sizing" not in buy and "allocation_weight" not in buy
    expected = (EQUITY * Decimal(str(settings.paper_exploration_risk_fraction)) / Decimal("100"))
    assert reserve.call_args.kwargs["quantity"] == max(Decimal("1"), expected.quantize(Decimal("1")))


# ------------------------------------------------------------------ the seed contract

def test_lineup_applies_adopted_sizing_modes_once_and_respects_operators(db, monkeypatch):  # noqa: F811
    from app.services import seed

    monkeypatch.setattr(seed, "SIZED_STRATEGIES", {
        "rsi2_pullback": {"sizing_mode": "volatility_target", "risk_per_trade": 0.0025}})
    for slug in ("rsi2_pullback", "macd_momentum", "ibs_mean_reversion"):
        db.add(Strategy(name=slug, strategy_type=slug, parameters={"k": 1}, is_active=True, current_status="research"))
    # An operator's earlier choice stands.
    db.add(Strategy(name="x", strategy_type="rsi2_pullback", parameters={"sizing_mode": "fixed_fraction"},
                    is_active=True, current_status="research"))
    db.commit()
    assert seed.ensure_strategy_lineup(db)["sizing_modes_applied"] == ["rsi2_pullback"]
    db.commit()
    rows = db.query(Strategy).order_by(Strategy.id).all()
    applied = rows[0]
    # Other lineup steps add their own keys (scope, market gate); the operator key ``k`` survives.
    assert applied.parameters["k"] == 1 and applied.parameters["sizing_mode"] == "volatility_target"
    assert applied.parameters["risk_per_trade"] == 0.0025
    assert ps.sizing_parameters(applied.parameters) == {"sizing_mode": "volatility_target", "risk_per_trade": 0.0025}
    assert "sizing_mode" not in rows[1].parameters and "sizing_mode" not in rows[2].parameters
    assert rows[3].parameters["sizing_mode"] == "fixed_fraction" and "risk_per_trade" not in rows[3].parameters
    audit = db.query(AuditLog).filter_by(action=seed.SIZING_MODE_ACTION).all()
    assert len(audit) == 1 and audit[0].entity_id == applied.id
    assert audit[0].payload["source"] == seed.SIZING_MODE_REPORT and audit[0].payload["paper_only"] is True
    assert "volatility_target" in audit[0].message and "0.25%" in audit[0].message
    assert seed.ensure_strategy_lineup(db)["sizing_modes_applied"] == []

    # An operator who removes the mode later is not overridden.
    applied.parameters = {"k": 1}
    db.commit()
    assert seed.ensure_strategy_lineup(db)["sizing_modes_applied"] == []
    assert "sizing_mode" not in db.get(Strategy, applied.id).parameters


def test_the_shipped_seed_adopts_nothing_until_a_backtest_says_so():
    from app.services import seed

    assert seed.SIZED_STRATEGIES == {}
    assert seed.SIZING_MODE_ACTION == "sizing_mode_update"
