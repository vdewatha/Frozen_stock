"""Regression tests for the strategy-signal / sizing / risk review (2026-10-27).

Each test reproduces a defect or a missing safeguard found in the review; it
fails on the pre-review code and passes after the fix.
"""
from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from app.services import portfolio_allocation, portfolio_risk, trade_candidates
from app.services.market_regime import classify_market_regime, regime_risk_multiplier
from app.services.news_sentiment import summarize_news_context
from app.services.opportunity_radar import _opportunity_score, _price_features
from app.services.risk import DEFAULT_RISK_RULES, PortfolioState, StrategyState, approve_trade, volatility_capped_exposure
from app.services.risk_settings import update_risk_settings
from app.services.strategies.base import clamp
from app.services.strategies.registry import get_strategy


def _frame(close, volume=1_000.0):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(close)),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
        }
    )


# --------------------------------------------------------------------------
# Strategy signals
# --------------------------------------------------------------------------

def test_clamp_maps_nan_to_neutral_not_full_confidence():
    # min(1.0, nan) returns 1.0 in Python, so NaN used to clamp to 100%.
    assert clamp(float("nan")) == 0.5
    assert clamp(float("inf")) == 0.5
    assert clamp(2.0) == 1.0 and clamp(-1.0) == 0.0


@pytest.mark.parametrize("slug", ["moving_average_crossover", "rsi_mean_reversion", "macd_momentum", "bollinger_mean_reversion", "channel_breakout", "trend_pullback", "ensemble"])
def test_warm_up_history_never_produces_an_actionable_signal(slug):
    signal = get_strategy(slug).generate_signal("X", _frame(np.linspace(100, 110, 10)))
    assert signal.action == "HOLD"
    assert signal.confidence < 0.55
    assert math.isfinite(signal.probability_up)


def test_short_history_ensemble_is_not_a_confident_buy():
    # Pre-fix: NaN moving averages clamped to confidence 1.0 / probability 1.0
    # and the ensemble emitted BUY with 0.68 confidence on 10 flat bars.
    signal = get_strategy("ensemble").generate_signal("X", _frame([100.0] * 10))
    assert signal.action == "HOLD"


@pytest.mark.parametrize("slug", ["rsi_mean_reversion", "bollinger_mean_reversion"])
def test_flat_prices_do_not_look_oversold(slug):
    # Pre-fix: zero gain and zero loss gave RSI 0 -> BUY at confidence 1.0.
    signal = get_strategy(slug).generate_signal("X", _frame([100.0] * 80))
    assert signal.action != "BUY"
    assert signal.confidence < 0.55


def test_rsi_is_100_when_only_gains():
    from app.services.strategies.base import rsi

    assert rsi(pd.Series(np.arange(1.0, 30.0))).iloc[-1] == 100.0
    assert rsi(pd.Series([5.0] * 30)).iloc[-1] == 50.0


def test_equal_moving_averages_are_hold_not_sell():
    signal = get_strategy("moving_average_crossover").generate_signal("X", _frame([100.0] * 80))
    assert signal.action == "HOLD"


def test_macd_confidence_is_neutral_for_a_negligible_histogram():
    # Pre-fix: any non-zero histogram got >= 0.55 confidence, exactly the
    # backtester's entry gate, so noise-sized crosses were tradable.
    close = [100.0] * 79 + [100.01]
    signal = get_strategy("macd_momentum").generate_signal("X", _frame(close))
    assert signal.action == "BUY"
    assert signal.confidence < 0.55


def test_trend_pullback_requires_a_warmed_up_slow_ema():
    signal = get_strategy("trend_pullback").generate_signal("X", _frame(np.linspace(100, 120, 61)))
    assert signal.action == "HOLD"
    assert signal.features.get("required_rows") == 100


def test_channel_breakout_without_volume_is_not_confirmed():
    close = list(np.linspace(100, 101, 70)) + [110.0]
    frame = _frame(close)
    frame["volume"] = float("nan")
    signal = get_strategy("channel_breakout").generate_signal("X", frame)
    assert signal.action == "HOLD"
    assert "Volume" in signal.reason


# --------------------------------------------------------------------------
# approve_trade
# --------------------------------------------------------------------------

def _buy(**extra):
    return {"symbol": "AAA", "strategy": "1", "action": "BUY", "confidence": 0.9} | extra


def test_nan_confidence_is_rejected():
    approved, reason = approve_trade(_buy(confidence=float("nan")), PortfolioState(), StrategyState())
    assert not approved and "finite" in reason


