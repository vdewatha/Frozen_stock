"""Market-regime gate: let range strategies buy in ranges and trend strategies in trends.

``classify_regime`` labels the last completed bar from Wilder's ADX (trend
strength) and the directional indicators (+DI/-DI):

* ``trend_up`` / ``trend_down``: ADX >= ``regime_trend_adx`` (default 25),
  direction from +DI vs -DI;
* ``range``: ADX < ``regime_range_adx`` (default 20);
* ``neutral``: in between, or too little history to tell.

A strategy row opts in with the ``regime_gate`` parameter; without it nothing
changes. The gate only turns a BUY into a HOLD (it never creates a BUY or
touches SELL/exit logic) and records the regime in the signal features:

* ``"range"``: BUY only in ``range``;
* ``"not_trending"``: BUY in ``range`` or ``neutral``;
* ``"not_downtrend"``: BUY unless ``trend_down`` (dip buying blocked only in
  a strong falling trend);
* ``"trend_up"``: BUY only in ``trend_up`` (for breakout entries).

The same wrapper runs in the backtester and in paper trading (both build
strategies through ``registry.get_strategy``), so a gated row trades exactly
what its backtest measured.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

import pandas as pd

from app.services.strategies.base import BaseStrategy, StrategySignalResult

REGIME_GATES = {
    "range": frozenset({"range"}),
    "not_trending": frozenset({"range", "neutral"}),
    "not_downtrend": frozenset({"range", "neutral", "trend_up"}),
    "trend_up": frozenset({"trend_up"}),
}
DEFAULT_ADX_WINDOW = 14
DEFAULT_TREND_ADX = 25.0
DEFAULT_RANGE_ADX = 20.0
# Wilder smoothing converges well inside this many bars; computing on the
# tail keeps the per-bar backtest cost flat on decades of history.
_REGIME_TAIL = 300


def wilder_adx(frame: pd.DataFrame, window: int = DEFAULT_ADX_WINDOW) -> pd.DataFrame:
    """Columns ``adx``, ``plus_di`` and ``minus_di`` (Wilder, 0-100)."""
    high = pd.to_numeric(frame["high"], errors="coerce").astype(float)
    low = pd.to_numeric(frame["low"], errors="coerce").astype(float)
    close = pd.to_numeric(frame["close"], errors="coerce").astype(float)
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    previous = close.shift(1)
    true_range = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
    alpha = 1.0 / window

    def smooth(series: pd.Series) -> pd.Series:
        return series.ewm(alpha=alpha, adjust=False, min_periods=window).mean()

    atr = smooth(true_range)
    plus_di = 100 * smooth(plus_dm) / atr.where(atr > 0)
    minus_di = 100 * smooth(minus_dm) / atr.where(atr > 0)
    total = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / total.where(total > 0)
    adx = dx.ewm(alpha=alpha, adjust=False, min_periods=window).mean()
    return pd.DataFrame({"adx": adx, "plus_di": plus_di, "minus_di": minus_di})


def classify_regime(frame: pd.DataFrame, *, window: int = DEFAULT_ADX_WINDOW,
                    trend_adx: float = DEFAULT_TREND_ADX, range_adx: float = DEFAULT_RANGE_ADX) -> dict:
    """The regime of the last bar of ``frame`` (completed bars only)."""
    if not all(column in frame.columns for column in ("high", "low", "close")) or len(frame) < 3 * window:
        return {"regime": "neutral", "adx": None, "plus_di": None, "minus_di": None,
                "reason": "not enough high/low/close history"}
    tail = frame.iloc[-_REGIME_TAIL:] if len(frame) > _REGIME_TAIL else frame
    last = wilder_adx(tail, window).iloc[-1]
    adx, plus_di, minus_di = (None if pd.isna(last[key]) else round(float(last[key]), 2)
                              for key in ("adx", "plus_di", "minus_di"))
    if adx is None or plus_di is None or minus_di is None:
        regime = "neutral"
    elif adx >= trend_adx:
        regime = "trend_up" if plus_di >= minus_di else "trend_down"
    elif adx < range_adx:
        regime = "range"
    else:
        regime = "neutral"
    return {"regime": regime, "adx": adx, "plus_di": plus_di, "minus_di": minus_di}


class RegimeGatedStrategy:
    """Wraps a strategy so its BUYs pass only in the regimes its gate allows."""

    def __init__(self, inner: BaseStrategy, gate: str, parameters: dict) -> None:
        if gate not in REGIME_GATES:
            raise ValueError(f"Unknown regime_gate {gate!r}; expected one of {sorted(REGIME_GATES)}")
        self._inner = inner
        self.gate = gate
        self.window = int(parameters.get("regime_adx_window", DEFAULT_ADX_WINDOW))
        self.trend_adx = float(parameters.get("regime_trend_adx", DEFAULT_TREND_ADX))
        self.range_adx = float(parameters.get("regime_range_adx", DEFAULT_RANGE_ADX))

    def __getattr__(self, name: str) -> Any:
        # slug, name, parameters, exit_signal, exit_policy, daily_bar_execution, ...
        return getattr(self._inner, name)

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        result = self._inner.generate_signal(symbol, data)
        state = classify_regime(data, window=self.window, trend_adx=self.trend_adx, range_adx=self.range_adx)
        allowed = state["regime"] in REGIME_GATES[self.gate]
        record = {**state, "gate": self.gate, "allowed": allowed}
        features = {**(result.features or {}), "regime_gate": record}
        if result.action != "BUY" or allowed:
            return replace(result, features=features)
        adx = "n/a" if state["adx"] is None else f"{state['adx']:.1f}"
        return replace(
            result, action="HOLD", features={**features, "gated_action": "BUY"},
            reason=f"Regime gate {self.gate}: {state['regime']} market (ADX {adx}) blocks entry. {result.reason}",
        )


def apply_regime_gate(strategy: BaseStrategy, parameters: Optional[dict]) -> BaseStrategy:
    gate = (parameters or {}).get("regime_gate")
    if not gate:
        return strategy
    return RegimeGatedStrategy(strategy, str(gate), parameters or {})  # type: ignore[return-value]
