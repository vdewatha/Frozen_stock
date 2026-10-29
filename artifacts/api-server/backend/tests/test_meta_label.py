"""Meta-label filter: replay parity, causal features, purged CV, thresholds, calibration, persistence."""
import json

import numpy as np
import pandas as pd
import pytest

from app.services import meta_label
from app.services.meta_label_primary import REPLAYABLE_STRATEGIES, replay_signals
from app.services.strategies.registry import get_strategy
from app.services.triple_barrier import BarrierConfig


def _walk(rows=900, seed=1, start="2012-01-02"):
    rng = np.random.default_rng(seed)
    close = 50 * np.exp(np.cumsum(rng.normal(0.0004, 0.016, rows)))
    open_ = close * (1 + rng.normal(0, 0.004, rows))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.007, rows)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.007, rows)))
    volume = rng.integers(1_000_000, 4_000_000, rows).astype(float)
    return pd.DataFrame({"date": pd.bdate_range(start, periods=rows).strftime("%Y-%m-%d"),
                         "open": open_, "high": high, "low": low, "close": close, "volume": volume})


def test_vectorised_replay_matches_strategy_classes_on_prefixes():
    prices = _walk(rows=420, seed=5)
    replay = replay_signals(prices)
    rng = np.random.default_rng(0)
    for slug in REPLAYABLE_STRATEGIES:
        strategy = get_strategy(slug)
        buys = np.flatnonzero(replay[slug]["action"].to_numpy() == "BUY")
        for t in [*rng.integers(0, len(prices), 25), *buys[:10]]:
            generated = strategy.generate_signal("TEST", prices.iloc[:t + 1])
            row = replay[slug].iloc[t]
            assert generated.action == row.action, (slug, t)
            assert generated.confidence == pytest.approx(row.confidence, abs=1e-9), (slug, t)
            assert generated.probability_up == pytest.approx(row.probability_up, abs=1e-9), (slug, t)


def test_market_features_are_causal():
    prices, spy = _walk(seed=2), _walk(seed=3)
    full = meta_label.market_features(prices, spy)
    cut = 600
    truncated = meta_label.market_features(prices.iloc[:cut + 1], spy.iloc[:cut + 1])
    columns = list(meta_label.MARKET_FEATURES)
    pd.testing.assert_frame_equal(full.loc[:cut, columns], truncated.loc[:cut, columns])
    # Garbage after the cut (symbol and SPY) cannot leak backwards.
    noisy, noisy_spy = prices.copy(), spy.copy()
    for frame in (noisy, noisy_spy):
        for column in ("open", "high", "low", "close"):
            frame.loc[cut + 1:, column] *= 3.0
    perturbed = meta_label.market_features(noisy, noisy_spy)
    pd.testing.assert_frame_equal(full.loc[:cut, columns], perturbed.loc[:cut, columns])


def test_feature_lookback_fits_the_paper_frame():
    prices, spy = _walk(seed=4), _walk(seed=6)
    full = meta_label.market_features(prices, spy)
    window = meta_label.latest_market_features(prices.iloc[-260:], spy.iloc[-260:])
    columns = [c for c in meta_label.MARKET_FEATURES if c != "buy_consensus"]
    np.testing.assert_allclose(window[columns].to_numpy(float), full[columns].iloc[[-1]].to_numpy(float), rtol=1e-9)


def test_build_events_labels_and_features_ignore_data_after_the_label_end():
    prices = {"AAA": _walk(seed=7), "SPY": _walk(seed=8)}
    events = meta_label.build_events(prices, prices["SPY"])
    assert len(events) > 200
    assert set(events["strategy"]) <= set(REPLAYABLE_STRATEGIES)
    assert (events["t1"] > events["t0"]).all()
    assert events["uniqueness"].between(0, 1, inclusive="right").all()
    cut_date = pd.Timestamp(prices["AAA"]["date"].iloc[650])
    perturbed = {key: frame.copy() for key, frame in prices.items()}
    for frame in perturbed.values():
        mask = pd.to_datetime(frame["date"]) > cut_date
        for column in ("open", "high", "low", "close"):
            frame.loc[mask, column] *= np.linspace(0.6, 1.4, mask.sum())
    after = meta_label.build_events(perturbed, perturbed["SPY"])
    columns = [*meta_label.feature_names(), "label", "net_return", "t0", "t1", "symbol", "strategy"]
    early = events.loc[events["t1"] <= cut_date, columns].reset_index(drop=True)
    early_after = after.loc[after["t1"] <= cut_date, columns].reset_index(drop=True)
    pd.testing.assert_frame_equal(early, early_after)