def test_nan_drawdowns_are_rejected():
    assert not approve_trade(_buy(), PortfolioState(daily_drawdown=float("nan")), StrategyState())[0]
    assert not approve_trade(_buy(), PortfolioState(), StrategyState(drawdown=float("nan")))[0]


def test_drawdown_exactly_at_limit_blocks():
    limit = DEFAULT_RISK_RULES["max_daily_drawdown"]
    approved, reason = approve_trade(_buy(), PortfolioState(daily_drawdown=limit), StrategyState())
    assert not approved and "drawdown" in reason.lower()


def test_gross_exposure_cap_blocks_even_when_symbol_has_room():
    book = {f"S{i}": 0.099 for i in range(9)} | {"BIG": 0.5}
    approved, reason = approve_trade(_buy(), PortfolioState(open_positions_by_symbol=book), StrategyState())
    assert not approved and "Gross exposure" in reason


def test_proposed_exposure_counts_against_symbol_cap():
    portfolio = PortfolioState(open_positions_by_symbol={"AAA": 0.09})
    assert approve_trade(_buy(proposed_exposure=0.005), portfolio, StrategyState())[0]
    approved, reason = approve_trade(_buy(proposed_exposure=0.05), portfolio, StrategyState())
    assert not approved and "symbol exposure" in reason


def test_ordinary_trade_still_approved():
    assert approve_trade(_buy(), PortfolioState(open_positions_by_symbol={"AAA": 0.02}), StrategyState()) == (True, "Approved for paper trading.")


def test_volatility_capped_exposure():
    assert volatility_capped_exposure(0.05) == pytest.approx(0.10)
    assert volatility_capped_exposure(0.01, {"max_risk_per_trade": 0.005}) == pytest.approx(0.25)
    assert volatility_capped_exposure(None) is None
    assert volatility_capped_exposure(float("nan")) is None


# --------------------------------------------------------------------------
# Market regime
# --------------------------------------------------------------------------

def test_sixty_day_selloff_is_not_classified_as_bull_trend():
    rally = np.linspace(100, 200, 180)
    selloff = np.linspace(200, 180, 61)[1:]  # -10% over 60 days, smooth (low vol)
    result = classify_market_regime(_frame(np.concatenate([rally, selloff])))
    assert result["spy_trend"] == "uptrend"
    assert result["rate_regime"] == "risk_off"
    assert result["market_regime"] != "bull_trend"


def test_regime_risk_multiplier_only_reduces():
    assert regime_risk_multiplier(None)["multiplier"] == 1.0
    assert regime_risk_multiplier({"market_regime": "bull_trend", "spy_trend": "uptrend"})["multiplier"] == 1.0
    bear = regime_risk_multiplier({"market_regime": "bear_stress", "spy_trend": "downtrend", "volatility_regime": "high_volatility"})
    assert bear["multiplier"] == 0.25 and "spy_trend=downtrend" in bear["reasons"]
    assert regime_risk_multiplier({"market_regime": "transition", "spy_trend": "downtrend"})["multiplier"] == 0.5


# --------------------------------------------------------------------------
# Trade candidates
# --------------------------------------------------------------------------

def test_prediction_must_clear_costs_with_margin():
    cost = trade_candidates.round_trip_cost()
    assert cost == pytest.approx(0.0012)
    thin = {"probability_up": 0.6, "expected_return": 0.0021, "horizon_days": 1}
    healthy = {"probability_up": 0.6, "expected_return": 0.01, "horizon_days": 5}
    assert trade_candidates._best_positive_prediction([thin], cost) is None
    assert trade_candidates._best_positive_prediction([thin, healthy], cost) is healthy
    assert trade_candidates._best_positive_prediction([healthy], 0.02) is None
    assert trade_candidates._best_positive_prediction([{"probability_up": float("nan"), "expected_return": 0.05}], cost) is None


def test_scanner_confidence_bar_matches_risk_gate():
    assert trade_candidates.MIN_STRATEGY_CONFIDENCE >= DEFAULT_RISK_RULES["min_confidence"]


def test_average_true_range_pct():
    frame = _frame([100.0] * 30)
    assert trade_candidates.average_true_range_pct(frame) == pytest.approx(0.02)
    assert trade_candidates.average_true_range_pct(frame.head(5)) is None


