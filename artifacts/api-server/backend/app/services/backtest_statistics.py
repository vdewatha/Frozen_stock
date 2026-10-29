"""Statistical-honesty helpers for strategy evaluation.

Pure functions only (no database, broker, or execution imports). They answer
two questions a raw backtest metric cannot:

* Is the sample large and consistent enough to distinguish the result from
  zero?  -> moving-block bootstrap confidence intervals.
* After trying several parameter sets / strategies on the same history, is
  the best Sharpe still better than what luck alone would produce?  ->
  probabilistic and deflated Sharpe ratio (Bailey & Lopez de Prado, 2012 and
  2014), always reported together with the number of trials.

All Sharpe ratios here are *per period* (not annualized) unless a name says
otherwise, matching the formulas in the cited papers.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Iterable, Sequence

import numpy as np

EULER_GAMMA = 0.5772156649015329
_NORMAL = NormalDist()
# Below this many observations a bootstrap interval is not informative.
MIN_BOOTSTRAP_OBSERVATIONS = 20


def _clean(values: Iterable[float]) -> np.ndarray:
    array = np.asarray([float(value) for value in values], dtype=float)
    return array[np.isfinite(array)]


def return_moments(returns: Iterable[float]) -> dict:
    """Moments needed by PSR/DSR. Kurtosis is Pearson (normal = 3)."""
    data = _clean(returns)
    n = int(data.size)
    if n < 2:
        return {"observations": n, "mean": float(data.mean()) if n else 0.0, "std": 0.0,
                "skewness": 0.0, "kurtosis": 3.0, "sharpe_per_period": 0.0}
    mean = float(data.mean())
    std = float(data.std(ddof=1))
    centered = data - mean
    population_std = float(np.sqrt(np.mean(centered ** 2)))
    if population_std > 0:
        skewness = float(np.mean(centered ** 3) / population_std ** 3)
        kurtosis = float(np.mean(centered ** 4) / population_std ** 4)
    else:
        skewness, kurtosis = 0.0, 3.0
    return {"observations": n, "mean": mean, "std": std, "skewness": skewness,
            "kurtosis": kurtosis, "sharpe_per_period": mean / std if std > 0 else 0.0}


def probabilistic_sharpe_ratio(sharpe: float, benchmark_sharpe: float, observations: int,
                               skewness: float = 0.0, kurtosis: float = 3.0) -> float | None:
    """P(true per-period Sharpe > benchmark) given the sample (Bailey & LdP 2012)."""
    if observations < 2:
        return None
    variance = 1 - skewness * sharpe + (kurtosis - 1) / 4 * sharpe ** 2
    if not math.isfinite(variance) or variance <= 0:
        return None
    z = (sharpe - benchmark_sharpe) * math.sqrt(observations - 1) / math.sqrt(variance)
    return float(_NORMAL.cdf(z))


def expected_maximum_sharpe(trials: int, sharpe_variance: float) -> float:
    """Expected best per-period Sharpe among ``trials`` zero-skill strategies."""
    if trials <= 1 or sharpe_variance <= 0 or not math.isfinite(sharpe_variance):
        return 0.0
    first = _NORMAL.inv_cdf(1 - 1 / trials)
    second = _NORMAL.inv_cdf(1 - 1 / (trials * math.e))
    return float(math.sqrt(sharpe_variance) * ((1 - EULER_GAMMA) * first + EULER_GAMMA * second))


def deflated_sharpe_ratio(sharpe: float, observations: int, *, trials: int,
                          trial_sharpes: Sequence[float] | None = None,
                          skewness: float = 0.0, kurtosis: float = 3.0) -> dict:
    """Deflated Sharpe ratio of the selected (best) trial.

    ``trial_sharpes`` are the per-period Sharpe ratios of every configuration
    tried on the same data (including the selected one). Their dispersion sets
    how high a Sharpe luck alone would produce. When only one trial was run,
    DSR equals PSR against zero.
    """
    trials = max(int(trials), 1)
    clean = _clean(trial_sharpes or [])
    variance = float(clean.var(ddof=1)) if clean.size > 1 else 0.0
    threshold = expected_maximum_sharpe(trials, variance)
    probability = probabilistic_sharpe_ratio(sharpe, threshold, observations, skewness, kurtosis)
    return {
        "trials": trials,
        "trial_sharpe_variance": round(variance, 8),
        "expected_max_sharpe_per_period_under_null": round(threshold, 6),
        "sharpe_per_period": round(float(sharpe), 6),
        "observations": int(observations),
        "deflated_sharpe_ratio": None if probability is None else round(probability, 4),
        "method": "bailey_lopez_de_prado_2014",
    }


def block_bootstrap_ci(returns: Iterable[float], *, samples: int = 1000, confidence: float = 0.95,
                       block_length: int | None = None, seed: int = 7,
                       periods_per_year: int = 252) -> dict:
    """Moving-block bootstrap CI for mean period return and annualized Sharpe.

    Blocks preserve short-range serial dependence (holding periods make daily
    strategy returns autocorrelated). A fixed seed keeps results reproducible.
    """
    data = _clean(returns)
    n = int(data.size)
    if n < MIN_BOOTSTRAP_OBSERVATIONS:
        return {"status": "insufficient_observations", "observations": n,
                "minimum_observations": MIN_BOOTSTRAP_OBSERVATIONS,
                "mean_return_ci": None, "sharpe_ci": None}
    block = int(block_length or max(1, round(n ** (1 / 3))))
    block = min(block, n)
    blocks_needed = math.ceil(n / block)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - block + 1, size=(samples, blocks_needed))
    index = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(samples, -1)[:, :n]
    resampled = data[index]
    means = resampled.mean(axis=1)
    stds = resampled.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sharpes = np.where(stds > 0, means / stds * math.sqrt(periods_per_year), 0.0)
    tail = (1 - confidence) / 2 * 100
    low_mean, high_mean = np.percentile(means, [tail, 100 - tail])
    low_sharpe, high_sharpe = np.percentile(sharpes, [tail, 100 - tail])
    return {
        "status": "ok", "observations": n, "confidence": confidence, "samples": samples,
        "block_length": block, "seed": seed, "method": "moving_block_bootstrap",
        "mean_return_ci": [round(float(low_mean), 8), round(float(high_mean), 8)],
        "sharpe_ci": [round(float(low_sharpe), 4), round(float(high_sharpe), 4)],
    }


def equity_returns(values: Sequence[float]) -> list[float]:
    """Simple period returns from a capital-anchored equity series."""
    result = []
    for previous, current in zip(values[:-1], values[1:]):
        previous, current = float(previous), float(current)
        result.append(current / previous - 1 if previous else 0.0)
    return result


def realized_drawdown(pnls: Iterable):
    """Largest peak-to-trough drop of cumulative realized P&L (from 0); >= 0.

    Type-preserving (Decimal in, Decimal out) so the paper entry protection
    and the backtester share one rule.
    """
    equity = peak = worst = 0
    for pnl in pnls:
        equity = equity + pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst
