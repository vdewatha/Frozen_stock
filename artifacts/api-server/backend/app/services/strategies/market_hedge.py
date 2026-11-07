"""Market hedge: a small long in the inverse ETF SH while the market gate is closed.

Four live strategies carry the SPY-trend market gate (``market_gate.py``):
no new longs while SPY's close is at or below its N-day average.  When the
gate is closed the book sits in cash and learns nothing from a falling
market.  This strategy is the mirror image of the gate, placed through the
normal paper *long* path: it buys SH (ProShares Short S&P 500, a daily
inverse of SPY) so that every existing cap, protection, reservation and exit
mechanism applies to the hedge exactly as to any other long.

Rule (``reports/hedge-20261106.md``, fixed before the backtest):

* **Entry** on the close of a SPY bar where the close is below the
  ``trend_sma``-day average (default 50) *and* the ``fast_sma``-day average
  (default 10) is below the 50-day one; fill at the next open.
* **Exit** when SPY closes back above its 50-day average (``exit_signal``,
  the policy's indicator exit), or on the 10% broker-side catastrophe stop
  on SH itself.  No take-profit leg; the long 250-session time stop only
  bounds the backtester's barrier.
* **Size**: ``hedge_fraction`` of equity is the fraction the backtest
  adopted (``ADOPTED_HEDGE_FRACTION``); it is reported in the signal
  features and in the row's parameters.  The paper cycle sizes every entry
  through its own risk rules (an exploration candidate is learning-sized),
  so the parameter is the target, not an order size.

The entry condition reads SPY, not the traded symbol.  Live, the SPY frame
comes from the same trusted daily history the market gate uses
(``set_market_history`` pins one for tests and one-off backtests); only SPY
bars dated on or before the last bar of the traded frame are consulted, so a
backtest over a prefix sees nothing from the future.  Missing or stale SPY
history fails closed (no entry; an open hedge keeps its stop).

``HEDGE_ONLY_SYMBOLS`` are instruments only this strategy may buy: the
registry instantiates every other strategy through ``fenced_class`` so a
trend or reversion rule that happens to fire on SH records a gated HOLD
instead of an order.  The market gate never applies here: the row carries no
``market_gate_sma`` and ``MARKET_GATED_STRATEGIES`` does not list it.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

from app.services.market_gate import MARKET_SYMBOL, spy_above_sma
from app.services.strategies.base import (
    BaseStrategy,
    ExitPolicy,
    StrategySignalResult,
    clamp,
    has_valid_history,
    neutral_signal,
)

logger = logging.getLogger(__name__)

HEDGE_SYMBOL = "SH"
HEDGE_ONLY_SYMBOLS: frozenset[str] = frozenset({HEDGE_SYMBOL})
TREND_WINDOW = 50
FAST_WINDOW = 10
HEDGE_SIZES = (0.02, 0.05, 0.10)
# The fraction of equity the 2026-11-06 backtest adopted, or None when no
# size passed the pre-registered rule (the seed then creates the row paused).
ADOPTED_HEDGE_FRACTION: Optional[float] = None
HEDGE_REPORT = "reports/hedge-20261106.md"
# 10% catastrophe stop on SH (a 10% SPY rally against the hedge), no target,
# a 250-session bound so the backtester's barrier stays finite; the real exit
# is the indicator (SPY reclaims its average).
HEDGE_EXIT_POLICY = ExitPolicy(stop_loss_pct=0.10, take_profit_pct=0.0, max_holding_sessions=250,
                               indicator_exit=True)
MAX_STALE_DAYS = 5
CACHE_SECONDS = 10 * 60
RETRY_SECONDS = 5 * 60


def hedge_state(close: pd.Series, trend_window: int = TREND_WINDOW, fast_window: int = FAST_WINDOW) -> dict:
    """Per SPY bar: ``entry`` (close below the trend average and the fast average below it) and ``exit``.

    Both are False until the averages are defined; ``exit`` is
    ``market_gate.spy_above_sma`` with the same window, so the hedge leaves on
    exactly the bar that reopens the gate for a 50-day-gated long.
    """
    series = pd.to_numeric(pd.Series(close), errors="coerce").astype(float).reset_index(drop=True)
    trend = series.rolling(trend_window, min_periods=trend_window).mean().to_numpy()
    fast = series.rolling(fast_window, min_periods=fast_window).mean().to_numpy()
    values = series.to_numpy()
    defined = np.isfinite(trend) & np.isfinite(fast) & np.isfinite(values)
    entry = defined & (values < trend) & (fast < trend)
    return {"entry": entry, "exit": spy_above_sma(series, trend_window), "trend": trend, "fast": fast}


# ----------------------------------------------------------------- SPY history

_lock = threading.Lock()
_history: Optional[pd.DataFrame] = None
_loaded_at = 0.0
_pinned = False


def _normalized(frame: Any) -> Optional[pd.DataFrame]:
    if not isinstance(frame, pd.DataFrame) or frame.empty or "close" not in frame or "date" not in frame:
        return None
    data = frame.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce", utc=True).dt.strftime("%Y-%m-%d")
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data = data.dropna(subset=["date", "close"]).drop_duplicates("date", keep="last").sort_values("date")
    return data.reset_index(drop=True)


def set_market_history(frame: Optional[pd.DataFrame]) -> None:
    """Pin the SPY daily frame (``date``/``close``); ``None`` unpins and clears the cache."""
    global _history, _loaded_at, _pinned
    with _lock:
        if frame is None:
            _history, _loaded_at, _pinned = None, 0.0, False
            return
        _history, _loaded_at, _pinned = _normalized(frame), time.time(), True


def _load_market_history() -> Optional[pd.DataFrame]:
    """SPY completed daily bars from the trusted history, cached per process."""
    global _history, _loaded_at
    with _lock:
        now = time.time()
        if _pinned or (_history is not None and now - _loaded_at < CACHE_SECONDS):
            return _history
        if _history is None and _loaded_at and now - _loaded_at < RETRY_SECONDS:
            return None
        try:
            from app.db.session import SessionLocal
            from app.services.trusted_data import trusted_history

            with SessionLocal() as db:
                frame = trusted_history(db, MARKET_SYMBOL, limit=400, require_active=False)[0]
            _history = _normalized(frame)
        except Exception as exc:  # the next signal fails closed; retried after RETRY_SECONDS
            logger.warning("market hedge: %s history unavailable: %s", MARKET_SYMBOL, exc)
            _history = None
        _loaded_at = now
        return _history


def _bar_date(data: pd.DataFrame) -> Optional[str]:
    if data is None or "date" not in data.columns or data.empty:
        return None
    stamp = pd.to_datetime(data["date"].iloc[-1], errors="coerce", utc=True)
    return None if pd.isna(stamp) else stamp.strftime("%Y-%m-%d")


def market_until(on: Optional[str]) -> Optional[pd.DataFrame]:
    """SPY bars dated on or before ``on`` (ISO date); None when unavailable or stale."""
    history = _load_market_history() if on else None
    if history is None:
        return None
    frame = history.loc[history["date"] <= on]
    if frame.empty:
        return None
    if date.fromisoformat(on) - date.fromisoformat(str(frame["date"].iloc[-1])) > timedelta(days=MAX_STALE_DAYS):
        return None
    return frame.reset_index(drop=True)


# ----------------------------------------------------------------- strategy

class MarketHedgeStrategy(BaseStrategy):
    """Long SH while SPY trades below a falling 50-day average; out when SPY reclaims it."""

    name = "Market Hedge"
    slug = "market_hedge"
    default_exit_policy = HEDGE_EXIT_POLICY
    default_symbol_scope = (HEDGE_SYMBOL,)
    daily_bar_execution = True

    def _params(self) -> dict:
        def integer(key: str, default: int) -> int:
            try:
                return max(int(self.parameters.get(key, default)), 2)
            except (TypeError, ValueError):
                return default

        fraction = self.parameters.get("hedge_fraction", ADOPTED_HEDGE_FRACTION)
        try:
            fraction = float(fraction) if fraction is not None else None
        except (TypeError, ValueError):
            fraction = None
        return {"trend_sma": integer("trend_sma", TREND_WINDOW), "fast_sma": integer("fast_sma", FAST_WINDOW),
                "hedge_fraction": fraction if fraction is not None and 0 < fraction <= 1 else None}

    def _market(self, data: pd.DataFrame, params: dict) -> tuple[Optional[pd.DataFrame], Optional[dict]]:
        spy = market_until(_bar_date(data))
        if spy is None:
            return None, None
        if len(spy) < params["trend_sma"]:
            return spy, None
        return spy, hedge_state(spy["close"], params["trend_sma"], params["fast_sma"])

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        params = self._params()
        features: dict = {"hedge_fraction": params["hedge_fraction"], "trend_sma": params["trend_sma"],
                          "fast_sma": params["fast_sma"]}
        if str(symbol).strip().upper() not in HEDGE_ONLY_SYMBOLS:
            return neutral_signal(f"{symbol} is not a hedge instrument ({', '.join(sorted(HEDGE_ONLY_SYMBOLS))}).",
                                  features)
        if not has_valid_history(data, 2):
            return neutral_signal("Insufficient SH history.", features | {"rows": 0 if data is None else len(data)})
        spy, state = self._market(data, params)
        if spy is None:
            return neutral_signal(f"{MARKET_SYMBOL} history unavailable or stale (fail closed).",
                                  features | {"market": "unavailable"})
        if state is None:
            return neutral_signal(f"{MARKET_SYMBOL} history is shorter than {params['trend_sma']} bars (fail closed).",
                                  features | {"market": "short", "rows": len(spy)})
        last = -1
        close, trend, fast = float(spy["close"].iloc[last]), float(state["trend"][last]), float(state["fast"][last])
        features.update({"spy_close": round(close, 4), "spy_trend_sma": round(trend, 4), "spy_fast_sma": round(fast, 4),
                         "spy_bar": str(spy["date"].iloc[last]), "gate_closed": bool(close <= trend)})
        if bool(state["entry"][last]):
            depth = (trend - close) / trend if trend > 0 else 0.0
            return StrategySignalResult(
                action="BUY",
                probability_up=clamp(0.55 + depth * 2),
                probability_down=clamp(0.45 - depth * 2),
                confidence=clamp(0.6 + depth * 5, 0.0, 0.9),
                reason=(f"{MARKET_SYMBOL} {close:.2f} below its {params['trend_sma']}-day average {trend:.2f} and the "
                        f"{params['fast_sma']}-day average {fast:.2f} is below it: hedge with {symbol}."),
                features=features,
            )
        why = "above" if close > trend else "at or below"
        return neutral_signal(f"{MARKET_SYMBOL} {close:.2f} {why} its {params['trend_sma']}-day average {trend:.2f}; "
                              f"{params['fast_sma']}-day {fast:.2f}.", features)

    def exit_signal(self, symbol: str, data: pd.DataFrame, entry_index: int) -> Optional[str]:
        params = self._params()
        if data is None or data.empty or len(data) - 1 < entry_index:
            return None
        spy, state = self._market(data, params)
        if state is None:
            return None  # keep holding; the broker stop still protects the lot
        return f"spy_above_sma{params['trend_sma']}" if bool(state["exit"][-1]) else None


# ----------------------------------------------------------------- fence

_FENCED: dict[type, type] = {}


def fence_signal(result: StrategySignalResult, symbol: str) -> StrategySignalResult:
    """A BUY on a hedge-only symbol from any other strategy becomes a gated HOLD."""
    if result.action != "BUY" or str(symbol).strip().upper() not in HEDGE_ONLY_SYMBOLS:
        return result
    features = {**(result.features or {}), "gated_action": "BUY",
                "hedge_fence": {"symbol": symbol, "allowed": False, "owner": MarketHedgeStrategy.slug}}
    return replace(result, action="HOLD", features=features,
                   reason=f"{symbol} is a hedge instrument reserved for {MarketHedgeStrategy.slug}. {result.reason}")


def fenced_class(cls: type[BaseStrategy]) -> type[BaseStrategy]:
    """``cls`` with ``generate_signal`` fenced off the hedge instruments (the hedge itself is returned as is).

    A subclass with the same name, so ``isinstance`` checks against the
    registry classes and the gate wrappers keep working.
    """
    if issubclass(cls, MarketHedgeStrategy):
        return cls
    fenced = _FENCED.get(cls)
    if fenced is None:
        def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
            return fence_signal(cls.generate_signal(self, symbol, data), symbol)

        fenced = type(cls.__name__, (cls,), {"generate_signal": generate_signal, "hedge_fenced": True,
                                              "__module__": cls.__module__, "__qualname__": cls.__qualname__,
                                              "__doc__": cls.__doc__})
        _FENCED[cls] = fenced
    return fenced