def _scan(signal, regime, predictions):
    db = MagicMock()
    asset_query = MagicMock()
    asset_query.filter.return_value.order_by.return_value.all.return_value = [("SPY",)]
    strategy_query = MagicMock()
    strategy_query.order_by.return_value.all.return_value = [
        SimpleNamespace(id=1, name="Test", strategy_type="test", parameters={}, current_status="paper_trading_active")
    ]
    db.query.side_effect = [asset_query, strategy_query]
    prices = _frame(np.linspace(100, 120, 150))
    with patch.object(trade_candidates.settings, "paper_execution_symbols", ["SPY"]), \
         patch.object(trade_candidates, "trusted_history", return_value=(prices, "database:yfinance")), \
         patch.object(trade_candidates, "summarize_macro_context", return_value={"summary": ""}), \
         patch.object(trade_candidates, "latest_market_regime", return_value=regime), \
         patch.object(trade_candidates, "predict_probabilities", return_value={"predictions": predictions}), \
         patch.object(trade_candidates, "summarize_news_context", return_value={"summary": ""}), \
         patch.object(trade_candidates, "strategy_research_for", return_value={}), \
         patch.object(trade_candidates, "get_strategy") as get_strategy, \
         patch.object(trade_candidates, "run_backtest", return_value={"score": 0.2, "rejected": False, "rejection_reasons": []}):
        get_strategy.return_value.generate_signal.return_value = signal
        return trade_candidates.scan_trade_candidates(db)["candidates"][0]


def test_scanner_records_structured_decision_trace():
    signal = SimpleNamespace(action="BUY", confidence=0.8, probability_up=0.6, reason="test")
    regime = {"market_regime": "bear_stress", "spy_trend": "downtrend", "volatility_regime": "high_volatility"}
    candidate = _scan(signal, regime, [{"probability_up": 0.6, "expected_return": 0.01, "horizon_days": 5}])
    trace = candidate["decision_trace"]
    assert candidate["candidate_status"] == "positive_candidate"
    assert trace["gates"] == {"model_supported": True, "strategy_supported": True, "backtest_supported": True}
    assert trace["expected_edge"]["net_expected_return"] == pytest.approx(0.01 - 0.0012)
    assert trace["thresholds"]["min_net_expected_edge"] == trade_candidates.MIN_NET_EXPECTED_EDGE
    assert candidate["regime_risk_multiplier"] == 0.25
    assert candidate["atr_pct"] is not None and candidate["volatility_exposure_cap"] is not None


def test_scanner_does_not_promote_confidence_below_risk_gate():
    signal = SimpleNamespace(action="BUY", confidence=0.56, probability_up=0.6, reason="test")
    candidate = _scan(signal, None, [{"probability_up": 0.6, "expected_return": 0.01, "horizon_days": 5}])
    assert candidate["candidate_status"] == "watch"


# --------------------------------------------------------------------------
# Allocation
# --------------------------------------------------------------------------

def _candidate(symbol, strategy, **extra):
    return {
        "symbol": symbol,
        "strategy": strategy,
        "candidate_status": "positive_candidate",
        "action": "BUY",
        "strategy_status": "paper_trading_active",
        "score": 1.0,
        "probability_up": 0.6,
        "expected_return": 0.01,
        "backtest_score": 0.2,
    } | extra


def _plan(candidates, gross_exposure=0.0, symbol_exposure=None):
    db = MagicMock()
    db.query.return_value.all.return_value = []
    db.query.return_value.filter.return_value.all.return_value = []
    risk = {
        "risk_limits": {"max_symbol_exposure": 0.10, "max_total_exposure": 1.0, "journal_feedback_allocation_multiplier_cap": 0.2},
        "positions": [],
        "symbol_exposure": [{"key": key, "exposure_pct": value} for key, value in (symbol_exposure or {}).items()],
        "paper_equity": 100_000.0,
        "open_positions": 0,
        "gross_exposure": gross_exposure,
        "alerts": [],
    }
    with patch.object(portfolio_allocation, "portfolio_risk_snapshot", return_value=risk), \
         patch.object(portfolio_allocation, "memory_replay_evaluation", return_value={}), \
         patch.object(portfolio_allocation, "get_trade_candidate_snapshot", return_value={"candidates": candidates}):
        plan = portfolio_allocation.allocation_plan(db)
    return {row["symbol"]: row for row in plan["recommendations"]}


def test_allocation_does_not_add_when_gross_cap_is_full():
    rows = _plan([_candidate("AAA", "s1")], gross_exposure=1.0)
    assert rows["AAA"]["recommendation"] == "wait_for_room"
    assert _plan([_candidate("AAA", "s1")], gross_exposure=0.2)["AAA"]["recommendation"] == "add"


