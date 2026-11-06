"""Regime-aware paper risk budget: per-regime records, the opt-in multiplier, features and the backtest rule."""
from __future__ import annotations

import sys
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import required_role
from app.models import EconomicIndicator, MarketPrice
from app.models.loss_feedback import StockAllocationWeight
from app.services import regime_allocation as ra
from app.services import strategy_allocation
from tests.test_loss_feedback_loop import _attribution, _strategy, db  # noqa: F401  (fixture)

UTC = timezone.utc
NOW = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
ENTRY_DAY = date(2026, 10, 28)  # the helper opens each trade five hours before NOW - 2 days


def _spy_history(db, *, rising: bool, days: int = 60, end: date = ENTRY_DAY):  # noqa: F811
    day = end - timedelta(days=days + 20)
    close = 400.0
    added = 0
    while added < days:
        if day.weekday() < 5:
            close += 1.0 if rising else -1.0
            db.add(MarketPrice(symbol="SPY", price_date=day, close=Decimal(str(close)), adjusted_close=Decimal(str(close))))
            added += 1
        day += timedelta(days=1)
    db.flush()


def _trade(db, strategy_type, net_return, *, vix, breadth=60.0, closed_at=None):  # noqa: F811
    row = _attribution(db, strategy_type=strategy_type, net_return=net_return, closed_at=closed_at)
    row.detail = {**row.detail, "diagnosis": {"entry_vix": vix, "entry_breadth": breadth}}
    db.flush()
    return row


def _spy_frame(rising: bool, bars: int = 60) -> pd.DataFrame:
    closes = [400.0 + (i if rising else -i) for i in range(bars)]
    return pd.DataFrame({"date": pd.bdate_range(end="2026-10-30", periods=bars).strftime("%Y-%m-%d"), "close": closes})


# ------------------------------------------------------------------ cells

def test_regime_cell_buckets_and_unknown_axes():
    assert ra.regime_cell(spy_above_sma=True, vix=19.9, breadth_pct=50.0)["key"] == "up/low/strong"
    assert ra.regime_cell(spy_above_sma=False, vix=20.0, breadth_pct=49.9)["key"] == "down/mid/weak"
    assert ra.volatility_label(30.0) == "mid" and ra.volatility_label(30.1) == "high"
    unknown = ra.regime_cell(spy_above_sma=None, vix=float("nan"), breadth_pct="n/a")
    assert unknown["key"] == "unknown/unknown/unknown" and not ra.cell_is_known(unknown)
    assert ra.cell_is_known(ra.regime_cell(spy_above_sma=True, vix=35, breadth_pct=10))


def test_current_regime_reads_the_market_gate_and_fresh_gauges(db):  # noqa: F811
    day = NOW.astimezone(ra.ET).date()
    db.add(EconomicIndicator(indicator_name="vix", observation_date=day, value=Decimal("25.5"), source="yfinance"))
    db.add(EconomicIndicator(indicator_name="breadth_above_200d", observation_date=day - timedelta(days=30),
                             value=Decimal("45"), source="yfinance"))  # stale: falls to unknown
    db.flush()
    regime = ra.current_regime(db, now=NOW, spy_frame=_spy_frame(rising=True))
    assert regime["cell"]["key"] == "up/mid/unknown"
    assert regime["readings"]["spy"]["close"] > regime["readings"]["spy"]["sma"]
    assert regime["readings"]["vix"]["fresh"] is True and regime["readings"]["breadth_above_200d"]["fresh"] is False
    # No SPY history, no gauges: every axis unknown, nothing raises.
    empty = ra.current_regime(db, now=NOW, spy_frame=pd.DataFrame({"date": [], "close": []}))
    assert empty["cell"]["trend"] == "unknown"


# ------------------------------------------------------------------ records and fallback

