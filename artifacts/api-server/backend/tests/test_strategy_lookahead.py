"""Look-ahead bias checks for every registered strategy.

For each strategy and several decision bars ``t``:

* the signal computed from ``data[:t+1]`` must not change when the bars after
  ``t`` are perturbed (wild prices, extra appended rows), even when the same
  strategy instance first saw the perturbed full frame (catches caches or
  state keyed on symbol that leak future rows);
* ``exit_signal`` obeys the same rule for an open position;
* in the backtester, trades that closed before the perturbation point are
  identical with and without the perturbed future.
"""
from __future__ import annotations

import math
import sys

import numpy as np
import pandas as pd
import pytest

from app.services.backtester import run_backtest
from app.services.strategies.base import neutral_signal
from app.services.strategies.registry import STRATEGY_REGISTRY, get_strategy

ROWS = 320
SLOW = {"model_predictive_long"}  # trains a forest per call


def _frame(seed: int = 11, rows: int = ROWS) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    # Trend + regime swings so crossovers, oversold dips and breakouts occur.
    drift = 0.0006 + 0.004 * np.sin(np.arange(rows) / 23.0)
    returns = drift + rng.normal(0, 0.014, rows)
    close = 100 * np.exp(np.cumsum(returns))
    open_ = close * (1 + rng.normal(0, 0.004, rows))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, rows)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, rows)))
    dates = pd.bdate_range("2023-01-02", periods=rows)
    return pd.DataFrame({
        "date": [d.date().isoformat() for d in dates],
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.integers(1_000_000, 3_000_000, rows).astype(float),
    })


def _perturb_future(frame: pd.DataFrame, t: int, extra: int = 15, seed: int = 99) -> pd.DataFrame:
    """Replace bars after ``t`` with wild values and append ``extra`` rows."""
    rng = np.random.default_rng(seed)
    out = frame.copy()
    future = out.index > t
    scale = rng.uniform(0.3, 3.0, future.sum())
    for column in ("open", "high", "low", "close"):
        out.loc[future, column] = out.loc[future, column] * scale
    out.loc[future, "high"] = out.loc[future, ["open", "high", "low", "close"]].max(axis=1) * 1.01
    out.loc[future, "low"] = out.loc[future, ["open", "high", "low", "close"]].min(axis=1) * 0.99
    tail = out.iloc[[-1] * extra].copy()
    tail["close"] = tail["close"] * np.linspace(0.5, 2.0, extra)
    tail["open"] = tail["close"]
    tail["high"] = tail["close"] * 1.02
    tail["low"] = tail["close"] * 0.98
    tail["date"] = [d.date().isoformat() for d in pd.bdate_range(
        pd.Timestamp(out["date"].iloc[-1]) + pd.offsets.BDay(), periods=extra)]
    return pd.concat([out, tail], ignore_index=True)


def _decision_bars(slug: str) -> list[int]:
    return [230, 290] if slug in SLOW else list(range(65, ROWS - 5, 4))


def _same(a, b) -> bool:
    return (
        a.action == b.action
        and math.isclose(a.confidence, b.confidence, rel_tol=0, abs_tol=1e-9)
        and math.isclose(a.probability_up, b.probability_up, rel_tol=0, abs_tol=1e-9)
    )


@pytest.mark.parametrize("slug", sorted(STRATEGY_REGISTRY))
def test_signal_at_t_ignores_future_bars(slug):
    base = _frame()
    for t in _decision_bars(slug):
        expected = get_strategy(slug).generate_signal("TEST", base.iloc[: t + 1].copy())
        perturbed = _perturb_future(base, t)
        strategy = get_strategy(slug)
        strategy.generate_signal("TEST", perturbed.copy())  # warm any state with the future
        observed = strategy.generate_signal("TEST", perturbed.iloc[: t + 1].copy())
        assert _same(expected, observed), (slug, t, expected.action, observed.action,
                                           expected.confidence, observed.confidence)


@pytest.mark.parametrize("slug", sorted(STRATEGY_REGISTRY))
def test_exit_signal_at_t_ignores_future_bars(slug):
    base = _frame()
    for t in _decision_bars(slug):
        entry_index = t - 3
        expected = get_strategy(slug).exit_signal("TEST", base.iloc[: t + 1].copy(), entry_index)
        perturbed = _perturb_future(base, t)
        strategy = get_strategy(slug)
        strategy.exit_signal("TEST", perturbed.copy(), entry_index)
        observed = strategy.exit_signal("TEST", perturbed.iloc[: t + 1].copy(), entry_index)
        assert expected == observed, (slug, t, expected, observed)


@pytest.mark.parametrize("slug", sorted(set(STRATEGY_REGISTRY) - SLOW))
def test_backtest_trades_before_perturbation_are_unchanged(slug):
    base = _frame()
    cut = 260
    perturbed = _perturb_future(base, cut)
    cut_date = base["date"].iloc[cut]
    before = [row for row in run_backtest("TEST", slug, base)["trades"] if row["exit_date"] <= cut_date]
    after = [row for row in run_backtest("TEST", slug, perturbed)["trades"] if row["exit_date"] <= cut_date]
    assert before == after, slug


class _LeakyStrategy:
    """Remembers the longest frame per symbol: a look-ahead bug the checks must catch."""
    _seen: dict = {}

    def generate_signal(self, symbol, data):
        if len(data) > len(self._seen.get(symbol, [])):
            self._seen[symbol] = data
        signal = neutral_signal("leaky")
        signal.confidence = float(self._seen[symbol]["close"].iloc[-1]) / 1000
        return signal


def test_harness_detects_a_leaky_strategy(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "get_strategy", lambda slug, parameters=None: _LeakyStrategy())
    _LeakyStrategy._seen.clear()
    with pytest.raises(AssertionError):
        test_signal_at_t_ignores_future_bars("leaky")
