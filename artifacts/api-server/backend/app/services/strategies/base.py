from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class StrategySignalResult:
    action: str
    probability_up: float
    probability_down: float
    confidence: float
    reason: str
    features: dict


class BaseStrategy(ABC):
    name: str
    slug: str

    def __init__(self, parameters: Optional[dict] = None) -> None:
        self.parameters = parameters or {}

    @abstractmethod
    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        raise NotImplementedError

    def exit_signal(self, symbol: str, data: pd.DataFrame, entry_index: int) -> Optional[str]:
        """Optional indicator exit for an open long position.

        ``data`` holds completed bars only (the last row is the latest
        completed bar) and ``entry_index`` is the positional row of the entry
        bar inside ``data``. Return a short reason string to exit at the next
        open, or ``None`` to keep holding. The default never exits, so
        strategies without an indicator exit rely on the stop, target and time
        stop only.
        """
        return None


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    """Bound ``value`` to ``[minimum, maximum]``.

    Non-finite input (NaN from a warm-up window, inf from a zero divisor) maps
    to the neutral midpoint.  Python's ``min``/``max`` silently turn NaN into
    the upper bound, which previously reported warm-up signals as 100%
    confident.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return (minimum + maximum) / 2
    if not math.isfinite(numeric):
        return (minimum + maximum) / 2
    return max(minimum, min(maximum, numeric))


def neutral_signal(reason: str, features: Optional[dict] = None) -> StrategySignalResult:
    """A HOLD that no confidence gate (>= 0.55) can act on."""
    return StrategySignalResult(
        action="HOLD",
        probability_up=0.5,
        probability_down=0.5,
        confidence=0.5,
        reason=reason,
        features=features or {},
    )


def has_valid_history(data: pd.DataFrame, required_rows: int) -> bool:
    """True when the frame has enough finite, positive closes for a window."""
    if data is None or "close" not in data or len(data) < required_rows:
        return False
    close = pd.to_numeric(data["close"], errors="coerce")
    tail = close.iloc[-required_rows:]
    return bool(tail.notna().all() and (tail > 0).all())


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Simple-average RSI with explicit flat-market handling.

    With no losses the RSI is 100 only when there were gains; a completely
    flat window is 50 (no momentum), not 0.  The previous ``loss.replace(0,
    1e-9)`` mapped a flat window to RSI 0, i.e. maximally "oversold", which
    fired mean-reversion BUY signals on halted or stale prices.
    """
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = -delta.clip(upper=0).rolling(window).mean()
    values = 100 - (100 / (1 + gain / loss.where(loss != 0)))
    values = values.mask((loss == 0) & (gain > 0), 100.0)
    values = values.mask((loss == 0) & (gain == 0), 50.0)
    return values
