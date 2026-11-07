"""Market hedge: signals on synthetic SPY/SH, the fence, the backtest's decision rule and the seed admission."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.core.config import DEFAULT_PAPER_HEDGE_SYMBOLS, settings
from app.models import AuditLog, Strategy
from app.services import seed
from app.services.entry_protections import EntryProtections, protection_config
from app.services.market_gate import gate_window
from app.services.strategies import market_hedge
from app.services.strategies.market_hedge import (
    HEDGE_SYMBOL, MarketHedgeStrategy, fenced_class, hedge_state, market_until, set_market_history,
)
from app.services.strategies.registry import get_strategy, is_known_strategy
from app.services.strategies.technical import RSIMeanReversionStrategy
from app.services.strategy_execution_policy import evaluate_policy_exit, exit_policy_for, symbol_scope_for
from app.services.trading_universe import execution_symbols, sector_group

from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_hedge_backtest as hb  # noqa: E402

NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _unpin():
    set_market_history(None)
    yield
    set_market_history(None)


def _frame(closes, start="2026-01-02", symbol_scale=1.0):
    closes = np.asarray(closes, dtype=float) * symbol_scale
    dates = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "open": closes, "high": closes * 1.01, "low": closes * 0.99,
                         "close": closes, "volume": 1_000_000})


def _falling_spy(n=80):
    """80 bars: 60 flat at 100, then 20 bars sliding to 90 (close < SMA50, SMA10 < SMA50)."""
    return _frame(np.concatenate([np.full(60, 100.0), np.linspace(99.5, 90.0, 20)]))


def _reclaimed_spy(n=80):
    """A dip that is fully recovered: the last close sits above the 50-day average."""
    return _frame(np.concatenate([np.full(60, 100.0), np.linspace(95.0, 90.0, 10), np.linspace(96.0, 104.0, 10)]))


# ----------------------------------------------------------------- signal rule

def test_hedge_state_marks_entry_below_a_falling_average_and_exit_on_reclaim():
    spy = _falling_spy()
    state = hedge_state(spy["close"])
    assert not state["entry"][:49].any() and not state["exit"][:49].any()  # undefined before 50 bars
    assert bool(state["entry"][-1]) and not bool(state["exit"][-1])
    state = hedge_state(_reclaimed_spy()["close"])
    assert bool(state["exit"][-1]) and not bool(state["entry"][-1])
    # Flat market: close == average is neither an entry (needs <) nor an exit (needs >).
    flat = hedge_state(pd.Series(np.full(60, 100.0)))
    assert not flat["entry"][-1] and not flat["exit"][-1]


def test_strategy_buys_sh_only_while_spy_sits_below_a_falling_average():
    spy = _falling_spy()
    set_market_history(spy)
    sh = _frame(np.linspace(30, 33, 80))
    strategy = get_strategy("market_hedge", {})
    signal = strategy.generate_signal("SH", sh)
    assert signal.action == "BUY" and signal.confidence >= 0.6
    assert signal.features["gate_closed"] is True and signal.features["spy_bar"] == spy["date"].iloc[-1]
    assert signal.features["hedge_fraction"] == market_hedge.ADOPTED_HEDGE_FRACTION
    assert strategy.exit_signal("SH", sh, 70) is None

    set_market_history(_reclaimed_spy())
    assert strategy.generate_signal("SH", sh).action == "HOLD"
    assert strategy.exit_signal("SH", sh, 70) == "spy_above_sma50"
    # Any other symbol is never a hedge.
    assert strategy.generate_signal("SPY", sh).action == "HOLD"


def test_strategy_sees_only_spy_bars_up_to_the_traded_frame_and_fails_closed_when_stale():
    spy = _frame(np.concatenate([np.full(60, 100.0), np.linspace(99.5, 90.0, 20), np.full(20, 110.0)]))
    set_market_history(spy)
    sh = _frame(np.linspace(30, 33, 80))  # ends on the 80th bar: SPY has not reclaimed yet
    strategy = get_strategy("market_hedge", {})
    assert strategy.generate_signal("SH", sh).action == "BUY"  # the later SPY bars are not consulted
    assert market_until(sh["date"].iloc[-1])["date"].iloc[-1] == sh["date"].iloc[-1]
    # SPY history that ends long before the traded bar is stale: no entry, no indicator exit.
    late = _frame(np.linspace(30, 33, 80), start="2026-06-01")
    assert strategy.generate_signal("SH", late).action == "HOLD"
    assert strategy.generate_signal("SH", late).features["market"] == "unavailable"
    assert strategy.exit_signal("SH", late, 70) is None
    set_market_history(None)
    assert strategy.generate_signal("SH", sh).reason.startswith("SPY history unavailable") or \
        strategy.generate_signal("SH", sh).action == "HOLD"


def test_registry_resolution_scope_exit_policy_and_market_gate_exemption():
    assert is_known_strategy("market_hedge")
    strategy = get_strategy("market_hedge", {})
    assert isinstance(strategy, MarketHedgeStrategy) and strategy.daily_bar_execution
    assert strategy.symbol_scope() == frozenset({"SH"})
    row = type("Row", (), {"strategy_type": "market_hedge", "parameters": {"symbol_scope": ["SH"]}})()
    assert symbol_scope_for(row) == frozenset({"SH"})
    policy = exit_policy_for(row)
    assert policy.indicator_exit and policy.stop_loss_pct == 0.10 and policy.take_profit_pct == 0.0
    # The market gate reads ``market_gate_sma``; the hedge row never carries it, so the cycle builds no gate for it.
    assert gate_window(row.parameters) == 0 and "market_hedge" not in seed.MARKET_GATED_STRATEGIES
    protections = EntryProtections(now=NOW, config=protection_config({}), market_gates={"macd_momentum": 50},
                                   market_states={50: {"allowed": False, "reason": "SPY below its 50-day average"}})
    assert protections.market_gate_block("macd_momentum") is not None
    assert protections.market_gate_block("market_hedge") is None


def test_policy_exit_fires_through_the_execution_policy_when_spy_reclaims():
    set_market_history(_reclaimed_spy())
    sh = _frame(np.linspace(30, 33, 80))
    row = type("Row", (), {"strategy_type": "market_hedge", "parameters": {}})()
    opened = datetime(2026, 4, 1, 14, 0, tzinfo=timezone.utc)
    now = datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc)
    decision = evaluate_policy_exit(row, "SH", sh, opened, now)
    assert decision is not None and decision["reason"] == "indicator_exit:spy_above_sma50"


# ----------------------------------------------------------------- fence, sector, universe

def test_other_strategies_are_fenced_off_the_hedge_instrument():
    prices = _frame(np.linspace(100, 80, 40))  # RSI far below 100: always a BUY for the threshold below
    strategy = get_strategy("rsi_mean_reversion", {"buy_threshold": 100})
    assert isinstance(strategy, RSIMeanReversionStrategy) and type(strategy).__name__ == "RSIMeanReversionStrategy"
    assert strategy.generate_signal("AAPL", prices).action == "BUY"
    fenced = strategy.generate_signal("SH", prices)
    assert fenced.action == "HOLD" and fenced.features["gated_action"] == "BUY"
    assert fenced.features["hedge_fence"]["owner"] == "market_hedge"
    assert fenced_class(RSIMeanReversionStrategy) is fenced_class(RSIMeanReversionStrategy)
    assert fenced_class(MarketHedgeStrategy) is MarketHedgeStrategy
    rule = get_strategy("rule_x9", {"rule": {"entry": [{"f": "rsi_2", "op": "<", "v": 101}], "exit": []}})
    assert rule.slug == "rule_x9" and rule.generate_signal("SH", _frame(np.linspace(100, 80, 300))).action == "HOLD"


def test_sh_is_its_own_sector_group_and_joins_the_universe_only_when_admitted(monkeypatch):
    assert sector_group("SH") == "inverse" and sector_group("SPY") == "index"
    assert bool(DEFAULT_PAPER_HEDGE_SYMBOLS) == (market_hedge.ADOPTED_HEDGE_FRACTION is not None)
    monkeypatch.setattr(settings, "paper_hedge_symbols", [])
    assert "SH" not in execution_symbols()
    monkeypatch.setattr(settings, "paper_hedge_symbols", ["SH"])
    symbols = execution_symbols()
    assert symbols[-1] == "SH" and symbols.count("SH") == 1


# ----------------------------------------------------------------- backtest script

def _results(base, hedged):
    out = {"baseline": {w: {"total_return": base[0], "max_drawdown": base[1]} for w in hb.WINDOWS}}
    for size, (ret, dd) in hedged.items():
        out[f"hedge_{size:g}"] = {w: {"total_return": ret, "max_drawdown": dd} for w in hb.WINDOWS}
    return out


def test_backtest_decision_takes_the_smallest_size_that_keeps_profit_and_cuts_drawdown():
    base = (0.50, 0.30)
    assert hb.decide(_results(base, {0.02: (0.49, 0.25), 0.05: (0.50, 0.28), 0.10: (0.55, 0.20)})) == \
        {"eligible": [0.05, 0.10], "adopted": 0.05}
    # Drawdown must be strictly lower; profit may be equal.
    assert hb.decide(_results(base, {0.02: (0.50, 0.30), 0.05: (0.52, 0.31), 0.10: (0.40, 0.10)})) == \
        {"eligible": [], "adopted": None}
    # One failing window disqualifies a size.
    results = _results(base, {0.02: (0.60, 0.20), 0.05: (0.60, 0.20), 0.10: (0.60, 0.20)})
    results["hedge_0.02"]["2020+"]["max_drawdown"] = 0.30
    results["hedge_0.05"]["2015+"]["total_return"] = 0.49
    assert hb.decide(results) == {"eligible": [0.10], "adopted": 0.10}


def test_synthetic_short_spy_inverts_spy_moves_and_chains_into_sh():
    spy = _frame([100, 102, 101, 103, 100, 99, 101, 104, 103, 105])
    synthetic = hb.synthetic_inverse(spy, "9999-12-31", borrow_bps_per_year=0.0)
    spy_returns = spy["close"].pct_change().to_numpy()[1:]
    assert np.allclose(synthetic["close"].pct_change().to_numpy()[1:], -spy_returns[1:])
    assert (synthetic["high"] >= synthetic["low"]).all() and len(synthetic) == len(spy) - 1
    costly = hb.synthetic_inverse(spy, "9999-12-31", borrow_bps_per_year=50.0)
    assert (costly["close"].to_numpy() < synthetic["close"].to_numpy()).all()
    sh = _frame([40, 41, 39], start=spy["date"].iloc[6])
    hedge, first = hb.hedge_frame(spy, sh)
    assert first == spy["date"].iloc[6] and len(hedge) == 5 + 3
    splice = hedge.iloc[4]["close"] * (1 - (spy["open"].iloc[6] / spy["close"].iloc[5] - 1))
    assert splice == pytest.approx(sh["open"].iloc[0])


def test_hedge_trades_enter_at_the_next_open_and_leave_when_spy_reclaims():
    spy = _frame(np.concatenate([np.full(60, 100.0), np.linspace(99.0, 90.0, 10), np.linspace(96.0, 106.0, 10)]))
    sh = _frame(np.concatenate([np.full(60, 30.0), np.linspace(30.3, 33.0, 10), np.linspace(31.2, 28.2, 10)]))
    trades = hb.hedge_trades(sh, spy)
    assert len(trades) == 1
    trade = trades.iloc[0]
    state = hedge_state(spy["close"])
    first_signal = int(np.flatnonzero(state["entry"])[0])
    assert int(trade["entry_idx"]) == first_signal + 1 and trade["barrier"] == "indicator"
    reclaim = int(np.flatnonzero(state["exit"][first_signal:])[0]) + first_signal
    assert int(trade["exit_idx"]) == reclaim + 1 and trade["strategy"] == "market_hedge" and trade["symbol"] == "SH"
    assert trade["net_return"] < trade["gross_return"]  # costs charged both sides


# ----------------------------------------------------------------- seed admission

def test_seed_creates_the_hedge_row_paused_when_no_size_was_adopted(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(market_hedge, "ADOPTED_HEDGE_FRACTION", None)
    assert seed.admit_market_hedge(db) == "paused"
    row = db.scalars(select(Strategy).where(Strategy.strategy_type == "market_hedge")).one()
    assert row.current_status == "paused" and row.parameters["symbol_scope"] == ["SH"]
    assert row.parameters["hedge_fraction"] == 0 and row.parameters["stop_loss_pct"] == 0.10
    event = db.scalars(select(AuditLog).where(AuditLog.action == seed.HEDGE_ADMISSION_ACTION)).one()
    assert event.entity_id == row.id and event.payload["adopted"] is False and event.payload["paper_only"] is True
    assert "paused" in event.message
    # Idempotent, and an operator's later choice stands.
    assert seed.admit_market_hedge(db) is None
    row.current_status = "paper_trading_candidate"
    db.flush()
    assert seed.admit_market_hedge(db) is None
    assert row.current_status == "paper_trading_candidate"
    db.delete(row)
    db.flush()
    assert seed.admit_market_hedge(db) is None  # deleted on purpose: never recreated
    assert db.query(Strategy).filter(Strategy.strategy_type == "market_hedge").count() == 0
    assert db.query(AuditLog).filter(AuditLog.action == seed.HEDGE_ADMISSION_ACTION).count() == 1


def test_seed_creates_the_hedge_row_as_a_candidate_when_a_size_was_adopted(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(market_hedge, "ADOPTED_HEDGE_FRACTION", 0.05)
    assert seed.admit_market_hedge(db) == "paper_trading_candidate"
    row = db.scalars(select(Strategy).where(Strategy.strategy_type == "market_hedge")).one()
    assert row.current_status == "paper_trading_candidate" and row.parameters["hedge_fraction"] == 0.05
    event = db.scalars(select(AuditLog).where(AuditLog.action == seed.HEDGE_ADMISSION_ACTION)).one()
    assert event.payload["adopted"] is True and event.payload["hedge_fraction"] == 0.05
    assert get_strategy(row.strategy_type, row.parameters).generate_signal(HEDGE_SYMBOL, _frame([1.0])).action == "HOLD"


def test_seed_never_recreates_a_row_the_operator_made_themselves(db):  # noqa: F811
    db.add(Strategy(name="my hedge", strategy_type="market_hedge", parameters={}, is_active=True,
                    current_status="research"))
    db.flush()
    assert seed.admit_market_hedge(db) is None
    assert db.query(AuditLog).filter(AuditLog.action == seed.HEDGE_ADMISSION_ACTION).count() == 0
    assert "hedge_admission" in seed.ensure_strategy_lineup(db)
