"""SPY market-direction gate: state, entry protection, one-time seed, backtest decision rule."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.models import AuditLog, Strategy
from app.services import seed
from app.services.entry_protections import load_entry_protections
from app.services.market_gate import MARKET_GATE_KEY, gate_window, market_state, spy_above_sma

from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


def _spy(closes):
    return pd.DataFrame({"date": pd.bdate_range("2026-01-02", periods=len(closes)).strftime("%Y-%m-%d"),
                         "close": closes})


def test_gate_window_parsing():
    assert gate_window(None) == 0 and gate_window({}) == 0
    assert gate_window({MARKET_GATE_KEY: 20}) == 20 and gate_window({MARKET_GATE_KEY: "50"}) == 50
    assert gate_window({MARKET_GATE_KEY: -3}) == 0 and gate_window({MARKET_GATE_KEY: "x"}) == 0


def test_spy_above_sma_is_causal_and_strict():
    close = pd.Series([10.0, 10, 10, 11, 9, 12])
    np.testing.assert_array_equal(spy_above_sma(close, 3), [False, False, False, True, False, True])
    # Bars after t never change bar t.
    longer = pd.concat([close, pd.Series([1.0, 1.0])], ignore_index=True)
    np.testing.assert_array_equal(spy_above_sma(longer, 3)[:6], spy_above_sma(close, 3))


def test_market_state():
    rising = _spy(list(np.linspace(100, 120, 60)))
    falling = _spy(list(np.linspace(120, 100, 60)))
    assert market_state(rising, 20)["allowed"] is True
    blocked = market_state(falling, 20)
    assert blocked["allowed"] is False and "at or below its 20-day average" in blocked["reason"]
    assert market_state(None, 20)["allowed"] is False
    assert market_state(_spy([100.0] * 10), 20)["allowed"] is False  # too short: fail closed
    assert market_state(falling, 0)["allowed"] is True


def _protections(db, spy, gates):
    account = SimpleNamespace(id=1, equity=Decimal("100000"), last_equity=Decimal("100000"))
    return load_entry_protections(db, account, now=NOW, rules={}, type_by_id={}, bounds_for=lambda _d: None,
                                  market_gates=gates, market_frame=spy)


def test_entry_protection_blocks_only_gated_strategies_in_a_falling_market(db):  # noqa: F811
    falling = _spy(list(np.linspace(120, 100, 60)))
    protections = _protections(db, lambda: falling, {"macd_momentum": 50, "trend_pullback": 10, "ibs_mean_reversion": 0})
    assert protections.load_error is None
    block = protections.check("AAPL", "macd_momentum")
    assert block is not None and "takes no new longs while the market falls" in block["reason"]
    assert protections.check("AAPL", "trend_pullback") is not None
    assert protections.check("SPY", "ibs_mean_reversion") is None
    assert protections.check("SPY", "bollinger_mean_reversion") is None
    assert set(protections.summary()["market_gate"]["states"]) == {"10", "50"}

    rising = _spy(list(np.linspace(100, 120, 60)))
    assert _protections(db, lambda: rising, {"macd_momentum": 50}).check("AAPL", "macd_momentum") is None


def test_spy_load_failure_fails_closed_for_gated_strategies_only(db):  # noqa: F811
    def broken():
        raise RuntimeError("feed down")

    protections = _protections(db, broken, {"channel_breakout": 20})
    assert protections.load_error is None
    assert protections.check("MSFT", "channel_breakout") is not None
    assert protections.check("MSFT", "rsi_mean_reversion") is None
    # No gated strategy: SPY is never loaded.
    assert _protections(db, broken, {"rsi_mean_reversion": 0}).market_states == {}


def test_seed_enables_the_backtested_gates_once_and_respects_operator_changes(db):  # noqa: F811
    for slug in ("macd_momentum", "channel_breakout", "trend_pullback", "rsi2_pullback", "ibs_mean_reversion"):
        db.add(Strategy(name=slug, strategy_type=slug, parameters={}, is_active=True,
                        current_status="paper_trading_candidate"))
    db.add(Strategy(name="tp2", strategy_type="trend_pullback", parameters={MARKET_GATE_KEY: 0}, is_active=True,
                    current_status="research"))
    db.flush()
    changed = seed.enable_market_gates(db)
    assert sorted(changed) == ["channel_breakout", "macd_momentum", "rsi2_pullback", "trend_pullback"]
    rows = {row.name: row for row in db.scalars(select(Strategy)).all()}
    assert rows["macd_momentum"].parameters[MARKET_GATE_KEY] == 50
    assert rows["channel_breakout"].parameters[MARKET_GATE_KEY] == 20
    assert rows["trend_pullback"].parameters[MARKET_GATE_KEY] == 10
    assert rows["rsi2_pullback"].parameters[MARKET_GATE_KEY] == 50
    assert MARKET_GATE_KEY not in rows["ibs_mean_reversion"].parameters
    assert rows["tp2"].parameters[MARKET_GATE_KEY] == 0  # operator's choice stands

    # An operator later removes the key: the seed never puts it back.
    rows["macd_momentum"].parameters = {}
    db.flush()
    assert seed.enable_market_gates(db) == []
    events = db.scalars(select(AuditLog).where(AuditLog.action == seed.MARKET_GATE_ACTION)).all()
    assert len(events) == 4
    assert "market_gates_enabled" in seed.ensure_strategy_lineup(db)


def test_backtest_decision_rule():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import run_market_gate_backtest as bt
    finally:
        sys.path.remove(str(scripts))

    def window(exp, dd, n=100):
        return {"trades": n, "expectancy_bps": exp, "portfolio_max_drawdown": dd}

    rows = {
        0: {w: window(10.0, 0.10) for w in bt.WINDOWS},
        10: {"all": window(12.0, 0.09), "2015+": window(9.0, 0.09), "2020+": window(15.0, 0.09)},  # worse 2015+
        20: {w: window(13.0, 0.105) for w in bt.WINDOWS},  # +3 everywhere, DD within 1.10x
        50: {w: window(20.0, 0.12) for w in bt.WINDOWS},   # better expectancy, DD too high
    }
    decision = bt.decide_gate(rows)
    assert decision == {"adopted": 20, "eligible": {20: pytest.approx(3.0)}}
    rows[20] = {w: window(13.0, 0.2) for w in bt.WINDOWS}
    assert bt.decide_gate(rows)["adopted"] == 0