def test_per_regime_records_fall_back_below_min_trades(db):  # noqa: F811
    _spy_history(db, rising=True)
    for _ in range(8):
        _trade(db, "mr", -0.02, vix=25.0)   # up/mid/strong: enough evidence, losing
    for _ in range(2):
        _trade(db, "mr", 0.03, vix=15.0)    # up/low/strong: too few trades
    records = ra.regime_records(db, ["mr"], now=NOW)
    cells = records["mr"]["cells"]
    assert set(cells) == {"up/mid/strong", "up/low/strong"}
    assert cells["up/mid/strong"]["trades"] == 8 and cells["up/mid/strong"]["status"] == "reduced"
    assert cells["up/mid/strong"]["multiplier"] < 1.0
    assert cells["up/low/strong"] == cells["up/low/strong"] | {"trades": 2, "status": "fallback_overall",
                                                                 "multiplier": 1.0}
    assert records["mr"]["overall"]["trades"] == 10
    losing = ra.regime_multiplier(records["mr"], {"key": "up/mid/strong"})
    assert losing["regime_multiplier"] < 1.0 and losing["regime_status"] == "reduced" and losing["regime_trades"] == 8
    thin = ra.regime_multiplier(records["mr"], {"key": "up/low/strong"})
    assert thin["regime_multiplier"] == 1.0 and thin["regime_status"] == "fallback_overall"
    unseen = ra.regime_multiplier(records["mr"], ra.regime_cell(spy_above_sma=False, vix=40, breadth_pct=10))
    assert unseen == {"regime_cell": "down/high/weak", "regime_multiplier": 1.0, "regime_status": "fallback_overall",
                      "regime_trades": 0, "regime_mean_return": None}


def test_trade_cells_use_spy_closes_before_the_entry_and_unknown_when_missing(db):  # noqa: F811
    row = _trade(db, "mr", 0.01, vix=22.0, breadth=40.0)
    assert ra.cell_for_attribution(db, row)["key"] == "unknown/mid/weak"  # no SPY history yet
    _spy_history(db, rising=False)
    assert ra.cell_for_attribution(db, row)["key"] == "down/mid/weak"
    cache: dict = {}
    assert ra.spy_trend_before(db, ENTRY_DAY, cache) is False and ENTRY_DAY in cache


# ------------------------------------------------------------------ bounds

def test_combined_multiplier_stays_inside_the_bounds_and_shrinks_first():
    policy = strategy_allocation.bounds()
    assert ra.combine(1.25, 1.25, policy) == policy["ceiling"]
    assert ra.combine(0.5, 0.5, policy) == policy["floor"]
    assert ra.combine(1.0, 0.8, policy) == 0.8
    loss = strategy_allocation.performance_multiplier([-0.004] * 10, policy)["multiplier"]
    gain = strategy_allocation.performance_multiplier([0.004] * 10, policy)["multiplier"]
    assert 1.0 - ra.combine(1.0, loss, policy) > ra.combine(1.0, gain, policy) - 1.0 > 0
    results = {"a": {"multiplier": 1.1, "status": "increased"}, "b": {"multiplier": 0.6, "status": "reduced"},
               "c": {"multiplier": 1.0, "status": "baseline"}}
    records = {"a": {"cells": {"up/low/strong": {"multiplier": 0.7, "status": "reduced", "trades": 6, "mean_return": -0.01}}},
               "b": {"cells": {"up/low/strong": {"multiplier": 0.5, "status": "reduced", "trades": 9, "mean_return": -0.05}}},
               "c": {"cells": {"up/low/strong": {"multiplier": 1.0, "status": "fallback_overall", "trades": 2}}}}
    out = strategy_allocation.apply_regime(results, records, {"key": "up/low/strong"}, policy)
    assert out["a"]["multiplier"] == pytest.approx(0.77) and out["a"]["status"] == "regime_reduced"
    assert out["a"]["base_multiplier"] == 1.1 and out["a"]["regime_cell"] == "up/low/strong"
    assert out["b"]["multiplier"] == policy["floor"]  # 0.6 x 0.5 never below the floor
    assert out["c"]["multiplier"] == 1.0 and out["c"]["status"] == "baseline"  # fallback leaves the overall weight


# ------------------------------------------------------------------ opt-in and evidence

