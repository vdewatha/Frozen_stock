"""Scorecard math, the research-only RSI(2)/IBS candidates and causality."""
import math

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.backtester import BacktestConfig, run_backtest
from app.services.strategies.base import BaseStrategy, neutral_signal
from app.services.strategies.registry import RESEARCH_ONLY_STRATEGY_SLUGS, STRATEGY_REGISTRY, get_strategy
from app.services.strategies.short_term_reversion import (
    IBSMeanReversionStrategy,
    RSI2PullbackStrategy,
    internal_bar_strength,
    wilder_rsi,
)
from app.services.strategy_scorecard import (
    StrategyVariant,
    default_variants,
    goal_assessment,
    run_scorecard,
    score_cell,
    trade_statistics,
    wilson_interval,
    window_metrics,
)


def _prices(rows: int = 600, seed: int = 3, drift: float = 0.0006, vol: float = 0.012) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol, rows)))
    opens = close * np.exp(rng.normal(0, 0.003, rows))
    high = np.maximum(opens, close) * (1 + np.abs(rng.normal(0, 0.004, rows)))
    low = np.minimum(opens, close) * (1 - np.abs(rng.normal(0, 0.004, rows)))
    return pd.DataFrame({
        "date": pd.bdate_range("2012-01-02", periods=rows).strftime("%Y-%m-%d"),
        "open": opens, "high": high, "low": low, "close": close,
        "volume": rng.integers(2_000_000, 4_000_000, rows).astype(float),
    })


# ---------------------------------------------------------------- statistics

def test_eighty_percent_at_one_to_four_payoff_is_break_even():
    stats = trade_statistics([0.01] * 8 + [-0.04] * 2)
    assert stats["trades"] == 10 and stats["wins"] == 8 and stats["losses"] == 2
    assert stats["win_rate"] == 0.8
    assert stats["avg_win_pct"] == pytest.approx(1.0)
    assert stats["avg_loss_pct"] == pytest.approx(4.0)
    assert stats["payoff_ratio"] == pytest.approx(0.25)
    assert stats["breakeven_win_rate"] == pytest.approx(0.8)
    assert stats["expectancy_bps"] == pytest.approx(0.0, abs=1e-9)
    assert stats["profit_factor"] == pytest.approx(1.0)
    assert not goal_assessment(stats, min_trades=1)["meets_goal"]


def test_expectancy_payoff_and_profit_factor_on_synthetic_trades():
    stats = trade_statistics([0.02, 0.02, 0.02, -0.01])
    assert stats["win_rate"] == 0.75
    assert stats["expectancy_pct"] == pytest.approx(1.25)
    assert stats["expectancy_bps"] == pytest.approx(125.0)
    assert stats["payoff_ratio"] == pytest.approx(2.0)
    assert stats["profit_factor"] == pytest.approx(6.0)
    # Too few trades for a bootstrap interval.
    assert stats["expectancy_ci_bps"] is None and stats["bootstrap"]["status"] == "insufficient_trades"


def test_scratch_trade_after_costs_is_a_loss_and_empty_input_is_safe():
    stats = trade_statistics([0.0, 0.01])
    assert stats["wins"] == 1 and stats["losses"] == 1 and stats["win_rate"] == 0.5
    empty = trade_statistics([])
    assert empty["trades"] == 0 and empty["win_rate"] is None and not goal_assessment(empty, min_trades=1)["meets_goal"]


def test_wilson_interval_matches_reference_values():
    low, high = wilson_interval(80, 100)
    assert low == pytest.approx(0.7112, abs=1e-3) and high == pytest.approx(0.8667, abs=1e-3)
    assert wilson_interval(0, 0) is None