def _synthetic_events(n_years=8, per_day=6, seed=0):
    """Events with a learnable feature; labels end 1-15 days after t0."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2010-01-04", periods=252 * n_years)
    t0 = np.repeat(days, per_day)
    names = meta_label.feature_names()
    x = pd.DataFrame(rng.normal(size=(len(t0), len(names))), columns=names)
    signal = x["ret_5"].to_numpy()
    label = (rng.uniform(size=len(t0)) < 1 / (1 + np.exp(-1.5 * signal))).astype(int)
    net = np.where(label == 1, rng.uniform(0.005, 0.03, len(t0)), -rng.uniform(0.005, 0.03, len(t0)))
    frame = x.assign(t0=t0, t1=t0 + pd.to_timedelta(rng.integers(1, 16, len(t0)), unit="D"), label=label,
                     net_return=net, uniqueness=1.0, symbol="AAA", strategy="macd_momentum")
    frame["event_idx"] = np.arange(len(frame))
    frame["entry_idx"] = frame["event_idx"] + 1
    frame["exit_idx"] = frame["event_idx"] + 1
    return frame


def test_three_way_split_is_purged_and_embargoed():
    events = _synthetic_events()
    test_start = pd.Timestamp("2016-01-01")
    split = meta_label.three_way_split(events, test_start, embargo_days=10)
    embargo = pd.Timedelta(days=10)
    assert split["training"]["t1"].max() < split["calibration_start"] - embargo
    assert split["calibration"]["t1"].max() < split["validation_start"] - embargo
    assert split["validation"]["t1"].max() < test_start - embargo
    assert split["training"]["t0"].max() < split["calibration"]["t0"].min()
    assert split["calibration"]["t0"].max() < split["validation"]["t0"].min()
    # Events whose labels straddle a boundary are purged, not moved.
    straddling = events.loc[(events["t0"] < split["calibration_start"]) & (events["t1"] >= split["calibration_start"])]
    assert not set(straddling.index) & set(split["training"].index)
    assert not set(straddling.index) & set(split["calibration"].index)


def test_walk_forward_never_trains_on_labels_that_end_in_later_partitions():
    events = _synthetic_events()
    config = meta_label.WalkForwardConfig(first_test_year=2014, model_kind="logistic_regression",
                                          policy=meta_label.ThresholdPolicy(min_win_rate=0.6, min_trades=30))
    result = meta_label.walk_forward(events, config)
    assert [row["test_year"] for row in result["audit"]] == list(range(2014, 2018))
    for row in result["audit"]:
        year = row["test_year"]
        assert pd.Timestamp(row["training_last_label_end"]) < pd.Timestamp(year - 2, 1, 1)
        assert pd.Timestamp(row["calibration_last_label_end"]) < pd.Timestamp(year - 1, 1, 1)
        assert pd.Timestamp(row["validation_last_label_end"]) < pd.Timestamp(year, 1, 1)
    predictions = result["predictions"]
    assert set(predictions["fold"]) == {2014, 2015, 2016, 2017}
    assert (predictions["t0"].dt.year == predictions["fold"]).all()
    kept = meta_label.summarize(predictions)["events"]
    assert kept["trades"] > 0 and kept["win_rate"] > predictions["label"].mean()


def test_select_threshold_maximises_expectancy_subject_to_win_rate_and_count():
    rng = np.random.default_rng(1)
    probability = np.linspace(0, 1, 1000)
    win = rng.uniform(size=1000) < probability
    net = np.where(win, 0.02, -0.02)
    policy = meta_label.ThresholdPolicy(min_win_rate=0.8, min_trades=50)
    selection = meta_label.select_threshold(probability, net, policy)
    assert selection["feasible"]
    kept = net[probability >= selection["threshold"]]
    assert len(kept) >= 50 and (kept > 0).mean() >= 0.8 and kept.mean() > 0
    assert selection["validation_trades"] == len(kept)
    # "total" keeps at least as many trades as "expectancy".
    total = meta_label.select_threshold(probability, net, meta_label.ThresholdPolicy(min_trades=50, objective="total"))
    assert total["validation_trades"] >= selection["validation_trades"]
    # Infeasible: coin-flip outcomes never reach 80% with 50 trades.
    coin = meta_label.select_threshold(probability, np.where(rng.uniform(size=1000) < 0.5, 0.02, -0.02), policy)
    assert not coin["feasible"] and coin["threshold"] is None and coin["best_achievable_win_rate"] < 0.8


def test_high_win_rate_with_negative_expectancy_is_rejected():
    probability = np.linspace(0, 1, 500)
    net = np.where(probability > 0.3, 0.001, -0.05)  # tiny wins, huge losses
    net[::5] = -0.05
    selection = meta_label.select_threshold(probability, net, meta_label.ThresholdPolicy(min_win_rate=0.75, min_trades=30))
    if selection["feasible"]:
        assert selection["validation_expectancy"] > 0


@pytest.mark.parametrize("method", ["isotonic", "platt"])
def test_calibration_maps_miscalibrated_scores_to_observed_rates(method):
    rng = np.random.default_rng(3)
    true_p = rng.uniform(0.2, 0.8, 20000)
    raw = true_p ** 3  # monotone but badly calibrated
    y = (rng.uniform(size=len(true_p)) < true_p).astype(int)
    calibrator = meta_label.fit_calibrator(raw[:10000], y[:10000], method=method)
    calibrated = meta_label.apply_calibrator(calibrator, raw[10000:])
    holdout = y[10000:]
    assert ((calibrated >= 0) & (calibrated <= 1)).all()
    assert abs(calibrated.mean() - holdout.mean()) < 0.02
    assert np.all(np.diff(meta_label.apply_calibrator(calibrator, np.sort(raw))) >= -1e-12)
    for low, high in ((0.3, 0.4), (0.5, 0.6), (0.6, 0.7)):
        mask = (calibrated >= low) & (calibrated < high)
        if mask.sum() > 300:
            assert abs(holdout[mask].mean() - calibrated[mask].mean()) < 0.06


def test_executable_trades_never_overlap_within_symbol_strategy():
    events = pd.DataFrame({"symbol": "AAA", "strategy": "s", "event_idx": [0, 1, 2, 6, 7],
                           "entry_idx": [1, 2, 3, 7, 8], "exit_idx": [5, 4, 9, 9, 12],
                           "net_return": [0.01, 0.02, 0.03, 0.04, 0.05],
                           "t0": pd.to_datetime(["2020-01-01"] * 5)})
    kept = meta_label.executable_trades(events, np.ones(5, bool))
    assert kept["event_idx"].tolist() == [0, 6]
    kept = meta_label.executable_trades(events, np.array([False, True, True, True, True]))
    assert kept["event_idx"].tolist() == [1, 6]


def _tiny_bundle(threshold=0.5):
    events = _synthetic_events(n_years=3, per_day=3)
    names = meta_label.feature_names()
    model = meta_label._fit(meta_label.make_classifier("logistic_regression"), events[names], events["label"],
                            events["uniqueness"].to_numpy())
    calibrator = meta_label.fit_calibrator(model.predict_proba(events[names])[:, 1], events["label"].to_numpy())
    return {"format": meta_label.MODEL_FORMAT, "model_kind": "logistic_regression", "feature_names": names,
            "strategies": list(REPLAYABLE_STRATEGIES), "threshold": threshold, "model": model,
            "calibrator": calibrator, "barrier": BarrierConfig().to_dict()}


def test_save_load_roundtrip_digest_and_scoring(tmp_path):
    bundle = _tiny_bundle(threshold=0.5)
    path = tmp_path / "meta.joblib"
    manifest = meta_label.save_model(bundle, path)
    on_disk = json.loads((tmp_path / "meta.joblib.json").read_text())
    assert on_disk["sha256"] == manifest["sha256"] and "model" not in on_disk
    loaded = meta_label.load_model(path)
    assert loaded["sha256"] == manifest["sha256"]
    prices, spy = _walk(rows=300, seed=11), _walk(rows=300, seed=12)
    scored = meta_label.score_buy_signal(loaded, "AAPL", "macd_momentum", 0.6, 0.55, prices, spy)
    assert scored["status"] == "scored" and 0 <= scored["probability"] <= 1
    assert scored["decision"] == ("take" if scored["probability"] >= 0.5 else "skip")
    assert meta_label.score_buy_signal(loaded, "AAPL", "model_predictive_long", 0.6, 0.5, prices, spy)["status"] \
        == "unsupported_strategy"
    assert meta_label.score_buy_signal(loaded, "AAPL", "macd_momentum", 0.6, 0.5, prices.iloc[:100], spy)["status"] \
        == "insufficient_history"
    no_threshold = dict(loaded, threshold=None)
    assert meta_label.score_buy_signal(no_threshold, "AAPL", "macd_momentum", 0.6, 0.5, prices, spy)["decision"] == "skip"
    # Tampering is detected.
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ValueError):
        meta_label.load_model(path)