def test_regime_weighting_is_opt_in_and_records_the_cell(db, monkeypatch):  # noqa: F811
    assert settings.paper_allocation_regime_enabled is False
    assert strategy_allocation.bounds()["regime_enabled"] is False
    _spy_history(db, rising=True)
    for _ in range(8):
        _trade(db, "mr", -0.02, vix=25.0)
    for _ in range(4):
        _trade(db, "mr", 0.03, vix=15.0)
    off = strategy_allocation.compute_weights(db, ["mr"], now=NOW)["mr"]
    assert "regime_cell" not in off and "base_multiplier" not in off
    monkeypatch.setattr(settings, "paper_allocation_regime_enabled", True)
    with patch("app.services.regime_allocation.current_regime",
               return_value={"cell": ra.regime_cell(spy_above_sma=True, vix=25, breadth_pct=60), "readings": {}}):
        on = strategy_allocation.compute_weights(db, ["mr"], now=NOW)["mr"]
        rows = strategy_allocation.session_weight_rows(db, ["mr"], session_date=date(2026, 10, 30), now=NOW)
    assert on["regime_cell"] == "up/mid/strong" and on["regime_trades"] == 8
    assert on["base_multiplier"] == off["multiplier"] and on["multiplier"] < on["base_multiplier"]
    assert on["status"] == "regime_reduced"
    stored = rows["mr"]
    assert stored.evidence["regime_cell"] == "up/mid/strong" and float(stored.multiplier) == on["multiplier"]
    features = strategy_allocation.allocation_features(stored)
    assert features["regime_cell"] == "up/mid/strong" and features["regime_enabled"] is True
    assert Decimal(features["multiplier"]) == Decimal(str(on["multiplier"]))
    assert features["base_multiplier"] == off["multiplier"] and features["regime_multiplier"] < 1.0
    projection = strategy_allocation.allocation_projection(db)
    assert projection["current"]["weights"][0]["regime_cell"] == "up/mid/strong"
    assert strategy_allocation.allocation_features(None)["status"] == "unweighted"


def test_the_cycle_records_the_allocation_on_the_buy_signal():
    from tests.test_stock_paper_autotrader import _Db, _account, signal_source
    from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle

    class Db(_Db):
        def begin_nested(self):
            return nullcontext()

    strategy = SimpleNamespace(id=1, strategy_type="moving_average_crossover", current_status="paper_trading_active")
    weight = SimpleNamespace(multiplier=Decimal("0.8"), status="regime_reduced", session_date=date(2026, 10, 30),
                             evidence={"base_multiplier": 0.95, "regime_cell": "down/mid/weak",
                                       "regime_multiplier": 0.842105, "regime_status": "reduced"})
    order = SimpleNamespace(id=42, status="accepted")
    tags = []
    with patch.object(settings, "paper_execution_symbols", ["AAPL"]), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         patch("app.services.strategy_allocation.session_weight_rows", return_value={"moving_average_crossover": weight}), \
         patch("app.services.stock_paper_autotrader._tag_signal", side_effect=lambda _db, sid, key, value: tags.append((sid, key, value))), \
         signal_source(False, return_value={"signal_id": 7, "signal_action": "BUY", "reference_price": "100"}), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order):
        result = run_stock_paper_signal_cycle(Db([strategy]))
    reserve.assert_called_once()
    [buy] = [item for item in result["results"] if item.get("signal_action") == "BUY"]
    assert buy["allocation_weight"] == "0.8"
    assert result["loss_feedback"]["allocation_regime_cells"] == ["down/mid/weak"]
    [(signal_id, _, allocation)] = [tag for tag in tags if tag[1] == "allocation"]
    assert signal_id == 7
    assert allocation["multiplier"] == "0.8" and allocation["regime_cell"] == "down/mid/weak"
    assert allocation["regime_multiplier"] == 0.842105 and allocation["base_multiplier"] == 0.95
    assert allocation["regime_enabled"] is False


# ------------------------------------------------------------------ surface

def test_scoreboard_has_a_regime_section(db, monkeypatch):  # noqa: F811
    from app.services import learning_scoreboard as board

    monkeypatch.setattr("app.services.stock_paper_ledger.active_paper_broker_name", lambda: "alpaca_paper")
    _strategy(db, "mr")
    _spy_history(db, rising=True)
    for _ in range(6):
        _trade(db, "mr", -0.02, vix=25.0)
    section = board.build_learning_scoreboard(db, now=NOW)["regime"]
    assert section["enabled"] is False and section["paper_only"] is True
    [row] = section["strategies"]
    assert row["strategy_type"] == "mr" and row["cells"][0]["cell"] == "up/mid/strong"
    assert row["cells"][0]["trades"] == 6 and row["cells"][0]["expectancy_bps"] == -200.0
    assert row["in_force"]["multiplier"] is None  # nothing stored for the session yet
    assert row["current_cell"]["regime_status"] == "fallback_overall"  # no SPY in the price table at NOW: unknown trend
    assert required_role("GET", "/stock-paper/learning-scoreboard") == "viewer"


