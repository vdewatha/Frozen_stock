"""Loss diagnosis: why each paper trade lost, and which failures repeat."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from app.models import IntradayBar
from app.services import loss_diagnosis as ld
from app.services import trade_attribution
from app.services.strategies.base import ExitPolicy
from app.services.strategies.registry import get_strategy
from app.services.strategies.technical import BACKTESTED_BRACKET_EXIT_POLICY
from tests.test_loss_feedback_loop import _book, db  # noqa: F401  (fixture)
from tests.test_paper_trade_scorecard import at

UTC = timezone.utc


def _loss(r=-0.03, **context):
    return ld.diagnose(gross_return=r, is_loss=True, **context)


@pytest.mark.parametrize("context, cause, family", [
    ({"benchmark_return": -0.03}, "missed_market_move", "missed"),
    ({"benchmark_return": -0.002, "sector_symbol": "XLK", "sector_return": -0.025}, "missed_sector_move", "missed"),
    ({"benchmark_return": 0.004, "sector_symbol": "XLK", "sector_return": 0.001}, "signal_wrong", "strategy"),
    ({"benchmark_return": -0.03, "earnings_in_hold": True}, "missed_earnings", "missed"),
    ({"benchmark_return": 0.0, "news_used": True, "news_sentiment": 0.6}, "news_wrong", "news"),
    ({"benchmark_return": 0.0, "news_used": True, "news_sentiment": -0.2}, "signal_wrong", "strategy"),
    ({}, "unknown_context", "unknown"),
])
def test_each_loss_gets_one_cause(context, cause, family):
    result = _loss(**context)
    assert result["cause"] == cause and result["family"] == family and result["lesson"]


def test_stock_falling_well_beyond_a_falling_market_is_the_signal():
    result = _loss(-0.06, benchmark_return=-0.02)
    assert result["cause"] == "signal_wrong" and result["excess_vs_market"] == pytest.approx(-0.04)
    assert "market_down" in result["contributing"]


def test_small_losses_are_noise_and_an_etf_is_not_explained_by_itself():
    assert _loss(-0.003, benchmark_return=-0.03)["cause"] == "noise"
    own = _loss(-0.03, benchmark_return=0.0, sector_symbol="SPY", sector_return=-0.03)
    assert own["cause"] == "signal_wrong" and own["sector_symbol"] is None


def test_wins_separate_edge_from_market_drift():
    drift = ld.diagnose(gross_return=0.02, is_loss=False, benchmark_return=0.03)
    edge = ld.diagnose(gross_return=0.03, is_loss=False, benchmark_return=0.005)
    assert drift["cause"] == "market_tailwind" and drift["family"] == "win"
    assert edge["cause"] == "edge"


def test_contributing_flags():
    result = _loss(benchmark_return=-0.03, concurrent_same_symbol=2, same_session_stops=7, exit_tested=False)
    assert result["contributing"] == ["crowded_symbol", "clustered_stops", "exit_untested", "market_down"]
    assert _loss(benchmark_return=0.0, same_session_stops=2, exit_tested=True)["contributing"] == []


def test_exit_tested_compares_the_entry_legs_with_the_declared_policy():
    policy = BACKTESTED_BRACKET_EXIT_POLICY
    assert ld.exit_tested(policy, entry_price=100, stop_price=97, take_profit_price=106) is True
    assert ld.exit_tested(policy, entry_price=100, stop_price=97, take_profit_price=103) is False
    assert ld.exit_tested(None, entry_price=100, stop_price=97, take_profit_price=103) is False
    assert ld.exit_tested(policy, entry_price=100, stop_price=None, take_profit_price=None) is None
    stop_only = ExitPolicy(stop_loss_pct=0.10, take_profit_pct=0.0)
    assert ld.exit_tested(stop_only, entry_price=100, stop_price=90, take_profit_price=None) is True


def test_news_is_read_from_signal_features():
    assert ld.news_from_features({"rsi": 4}) == (False, None)
    assert ld.news_from_features({"news_context": {"sentiment_score": 0.4}}) == (True, 0.4)
    assert ld.news_from_features({"sentiment": "n/a"}) == (True, None)


def test_earnings_between():
    start, end = datetime(2026, 10, 28, 15, tzinfo=UTC), datetime(2026, 11, 3, 15, tzinfo=UTC)
    events = pd.DataFrame({"reported_at": [pd.Timestamp("2026-10-30 16:05", tz="America/New_York")],
                           "surprise_pct": [3.0]})
    assert ld.earnings_between("AAPL", start, end, lambda s: events) is True
    assert ld.earnings_between("AAPL", end, end + timedelta(days=3), lambda s: events) is False
    assert ld.earnings_between("AAPL", start, end, lambda s: None) is None
    assert ld.earnings_between("AAPL", start, end, None) is None
    assert ld.earnings_between("SPY", start, end, None) is False  # funds have no earnings


def _detail(strategy, cause, day, symbol="AAPL"):
    family = ld.FAMILIES[cause]
    return {"strategy_type": strategy, "symbol": symbol, "closed_at": f"2026-11-{day:02d}T15:00:00+00:00",
            "diagnosis": {"cause": cause, "family": family, "contributing": ["clustered_stops"]}}


def test_repeating_failures_flag_a_cause_that_keeps_coming_back():
    rows = [_detail("mpl", "missed_market_move", d, s) for d, s in [(2, "AAPL"), (3, "ADBE"), (5, "META")]]
    rows += [_detail("mpl", "signal_wrong", 4), _detail("mpl", "edge", 6), _detail("mpl", "noise", 6)]
    rows += [_detail("bb", "missed_market_move", 5, "XLV"), _detail("bb", "edge", 5)]
    found = ld.repeating_failures(rows)
    keys = [(item["strategy_type"], item["cause"]) for item in found]
    assert keys == [("all", "missed_market_move"), ("mpl", "missed_market_move")]
    mpl = found[1]
    assert mpl["count"] == 3 and mpl["losses_in_window"] == 5 and mpl["share_of_losses"] == 0.6
    assert mpl["symbols"] == ["AAPL", "ADBE", "META"] and mpl["proposal"] and mpl["auto_apply"] is False
    assert mpl["contributing"] == {"clustered_stops": 5}
    # Two of a cause, or a cause that is a small share of the losses, is not yet a pattern.
    assert ld.repeating_failures(rows[:2]) == []


def test_cause_breakdown():
    rows = [_detail("mpl", "missed_market_move", 2), _detail("mpl", "edge", 3), {"diagnosis": None}]
    summary = ld.cause_breakdown(rows)
    assert summary["diagnosed"] == 2 and summary["by_family"] == {"missed": 1, "win": 1}


def _bar(db, symbol, moment, close):
    db.add(IntradayBar(symbol=symbol, timeframe="1m", opened_at=moment, open=close, high=close, low=close,
                       close=close, volume=1000, exchange_timestamp=moment))


def test_round_trip_attribution_records_the_diagnosis(db):  # noqa: F811
    account, _trend, _entry, _fill = _book(db)  # AAPL bought 14:01, stopped 18:00 at -3.7%
    for symbol, start, end in [("SPY", 700, 679), ("XLK", 300, 297)]:
        _bar(db, symbol, at(14, 1), start)
        _bar(db, symbol, at(18), end)
    db.flush()
    row = trade_attribution.attribute_round_trips(db, account_id=account.id, earnings_lookup=None)[0]
    diagnosis = row.detail["diagnosis"]
    assert diagnosis["cause"] == "missed_market_move"
    assert diagnosis["benchmark_return"] == pytest.approx(-0.03)
    assert diagnosis["sector_symbol"] == "XLK" and diagnosis["sector_return"] == pytest.approx(-0.01)
    assert diagnosis["earnings_in_hold"] is None and diagnosis["exit_tested"] is False  # trend_a declares no exits
    assert set(diagnosis["contributing"]) == {"exit_untested", "market_down"}

    summary = trade_attribution.attribution_summary(trade_attribution.current_attributions(db))
    assert summary["diagnosis"]["by_cause"] == {"missed_market_move": 1}

    coverage = trade_attribution.attribution_coverage(db, account_id=account.id)
    assert coverage == {"closed_round_trips": 1, "attributed": 1, "missing_entry_order_ids": []}


def test_an_earnings_report_inside_the_hold_is_the_cause(db):  # noqa: F811
    account, *_ = _book(db)
    events = pd.DataFrame({"reported_at": [pd.Timestamp(at(16))], "surprise_pct": [-4.0]})
    row = trade_attribution.attribute_round_trips(db, account_id=account.id, earnings_lookup=lambda s: events)[0]
    assert row.detail["diagnosis"]["cause"] == "missed_earnings"


def test_technical_strategies_trade_their_backtested_exits():
    for slug in ["moving_average_crossover", "rsi_mean_reversion", "macd_momentum", "ensemble",
                 "model_predictive_long", "bollinger_mean_reversion", "channel_breakout", "trend_pullback"]:
        policy = get_strategy(slug).exit_policy()
        assert (policy.stop_loss_pct, policy.take_profit_pct, policy.max_holding_sessions) == (0.03, 0.06, 20), slug
        assert policy.sell_signal_exit is False
    # Short-term reversion keeps its own tested exits and never emitted SELLs anyway.
    assert get_strategy("rsi2_pullback").exit_policy().sell_signal_exit is True
    # A row override still applies and keeps the flag.
    custom = get_strategy("macd_momentum", {"take_profit_pct": 0.08}).exit_policy()
    assert custom.take_profit_pct == 0.08 and custom.sell_signal_exit is False


def test_intraday_attribution_job_is_scheduled_on_the_fast_learning_queue():
    from app.tasks.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["trade-attribution"]
    assert entry["task"] == "app.tasks.jobs.trade_attribution_job"
    assert celery_app.conf.task_routes["app.tasks.jobs.trade_attribution_job"] == {"queue": "learning_fast"}
