"""Step 5 pairs kill test: the pre-registered rule, the rolling beta and the hedge arithmetic."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_pairs_kill_test as pk  # noqa: E402


def test_clustered_mean_matches_cr0_by_hand():
    values = np.array([0.01, 0.03, -0.01, 0.02, 0.0])
    clusters = ["a", "a", "b", "c", "c"]
    out = pk.clustered_mean(values, clusters)
    mean = values.mean()
    sums = np.array([0.01 + 0.03 - 2 * mean, -0.01 - mean, 0.02 + 0.0 - 2 * mean])
    se = np.sqrt((sums ** 2).sum()) / len(values)
    assert out["mean_bps"] == pytest.approx(mean * 1e4, abs=0.01)
    assert out["se_bps"] == pytest.approx(se * 1e4, abs=0.01)
    assert out["ci_bps"][0] == pytest.approx((mean - 1.96 * se) * 1e4, abs=0.05)
    assert out["clusters"] == 3
    assert pk.clustered_mean(np.array([np.nan, 0.1]), ["a", "b"])["mean_bps"] is None


def test_proceed_needs_60_bps_and_an_interval_above_zero():
    assert pk.proceed({"mean_bps": 61.0, "ci_bps": [5.0, 117.0]})["passes"]
    small = pk.proceed({"mean_bps": 12.5, "ci_bps": [6.3, 18.6]})        # the default rule's reading
    assert small["ci_ok"] and not small["mean_ok"] and not small["passes"]
    assert not pk.proceed({"mean_bps": 70.0, "ci_bps": [-1.0, 141.0]})["passes"]
    assert not pk.proceed({"mean_bps": None, "ci_bps": None})["passes"]


def test_lane_decision_branches():
    assert pk.lane_decision({"passes": True}, None)["decision"] == "proceed_to_stage_a"
    assert pk.lane_decision({"passes": False}, {"passes": True})["decision"] == "close_uptrend_version_stage_a_next"
    assert pk.lane_decision({"passes": False}, {"passes": False})["decision"] == "close_market_neutral_lane"
    with pytest.raises(ValueError):
        pk.lane_decision({"passes": False}, None)


def test_no_uptrend_variant_drops_only_the_sma200_condition():
    from app.services.strategies.sector_reversal import DEFAULT_RULE

    variant = pk.no_uptrend(DEFAULT_RULE)
    assert variant["entry"] == [{"f": "rel_z_5", "op": "<", "v": -2}]
    assert variant["exit"] == DEFAULT_RULE["exit"]
    assert len(DEFAULT_RULE["entry"]) == 2           # the default rule is untouched


def test_rolling_beta_recovers_a_known_slope_and_is_causal():
    rng = np.random.default_rng(5)
    x = rng.normal(0, 0.01, 300)
    y = 1.5 * x + rng.normal(0, 0.001, 300)
    beta = pk.rolling_beta(y, x)
    assert np.isnan(beta[:pk.BETA_MIN_OBSERVATIONS - 1]).all()
    assert beta[100] == pytest.approx(1.5, abs=0.05)
    # changing a later return never changes an earlier beta
    y2 = y.copy()
    y2[200:] *= -1
    assert np.allclose(pk.rolling_beta(y2, x)[:200], beta[:200], equal_nan=True)


def test_hedge_subtracts_beta_times_the_etf_leg_over_the_same_hold():
    n = 120
    dates = pd.bdate_range("2016-01-04", periods=n).strftime("%Y-%m-%d")
    rng = np.random.default_rng(6)
    etf_close = 50 * np.cumprod(1 + rng.normal(0, 0.01, n))
    etf_ret = np.r_[0.0, etf_close[1:] / etf_close[:-1] - 1]
    stock_close = 20 * np.cumprod(1 + 2.0 * etf_ret)
    etf = pd.DataFrame({"date": dates, "open": etf_close * 0.999, "close": etf_close})
    stock = pd.DataFrame({"date": dates, "open": stock_close, "close": stock_close,
                          "sector_close": etf_close / etf_close[0]})
    frames = {"XLK": etf, "AAA": stock}
    trades = pd.DataFrame([{"symbol": "AAA", "event_idx": 99, "entry_idx": 100, "exit_idx": 103,
                            "barrier": "indicator", "gross_return": 0.02}])
    out = pk.hedge(trades, frames, {"AAA": np.array(["XLK"] * n, dtype=object)})
    row = out.iloc[0]
    assert row["etf"] == "XLK"
    assert row["beta"] == pytest.approx(2.0, abs=1e-6)
    leg = etf["open"].iloc[103] / etf["open"].iloc[100] - 1
    assert row["etf_return"] == pytest.approx(leg)
    assert row["residual"] == pytest.approx(0.02 - 2.0 * leg)
    # an intraday stop exit uses the ETF's close that session
    stop = pk.hedge(trades.assign(barrier="stop_loss"), frames, {"AAA": np.array(["XLK"] * n, dtype=object)})
    assert stop.iloc[0]["etf_return"] == pytest.approx(etf["close"].iloc[103] / etf["open"].iloc[100] - 1)
    # no assignment on the decision bar: no residual
    names = np.array(["XLK"] * n, dtype=object)
    names[99] = None
    assert np.isnan(pk.hedge(trades, frames, {"AAA": names}).iloc[0]["residual"])


def test_buckets_and_crisis_periods():
    assert [pk.bucket_of(v) for v in (12.0, 15.0, 19.9, 22.0, 25.0, 80.0)] == \
        ["<15", "15-20", "15-20", "20-25", ">=25", ">=25"]
    assert pk.bucket_of(float("nan")) is None
    assert set(pk.CRISIS_PERIODS) == {"2015-16", "2018 Q4", "2020", "2022", "2025"}
    assert pk.CRISIS_PERIODS["2018 Q4"] == ("2018-10-01", "2019-01-01")