# ------------------------------------------------------------------ the backtest's rule

@pytest.fixture()
def backtest():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import run_regime_allocation_backtest as bt
        yield bt
    finally:
        sys.path.remove(str(scripts))


def _arms(regime_exp, regime_dd, *, resized=10):
    metrics = {}
    for arm, exp, dd in (("equal", (10.0, 20.0, 15.0), (0.30, 0.20, 0.20)),
                         ("performance", (10.5, 19.0, 15.5), (0.28, 0.19, 0.19)),
                         ("regime", regime_exp, regime_dd)):
        metrics[arm] = {window: {"trades": 100, "weighted_expectancy_bps": e, "max_drawdown": d,
                                 "trades_resized": resized if arm == "regime" else 0}
                        for window, e, d in zip(("all", "2015+", "2020+"), exp, dd)}
    return metrics


def test_backtest_decision_rule_adopts_only_when_no_worse_than_both_arms_everywhere(backtest):
    good = backtest.decide(_arms((10.5, 20.0, 16.0), (0.28, 0.19, 0.19)))
    assert good["adopt"] is True and good["paper_allocation_regime_enabled"] is True and good["reasons"] == []
    assert good["min_expectancy_margin_bps"] == 0.0
    lower = backtest.decide(_arms((10.5, 19.5, 16.0), (0.28, 0.19, 0.19)))  # below equal in 2015+
    assert lower["adopt"] is False and lower["reasons"] == ["2015+: expectancy below equal (19.5 < 20.0 bps)"]
    deeper = backtest.decide(_arms((10.5, 20.0, 16.0), (0.28, 0.19, 0.21)))  # drawdown worse in 2020+
    assert deeper["adopt"] is False and all("2020+: drawdown worse" in reason for reason in deeper["reasons"])
    never = backtest.decide(_arms((10.5, 20.0, 16.0), (0.28, 0.19, 0.19), resized=0))
    assert never["adopt"] is False and "never changed a size" in never["reasons"][0]


def test_backtest_weights_are_causal_and_fall_back_on_thin_cells(backtest):
    policy = strategy_allocation.bounds()
    rows = pd.DataFrame([
        *[{"strategy": "mr", "entry_date": "2026-01-02", "exit_date": "2026-01-10", "net_return": -0.02, "cell": "up/mid/strong"}
          for _ in range(6)],
        *[{"strategy": "mr", "entry_date": "2026-01-03", "exit_date": "2026-01-12", "net_return": 0.03, "cell": "up/low/strong"}
          for _ in range(2)],
        {"strategy": "mr", "entry_date": "2026-01-20", "exit_date": "2026-02-01", "net_return": 0.5, "cell": "up/mid/strong"},
    ])
    rows["entry_date"] = pd.to_datetime(rows["entry_date"])
    rows["exit_date"] = pd.to_datetime(rows["exit_date"])
    records = backtest.Records(rows)
    # Nothing closed before the first entries: every arm is 1.
    assert backtest.weights_for_day(records, ["mr"], pd.Timestamp("2026-01-05"), "up/mid/strong", policy)["mr"] == {
        "equal": 1.0, "performance": 1.0, "regime": 1.0, "cell_trades": 0, "regime_factor": 1.0}
    # On 2026-01-15 the eight January trades are closed; the 2026-02-01 exit is still in the future.
    weights = backtest.weights_for_day(records, ["mr"], pd.Timestamp("2026-01-15"), "up/mid/strong", policy)["mr"]
    assert weights["cell_trades"] == 6 and weights["regime_factor"] < 1.0
    assert weights["regime"] < weights["performance"] < 1.0 and weights["regime"] >= policy["floor"]
    thin = backtest.weights_for_day(records, ["mr"], pd.Timestamp("2026-01-15"), "up/low/strong", policy)["mr"]
    assert thin["cell_trades"] == 2 and thin["regime_factor"] == 1.0 and thin["regime"] == thin["performance"]
    sized = backtest.size_trades(rows, policy)
    assert list(sized.columns[-4:]) == ["w_equal", "w_performance", "w_regime", "cell_trades"]
    assert (sized["w_equal"] == 1.0).all() and sized.loc[8, "w_regime"] < sized.loc[8, "w_performance"]
