"""Triple-barrier labels: execution parity with the backtester, net-of-cost labels, causality."""
import math

import numpy as np
import pandas as pd
import pytest

from app.services.backtester import BacktestConfig, _liquidity, execution_cost_bps
from app.services.triple_barrier import (
    BarrierConfig,
    average_uniqueness,
    barrier_widths,
    label_events,
    prior_liquidity,
    side_cost_bps,
)


def _flat(rows=40, price=100.0):
    dates = pd.bdate_range("2020-01-01", periods=rows)
    return pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "open": price, "high": price, "low": price,
                         "close": price, "volume": 1_000_000.0})


def _walk(rows=400, seed=3):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, rows)))
    open_ = close * (1 + rng.normal(0, 0.003, rows))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, rows)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, rows)))
    return pd.DataFrame({"date": pd.bdate_range("2015-01-01", periods=rows).strftime("%Y-%m-%d"),
                         "open": open_, "high": high, "low": low, "close": close,
                         "volume": rng.integers(1_000_000, 3_000_000, rows).astype(float)})


FLAT_COSTS = BarrierConfig(cost_model="flat", slippage_bps=5, fees_bps=1)


def _expected_net(entry_open, exit_raw, config=FLAT_COSTS):
    entry = entry_open * (1 + config.slippage_bps / 1e4)
    exit_ = exit_raw * (1 - config.slippage_bps / 1e4)
    fee = config.fees_bps / 1e4
    return exit_ * (1 - fee) / (entry * (1 + fee)) - 1


def test_take_profit_touched_intrabar_is_a_win_at_the_level_net_of_costs():
    prices = _flat()
    prices.loc[13, "high"] = 104.0  # entry bar is 11 (decision on 10)
    labels = label_events(prices, [10], FLAT_COSTS)
    row = labels.iloc[0]
    entry = 100 * (1 + 5e-4)
    assert row.entry_idx == 11 and row.exit_idx == 13 and row.barrier == "take_profit"
    assert row.entry_price == pytest.approx(entry)
    assert row.net_return == pytest.approx(_expected_net(100.0, entry * 1.03))
    assert row.label == 1


def test_stop_first_when_one_bar_touches_both_levels():
    prices = _flat()
    prices.loc[12, ["high", "low"]] = [104.0, 96.0]
    row = label_events(prices, [10], FLAT_COSTS).iloc[0]
    assert row.barrier == "stop_loss" and row.label == 0 and row.exit_idx == 12


def test_gap_through_stop_fills_at_the_open_not_the_level():
    prices = _flat()
    prices.loc[13:, ["open", "high", "low", "close"]] = 90.0
    row = label_events(prices, [10], FLAT_COSTS).iloc[0]
    assert row.barrier == "stop_loss_gap" and row.exit_idx == 13
    assert row.net_return == pytest.approx(_expected_net(100.0, 90.0))


def test_vertical_barrier_exits_at_open_after_max_holding_days_and_costs_make_flat_a_loss():
    prices = _flat()
    row = label_events(prices, [10], FLAT_COSTS).iloc[0]
    assert row.barrier == "vertical" and row.exit_idx == 11 + FLAT_COSTS.max_holding_days
    assert row.net_return < 0 and row.label == 0  # zero gross move is a net loss


def test_small_gross_gain_below_round_trip_cost_is_labelled_a_loss():
    prices = _flat()
    prices.loc[21:, ["open", "high", "low", "close"]] = 100.05  # +5bps, round trip costs ~12bps
    row = label_events(prices, [10], FLAT_COSTS).iloc[0]
    assert row.gross_return > 0 and row.net_return < 0 and row.label == 0


def test_unresolved_path_at_end_of_data_is_dropped():
    prices = _flat(rows=15)
    assert label_events(prices, [10], FLAT_COSTS).empty
    assert label_events(prices, [14], FLAT_COSTS).empty  # no next-open entry bar


