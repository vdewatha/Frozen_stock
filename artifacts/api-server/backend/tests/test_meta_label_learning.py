"""Meta-label self-learning: RSI(2)/IBS labels, paper outcomes, expectancy threshold, off-hours retrain."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.core.config import Settings, settings
from app.services import meta_label, meta_label_learning
from app.services.meta_label_primary import (
    CONSENSUS_STRATEGIES,
    REPLAYABLE_STRATEGIES,
    replay_exit_signals,
    replay_signals,
)
from app.services.strategies.registry import get_strategy
from app.services.triple_barrier import BarrierConfig, label_events

from tests.test_loss_feedback_loop import _attribution, db  # noqa: F401  (fixture)
from tests.test_meta_label import _walk

UTC = timezone.utc


def test_short_term_reversion_strategies_are_replayed_with_their_exits():
    assert {"rsi2_pullback", "ibs_mean_reversion"} <= set(REPLAYABLE_STRATEGIES)
    assert "rsi2_pullback" not in CONSENSUS_STRATEGIES
    prices = _walk(rows=420, seed=21)
    for slug in ("rsi2_pullback", "ibs_mean_reversion"):
        strategy = get_strategy(slug)
        exits = replay_exit_signals(prices, slug)
        for t in range(250, 420, 7):
            reason = strategy.exit_signal("SPY", prices.iloc[:t + 1], 0)
            assert bool(reason) == bool(exits[t]), (slug, t)
    assert replay_exit_signals(prices, "macd_momentum") is None


def _flat(rows=40, price=100.0):
    dates = pd.bdate_range("2020-01-02", periods=rows).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "open": price, "high": price * 1.001, "low": price * 0.999,
                         "close": price, "volume": 1e6})


def test_indicator_exit_fills_at_the_next_open_and_zero_target_disables_the_upper_barrier():
    prices = _flat()
    prices.loc[25:, ["open", "high", "low", "close"]] = 130.0  # +30% would hit any target
    config = BarrierConfig(take_profit_pct=0.0, stop_loss_pct=0.10, max_holding_days=10, cost_model="flat",
                           slippage_bps=0.0, fees_bps=0.0)
    no_exit = label_events(prices, [20], config)
    assert no_exit["barrier"].iloc[0] == "vertical" and no_exit["exit_idx"].iloc[0] == 31
    signals = np.zeros(len(prices), dtype=bool)
    signals[23] = True  # decided on the close of bar 23
    labelled = label_events(prices, [20], config, exit_signals=signals)
    assert labelled["barrier"].iloc[0] == "indicator" and labelled["exit_idx"].iloc[0] == 24
    with pytest.raises(ValueError):
        label_events(prices, [20], config, exit_signals=signals[:-1])


def test_reversion_events_use_the_paper_exit_policy():
    prices = {"SPY": _walk(rows=900, seed=31), "QQQ": _walk(rows=900, seed=32)}
    events = meta_label.build_events(prices, prices["SPY"])
    reversion = events.loc[events["strategy"].isin(["rsi2_pullback", "ibs_mean_reversion"])]
    assert len(reversion) > 20
    assert set(reversion["barrier"]) <= {"indicator", "vertical", "stop_loss", "stop_loss_gap"}
    assert (reversion["stop_loss_pct"] == 0.10).all() and (reversion["holding_days"] <= 10).all()
    other = events.loc[events["strategy"] == "macd_momentum"]
    assert (other["take_profit_pct"] == 0.03).all()
    assert (events["weight"] == events["uniqueness"]).all() and set(events["source"]) == {"replay"}


def test_default_threshold_keeps_every_trade_that_adds_profit():
    # Owner goal since 2026-11-06: profit after costs. No win-rate floor; the
    # cutoff maximises total net return on the validation fold.
    assert Settings().meta_label_min_win_rate == 0.0 and Settings().meta_label_objective == "total"
    assert meta_label.ThresholdPolicy().min_win_rate == meta_label.DEFAULT_MIN_WIN_RATE == 0.0
    rng = np.random.default_rng(3)
    probability = rng.uniform(size=4000)
    # Higher probability -> more wins, but nothing approaches 80%.
    win = rng.uniform(size=4000) < 0.4 + 0.3 * probability
    net = np.where(win, 0.012, -0.01)
    old = meta_label.select_threshold(probability, net, meta_label.ThresholdPolicy(min_win_rate=0.80))
    new = meta_label.select_threshold(probability, net)
    assert old["threshold"] is None
    assert new["threshold"] is not None and new["validation_expectancy"] > 0
    total = meta_label.select_threshold(probability, net, meta_label.ThresholdPolicy(objective="total"))
    # Maximising the total keeps more trades than maximising the average.
    assert total["validation_trades"] > new["validation_trades"]
    assert total["validation_trades"] * total["validation_expectancy"] >= \
        new["validation_trades"] * new["validation_expectancy"]


def _outcome(symbol, strategy, opened, closed, net, **extra):
    return {"symbol": symbol, "strategy": strategy, "opened_at": opened, "closed_at": closed, "net_return": net} | extra


def test_paper_outcomes_replace_replayed_labels_and_carry_extra_weight():
    prices = {"SPY": _walk(rows=900, seed=41), "QQQ": _walk(rows=900, seed=42)}
    events = meta_label.build_events(prices, prices["SPY"])
    replayed = events.loc[(events["symbol"] == "QQQ") & (events["strategy"] == "ibs_mean_reversion")].iloc[5]
    decision = pd.Timestamp(replayed["t0"])
    entry = prices["QQQ"]["date"].iloc[int(replayed["event_idx"]) + 1]
    flipped = -0.02 if replayed["label"] == 1 else 0.02
    spy_actions = replay_signals(prices["SPY"])["macd_momentum"]["action"].to_numpy()
    quiet = int(next(t for t in range(400, 800) if spy_actions[t] != "BUY"))
    outcomes = [
        _outcome("QQQ", "ibs_mean_reversion", f"{entry}T14:30:00+00:00", f"{entry}T20:00:00+00:00", flipped,
                 confidence=0.7, probability_up=0.6),
        # A decision the replay never made (e.g. different live parameters) is appended.
        _outcome("SPY", "macd_momentum", f"{prices['SPY']['date'].iloc[quiet + 1]}T15:00:00Z",
                 f"{prices['SPY']['date'].iloc[quiet + 4]}T15:00:00Z", 0.01),
        _outcome("QQQ", "model_predictive_long", "2014-06-02T15:00:00Z", "2014-06-05T15:00:00Z", 0.01),
        _outcome("ZZZ", "macd_momentum", "2014-06-02T15:00:00Z", "2014-06-05T15:00:00Z", 0.01),
        _outcome("QQQ", "macd_momentum", "2030-01-02T15:00:00Z", "2030-01-05T15:00:00Z", 0.01),
    ]
    merged, summary = meta_label.merge_paper_outcomes(events, outcomes, prices, prices["SPY"], weight=4.0)
    assert summary["paper_outcomes"] == 2 and summary["replaced"] == 1 and summary["appended"] == 1
    assert summary["skipped"] == {"unsupported_strategy": 1, "no_price_history": 1, "outside_price_history": 1}
    assert len(merged) == len(events) + 1
    paper = merged.loc[merged["source"] == "paper"]
    row = paper.loc[paper["symbol"] == "QQQ"].iloc[0]
    assert pd.Timestamp(row["t0"]) == decision and row["label"] == int(flipped > 0)
    assert row["weight"] == pytest.approx(4.0 * replayed["uniqueness"])
    assert row["primary_confidence"] == 0.7
    # Decision-time features are the replay's (the same causal frame).
    for name in ("rsi_2", "ibs", "dist_sma_200", "spy_vol_20"):
        assert row[name] == pytest.approx(replayed[name])
    assert merged[meta_label.feature_names()].notna().all(axis=None)


@pytest.fixture()
def history():
    frames = {"SPY": _walk(rows=1500, seed=51, start="2011-01-03"),
              "QQQ": _walk(rows=1500, seed=52, start="2011-01-03"),
              "IWM": _walk(rows=1500, seed=53, start="2011-01-03")}
    return lambda symbol: frames[symbol].copy()


def _retrain_settings(tmp_path):
    return [patch.object(settings, "paper_meta_label_model_path", str(tmp_path / "meta.joblib")),
            patch.object(settings, "paper_execution_symbols", ["QQQ", "IWM"]),
            patch.object(settings, "meta_label_history_start", "2011-01-01"),
            patch.object(settings, "meta_label_retrain_min_new_outcomes", 2)]


def test_retrain_promotes_a_candidate_and_is_not_due_again_until_new_trades_close(db, tmp_path, history):  # noqa: F811
    patches = _retrain_settings(tmp_path)
    for item in patches:
        item.start()
    try:
        now = datetime(2017, 1, 10, 23, 0, tzinfo=UTC)
        opened = datetime(2016, 6, 15, 14, 30, tzinfo=UTC)
        _attribution(db, strategy_type="ibs_mean_reversion", symbol="QQQ", net_return=0.01,
                     closed_at=opened + timedelta(days=2))
        result = meta_label_learning.retrain_meta_label(db, now=now, loader=history)
        assert result["status"] == "promoted", result
        assert result["paper"]["paper_outcomes"] == 1
        manifest = json.loads((tmp_path / "meta.joblib.json").read_text())
        assert manifest["trained_at"] == now.isoformat()
        assert manifest["policy"]["min_win_rate"] == 0.0 and manifest["policy"]["objective"] == "total"
        assert set(manifest["strategy_barriers"]) == {"rsi2_pullback", "ibs_mean_reversion"}
        bundle = meta_label.load_model(tmp_path / "meta.joblib")
        assert bundle["sha256"] == result["sha256"]

        assert meta_label_learning.retrain_meta_label(db, now=now + timedelta(days=1), loader=history)["status"] \
            == "not_due"
        for _ in range(2):
            _attribution(db, strategy_type="rsi2_pullback", symbol="IWM", net_return=-0.01,
                         closed_at=now + timedelta(days=1, hours=1))
        due, reason = meta_label_learning.retrain_due(db, now=now + timedelta(days=2))
        assert due and "2 paper trades" in reason
        assert meta_label_learning.retrain_due(db, now=now + timedelta(days=8))[0]
    finally:
        for item in patches:
            item.stop()


def test_an_infeasible_candidate_never_replaces_a_feasible_incumbent(db, tmp_path, history):  # noqa: F811
    patches = _retrain_settings(tmp_path)
    for item in patches:
        item.start()
    try:
        path = tmp_path / "meta.joblib"
        incumbent = meta_label.save_model({"format": meta_label.MODEL_FORMAT, "threshold": 0.6, "model": None,
                                           "calibrator": None, "feature_names": [], "strategies": []}, path)
        with patch.object(settings, "meta_label_min_win_rate", 0.99):
            result = meta_label_learning.retrain_meta_label(db, now=datetime(2017, 1, 10, 23, tzinfo=UTC),
                                                            force=True, loader=history)
        assert result["status"] == "kept_incumbent" and result["threshold"] is None
        assert json.loads((tmp_path / "meta.joblib.json").read_text())["sha256"] == incumbent["sha256"]
        assert not (tmp_path / "meta.joblib.prev").exists()
    finally:
        for item in patches:
            item.stop()


def test_retrain_is_blocked_without_spy_and_can_be_disabled(db, tmp_path):  # noqa: F811
    patches = _retrain_settings(tmp_path)
    for item in patches:
        item.start()
    try:
        def no_data(symbol):
            raise OSError("offline")

        result = meta_label_learning.retrain_meta_label(db, loader=no_data)
        assert result["status"] == "blocked" and "SPY" in result["missing_symbols"]
        with patch.object(settings, "meta_label_retrain_enabled", False):
            assert meta_label_learning.retrain_meta_label(db)["status"] == "disabled"
    finally:
        for item in patches:
            item.stop()


def test_retrain_job_is_scheduled_off_hours_on_the_learning_queue():
    from app.tasks import jobs
    from app.tasks.celery_app import celery_app

    entry = celery_app.conf.beat_schedule.get("meta-label-retrain")
    assert entry is not None and entry["task"] == "app.tasks.jobs.meta_label_retrain_job"
    assert celery_app.conf.task_routes["app.tasks.jobs.meta_label_retrain_job"] == {"queue": "learning"}
    session = (datetime(2026, 11, 2, 14, 30, tzinfo=UTC), datetime(2026, 11, 2, 21, 0, tzinfo=UTC))
    with patch("app.services.intraday_data.session_bounds", return_value=session), \
            patch.object(jobs, "datetime") as clock:
        clock.now.return_value = datetime(2026, 11, 2, 16, 0, tzinfo=UTC)
        assert jobs.meta_label_retrain_job()["status"] == "skipped"
