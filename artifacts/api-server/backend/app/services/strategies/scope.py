"""Entry scope: let a strategy buy only when one of its own entry features says it may.

The signal autopsy (``signal_autopsy.py``) compares the entry-time features
of a strategy's losses with those of its wins.  When one feature separates
them, the experiment engine replays a one-condition *scope* rule on it and,
if the pre-registered rule passes, writes it to the strategy row as
``parameters["scope"]`` in the ``rules.py`` condition format::

    {"f": "rsi_2", "op": "<", "v": 10.0}

This module is the live half: ``apply_scope`` wraps the strategy the way
``vix_gate.apply_vix_gate`` does, so a BUY whose decision bar fails the
condition becomes a HOLD (``gated_action: "BUY"``, the feature value and the
condition in ``features["scope"]``).  SELLs, exits and HOLDs are untouched.
The condition is evaluated on the frame the strategy decided on (the last
row is the decision bar), with ``rules.feature``, so the live decision and
the replay (``gate_replay.simulate``) read the same number.  A value the
frame cannot supply (too little history, no high/low) fails the condition,
as a rule condition does, and is flagged ``scope_unavailable``.

An invalid ``scope`` parameter (unknown feature, bad operator, non-numeric
threshold) is ignored, never applied half-way.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd

from app.services.strategies import rules
from app.services.strategies.base import BaseStrategy, StrategySignalResult

logger = logging.getLogger(__name__)
SCOPE_KEY = "scope"
_RUNTIME_TAIL = 400


def scope_condition(parameters: Optional[Mapping]) -> Optional[dict]:
    """The validated one-condition scope rule in ``parameters`` (``None`` when unset or invalid)."""
    raw = (parameters or {}).get(SCOPE_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        condition = {"f": str(raw["f"]), "op": str(raw["op"]), "v": float(raw["v"])}
        if not np.isfinite(condition["v"]) or isinstance(raw["v"], bool):
            return None
        rules.validate_rule({"entry": [condition]})
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("ignoring invalid scope parameter %r: %s", raw, exc)
        return None
    return condition


def describe_scope(condition: Optional[Mapping]) -> str:
    if not condition:
        return "no entry scope"
    return f"BUY only when {condition['f']} {condition['op']} {float(condition['v']):g} on the decision bar"


def scope_label(condition: Optional[Mapping]) -> str:
    if not condition:
        return "off"
    return f"{condition['f']}{condition['op']}{float(condition['v']):g}"


def scope_mask(frame: pd.DataFrame, condition: Mapping, cache: Optional[dict] = None) -> np.ndarray:
    """Per bar: the condition holds on that bar's close (False where the feature is not finite)."""
    values = rules.feature(frame, condition["f"], cache).to_numpy(float)
    return np.isfinite(values) & rules._OPS[condition["op"]](values, float(condition["v"]))


def scope_reading(data: pd.DataFrame, condition: Mapping) -> dict:
    """The feature on the decision bar (the frame's last row) and whether the condition holds."""
    if data is None or data.empty or "close" not in data:
        return {"feature": condition["f"], "value": None, "allowed": False, "reason": "scope_unavailable"}
    frame = (data.iloc[-_RUNTIME_TAIL:] if len(data) > _RUNTIME_TAIL else data).reset_index(drop=True)
    try:
        value = float(rules.feature(frame, condition["f"]).iloc[-1])
    except (KeyError, ValueError, TypeError, IndexError):
        value = float("nan")
    if not np.isfinite(value):
        return {"feature": condition["f"], "value": None, "allowed": False, "reason": "scope_unavailable"}
    allowed = bool(rules._OPS[condition["op"]](value, float(condition["v"])))
    return {"feature": condition["f"], "value": round(value, 6), "allowed": allowed}


class ScopedStrategy:
    """Wraps a strategy so its BUYs pass only when the scope condition holds on the decision bar."""

    def __init__(self, inner: BaseStrategy, condition: Mapping) -> None:
        self._inner = inner
        self.condition = dict(condition)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        result = self._inner.generate_signal(symbol, data)
        if result.action != "BUY":
            return result  # only BUYs are scoped
        reading = scope_reading(data, self.condition)
        record = {**reading, "condition": dict(self.condition), "rule": describe_scope(self.condition)}
        features = {**(result.features or {}), SCOPE_KEY: record}
        if reading["allowed"]:
            return replace(result, features=features)
        value = "unavailable" if reading["value"] is None else f"{reading['value']:g}"
        return replace(result, action="HOLD", features={**features, "gated_action": "BUY"},
                       reason=(f"Entry scope: {self.condition['f']} = {value} fails "
                               f"{self.condition['f']} {self.condition['op']} {float(self.condition['v']):g}. "
                               f"{result.reason}"))


def apply_scope(strategy: BaseStrategy, parameters: Optional[dict]) -> BaseStrategy:
    condition = scope_condition(parameters)
    if condition is None:
        return strategy
    return ScopedStrategy(strategy, condition)  # type: ignore[return-value]
