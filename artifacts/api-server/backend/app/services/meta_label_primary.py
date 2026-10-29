"""Vectorised replay of the registry strategies' signals over a full history.

Calling ``strategy.generate_signal(symbol, prices.iloc[:t + 1])`` for every
bar of 15 years x 45 symbols x 7 strategies is O(n^2) and takes hours.  Every
indicator these strategies use is a trailing rolling window or an
``adjust=False`` EWM, so the value at bar ``t`` computed once over the whole
series equals the value computed on the prefix ending at ``t``: the replay is
causal by construction.  ``tests/test_meta_label.py`` checks parity against
the real strategy classes on sampled prefixes.

``model_predictive_long`` is not replayed: it re-fits a random forest on every
call, which is infeasible per bar over this history.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.strategies.base import rsi as compute_rsi

REPLAYABLE_STRATEGIES = (
    "moving_average_crossover",
    "rsi_mean_reversion",
    "macd_momentum",
    "ensemble",
    "bollinger_mean_reversion",
    "channel_breakout",
    "trend_pullback",
)


def _clamp(values) -> np.ndarray:
    """Vector ``strategies.base.clamp``: NaN/inf -> 0.5, else clip to [0, 1]."""
    array = np.asarray(values, dtype=float)
    return np.where(np.isfinite(array), np.clip(array, 0.0, 1.0), 0.5)


def _valid_history(close: pd.Series, required_rows: int) -> np.ndarray:
    """Vector ``has_valid_history``: the last ``required_rows`` closes are finite and > 0."""
    good = (close.notna() & (close > 0)).astype(float)
    return (good.rolling(required_rows, min_periods=required_rows).sum() == required_rows).to_numpy()


def _frame(action, confidence, probability_up, valid) -> pd.DataFrame:
    action = np.where(valid, action, "HOLD")
    confidence = np.where(valid, confidence, 0.5)
    probability_up = np.where(valid, probability_up, 0.5)
    return pd.DataFrame({"action": action, "confidence": confidence, "probability_up": probability_up})


def moving_average_crossover(prices: pd.DataFrame, short_window: int = 20, long_window: int = 50) -> pd.DataFrame:
    close = prices["close"].astype(float)
    short_ma = close.rolling(short_window).mean()
    long_ma = close.rolling(long_window).mean()
    distance = ((short_ma - long_ma) / close).to_numpy()
    action = np.where(short_ma > long_ma, "BUY", np.where(short_ma < long_ma, "SELL", "HOLD"))
    return _frame(action, _clamp(0.5 + np.abs(distance) * 6), _clamp(0.5 + distance * 5),
                  _valid_history(close, max(short_window, long_window)))


def rsi_mean_reversion(prices: pd.DataFrame, buy_threshold: float = 30, sell_threshold: float = 70) -> pd.DataFrame:
    close = prices["close"].astype(float)
    value = compute_rsi(close, 14).to_numpy()
    buy, sell = value <= buy_threshold, value >= sell_threshold
    action = np.where(buy, "BUY", np.where(sell, "SELL", "HOLD"))
    confidence = np.where(buy, _clamp((buy_threshold - value) / buy_threshold + 0.55),
                          np.where(sell, _clamp((value - sell_threshold) / (100 - sell_threshold) + 0.55), 0.5))
    return _frame(action, confidence, _clamp((50 - value) / 100 + 0.5), _valid_history(close, 15))


def macd_momentum(prices: pd.DataFrame, fast_span: int = 12, slow_span: int = 26, signal_span: int = 9) -> pd.DataFrame:
    close = prices["close"].astype(float)
    macd = close.ewm(span=fast_span, adjust=False).mean() - close.ewm(span=slow_span, adjust=False).mean()
    histogram = (macd - macd.ewm(span=signal_span, adjust=False).mean()).to_numpy()
    scale = np.maximum(close.to_numpy(), 1)
    action = np.where(histogram > 0, "BUY", np.where(histogram < 0, "SELL", "HOLD"))
    return _frame(action, _clamp(0.5 + np.abs(histogram) / scale * 20), _clamp(0.5 + histogram / scale * 15),
                  _valid_history(close, slow_span + signal_span))


def ensemble(prices: pd.DataFrame) -> pd.DataFrame:
    members = [moving_average_crossover(prices), rsi_mean_reversion(prices), macd_momentum(prices)]
    weighted = sum(m["probability_up"].to_numpy() * m["confidence"].to_numpy() for m in members)
    confidence_sum = sum(m["confidence"].to_numpy() for m in members)
    confidence_sum = np.where(confidence_sum == 0, 1, confidence_sum)
    probability_up = _clamp(weighted / confidence_sum)
    confidence = _clamp(sum(m["confidence"].to_numpy() for m in members) / len(members))
    action = np.where(probability_up >= 0.57, "BUY", np.where(probability_up <= 0.43, "SELL", "HOLD"))
    return pd.DataFrame({"action": action, "confidence": confidence, "probability_up": probability_up})


def bollinger_mean_reversion(prices: pd.DataFrame, window: int = 20, std_dev: float = 2.0,
                             rsi_buy: float = 35) -> pd.DataFrame:
    close = prices["close"].astype(float)
    middle = close.rolling(window).mean()
    std = close.rolling(window).std()
    lower, upper = middle - std_dev * std, middle + std_dev * std
    value = compute_rsi(close, 14).to_numpy()
    c, lo, mid = close.to_numpy(), lower.to_numpy(), middle.to_numpy()
    band_width = np.maximum((upper - lower).to_numpy(), 1e-9)
    position = (c - lo) / band_width
    buy = (c <= lo) & (value <= rsi_buy)
    room = np.maximum(np.where(c != 0, mid / c - 1, 0), 0)
    oversold = np.maximum(rsi_buy - value, 0)
    confidence = np.where(buy, _clamp(0.52 + (1 - position) * 0.18 + oversold / 100), 0.5)
    probability_up = _clamp(0.5 + room * 4 + oversold / 250)
    valid = _valid_history(close, max(window, 15)) & (std.to_numpy() > 0)
    return _frame(np.where(buy, "BUY", "HOLD"), confidence, probability_up, valid)


def channel_breakout(prices: pd.DataFrame, lookback: int = 55, volume_window: int = 20) -> pd.DataFrame:
    close = prices["close"].astype(float)
    volume = pd.to_numeric(prices["volume"], errors="coerce").astype(float)
    prior_high = close.rolling(lookback).max().shift(1).to_numpy()
    avg_volume = volume.rolling(volume_window).mean().to_numpy()
    latest_volume = volume.to_numpy()
    volume_ok = np.isfinite(avg_volume) & np.isfinite(latest_volume) & (avg_volume > 0)
    ratio = np.where(volume_ok, latest_volume / np.where(volume_ok, avg_volume, 1), np.nan)
    c = close.to_numpy()
    strength = np.where(prior_high != 0, c / prior_high - 1, 0)
    buy = (c > prior_high) & (ratio >= 1.05)
    confidence = np.where(buy, _clamp(0.55 + strength * 10 + np.maximum(ratio - 1, 0) * 0.10), 0.5)
    probability_up = _clamp(0.5 + strength * 8 + np.maximum(ratio - 1, 0) * 0.06)
    valid = _valid_history(close, max(lookback + 1, volume_window)) & volume_ok
    return _frame(np.where(buy, "BUY", "HOLD"), confidence, probability_up, valid)


def trend_pullback(prices: pd.DataFrame, fast_ema: int = 20, slow_ema: int = 100, rsi_pullback: float = 58) -> pd.DataFrame:
    close = prices["close"].astype(float)
    fast = close.ewm(span=fast_ema, adjust=False).mean().to_numpy()
    slow = close.ewm(span=slow_ema, adjust=False).mean().to_numpy()
    value = compute_rsi(close, 14).to_numpy()
    c = close.to_numpy()
    trend = np.where(slow != 0, fast / slow - 1, 0)
    pullback = np.where(fast != 0, c / fast - 1, 0)
    buy = (trend > 0) & (pullback >= -0.035) & (pullback <= 0.005) & (value >= 38) & (value <= rsi_pullback)
    confidence = np.where(buy, _clamp(0.54 + trend * 4 + np.maximum(-pullback, 0) * 3), 0.5)
    probability_up = _clamp(0.5 + trend * 3 + np.maximum(-pullback, 0) * 2)
    return _frame(np.where(buy, "BUY", "HOLD"), confidence, probability_up,
                  _valid_history(close, max(fast_ema, slow_ema, 15)))


_REPLAY = {
    "moving_average_crossover": moving_average_crossover,
    "rsi_mean_reversion": rsi_mean_reversion,
    "macd_momentum": macd_momentum,
    "ensemble": ensemble,
    "bollinger_mean_reversion": bollinger_mean_reversion,
    "channel_breakout": channel_breakout,
    "trend_pullback": trend_pullback,
}


def replay_signals(prices: pd.DataFrame, strategies: tuple[str, ...] = REPLAYABLE_STRATEGIES) -> dict[str, pd.DataFrame]:
    """Per-bar ``action/confidence/probability_up`` for each strategy (default parameters)."""
    frame = prices.reset_index(drop=True)
    return {slug: _REPLAY[slug](frame) for slug in strategies}
