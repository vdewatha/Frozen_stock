"""Triple-barrier labels for primary-strategy BUY events (AFML ch. 3-4).

A *primary* strategy emits a BUY decision on the close of bar ``t``.  The
candidate trade it implies is executed exactly like ``backtester.run_backtest``
would execute it:

* entry at the open of bar ``t + 1`` plus the execution cost estimated from
  bars ``<= t`` only (``backtester.execution_cost_bps``, volatility-scaled by
  default) and the per-side fee;
* an upper barrier (take-profit) and a lower barrier (stop-loss) as a
  fraction of the cost-inclusive entry price, either fixed or scaled by the
  causal daily volatility at decision time;
* a vertical barrier after ``max_holding_days`` bars, exited at that bar's
  open, mirroring the backtester's ``max_holding_period`` exit;
* gaps through a level fill at the open (never at the better level price);
  when one bar touches both levels the stop is assumed to have been hit first.

The label is ``1`` when the trade's **net** return after both sides' costs and
fees is positive, else ``0``.  Events whose path is still open when the data
ends are dropped (their label is not yet knowable).

Everything a label needs is either decision-time information (barrier widths,
entry cost inputs use bars ``<= t``) or the realised path between entry and
exit, so perturbing bars after an event's exit cannot change that event's
label.  ``average_uniqueness`` gives AFML sample weights for overlapping
labels.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from app.services.backtester import BacktestConfig


@dataclass(frozen=True)
class BarrierConfig:
    # Fixed geometry defaults match the paper path's broker-side bracket
    # (DEFAULT_POSITION_STOP_LOSS_PCT / DEFAULT_POSITION_TAKE_PROFIT_PCT = 3%).
    take_profit_pct: float = 0.03
    stop_loss_pct: float = 0.03
    max_holding_days: int = 10
    # When True the barriers are ``multiplier * sigma_daily * sqrt(H)`` with
    # sigma from the ``volatility_lookback`` log returns ending at bar t,
    # clipped to [min_barrier_pct, max_barrier_pct].
    volatility_scaled: bool = False
    take_profit_sigma: float = 1.0
    stop_loss_sigma: float = 1.0
    volatility_lookback: int = 20
    min_barrier_pct: float = 0.01
    max_barrier_pct: float = 0.15
    # Cost model (same semantics and defaults as BacktestConfig).
    fees_bps: float = 1.0
    slippage_bps: float = 5.0
    cost_model: str = "volatility_scaled"
    volatility_cost_fraction: float = 0.05
    impact_coefficient: float = 1.0
    # Nominal order size used for the square-root impact term.
    position_notional: float = 10_000.0

    def __post_init__(self) -> None:
        if self.max_holding_days < 1:
            raise ValueError("max_holding_days must be >= 1")
        if self.volatility_lookback < 2:
            raise ValueError("volatility_lookback must be >= 2")
        if not self.volatility_scaled and (self.take_profit_pct <= 0 or not 0 < self.stop_loss_pct < 1):
            raise ValueError("fixed barriers must be positive (stop below 100%)")
        if self.volatility_scaled and (self.take_profit_sigma <= 0 or self.stop_loss_sigma <= 0):
            raise ValueError("volatility multipliers must be positive")
        if not 0 < self.min_barrier_pct <= self.max_barrier_pct < 1:
            raise ValueError("barrier clip bounds must satisfy 0 < min <= max < 1")
        if self.cost_model not in {"flat", "volatility_scaled"}:
            raise ValueError(f"Unknown cost model: {self.cost_model}")

    def backtest_config(self) -> BacktestConfig:
        return BacktestConfig(
            fees_bps=self.fees_bps, slippage_bps=self.slippage_bps, cost_model=self.cost_model,
            volatility_lookback=self.volatility_lookback, volatility_cost_fraction=self.volatility_cost_fraction,
            impact_coefficient=self.impact_coefficient,
        )

    def to_dict(self) -> dict:
        return asdict(self)


def prior_liquidity(prices: pd.DataFrame, lookback: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-bar ``(sigma_bps, adv)`` computed from bars strictly before each bar.

    Vectorised equivalent of ``backtester._liquidity(prices.iloc[:idx])``:
    element ``idx`` only uses rows ``< idx``.  ``adv`` is NaN when unknown.
    """
    close = pd.to_numeric(prices["close"], errors="coerce").astype(float)
    log_returns = np.log(close).diff()
    sigma = log_returns.rolling(lookback, min_periods=2).std(ddof=1).shift(1)
    sigma_bps = (sigma * 10_000).where(np.isfinite(sigma), 0.0).fillna(0.0).to_numpy()
    if "volume" in prices.columns:
        volume = pd.to_numeric(prices["volume"], errors="coerce").astype(float)
        adv = volume.rolling(lookback, min_periods=1).mean().shift(1)
        adv = adv.where(np.isfinite(adv) & (adv > 0)).to_numpy()
    else:
        adv = np.full(len(prices), np.nan)
    return sigma_bps, adv


