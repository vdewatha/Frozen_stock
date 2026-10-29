from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class ExitPolicy:
    """A strategy's own exit plan for paper execution.

    ``None`` for a percentage means "use the risk-rule default"
    (``position_stop_loss_pct`` / ``position_take_profit_pct``); ``0`` means
    "no leg" (a stop-only entry is sent as an OTO order).  The catastrophe stop
    is the broker-side stop leg.  ``max_holding_sessions`` is a time stop in
    completed regular sessions since the entry session (the backtester's
    ``max_holding_days``), and ``indicator_exit`` makes the paper cycle consult
    :meth:`BaseStrategy.exit_signal` on completed daily bars.
    """

    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    max_holding_sessions: Optional[int] = None
    indicator_exit: bool = False


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
    # Strategies that declare an exit policy / symbol scope override these.
    # ``None`` keeps today's defaults: risk-rule bracket legs, SELL-signal
    # exits only, and the whole paper execution universe.
    default_exit_policy: Optional[ExitPolicy] = None
    default_symbol_scope: Optional[tuple[str, ...]] = None

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

    def exit_policy(self) -> Optional[ExitPolicy]:
        """The declared exit policy with per-row parameter overrides applied.

        ``parameters`` may override ``stop_loss_pct`` (0 < x < 1),
        ``take_profit_pct`` (>= 0; 0 = no take-profit leg) and
        ``max_holding_sessions`` (>= 1).  Invalid overrides are ignored so a bad
        row can never remove the declared catastrophe stop.
        """
        policy = self.default_exit_policy
        if policy is None:
            return None
        stop = _float_param(self.parameters.get("stop_loss_pct"), lambda v: 0 < v < 1)
        take = _float_param(self.parameters.get("take_profit_pct"), lambda v: v >= 0)
        hold = _float_param(self.parameters.get("max_holding_sessions"), lambda v: v >= 1 and v == int(v))
        return ExitPolicy(
            stop_loss_pct=stop if stop is not None else policy.stop_loss_pct,
            take_profit_pct=take if take is not None else policy.take_profit_pct,
            max_holding_sessions=int(hold) if hold is not None else policy.max_holding_sessions,
            indicator_exit=policy.indicator_exit,
        )

    def symbol_scope(self) -> Optional[frozenset[str]]:
        """Symbols this strategy may trade; ``None`` = the whole universe.

        ``parameters["symbol_scope"]`` (a list or a comma-separated string)
        overrides the class default.
        """
        raw = self.parameters.get("symbol_scope", self.default_symbol_scope)
        if raw is None:
            return None
        if isinstance(raw, str):
            raw = raw.split(",")
        symbols = frozenset(str(item).strip().upper() for item in raw if str(item).strip())
        return symbols or None


def _float_param(value, valid) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) and valid(numeric) else None


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