def test_bootstrap_ci_brackets_the_mean_and_clusters_are_respected():
    rng = np.random.default_rng(0)
    returns = rng.normal(0.002, 0.01, 400)
    stats = trade_statistics(returns, samples=1000, seed=1)
    low, high = stats["expectancy_ci_bps"]
    assert low < stats["expectancy_bps"] < high
    assert stats["win_rate_ci"][0] < stats["win_rate"] < stats["win_rate_ci"][1]
    # Fewer, larger clusters (correlated trades) widen the interval.
    clustered = trade_statistics(returns, clusters=[i // 40 for i in range(400)], samples=1000, seed=1)
    assert clustered["bootstrap"]["clusters"] == 10
    assert (clustered["expectancy_ci_bps"][1] - clustered["expectancy_ci_bps"][0]) >= 0
    # One cluster: every resample is the full sample, so the CI collapses.
    single = trade_statistics(returns, clusters=["m"] * 400, samples=200, seed=1)
    assert single["expectancy_ci_bps"][0] == pytest.approx(single["expectancy_ci_bps"][1])
    assert single["expectancy_ci_bps"][0] == pytest.approx(stats["expectancy_bps"], abs=0.01)


def test_goal_requires_win_rate_expectancy_ci_and_trade_count():
    rng = np.random.default_rng(5)
    good = np.where(rng.random(300) < 0.9, 0.01, -0.02)
    stats = trade_statistics(good, samples=500, seed=2)
    verdict = goal_assessment(stats, min_trades=100)
    assert verdict["meets_goal"] and verdict["checks"]["expectancy_ci_low_gt_0"]
    assert not goal_assessment(stats, min_trades=1000)["meets_goal"]
    high_win_negative = trade_statistics(np.where(rng.random(300) < 0.85, 0.005, -0.05), samples=500, seed=2)
    assert high_win_negative["win_rate"] >= 0.8
    assert not goal_assessment(high_win_negative, min_trades=100)["meets_goal"]


# ---------------------------------------------------------------- strategies

def test_new_strategies_are_registered_and_seeded_as_scoped_candidates():
    assert {"rsi2_pullback", "ibs_mean_reversion"} <= set(STRATEGY_REGISTRY)
    assert RESEARCH_ONLY_STRATEGY_SLUGS == frozenset()
    import app.models  # noqa: F401
    import app.models.stock_paper  # noqa: F401  (all FK targets)
    from app.db.base import Base
    from app.models import Strategy
    from app.services.seed import seed_defaults

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_defaults(db)
        rows = {row.strategy_type: row for row in db.query(Strategy).all()}
        for slug in ("rsi2_pullback", "ibs_mean_reversion"):
            assert rows[slug].current_status == "paper_trading_candidate"
            assert rows[slug].parameters["symbol_scope"] == ["SPY", "QQQ", "IWM", "DIA"]
            assert get_strategy(slug, rows[slug].parameters).symbol_scope() == {"SPY", "QQQ", "IWM", "DIA"}
        assert rows["moving_average_crossover"].current_status == "paper_trading_candidate"
    engine.dispose()


def test_wilder_rsi_edge_cases_and_ibs():
    assert wilder_rsi(pd.Series(np.arange(1.0, 30.0)), 2).iloc[-1] == 100.0
    assert wilder_rsi(pd.Series([5.0] * 30), 2).iloc[-1] == 50.0
    falling = wilder_rsi(pd.Series(np.r_[np.arange(1.0, 30.0), [20.0, 15.0]]), 2).iloc[-1]
    assert falling <= 10
    bars = pd.DataFrame({"high": [10.0, 10.0, 10.0], "low": [8.0, 8.0, 10.0], "close": [8.2, 9.8, 10.0]})
    assert list(internal_bar_strength(bars).round(2)) == [0.1, 0.9, 0.5]


def _uptrend_with_dip(dip: bool) -> pd.DataFrame:
    close = list(np.linspace(100, 160, 260))
    if dip:
        close += [156.0, 151.0]
    frame = pd.DataFrame({"close": close})
    frame["open"] = frame["close"]
    frame["high"] = frame["close"] * 1.005
    frame["low"] = frame["close"] * 0.995
    frame["volume"] = 1e6
    frame["date"] = pd.bdate_range("2020-01-01", periods=len(frame)).strftime("%Y-%m-%d")
    return frame


def test_rsi2_buys_dips_only_above_the_trend_and_exits_on_rule():
    strategy = RSI2PullbackStrategy({"entry_threshold": 10})
    assert strategy.generate_signal("X", _uptrend_with_dip(True)).action == "BUY"
    assert strategy.generate_signal("X", _uptrend_with_dip(False)).action == "HOLD"
    downtrend = _uptrend_with_dip(True)
    downtrend["close"] = downtrend["close"].iloc[::-1].to_numpy()
    assert strategy.generate_signal("X", downtrend).action == "HOLD"
    assert strategy.generate_signal("X", _uptrend_with_dip(True).head(100)).action == "HOLD"

    held = pd.DataFrame({"close": [100.0, 99.0, 98.0, 97.0, 96.0, 99.5]})
    sma_exit = RSI2PullbackStrategy({"exit_rule": "sma", "exit_sma_window": 5})
    up_exit = RSI2PullbackStrategy({"exit_rule": "up_close"})
    assert sma_exit.exit_signal("X", held.head(5), 4) is None
    assert sma_exit.exit_signal("X", held, 4) == "close_above_sma5"
    assert up_exit.exit_signal("X", held.head(5), 4) is None
    assert up_exit.exit_signal("X", held, 4) == "first_up_close"


def test_ibs_buys_weak_closes_in_an_uptrend_and_exits_on_rule():
    frame = _uptrend_with_dip(False)
    weak = frame.copy()
    weak.loc[weak.index[-1], ["high", "low", "close"]] = [161.0, 158.0, 158.2]
    strategy = IBSMeanReversionStrategy({"entry_threshold": 0.2})
    assert strategy.generate_signal("X", weak).action == "BUY"
    assert strategy.generate_signal("X", frame).action == "HOLD"  # IBS = 0.5
    strong = weak.copy()
    strong.loc[strong.index[-1], ["high", "low", "close"]] = [161.0, 158.0, 160.9]
    assert strategy.exit_signal("X", strong, len(strong) - 2) == "ibs_high"
    assert strategy.exit_signal("X", weak, len(weak) - 2) is None
    up = IBSMeanReversionStrategy({"exit_rule": "up_close"})
    assert up.exit_signal("X", frame, len(frame) - 2) == "first_up_close"


# ---------------------------------------------------------------- backtester exit hook

class _ExitAfterTwoBars(BaseStrategy):
    slug = "test_exit_after_two"

    def generate_signal(self, symbol, data):
        if len(data) == 62:
            from app.services.strategies.base import StrategySignalResult

            return StrategySignalResult("BUY", 0.6, 0.4, 0.9, "test", {})
        return neutral_signal("hold")

    def exit_signal(self, symbol, data, entry_index):
        return "done" if len(data) - 1 >= entry_index + 1 else None


def test_indicator_exit_fills_at_next_open_after_the_signal_bar():
    prices = pd.DataFrame({
        "date": pd.bdate_range("2025-01-01", periods=70).strftime("%Y-%m-%d"),
        "open": [100.0] * 64 + [104.0] + [100.0] * 5,
        "high": 106.0, "low": 99.0, "close": 100.0, "volume": 1e9,
    })
    from unittest.mock import patch

    with patch("app.services.backtester.get_strategy", return_value=_ExitAfterTwoBars()):
        result = run_backtest("T", "test", prices, BacktestConfig(
            cost_model="flat", fees_bps=0, slippage_bps=0, stop_loss_pct=0.5, take_profit_pct=5))
    trade = result["trades"][0]
    # Entry at bar 62's open; the exit is decided on completed bar 63 and
    # filled at bar 64's open (104), never at bar 63's close.
    assert trade["entry_date"] == prices["date"][62]
    assert trade["exit_date"] == prices["date"][64]
    assert trade["exit_reason"] == "signal_exit:done"
    assert trade["exit_price"] == 104.0


# ---------------------------------------------------------------- causality

@pytest.mark.parametrize("slug,parameters", [
    ("rsi2_pullback", {"entry_threshold": 15, "exit_rule": "sma"}),
    ("ibs_mean_reversion", {"entry_threshold": 0.2, "exit_rule": "ibs"}),
])
def test_no_look_ahead_future_bars_do_not_change_past_trades(slug, parameters):
    prices = _prices(700)
    cut = 500
    altered = prices.copy()
    rng = np.random.default_rng(99)
    shock = np.exp(rng.normal(0, 0.05, len(prices) - cut))
    for column in ("open", "high", "low", "close"):
        altered.loc[cut:, column] = altered.loc[cut:, column].to_numpy() * shock
    altered.loc[cut:, "high"] = altered.loc[cut:, ["open", "high", "low", "close"]].max(axis=1)
    altered.loc[cut:, "low"] = altered.loc[cut:, ["open", "high", "low", "close"]].min(axis=1)
    config = BacktestConfig(stop_loss_pct=0.1, take_profit_pct=10, max_holding_days=10)
    original = run_backtest("T", slug, prices, config, parameters)
    perturbed = run_backtest("T", slug, altered, config, parameters)
    boundary = prices["date"][cut]
    before = [t for t in original["trades"] if t["exit_date"] < boundary]
    assert before, "fixture should produce trades before the cut"
    assert before == [t for t in perturbed["trades"] if t["exit_date"] < boundary]
    # Signals on a prefix never depend on later rows.
    strategy = get_strategy(slug, parameters)
    for t in (300, 420, cut - 1):
        assert strategy.generate_signal("T", prices.iloc[:t]).action == strategy.generate_signal("T", altered.iloc[:t]).action


# ---------------------------------------------------------------- scorecard

def test_window_metrics_reconstruct_exposure_and_window_slicing():
    prices = _prices(700)
    variant = StrategyVariant("v", "rsi2_pullback", {"entry_threshold": 15},
                              {"stop_loss_pct": 0.1, "take_profit_pct": 10, "max_holding_days": 10})
    config = variant.config()
    result = run_backtest("T", "rsi2_pullback", prices, config, variant.parameters)
    full = window_metrics(result, prices, None, config, samples=200)
    assert full["trades"] == result["number_of_trades"]
    assert full["average_exposure"] == pytest.approx(result["average_exposure"], abs=2e-3)
    assert full["total_return"] == pytest.approx(result["total_return"], abs=1e-4)
    assert full["buy_and_hold_return"] == pytest.approx(result["baselines"]["buy_and_hold"]["total_return"], abs=1e-4)
    assert full["exposure_matched_buy_and_hold_return"] == pytest.approx(
        full["average_exposure"] * full["buy_and_hold_return"], abs=1e-4)
    later = window_metrics(result, prices, prices["date"][400], config, samples=200)
    assert later["window_start"] == prices["date"][400]
    assert later["trades"] == sum(1 for t in result["trades"] if t["entry_date"] >= prices["date"][400])
    assert window_metrics(result, prices, "2999-01-01", config) is None


def test_scorecard_pools_symbols_and_counts_every_variant_as_a_trial():
    prices = {"AAA": _prices(650, seed=1), "BBB": _prices(650, seed=2)}
    variants = [
        StrategyVariant("rsi2[a]", "rsi2_pullback", {"entry_threshold": 10},
                        {"stop_loss_pct": 0.1, "take_profit_pct": 10, "max_holding_days": 5}, family="rsi2_pullback"),
        StrategyVariant("ibs[a]", "ibs_mean_reversion", {"entry_threshold": 0.2},
                        {"stop_loss_pct": 0.1, "take_profit_pct": 10, "max_holding_days": 5}, family="ibs"),
        StrategyVariant("macd", "macd_momentum", family="macd"),
    ]
    windows = {"all": None, "late": "2013-06-01"}
    card = run_scorecard(prices, variants, windows=windows, workers=1, samples=200)
    assert card["trials_per_window"] == {"all": 3, "late": 3}
    for row in card["pooled"]:
        assert row["dsr_trials"] == 3
        cells = [c for c in card["per_symbol"] if c["variant_id"] == row["variant_id"] and c["window"] == row["window"]]
        assert row["trades"] == sum(c["trades"] for c in cells)
        assert row["symbols"] == 2
    assert all(c["dsr_trials"] == 6 for c in card["per_symbol"])
    assert not card["errors"]
    single = score_cell("AAA", variants[0], prices["AAA"], windows, samples=200)
    assert set(single["windows"]) == {"all", "late"}


def test_default_variants_cover_all_existing_strategies_and_candidate_grids():
    variants = default_variants()
    families = {v.family for v in variants}
    assert {"moving_average_crossover", "macd_momentum", "rsi_mean_reversion", "bollinger_mean_reversion",
            "channel_breakout", "trend_pullback", "ensemble", "rsi2_pullback", "ibs_mean_reversion"} == families
    assert len([v for v in variants if v.family == "rsi2_pullback"]) == 12
    assert len({v.variant_id for v in variants}) == len(variants)
    model = default_variants(include_model_predictive=True)[-1]
    assert model.strategy == "model_predictive_long" and model.windows == ("post2020",)
    assert math.isclose(variants[-1].config().stop_loss_pct, 0.10)
