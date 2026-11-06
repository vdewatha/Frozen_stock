"""Market-fear gate: let a strategy buy only when the VIX says it may.

The loss review's most common missed cause is a market-wide move. The ADX
gate (``regime.py``) reads one symbol's own trend; this gate reads the whole
market through the CBOE VIX close of the last completed bar's date. The VIX
is known by 16:15 New York time on that date, before the next open the
backtester fills at, so there is no lookahead.

A strategy row opts in with the ``vix_gate`` parameter; without it nothing
changes:

* ``"below"``: BUY only when VIX < ``vix_level`` (default 25): avoid fear;
* ``"above"``: BUY only when VIX >= ``vix_level`` (default 20): buy dips only
  when fear is elevated (the classic short-term reversion view);
* ``"not_rising"``: BUY unless the VIX rose more than ``vix_rise_pct``
  (default 10%) over the last ``vix_rise_days`` (default 5) sessions.

Like the ADX gate it only turns a BUY into a HOLD, never touches SELL or exit
logic, and records the reading in the signal features. When no VIX close is
available within ``MAX_STALE_DAYS`` of the bar the gate fails open (the BUY
passes, flagged ``vix_unavailable``), so a data outage cannot silently halt a
strategy; the scoreboard and the features show it.

History comes from, in order: ``set_vix_history`` (tests and one-off
backtests), the CSV at ``$VIX_HISTORY_CSV`` (``date,close``; inherited by
backtest worker processes), or a yfinance ``^VIX`` download cached for
``CACHE_SECONDS`` per process.
"""
from __future__ import annotations

import bisect
import logging
import os
import threading
import time
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd

from app.services.strategies.base import BaseStrategy, StrategySignalResult

logger = logging.getLogger(__name__)

VIX_GATES = frozenset({"below", "above", "not_rising"})
DEFAULT_LEVELS = {"below": 25.0, "above": 20.0}
DEFAULT_RISE_PCT = 10.0
DEFAULT_RISE_DAYS = 5
MAX_STALE_DAYS = 5
CACHE_SECONDS = 6 * 3600
# A failed download is retried after this long instead of on every signal.
RETRY_SECONDS = 15 * 60


class _VixHistory:
    def __init__(self, closes: dict[str, float]) -> None:
        self.dates = sorted(closes)
        self.closes = [float(closes[d]) for d in self.dates]

    def position(self, on: str) -> Optional[int]:
        """Index of the latest close dated on or before ``on`` (ISO date)."""
        index = bisect.bisect_right(self.dates, on) - 1
        return index if index >= 0 else None


_lock = threading.Lock()
_history: Optional[_VixHistory] = None
_loaded_at = 0.0
_pinned = False