def test_vectorised_liquidity_and_cost_match_backtester():
    prices = _walk()
    config = BarrierConfig()
    backtest = BacktestConfig()
    sigma_bps, adv = prior_liquidity(prices, config.volatility_lookback)
    for idx in (1, 2, 3, 5, 21, 22, 150, 399):
        expected_sigma, expected_adv = _liquidity(prices.iloc[:idx], backtest)
        assert sigma_bps[idx] == pytest.approx(expected_sigma, abs=1e-9)
        assert (expected_adv is None and math.isnan(adv[idx])) or adv[idx] == pytest.approx(expected_adv)
        quantity = 1234.0
        expected = execution_cost_bps(prices.iloc[:idx], quantity, backtest)["total_bps"]
        assert side_cost_bps(sigma_bps[idx], adv[idx], quantity, config) == pytest.approx(expected)


@pytest.mark.parametrize("config", [BarrierConfig(), BarrierConfig(volatility_scaled=True)])
def test_perturbing_bars_after_an_exit_does_not_change_earlier_labels(config):
    prices = _walk()
    events = list(range(30, 380, 3))
    base = label_events(prices, events, config)
    cut = 250
    perturbed = prices.copy()
    rng = np.random.default_rng(99)
    scale = rng.uniform(0.5, 1.5, len(prices) - cut)
    for column in ("open", "high", "low", "close"):
        perturbed.loc[cut:, column] = perturbed.loc[cut:, column].to_numpy() * scale
    perturbed.loc[cut:, "volume"] = perturbed.loc[cut:, "volume"] * 7
    after = label_events(perturbed, events, config)
    early = base.loc[base.exit_idx < cut].reset_index(drop=True)
    assert len(early) > 20
    pd.testing.assert_frame_equal(early, after.loc[after.exit_idx < cut].reset_index(drop=True).iloc[:len(early)])
    # Barrier widths are decision-time quantities.
    take_a, stop_a = barrier_widths(prices, config)
    take_b, stop_b = barrier_widths(perturbed, config)
    np.testing.assert_array_equal(take_a[:cut], take_b[:cut])
    np.testing.assert_array_equal(stop_a[:cut], stop_b[:cut])


def test_volatility_scaled_barriers_follow_sigma_and_respect_clip():
    prices = _walk()
    config = BarrierConfig(volatility_scaled=True, take_profit_sigma=1.0, stop_loss_sigma=2.0)
    take, stop = barrier_widths(prices, config)
    t = 100
    sigma = np.log(prices["close"]).diff().iloc[t - 19:t + 1].std(ddof=1)
    assert take[t] == pytest.approx(np.clip(sigma * math.sqrt(10), 0.01, 0.15))
    assert stop[t] == pytest.approx(np.clip(2 * sigma * math.sqrt(10), 0.01, 0.15))
    assert np.isnan(take[5])  # not enough history yet -> event skipped


def test_average_uniqueness():
    np.testing.assert_allclose(average_uniqueness([0, 5], [2, 7]), [1.0, 1.0])
    np.testing.assert_allclose(average_uniqueness([0, 0], [3, 3]), [0.5, 0.5])
    # [0,3] and [2,5] overlap on bars 2-3: (1+1+.5+.5)/4 each.
    np.testing.assert_allclose(average_uniqueness([0, 2], [3, 5]), [0.75, 0.75])
    # Concurrency is per group.
    np.testing.assert_allclose(average_uniqueness([0, 0], [3, 3], ["A", "B"]), [1.0, 1.0])
    with pytest.raises(ValueError):
        average_uniqueness([3], [1])


def test_invalid_configuration_is_rejected():
    with pytest.raises(ValueError):
        BarrierConfig(max_holding_days=0)
    with pytest.raises(ValueError):
        BarrierConfig(stop_loss_pct=1.5)
    with pytest.raises(ValueError):
        BarrierConfig(cost_model="magic")
