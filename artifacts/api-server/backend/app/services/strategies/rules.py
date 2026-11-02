"""Declarative long-only daily-bar rules: the strategy-discovery language.

A rule is plain data stored in a Strategy row's ``parameters["rule"]``::

    {"entry": [{"f": "dist_sma_200", "op": ">", "v": 0}, {"f": "rsi_2", "op": "<", "v": 10}],
     "exit": [{"f": "dist_sma_5", "op": ">", "v": 0}]}

Entry fires on the close of bar t when *every* entry condition holds (the
fill is the next open, as for every daily-bar strategy).  The indicator exit
fires on a bar's close when *any* exit condition holds.  The stop, target and
time stop are the usual ``stop_loss_pct`` / ``take_profit_pct`` /
``max_holding_sessions`` parameters, so the paper path and the backtester
handle them exactly as for the hand-written strategies.

Every feature is a trailing window (or the trading calendar, which is known in
advance), so ``rule_frame`` over a whole history equals the value computed on
each prefix: discovery evaluates rules vectorised over decades of bars and the
paper cycle evaluates the same function on its 260-bar frame.  The longest
lookback is ``MAX_RULE_LOOKBACK`` bars.

Discovered rules are registered under ``rule_<id>`` slugs (``RULE_SLUG_PREFIX``)
so each one has its own ``strategy_type`` for attribution, sizing and the
meta-label modes; ``registry.get_strategy`` resolves the prefix here.
"""
from __future__ import annotations

import operator
import re
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from app.services.strategies.base import (
    BaseStrategy,
    ExitPolicy,
    StrategySignalResult,
    has_valid_history,
    neutral_signal,
)
from app.services.strategies.short_term_reversion import internal_bar_strength, wilder_rsi

RULE_SLUG_PREFIX = "rule_"
MAX_RULE_LOOKBACK = 252
# Indicators that need warm-up (Wilder RSI, rolling std) are computed on at
# most this many trailing bars at runtime; enough for exact thresholds.
_RUNTIME_TAIL = 400
_OPS: dict[str, Callable] = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
                             "==": operator.eq}
_FEATURE = re.compile(r"^(?P<name>[a-z_]+?)(?:_(?P<n>\d+))?(?:_(?P<m>\d+))?$")


def _close(frame: pd.DataFrame) -> pd.Series:
    return pd.to_numeric(frame["close"], errors="coerce").astype(float).reset_index(drop=True)


def _calendar(frame: pd.DataFrame) -> pd.DataFrame:
    """Weekday-calendar position of each bar inside its month (holidays ignored)."""
    dates = pd.to_datetime(frame["date"].astype(str).str[:10]).reset_index(drop=True)
    month_start = dates.dt.to_period("M").dt.start_time
    month_end = dates.dt.to_period("M").dt.end_time.dt.normalize()
    day_of_month = np.busday_count(month_start.values.astype("datetime64[D]"),
                                   dates.values.astype("datetime64[D]")) + 1
    remaining = np.busday_count(dates.values.astype("datetime64[D]") + 1,
                                month_end.values.astype("datetime64[D]") + 1)
    return pd.DataFrame({"tdom": day_of_month, "tdays_left": remaining, "dow": dates.dt.dayofweek})


def feature(frame: pd.DataFrame, name: str, cache: Optional[dict] = None) -> pd.Series:
    """One named feature over the whole frame (row t uses bars <= t only).

    Names: ``ret_N``, ``rsi_N``, ``sma_N`` (dist_sma_N = close / SMA - 1),
    ``dist_sma_N``, ``at_low_N`` / ``at_high_N`` (close is the N-bar
    min / max), ``down_streak`` / ``up_streak``, ``ibs``, ``gap``,
    ``bb_pctb_N``, ``atr_pct_N``, ``vol_ratio_N_M`` (N- over M-bar realised
    volatility), ``tdom``, ``tdays_left``, ``dow``.
    """
    if cache is not None and name in cache:
        return cache[name]
    match = _FEATURE.match(name)
    if not match:
        raise ValueError(f"Unknown rule feature: {name}")
    kind, n, m = match["name"], match["n"], match["m"]
    n = int(n) if n else None
    m = int(m) if m else None
    close = _close(frame)
    if kind == "ret" and n:
        value = close / close.shift(n) - 1
    elif kind == "rsi" and n:
        value = wilder_rsi(close, n)
    elif kind == "dist_sma" and n:
        value = close / close.rolling(n).mean() - 1
    elif kind == "at_low" and n:
        low = close.rolling(n).min()
        value = (close <= low).astype(float).where(low.notna())
    elif kind == "at_high" and n:
        high = close.rolling(n).max()
        value = (close >= high).astype(float).where(high.notna())
    elif kind in {"down_streak", "up_streak"} and n is None:
        step = np.sign(close.diff()).fillna(0.0)
        hit = (step < 0) if kind == "down_streak" else (step > 0)
        groups = (~hit).cumsum()
        value = hit.astype(float).groupby(groups).cumsum()
    elif kind == "ibs" and n is None:
        value = internal_bar_strength(frame.reset_index(drop=True)) if {"high", "low"}.issubset(frame.columns) \
            else pd.Series(np.nan, index=close.index)
    elif kind == "gap" and n is None:
        open_ = pd.to_numeric(frame["open"], errors="coerce").astype(float).reset_index(drop=True)
        value = open_ / close.shift(1) - 1
    elif kind == "bb_pctb" and n:
        mean, std = close.rolling(n).mean(), close.rolling(n).std()
        value = (close - (mean - 2 * std)) / (4 * std).where(std > 0)
    elif kind == "atr_pct" and n:
        high = pd.to_numeric(frame["high"], errors="coerce").astype(float).reset_index(drop=True)
        low = pd.to_numeric(frame["low"], errors="coerce").astype(float).reset_index(drop=True)
        previous = close.shift(1)
        true_range = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
        value = true_range.rolling(n).mean() / close
    elif kind == "vol_ratio" and n and m:
        log_returns = np.log(close).diff()
        value = log_returns.rolling(n).std() / log_returns.rolling(m).std()
    elif kind in {"tdom", "tdays_left", "dow"} and n is None:
        value = _calendar(frame)[kind].astype(float)
    else:
        raise ValueError(f"Unknown rule feature: {name}")
    value = value.reset_index(drop=True).astype(float)
    if cache is not None:
        cache[name] = value
    return value


