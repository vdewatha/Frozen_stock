"""Replay a live strategy's paper path over daily history under a given gate setup.

This is the engine behind ``scripts/run_market_gate_backtest.py`` (the market
gate, slot allocation and VIX reports), moved into a service so the bot can run
the same experiments itself (``experiment_engine.py``).  Nothing here places or
blocks an order; it only replays history.

Each strategy is replayed as it trades on paper:

* the vectorised replicas in ``meta_label_primary`` (parity-tested against the
  strategy classes); a BUY needs confidence >= 0.55 as in the backtester;
* RSI(14) and Bollinger keep their live ADX ``range`` gate, RSI(2) its widened
  scope, IBS its seeded IBS < 0.1 on the index ETFs;
* exits: the tested 3% stop / 6% target / 20 sessions for the technical
  strategies, 10% stop / 10 sessions / indicator exit for RSI(2) and IBS;
* fills follow ``triple_barrier.label_events``: decision on the close,
  next-open entry, volatility-scaled costs both sides, stop first, gaps at the
  open; one position per symbol per strategy.

A *setup* is the gate configuration the strategy trades with:

* ``market_gate_sma``: no entry while SPY's close is at or below its N-day
  average (``market_gate.spy_above_sma``, the live rule);
* ``vix_gate`` / ``vix_level``: ``"below"`` enters only when the VIX close of
  the decision bar is under the level, ``"above"`` only at or over it;
* ``earnings_blackout_sessions``: no entry when an earnings report lands
  within the next N sessions (the live ``earnings_calendar`` rule, replayed
  from the ``earn_surprise`` reaction bars that ``earnings_history`` attaches);
* ``stop_loss_pct`` / ``take_profit_pct`` / ``max_holding_sessions``: the exit
  geometry (``barrier_for``), the same keys a strategy row's parameters use
  to override its exit policy.  Missing keys keep the fixed barrier above, so
  a setup built from the live policy replays exactly as before.

``simulate(..., universe=mask)`` takes an optional per-date membership mask
(``symbol, dates -> bool array``, e.g. ``point_in_time_universe.Membership``)
that is ANDed into the entries: a bar outside the symbol's membership never
opens a trade.  ``None`` (the default) replays today's list as before.

Model Predictive Long refits a forest per bar and is not replayable.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping, Optional

import numpy as np
import pandas as pd

from app.services import meta_label_primary as primary
from app.services.earnings_history import SURPRISE_COLUMN
from app.services.market_gate import spy_above_sma
from app.services.meta_label import REVERSION_BARRIER
from app.services.seed import RSI2_WIDENED_SCOPE
from app.services.strategies.regime import DEFAULT_RANGE_ADX, wilder_adx
from app.services.strategy_discovery import _executable, _marked_returns
from app.services.strategy_scorecard import trade_statistics
from app.services.triple_barrier import BarrierConfig, label_events

INDEX = ("SPY", "QQQ", "IWM", "DIA")
BRACKET = BarrierConfig(take_profit_pct=0.06, stop_loss_pct=0.03, max_holding_days=20)
TECHNICAL = ("moving_average_crossover", "rsi_mean_reversion", "macd_momentum", "ensemble",
             "bollinger_mean_reversion", "channel_breakout", "trend_pullback")
RANGE_GATED = {"rsi_mean_reversion", "bollinger_mean_reversion"}
REVERSION = ("rsi2_pullback", "ibs_mean_reversion")
STRATEGIES = (*TECHNICAL, *REVERSION)
WINDOWS = {"all": None, "2015+": "2015-01-01", "2020+": "2020-01-01"}
MIN_CONFIDENCE = 0.55
GATE_KEYS = ("market_gate_sma", "vix_gate", "vix_level", "earnings_blackout_sessions")
EXIT_KEYS = ("stop_loss_pct", "take_profit_pct", "max_holding_sessions")
SETUP_KEYS = (*GATE_KEYS, *EXIT_KEYS)


def replayable(strategy: str) -> bool:
    return strategy in STRATEGIES


def scope(strategy: str, symbols: list[str]) -> list[str]:
    if strategy == "rsi2_pullback":
        return [s for s in symbols if s in RSI2_WIDENED_SCOPE]
    if strategy == "ibs_mean_reversion":
        return [s for s in symbols if s in INDEX]
    return symbols


def entries(strategy: str, frame: pd.DataFrame, replay: dict) -> np.ndarray:
    signal = primary.ibs_mean_reversion(frame, entry_threshold=0.1) if strategy == "ibs_mean_reversion" \
        else replay[strategy]
    buy = (signal["action"].to_numpy() == "BUY") & (signal["confidence"].to_numpy() >= MIN_CONFIDENCE)
    if strategy in RANGE_GATED:
        adx = wilder_adx(frame)["adx"].to_numpy()
        buy &= np.isfinite(adx) & (adx < DEFAULT_RANGE_ADX)
    return buy


def _aligned(frame: pd.DataFrame, series: pd.Series, fill) -> np.ndarray:
    dates = pd.to_datetime(frame["date"])
    return series.reindex(dates, method="ffill").fillna(fill).to_numpy()


def spy_gate_on(frame: pd.DataFrame, spy: Optional[pd.DataFrame], window: int) -> np.ndarray:
    """Per bar: the market gate lets an entry through (always when ``window`` is 0)."""
    if not window:
        return np.ones(len(frame), dtype=bool)
    if spy is None:
        return np.zeros(len(frame), dtype=bool)  # the live gate fails closed without SPY
    above = pd.Series(spy_above_sma(spy["close"], window), index=pd.to_datetime(spy["date"]))
    return _aligned(frame, above, False).astype(bool)


def vix_gate_on(frame: pd.DataFrame, vix: Optional[pd.DataFrame], gate: Optional[str],
                level: Optional[float]) -> np.ndarray:
    """Per bar: the VIX gate lets an entry through.  No VIX history fails open, as live."""
    if not gate:
        return np.ones(len(frame), dtype=bool)
    if vix is None or vix.empty:
        return np.ones(len(frame), dtype=bool)
    closes = pd.Series(pd.to_numeric(vix["close"], errors="coerce").to_numpy(float), index=pd.to_datetime(vix["date"]))
    reading = _aligned(frame, closes, np.nan)
    known = np.isfinite(reading)
    threshold = float(level if level is not None else (25.0 if gate == "below" else 20.0))
    if gate == "below":
        return ~known | (reading < threshold)
    if gate == "above":
        return ~known | (reading >= threshold)
    raise ValueError(f"unsupported vix_gate {gate!r} for replay")


def earnings_gate_on(frame: pd.DataFrame, sessions: int) -> np.ndarray:
    """Per bar: no report reacts within the next ``sessions`` bars (decision on bar t, entry at t+1)."""
    if not sessions or SURPRISE_COLUMN not in frame:
        return np.ones(len(frame), dtype=bool)
    reaction = np.flatnonzero(np.isfinite(frame[SURPRISE_COLUMN].to_numpy(float)))
    allowed = np.ones(len(frame), dtype=bool)
    for bar in reaction:
        allowed[max(bar - sessions, 0):bar] = False
    return allowed


def setup_from_parameters(parameters: Optional[Mapping]) -> dict:
    """The replayable gate setup a strategy row trades with (missing keys = off)."""
    params = dict(parameters or {})

    def integer(key: str) -> int:
        try:
            return max(int(params.get(key) or 0), 0)
        except (TypeError, ValueError):
            return 0

    gate = params.get("vix_gate") or None
    level = params.get("vix_level")
    try:
        level = float(level) if level is not None else None
    except (TypeError, ValueError):
        level = None

    def number(key: str, valid) -> Optional[float]:
        try:
            value = float(params[key])
        except (KeyError, TypeError, ValueError):
            return None
        return value if np.isfinite(value) and valid(value) else None

    hold = number("max_holding_sessions", lambda v: v >= 1 and v == int(v))
    return {"market_gate_sma": integer("market_gate_sma"), "vix_gate": gate if gate in ("below", "above") else None,
            "vix_level": level, "earnings_blackout_sessions": integer("earnings_blackout_sessions"),
            "stop_loss_pct": number("stop_loss_pct", lambda v: 0 < v < 1),
            "take_profit_pct": number("take_profit_pct", lambda v: v >= 0),
            "max_holding_sessions": int(hold) if hold is not None else None}


def barrier_for(strategy: str, setup: Optional[Mapping] = None) -> BarrierConfig:
    """The exit geometry a setup replays with: the strategy's fixed barrier, overridden key by key."""
    base = REVERSION_BARRIER if strategy in REVERSION else BRACKET
    setup = setup_from_parameters(setup)
    changes = {}
    if setup["stop_loss_pct"] is not None:
        changes["stop_loss_pct"] = float(setup["stop_loss_pct"])
    if setup["take_profit_pct"] is not None:
        changes["take_profit_pct"] = float(setup["take_profit_pct"])
    if setup["max_holding_sessions"] is not None:
        changes["max_holding_days"] = int(setup["max_holding_sessions"])
    return replace(base, **changes) if changes else base