def test_allocation_target_scales_down_for_volatility_and_regime():
    rows = _plan([
        _candidate("CALM", "s1"),
        _candidate("WILD", "s1", volatility_exposure_cap=0.04),
        _candidate("BEAR", "s1", regime_risk_multiplier=0.25),
    ])
    assert rows["CALM"]["target_exposure_pct"] == pytest.approx(0.10)
    assert rows["WILD"]["target_exposure_pct"] == pytest.approx(0.04)
    assert rows["BEAR"]["target_exposure_pct"] == pytest.approx(0.025)
    assert rows["WILD"]["sizing_context"]["volatility_exposure_cap"] == pytest.approx(0.04)


def test_executor_adds_once_per_symbol_per_pass():
    rows = [
        {"symbol": "AAA", "strategy": "s1", "recommendation": "add", "reason": "r"},
        {"symbol": "AAA", "strategy": "s2", "recommendation": "add", "reason": "r"},
        {"symbol": "BBB", "strategy": "s1", "recommendation": "add", "reason": "r"},
    ]
    with patch.object(portfolio_allocation, "allocation_plan", return_value={"recommendations": rows}), \
         patch.object(portfolio_allocation, "readiness_snapshot", return_value={"checks": []}), \
         patch.object(portfolio_allocation, "write_audit_log"):
        result = portfolio_allocation.execute_allocation_plan(MagicMock(), dry_run=True, max_actions=5)
    assert [(a["symbol"], a["strategy"]) for a in result["actions"]] == [("AAA", "s1"), ("BBB", "s1")]
    assert any("already received an add" in row["skip_reason"] for row in result["skipped"])


# --------------------------------------------------------------------------
# Portfolio risk monitor
# --------------------------------------------------------------------------

def test_portfolio_risk_alerts_on_gross_exposure():
    trades = [
        SimpleNamespace(id=i, symbol=f"S{i}", entry_price=100.0, quantity=95.0, side="BUY", strategy_id=None, entry_time=None)
        for i in range(11)
    ]

    def query(model):
        q = MagicMock()
        if model is portfolio_risk.RiskRule:
            q.filter.return_value.order_by.return_value.first.return_value = None
        elif model is portfolio_risk.PaperTrade:
            q.filter.return_value.order_by.return_value.all.return_value = trades
        else:
            q.all.return_value = []
        return q

    db = MagicMock()
    db.query.side_effect = query
    with patch.object(portfolio_risk, "_latest_price", return_value=(100.0, "database:yfinance")):
        snapshot = portfolio_risk.portfolio_risk_snapshot(db)
    assert snapshot["gross_exposure"] == pytest.approx(1.045)
    assert any(alert["label"] == "Gross exposure" and alert["severity"] == "breach" for alert in snapshot["alerts"])
    assert snapshot["risk_limits"]["max_total_exposure"] == 1.0


# --------------------------------------------------------------------------
# News / opportunity radar
# --------------------------------------------------------------------------

def test_synthetic_news_is_flagged_and_ignored_by_opportunity_score():
    rows = [SimpleNamespace(id=1, title="X resilient growth", source="mock_news", published_at=None, sentiment_score=1.0, relevance_score=0.9)]
    with patch("app.services.news_sentiment.list_news_articles", return_value=rows):
        news = summarize_news_context(MagicMock(), "X", auto_seed=False)
    assert news["synthetic_only"] is True
    features = {"trend_label": "mixed"}
    assert _opportunity_score(None, news, features) == _opportunity_score(None, {"average_sentiment": 0}, features)


def test_twenty_day_return_available_with_21_rows():
    frame = _frame(np.linspace(100, 120, 21))
    assert _price_features(frame)["return_20d"] == pytest.approx(0.2)


# --------------------------------------------------------------------------
# Risk settings
# --------------------------------------------------------------------------

@pytest.mark.parametrize("updates", [{"min_confidence": float("nan")}, {"max_risk_per_trade": 5}, {"max_open_positions": True}, {"max_open_positions": 2.5}])
def test_risk_settings_reject_invalid_values(updates):
    rule = SimpleNamespace(id=1, value={})
    with patch("app.services.risk_settings.get_active_risk_rule", return_value=rule), \
         patch("app.services.risk_settings.write_audit_log"):
        with pytest.raises(ValueError):
            update_risk_settings(MagicMock(), updates)
    assert rule.value == {}


def test_risk_settings_accept_valid_values():
    rule = SimpleNamespace(id=1, value={})
    with patch("app.services.risk_settings.get_active_risk_rule", return_value=rule), \
         patch("app.services.risk_settings.write_audit_log"):
        update_risk_settings(MagicMock(), {"min_confidence": 0.6, "max_open_positions": 8})
    assert rule.value["min_confidence"] == 0.6 and rule.value["max_open_positions"] == 8
    assert rule.value["paper_only"] is True