def side_cost_bps(sigma_bps: float, adv: float, quantity: float, config: BarrierConfig) -> float:
    """Same arithmetic as ``backtester.execution_cost_bps`` for one side."""
    base = float(config.slippage_bps)
    if config.cost_model == "flat":
        return base
    volatility_bps = config.volatility_cost_fraction * sigma_bps
    participation = quantity / adv if adv and math.isfinite(adv) and adv > 0 and quantity > 0 else None
    impact = config.impact_coefficient * sigma_bps * math.sqrt(participation) if participation else 0.0
    return min(base + volatility_bps + impact, 5_000.0)


def decision_volatility(prices: pd.DataFrame, lookback: int) -> np.ndarray:
    """Daily log-return std over the ``lookback`` returns ending at each bar (inclusive)."""
    close = pd.to_numeric(prices["close"], errors="coerce").astype(float)
    return np.log(close).diff().rolling(lookback, min_periods=lookback).std(ddof=1).to_numpy()


def barrier_widths(prices: pd.DataFrame, config: BarrierConfig) -> tuple[np.ndarray, np.ndarray]:
    """Take-profit / stop-loss fractions known at the close of each bar."""
    n = len(prices)
    if not config.volatility_scaled:
        return np.full(n, config.take_profit_pct), np.full(n, config.stop_loss_pct)
    horizon = decision_volatility(prices, config.volatility_lookback) * math.sqrt(config.max_holding_days)
    take = np.clip(config.take_profit_sigma * horizon, config.min_barrier_pct, config.max_barrier_pct)
    stop = np.clip(config.stop_loss_sigma * horizon, config.min_barrier_pct, config.max_barrier_pct)
    return take, stop


LABEL_COLUMNS = [
    "event_idx", "entry_idx", "exit_idx", "decision_date", "entry_date", "exit_date", "entry_price",
    "exit_price", "take_profit_pct", "stop_loss_pct", "barrier", "holding_days", "gross_return",
    "net_return", "label",
]


