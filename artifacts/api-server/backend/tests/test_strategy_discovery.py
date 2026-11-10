"""Strategy discovery: rule language, vectorised evaluation, fixed gate, paper admission."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import AuditLog, Strategy
from app.services import strategy_discovery as sd
from app.services.strategies import rules
from app.services.strategies.registry import get_strategy, is_known_strategy
from app.services.strategies.rules import RuleStrategy

from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)
from tests.test_meta_label import _walk

UTC = timezone.utc
RULES = [
    {"entry": [{"f": "dist_sma_200", "op": ">", "v": 0}, {"f": "rsi_2", "op": "<", "v": 15}],
     "exit": [{"f": "dist_sma_5", "op": ">", "v": 0}]},
    {"entry": [{"f": "down_streak", "op": ">=", "v": 3}], "exit": [{"f": "ret_1", "op": ">", "v": 0}]},
    {"entry": [{"f": "tdays_left", "op": "==", "v": 2}], "exit": [{"f": "tdom", "op": "==", "v": 3}]},
    {"entry": [{"f": "bb_pctb_20", "op": "<", "v": 0.1}, {"f": "vol_ratio_20_100", "op": "<", "v": 1.2}],
     "exit": [{"f": "bb_pctb_20", "op": ">", "v": 0.5}]},
    {"entry": [{"f": "gap", "op": "<", "v": -0.002}, {"f": "ibs", "op": ">", "v": 0.4}],
     "exit": [{"f": "at_high_10", "op": "==", "v": 1}]},
    {"entry": [{"f": "at_low_10", "op": "==", "v": 1}, {"f": "atr_pct_14", "op": ">", "v": 0.0}],
     "exit": [{"f": "ret_5", "op": ">", "v": 0.01}]},
]


@pytest.mark.parametrize("rule", RULES)
def test_rule_strategy_matches_the_vectorised_rule_on_every_prefix(rule):
    prices = _walk(rows=520, seed=17)
    strategy = get_strategy("rule_abc", {"rule": rule})
    entries, exits = rules.entry_signals(prices, rule), rules.exit_signals(prices, rule)
    assert entries.any()
    for t in range(260, 520):
        prefix = prices.iloc[:t + 1]
        assert (strategy.generate_signal("SPY", prefix).action == "BUY") == entries[t], t
        assert bool(strategy.exit_signal("SPY", prefix, 0)) == exits[t], t
    # The paper path's 260-bar frame gives the same decision as the full history.
    assert (strategy.generate_signal("SPY", prices.iloc[-260:]).action == "BUY") == entries[-1]


def test_features_are_causal():
    prices = _walk(rows=600, seed=4)
    names = ["ret_5", "rsi_2", "dist_sma_200", "at_low_10", "at_high_252", "down_streak", "up_streak", "ibs", "gap",
             "bb_pctb_20", "atr_pct_14", "vol_ratio_20_100", "tdom", "tdays_left", "dow"]
    noisy = prices.copy()
    noisy.loc[401:, ["open", "high", "low", "close"]] *= 1.7
    for name in names:
        np.testing.assert_allclose(rules.feature(prices, name).to_numpy()[:401],
                                   rules.feature(noisy, name).to_numpy()[:401], equal_nan=True, err_msg=name)


def test_calendar_features():
    frame = pd.DataFrame({"date": ["2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"],
                          "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0})
    calendar = rules._calendar(frame)
    # November 2026 has 21 weekdays; Nov 2 is the first.
    assert calendar["tdays_left"].tolist() == [2, 1, 0, 20, 19]
    assert calendar["tdom"].tolist() == [20, 21, 22, 1, 2]
    assert calendar["dow"].tolist() == [2, 3, 4, 0, 1]


def test_invalid_rules_are_rejected_and_rule_slugs_resolve():
    for bad in ({}, {"entry": []}, {"entry": [{"f": "nope_3", "op": "<", "v": 1}]},
                {"entry": [{"f": "rsi_2", "op": "!=", "v": 1}]}, {"entry": [{"f": "rsi_2", "op": "<", "v": "x"}]},
                {"entry": [{"f": "dist_sma_400", "op": ">", "v": 0}]}):
        with pytest.raises(ValueError):
            rules.validate_rule(bad)
    strategy = get_strategy("rule_x1", {"rule": RULES[0], "stop_loss_pct": 0.07, "max_holding_sessions": 5,
                                        "symbol_scope": ["SPY", "QQQ"]})
    assert isinstance(strategy, RuleStrategy) and strategy.slug == "rule_x1" and strategy.daily_bar_execution
    policy = strategy.exit_policy()
    assert (policy.stop_loss_pct, policy.take_profit_pct, policy.max_holding_sessions, policy.indicator_exit) == \
        (0.07, 0.0, 5, True)
    assert strategy.symbol_scope() == frozenset({"SPY", "QQQ"})
    assert is_known_strategy("rule_x1") and is_known_strategy("rsi2_pullback") and not is_known_strategy("nope")
    with pytest.raises(ValueError):
        get_strategy("rule_x2", {})


def test_library_is_deterministic_valid_and_split_by_universe():
    symbols = ["SPY", "QQQ", "XLK", "XLF", "AAPL", "MSFT"]
    library = sd.candidate_library(symbols)
    assert library == sd.candidate_library(symbols)
    assert len({row["id"] for row in library}) == len(library)
    assert {row["universe"] for row in library} == {"index_etfs", "sector_etfs", "single_stocks"}
    assert {row["family"] for row in library} >= {"pullback_in_uptrend", "turn_of_month", "breakout_momentum"}
    by_universe = {row["universe"]: row["symbols"] for row in library}
    assert by_universe == {"index_etfs": ["SPY", "QQQ"], "sector_etfs": ["XLK", "XLF"],
                           "single_stocks": ["AAPL", "MSFT"]}
    for row in library:
        assert rules.rule_lookback(row["rule"]) <= rules.MAX_RULE_LOOKBACK + 1
        assert 0 < row["stop_loss_pct"] < 1 and row["max_holding_sessions"] >= 1


def test_marked_returns_compound_to_the_trade_return():
    close = np.array([100.0, 102.0, 101.0, 105.0, 104.0, 104.0])
    labels = pd.DataFrame({"entry_idx": [1, 5], "exit_idx": [4, 5], "entry_price": [101.0, 104.5],
                           "exit_price": [103.0, 103.9]})
    marked = sd._marked_returns(labels, close)
    assert np.prod(1 + marked[1:5]) - 1 == pytest.approx(103.0 / 101.0 - 1)
    assert marked[5] == pytest.approx(103.9 / 104.5 - 1)
    assert marked[0] == 0


def test_evaluation_trades_without_overlap_and_splits_the_holdout():
    frames = {"SPY": _walk(rows=1600, seed=21, start="2012-01-02"),
              "QQQ": _walk(rows=1600, seed=22, start="2012-01-02")}
    candidate = {"id": "x", "family": "down_streak", "universe": "index_etfs", "symbols": ["SPY", "QQQ", "DIA"],
                 "rule": RULES[1], "stop_loss_pct": 0.10, "take_profit_pct": 0.0, "max_holding_sessions": 10}
    evaluation = sd.evaluate_candidate(candidate, frames)
    trades = evaluation["trades"]
    assert len(trades) > 50 and set(trades["symbol"]) == {"SPY", "QQQ"}
    for _symbol, group in trades.groupby("symbol"):
        group = group.sort_values("entry_idx")
        assert (group["entry_idx"].to_numpy()[1:] > group["exit_idx"].to_numpy()[:-1]).all()
    summary = sd.summarize_candidate(candidate, evaluation, pd.Timestamp("2016-06-01"))
    holdout = (pd.to_datetime(trades["t0"]) >= "2016-06-01").sum()
    assert summary["holdout"]["trades"] == holdout
    assert summary["discovery"]["net"]["trades"] == len(trades) - holdout
    assert summary["observations"] > 0


def _summary(**overrides):
    base = {"discovery": {"net": {"trades": 400, "expectancy_bps": 30.0, "expectancy_ci_bps": [5.0, 55.0]},
                          "excess_over_drift": {"trades": 400, "expectancy_bps": 20.0, "expectancy_ci_bps": [2.0, 40.0]}},
            "post_period": {"trades": 120, "expectancy_bps": 25.0}, "holdout": {"trades": 40, "expectancy_bps": 10.0},
            "sharpe_per_period": 0.05, "observations": 5000, "skewness": 0.0, "kurtosis": 3.0}
    for key, value in overrides.items():
        base[key] = value
    return base


def test_gate_tiers():
    strong = sd.gate(_summary(sharpe_per_period=0.12), trials=50, trial_sharpes=[0.0, 0.01, -0.01, 0.02])
    assert strong["tier"] == "proven" and strong["passed"] and all(strong["checks"].values())
    lucky = sd.gate(_summary(sharpe_per_period=0.02), trials=300, trial_sharpes=list(np.linspace(-0.05, 0.08, 300)))
    assert lucky["tier"] == "explore" and not lucky["checks"]["deflated_sharpe"]
    drift_only = _summary()
    drift_only["discovery"]["excess_over_drift"]["expectancy_ci_bps"] = [-5.0, 40.0]
    assert sd.gate(drift_only, trials=1, trial_sharpes=[0.05])["tier"] == "rejected"
    bad_holdout = sd.gate(_summary(holdout={"trades": 40, "expectancy_bps": -3.0}), trials=1, trial_sharpes=[0.05])
    assert bad_holdout["tier"] == "rejected" and not bad_holdout["checks"]["holdout_positive"]


def _row(cid, tier, family="down_streak"):
    return {"id": cid, "family": family, "universe": "index_etfs", "rule": RULES[1],
            "description": rules.describe_rule(RULES[1]), "stop_loss_pct": 0.1, "take_profit_pct": 0.0,
            "max_holding_sessions": 10, "symbols": ["SPY", "QQQ"],
            "gate": {"tier": tier, "passed": tier == "proven", "checks": {"deflated_sharpe": tier == "proven"},
                     "deflated_sharpe": 0.1},
            "discovery": {"net": {"trades": 300}}, "post_period": {"expectancy_bps": 10.0},
            "holdout": {"trades": 30, "win_rate": 0.6, "expectancy_bps": 12.0}}


def test_admission_respects_tiers_caps_and_retirement(db):  # noqa: F811
    now = datetime(2026, 11, 8, 15, tzinfo=UTC)
    result = {"candidates": [_row("a1", "proven"), _row("b2", "explore"), _row("c3", "explore"),
                             _row("d4", "rejected")]}
    strict = sd.admit_survivors(db, result, now=now, require_proven=True)
    assert strict["admitted"] == ["rule_a1"]
    loose = sd.admit_survivors(db, result, now=now, max_new=1)
    assert loose["admitted"] == ["rule_b2"] and loose["refreshed"] == ["rule_a1"]
    rows = {row.strategy_type: row for row in db.scalars(select(Strategy)).all()}
    assert rows["rule_a1"].current_status == "paper_trading_candidate"
    assert rows["rule_a1"].parameters["symbol_scope"] == ["SPY", "QQQ"]
    assert rows["rule_a1"].parameters["discovery"]["gate"]["tier"] == "proven"
    assert isinstance(get_strategy("rule_a1", rows["rule_a1"].parameters), RuleStrategy)

    # b2 was promoted by the forward-trial loop; a1 is still a candidate.  Both now fail.
    rows["rule_b2"].current_status = "paper_trading_active"
    later = {"candidates": [_row("a1", "rejected"), _row("b2", "rejected"), _row("c3", "explore")]}
    outcome = sd.admit_survivors(db, later, now=now, max_live=2)
    assert outcome["retired"] == ["rule_a1"] and outcome["admitted"] == ["rule_c3"]
    assert rows["rule_a1"].current_status == "retired"
    assert rows["rule_b2"].current_status == "paper_trading_active"
    actions = [row.action for row in db.scalars(select(AuditLog).where(AuditLog.event_type == "strategy_discovery"))]
    assert actions.count("admit") == 3 and actions.count("retire") == 1


def test_scheduled_run_admits_and_audits(db):  # noqa: F811
    frames = {s: _walk(rows=1800, seed=30 + i, start="2014-01-02") for i, s in enumerate(["SPY", "QQQ", "XLK"])}
    with patch.object(settings, "paper_execution_symbols", ["SPY", "QQQ", "XLK"]), \
            patch.object(settings, "strategy_discovery_history_start", "2014-01-01"), \
            patch.object(settings, "strategy_discovery_mutation_enabled", True):  # paused by default since 2026-11-10
        result = sd.run_strategy_discovery(db, now=datetime(2026, 11, 8, 15, tzinfo=UTC),
                                           loader=lambda symbol: frames[symbol].copy())
        assert result["status"] == "complete" and result["library"] == len(sd.candidate_library(frames))
        assert result["trials"] >= result["library"] and result["mutation"]["status"] == "complete"
        assert len(result["top"]) == 10 and result["missing_symbols"] == []
        audit = db.scalars(select(AuditLog).where(AuditLog.event_type == "strategy_discovery",
                                                  AuditLog.action == "run")).one()
        assert "candidates" in audit.message
        with patch.object(settings, "strategy_discovery_enabled", False):
            assert sd.run_strategy_discovery(db)["status"] == "disabled"


def test_discovery_job_is_scheduled_weekly_on_the_learning_queue():
    from app.tasks.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["strategy-discovery"]
    assert entry["task"] == "app.tasks.jobs.strategy_discovery_job"
    assert celery_app.conf.task_routes["app.tasks.jobs.strategy_discovery_job"] == {"queue": "learning"}


def test_a_discovered_rule_runs_through_the_paper_signal_batch_inside_its_scope(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.base import Base
    from app.models import StrategySignal
    from app.services import stock_paper_signal_batch as batch
    from tests.test_stock_paper_signal_batch import _DataDouble

    always = {"entry": [{"f": "ret_1", "op": ">", "v": -1}], "exit": [{"f": "ret_1", "op": ">", "v": 5}]}
    engine = create_engine(f"sqlite:///{tmp_path / 'batch.sqlite'}")
    Base.metadata.create_all(engine)
    batch.clear_signal_cache()
    with Session(engine) as session:
        session.add(Strategy(name="Discovered test", strategy_type="rule_t1", is_active=True,
                             current_status="paper_trading_candidate",
                             parameters={"rule": always, "symbol_scope": ["SPY"], "stop_loss_pct": 0.1,
                                         "take_profit_pct": 0, "max_holding_sessions": 10}))
        session.commit()
        data = _DataDouble(["AAPL", "SPY"])
        stack = data.patches()
        for item in stack:
            item.start()
        try:
            outcomes, _stats = batch.generate_cycle_signals(session, ["AAPL", "SPY"],
                                                            session.query(Strategy).all())
        finally:
            for item in stack:
                item.stop()
            batch.clear_signal_cache()
        assert ("AAPL", "rule_t1") not in outcomes
        produced = outcomes[("SPY", "rule_t1")]
        assert isinstance(produced, dict), produced
        signal = session.query(StrategySignal).one()
        assert signal.symbol == "SPY" and signal.features["research_only"] is True
        # The rule fires; outside the daily-bar entry window the paper path withholds the BUY.
        assert "Rule rule_t1: BUY when ret_1 > -1" in signal.reason
        assert signal.action == "BUY" or (signal.action == "HOLD" and "BUY withheld" in signal.reason)
        assert signal.features["rule"] == rules.describe_rule(always)
    engine.dispose()