def _lookback(name: str) -> int:
    match = _FEATURE.match(name)
    numbers = [int(x) for x in (match["n"], match["m"]) if x] if match else []
    return max(numbers, default=1) + 1


def rule_lookback(rule: dict) -> int:
    names = [c["f"] for c in [*rule.get("entry", []), *rule.get("exit", [])]]
    return max((_lookback(name) for name in names), default=2)


def validate_rule(rule: Any) -> dict:
    """Return the rule if every condition names a known feature and operator; raise otherwise."""
    if not isinstance(rule, dict) or not rule.get("entry"):
        raise ValueError("a rule needs at least one entry condition")
    probe = pd.DataFrame({"date": pd.bdate_range("2020-01-01", periods=3).strftime("%Y-%m-%d"),
                          "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0})
    for condition in [*rule["entry"], *rule.get("exit", [])]:
        if condition.get("op") not in _OPS or not isinstance(condition.get("v"), (int, float)):
            raise ValueError(f"invalid rule condition: {condition}")
        feature(probe, condition["f"])
    if rule_lookback(rule) > MAX_RULE_LOOKBACK + 1:
        raise ValueError("rule lookback exceeds the paper frame")
    return rule


def _all(frame: pd.DataFrame, conditions: list[dict], cache: Optional[dict], default: bool) -> np.ndarray:
    if not conditions:
        return np.full(len(frame), default, dtype=bool)
    out = np.ones(len(frame), dtype=bool) if default else np.zeros(len(frame), dtype=bool)
    for condition in conditions:
        values = feature(frame, condition["f"], cache).to_numpy()
        hit = np.isfinite(values) & _OPS[condition["op"]](values, float(condition["v"]))
        out = (out & hit) if default else (out | hit)
    return out


def entry_signals(frame: pd.DataFrame, rule: dict, cache: Optional[dict] = None) -> np.ndarray:
    """Per-bar entry decision (all entry conditions), False before the lookback is filled."""
    entries = _all(frame, rule["entry"], cache, default=True)
    entries[: rule_lookback(rule) - 1] = False
    return entries


def exit_signals(frame: pd.DataFrame, rule: dict, cache: Optional[dict] = None) -> np.ndarray:
    """Per-bar indicator exit decision (any exit condition)."""
    return _all(frame, rule.get("exit", []), cache, default=False)


def describe_rule(rule: dict) -> str:
    def text(conditions):
        return " and ".join(f"{c['f']} {c['op']} {c['v']:g}" for c in conditions)

    exit_text = text(rule.get("exit", [])).replace(" and ", " or ")
    return f"BUY when {text(rule['entry'])}" + (f"; exit when {exit_text}" if exit_text else "")


class RuleStrategy(BaseStrategy):
    """A discovered rule (``parameters["rule"]``) traded like any daily-bar strategy.

    The exit policy defaults to a 10% catastrophe stop, no target and a
    10-session time stop; the row's ``stop_loss_pct`` / ``take_profit_pct`` /
    ``max_holding_sessions`` override it (validated by ``BaseStrategy``).
    """

    name = "Discovered rule"
    slug = "rule"
    default_exit_policy = ExitPolicy(stop_loss_pct=0.10, take_profit_pct=0.0, max_holding_sessions=10,
                                     indicator_exit=True)
    daily_bar_execution = True

    def __init__(self, parameters: Optional[dict] = None, slug: str = "rule") -> None:
        super().__init__(parameters)
        self.slug = slug
        self.rule = validate_rule((self.parameters or {}).get("rule"))

    def _frame(self, data: pd.DataFrame) -> pd.DataFrame:
        tail = data.iloc[-max(_RUNTIME_TAIL, rule_lookback(self.rule)):] if len(data) > _RUNTIME_TAIL else data
        return tail.reset_index(drop=True)

    def generate_signal(self, symbol: str, data: pd.DataFrame) -> StrategySignalResult:
        required = rule_lookback(self.rule)
        if not has_valid_history(data, required):
            return neutral_signal(f"Insufficient history for the rule ({required} bars).",
                                  {"required_rows": required, "rows": 0 if data is None else len(data)})
        frame = self._frame(data)
        cache: dict = {}
        fire = bool(entry_signals(frame, self.rule, cache)[-1])
        features = {c["f"]: (None if not np.isfinite(v := float(feature(frame, c["f"], cache).iloc[-1])) else v)
                    for c in self.rule["entry"]}
        features["rule"] = describe_rule(self.rule)
        if fire:
            return StrategySignalResult(action="BUY", probability_up=0.6, probability_down=0.4, confidence=0.6,
                                        reason=f"Rule {self.slug}: {describe_rule(self.rule)}.", features=features)
        return neutral_signal(f"Rule {self.slug}: entry conditions not met.", features)

    def exit_signal(self, symbol: str, data: pd.DataFrame, entry_index: int) -> Optional[str]:
        if data is None or len(data) < 2 or len(data) - 1 < entry_index or not self.rule.get("exit"):
            return None
        return "rule_exit" if bool(exit_signals(self._frame(data), self.rule)[-1]) else None