def simulate(strategy: str, frames: Mapping[str, pd.DataFrame], spy: Optional[pd.DataFrame],
             setup: Optional[Mapping] = None, replays: Optional[Mapping[str, dict]] = None, *,
             vix: Optional[pd.DataFrame] = None, window: Optional[int] = None,
             universe: Optional[Callable[[str, pd.DatetimeIndex], np.ndarray]] = None) -> pd.DataFrame:
    """Every executable trade of ``strategy`` under ``setup`` (``window`` is the legacy market-gate-only form).

    ``universe(symbol, dates)`` is an optional per-bar membership mask ANDed
    into the entries (point-in-time universe); ``None`` keeps today's list.
    """
    setup = dict(setup or {})
    if window is not None:
        setup["market_gate_sma"] = window
    setup = setup_from_parameters(setup)
    barrier = barrier_for(strategy, setup)
    out = []
    for symbol in scope(strategy, list(frames)):
        frame = frames[symbol]
        replay = replays[symbol] if replays is not None else primary.replay_signals(frame)
        buy = (entries(strategy, frame, replay)
               & spy_gate_on(frame, spy, setup["market_gate_sma"])
               & vix_gate_on(frame, vix, setup["vix_gate"], setup["vix_level"])
               & earnings_gate_on(frame, setup["earnings_blackout_sessions"]))
        if universe is not None:
            buy &= np.asarray(universe(symbol, pd.to_datetime(frame["date"])), dtype=bool)
        positions = np.flatnonzero(buy)
        if not len(positions):
            continue
        labels = label_events(frame, positions, barrier, exit_signals=primary.replay_exit_signals(frame, strategy))
        if labels.empty:
            continue
        out.append(_executable(labels).assign(symbol=symbol, strategy=strategy))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def window_metrics(trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame], start) -> dict:
    """Per-trade statistics and the equal-weight portfolio drawdown from ``start`` (None = all)."""
    empty = {"trades": 0, "win_rate": None, "expectancy_bps": None, "expectancy_ci_bps": None,
             "profit_factor": None, "total_profit_bps": 0.0, "portfolio_max_drawdown": 0.0}
    if trades is None or trades.empty:
        return empty
    start = None if start is None else pd.Timestamp(start)
    sub = trades if start is None else trades.loc[pd.to_datetime(trades["decision_date"]) >= start]
    if sub.empty:
        return empty
    months = pd.to_datetime(sub["decision_date"]).dt.to_period("M").astype(str).tolist()
    stats = trade_statistics(sub["net_return"].to_numpy(), clusters=months)
    daily = []
    for symbol, group in sub.groupby("symbol"):
        frame = frames[symbol]
        series = pd.Series(_marked_returns(group, frame["close"].to_numpy(float)), index=pd.to_datetime(frame["date"]))
        daily.append(series if start is None else series.loc[series.index >= start])
    portfolio = pd.concat(daily, axis=1).mean(axis=1).sort_index() if daily else pd.Series(dtype=float)
    equity = np.cumprod(1 + portfolio.to_numpy()) if len(portfolio) else np.array([1.0])
    curve = np.concatenate([[1.0], equity])
    drawdown = float(np.max(1 - curve / np.maximum.accumulate(curve)))
    expectancy = stats["expectancy_bps"]
    return {"trades": stats["trades"], "win_rate": stats["win_rate"], "expectancy_bps": expectancy,
            "expectancy_ci_bps": stats.get("expectancy_ci_bps"), "profit_factor": stats["profit_factor"],
            "total_profit_bps": round(float(stats["trades"] * expectancy), 1) if expectancy is not None else 0.0,
            "portfolio_max_drawdown": round(drawdown, 4)}


def window_rows(trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame]) -> dict:
    return {name: window_metrics(trades, frames, start) for name, start in WINDOWS.items()}