def _from_frame(frame: pd.DataFrame) -> dict[str, float]:
    data = frame.copy()
    data.columns = [str(column).lower() for column in data.columns]
    dates = pd.to_datetime(data["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    closes = pd.to_numeric(data["close"], errors="coerce")
    return {d: float(c) for d, c in zip(dates, closes) if isinstance(d, str) and pd.notna(c) and c > 0}


def set_vix_history(history: Optional[pd.DataFrame | pd.Series | dict]) -> None:
    """Pin the history (``date``/``close`` frame, date-indexed series or dict); ``None`` unpins."""
    global _history, _loaded_at, _pinned
    with _lock:
        if history is None:
            _history, _loaded_at, _pinned = None, 0.0, False
            return
        if isinstance(history, pd.Series):
            history = pd.DataFrame({"date": history.index, "close": history.to_numpy()})
        closes = _from_frame(history) if isinstance(history, pd.DataFrame) else {
            pd.Timestamp(k).strftime("%Y-%m-%d"): float(v) for k, v in history.items()}
        _history, _loaded_at, _pinned = _VixHistory(closes), time.time(), True


def download_vix_history(start: str = "1990-01-01") -> pd.DataFrame:
    import yfinance as yf

    frame = yf.download("^VIX", start=start, interval="1d", auto_adjust=False, progress=False, threads=False)
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["date", "close"])
    close = frame["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return pd.DataFrame({"date": pd.to_datetime(close.index).strftime("%Y-%m-%d"), "close": close.to_numpy()})


def _load() -> Optional[_VixHistory]:
    global _history, _loaded_at
    with _lock:
        now = time.time()
        if _pinned or (_history is not None and now - _loaded_at < CACHE_SECONDS):
            return _history
        if _history is None and _loaded_at and now - _loaded_at < RETRY_SECONDS:
            return None
        try:
            path = os.environ.get("VIX_HISTORY_CSV")
            frame = pd.read_csv(path) if path else download_vix_history()
            closes = _from_frame(frame)
            if closes:
                _history = _VixHistory(closes)
        except Exception as exc:  # network or parse failure: keep the last good copy
            logger.warning("VIX history unavailable: %s", exc)
        _loaded_at = now
        return _history


def _bar_date(data: pd.DataFrame) -> Optional[str]:
    if "date" not in data.columns or data.empty:
        return None
    stamp = pd.to_datetime(data["date"].iloc[-1], errors="coerce", utc=True)
    return None if pd.isna(stamp) else stamp.strftime("%Y-%m-%d")


def vix_reading(on: Optional[str], *, rise_days: int = DEFAULT_RISE_DAYS) -> dict:
    """The VIX close for bar date ``on`` and its ``rise_days`` change in percent."""
    history = _load() if on else None
    index = history.position(on) if history is not None else None
    if index is None:
        return {"vix": None, "vix_date": None, "vix_change_pct": None, "reason": "vix_unavailable"}
    vix_date = history.dates[index]
    if date.fromisoformat(on) - date.fromisoformat(vix_date) > timedelta(days=MAX_STALE_DAYS):
        return {"vix": None, "vix_date": vix_date, "vix_change_pct": None, "reason": "vix_unavailable"}
    vix = history.closes[index]
    change = None
    if index >= rise_days:
        previous = history.closes[index - rise_days]
        change = round(100.0 * (vix / previous - 1.0), 2) if previous > 0 else None
    return {"vix": round(vix, 2), "vix_date": vix_date, "vix_change_pct": change}


class VixGatedStrategy:
    """Wraps a strategy so its BUYs pass only when the VIX gate allows."""

    def __init__(self, inner: BaseStrategy, gate: str, parameters: dict) -> None:
        if gate not in VIX_GATES:
            raise ValueError(f"Unknown vix_gate {gate!r}; expected one of {sorted(VIX_GATES)}")
        self._inner = inner
        self.gate = gate
        self.level = float(parameters.get("vix_level", DEFAULT_LEVELS.get(gate, 0.0)))
        self.rise_pct = float(parameters.get("vix_rise_pct", DEFAULT_RISE_PCT))
        self.rise_days = int(parameters.get("vix_rise_days", DEFAULT_RISE_DAYS))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _allowed(self, reading: dict) -> bool:
        vix = reading["vix"]
        if vix is None:
            return True  # fail open; recorded as vix_unavailable
        if self.gate == "below":
            return vix < self.level
        if self.gate == "above":
            return vix >= self.level
        change = reading["vix_change_pct"]
        return change is None or change <= self.rise_pct

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        result = self._inner.generate_signal(symbol, data)
        if result.action != "BUY":
            # Only BUYs are gated; skip the lookup on the common HOLD path.
            return result
        reading = vix_reading(_bar_date(data), rise_days=self.rise_days)
        allowed = self._allowed(reading)
        record = {**reading, "gate": self.gate, "level": self.level, "allowed": allowed}
        if self.gate == "not_rising":
            record.update(rise_pct=self.rise_pct, rise_days=self.rise_days)
        features = {**(result.features or {}), "vix_gate": record}
        if allowed:
            return replace(result, features=features)
        if self.gate == "not_rising":
            why = f"VIX up {reading['vix_change_pct']:.1f}% in {self.rise_days} sessions"
        else:
            why = f"VIX {reading['vix']:.1f} {'>=' if self.gate == 'below' else '<'} {self.level:g}"
        return replace(result, action="HOLD", features={**features, "gated_action": "BUY"},
                       reason=f"VIX gate {self.gate}: {why} blocks entry. {result.reason}")


def apply_vix_gate(strategy: BaseStrategy, parameters: Optional[dict]) -> BaseStrategy:
    gate = (parameters or {}).get("vix_gate")
    if not gate:
        return strategy
    return VixGatedStrategy(strategy, str(gate), parameters or {})  # type: ignore[return-value]