def label_events(prices: pd.DataFrame, event_positions: Iterable[int],
                 config: BarrierConfig | None = None) -> pd.DataFrame:
    """Triple-barrier labels for decisions made on the close of ``event_positions``.

    ``prices`` must be one symbol's daily bars in ascending date order with
    ``date, open, high, low, close`` (``volume`` optional).  Returns one row
    per *resolved* event (see ``LABEL_COLUMNS``).
    """
    config = config or BarrierConfig()
    frame = prices.reset_index(drop=True)
    n = len(frame)
    opens = pd.to_numeric(frame["open"], errors="coerce").to_numpy(float)
    closes = pd.to_numeric(frame["close"], errors="coerce").to_numpy(float)
    if {"high", "low"}.issubset(frame.columns):
        highs = pd.to_numeric(frame["high"], errors="coerce").to_numpy(float)
        lows = pd.to_numeric(frame["low"], errors="coerce").to_numpy(float)
        highs = np.where(np.isfinite(highs), highs, np.maximum(opens, closes))
        lows = np.where(np.isfinite(lows), lows, np.minimum(opens, closes))
    else:
        highs, lows = np.maximum(opens, closes), np.minimum(opens, closes)
    highs = np.maximum.reduce([highs, opens, closes])
    lows = np.minimum.reduce([lows, opens, closes])
    dates = frame["date"].astype(str).to_numpy()
    sigma_bps, adv = prior_liquidity(frame, config.volatility_lookback)
    take_widths, stop_widths = barrier_widths(frame, config)
    fee = config.fees_bps / 10_000
    horizon = config.max_holding_days
    rows = []
    for raw in event_positions:
        t = int(raw)
        entry = t + 1
        if t < 0 or entry >= n:
            continue
        take_pct, stop_pct = float(take_widths[t]), float(stop_widths[t])
        open_entry = opens[entry]
        if not (math.isfinite(take_pct) and math.isfinite(stop_pct) and math.isfinite(open_entry) and open_entry > 0):
            continue
        quantity = config.position_notional / open_entry
        entry_price = open_entry * (1 + side_cost_bps(sigma_bps[entry], adv[entry], quantity, config) / 10_000)
        stop_level = entry_price * (1 - stop_pct)
        target_level = entry_price * (1 + take_pct)
        exit_idx = exit_raw = barrier = None
        for idx in range(entry, min(entry + horizon, n - 1) + 1):
            if idx > entry:
                open_ = opens[idx]
                if open_ <= stop_level:
                    exit_idx, exit_raw, barrier = idx, open_, "stop_loss_gap"
                elif open_ >= target_level:
                    exit_idx, exit_raw, barrier = idx, open_, "take_profit_gap"
                elif idx - entry >= horizon:
                    exit_idx, exit_raw, barrier = idx, open_, "vertical"
                if exit_idx is not None:
                    break
            if lows[idx] <= stop_level:
                exit_idx, exit_raw, barrier = idx, stop_level, "stop_loss"
                break
            if highs[idx] >= target_level:
                exit_idx, exit_raw, barrier = idx, target_level, "take_profit"
                break
        if exit_idx is None:
            continue  # path unresolved at the end of the data
        exit_price = exit_raw * (1 - side_cost_bps(sigma_bps[exit_idx], adv[exit_idx], quantity, config) / 10_000)
        net = (exit_price * (1 - fee)) / (entry_price * (1 + fee)) - 1
        rows.append((t, entry, exit_idx, dates[t], dates[entry], dates[exit_idx], entry_price, exit_price,
                     take_pct, stop_pct, barrier, exit_idx - entry, exit_raw / open_entry - 1, net, int(net > 0)))
    return pd.DataFrame(rows, columns=LABEL_COLUMNS)


def average_uniqueness(starts: Sequence[int], ends: Sequence[int], groups: Sequence | None = None) -> np.ndarray:
    """AFML average uniqueness of each label over its ``[start, end]`` bar span.

    Concurrency is counted within each group (normally one symbol, across
    every strategy's events on it).  Returns weights in (0, 1].
    """
    starts = np.asarray(starts, dtype=int)
    ends = np.asarray(ends, dtype=int)
    if len(starts) != len(ends):
        raise ValueError("starts and ends must have equal length")
    if (ends < starts).any():
        raise ValueError("label end precedes its start")
    group_values = np.zeros(len(starts), dtype=int) if groups is None else pd.factorize(np.asarray(groups))[0]
    weights = np.zeros(len(starts), dtype=float)
    for group in np.unique(group_values):
        mask = np.flatnonzero(group_values == group)
        if not len(mask):
            continue
        s, e = starts[mask], ends[mask]
        size = int(e.max()) + 2
        delta = np.zeros(size, dtype=float)
        np.add.at(delta, s, 1.0)
        np.add.at(delta, e + 1, -1.0)
        inverse = np.zeros(size, dtype=float)
        concurrency = np.cumsum(delta)
        inverse[concurrency > 0] = 1.0 / concurrency[concurrency > 0]
        cumulative = np.concatenate([[0.0], np.cumsum(inverse)])
        weights[mask] = (cumulative[e + 1] - cumulative[s]) / (e - s + 1)
    return weights
