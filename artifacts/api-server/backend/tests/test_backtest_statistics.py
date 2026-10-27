import math
from statistics import NormalDist

import numpy as np
import pytest

from app.services.backtest_statistics import (
    block_bootstrap_ci,
    deflated_sharpe_ratio,
    expected_maximum_sharpe,
    probabilistic_sharpe_ratio,
    return_moments,
)


def test_probabilistic_sharpe_matches_closed_form():
    expected = NormalDist().cdf(0.1 * math.sqrt(100) / math.sqrt(1 + 2 / 4 * 0.01))
    assert probabilistic_sharpe_ratio(0.1, 0.0, 101) == pytest.approx(expected)
    assert probabilistic_sharpe_ratio(0.1, 0.0, 1) is None


def test_expected_max_sharpe_grows_with_trials():
    assert expected_maximum_sharpe(1, 0.01) == 0
    values = [expected_maximum_sharpe(n, 0.01) for n in (2, 10, 100, 1000)]
    assert values == sorted(values) and values[0] > 0
    # Bailey & Lopez de Prado: ~0.1 * 3.26 for 1,000 trials with variance 0.01.
    assert values[-1] == pytest.approx(0.1 * 3.2552, rel=1e-3)


def test_deflated_sharpe_penalizes_many_trials():
    trial_sharpes = [0.02, -0.03, 0.05, 0.0, 0.08, -0.01, 0.04, 0.12]
    single = deflated_sharpe_ratio(0.12, 250, trials=1)
    many = deflated_sharpe_ratio(0.12, 250, trials=len(trial_sharpes), trial_sharpes=trial_sharpes)
    assert single["deflated_sharpe_ratio"] == round(probabilistic_sharpe_ratio(0.12, 0, 250), 4)
    assert many["deflated_sharpe_ratio"] < single["deflated_sharpe_ratio"]
    assert many["trials"] == 8 and many["expected_max_sharpe_per_period_under_null"] > 0


def test_return_moments_normal_kurtosis_convention():
    data = np.random.default_rng(1).normal(0.001, 0.01, 20_000)
    moments = return_moments(data)
    assert moments["kurtosis"] == pytest.approx(3, abs=0.15)
    assert moments["skewness"] == pytest.approx(0, abs=0.1)
    assert return_moments([0.01])["sharpe_per_period"] == 0


def test_block_bootstrap_is_deterministic_and_brackets_the_mean():
    data = np.random.default_rng(3).normal(0.0, 0.01, 250)
    first = block_bootstrap_ci(data, seed=11)
    second = block_bootstrap_ci(data, seed=11)
    assert first == second
    low, high = first["mean_return_ci"]
    assert low < data.mean() < high
    # Zero-mean noise: the interval must not exclude zero.
    assert low < 0 < high
    assert block_bootstrap_ci(data[:10])["status"] == "insufficient_observations"


def test_block_bootstrap_excludes_zero_for_strong_consistent_edge():
    data = np.random.default_rng(4).normal(0.004, 0.005, 250)
    low, _high = block_bootstrap_ci(data)["mean_return_ci"]
    assert low > 0
