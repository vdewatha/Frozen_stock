"""Per-strategy paper execution policy: symbol scope and exit plan.

A strategy class may declare ``default_exit_policy`` and
``default_symbol_scope`` (see ``app/services/strategies/base.py``); a Strategy
row's ``parameters`` can override both.  Strategies that declare neither keep
today's behaviour: the risk-rule bracket legs, SELL-signal exits and the whole
paper execution universe.

Exit timing (paper vs backtest).  Both decide on *completed* daily bars only.
The backtester fills an indicator or time-stop exit at the next bar's open.
The paper cycle evaluates the same rule on the trusted daily frame, which
withholds today's in-progress bar, so a condition set by session D's close is
acted on by the first regular-session cycle of session D+1 (from the open on,
once the daily import has stored bar D), as a limit sell at the current
intraday reference price.  Fills therefore land near, not exactly at, the
next open.  The time stop counts NYSE sessions from the entry session
(inclusive) to today (exclusive), which equals the backtester's
``holding_days = idx - entry_idx`` and does not depend on the daily import.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import logging
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from app.services.exchange_sessions import is_nyse_session
from app.services.strategies.base import BaseStrategy, ExitPolicy
from app.services.strategies.registry import get_strategy

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
# A lot older than this many calendar days has certainly passed any time stop
# we allow; the count is capped rather than walking an unbounded calendar.
_MAX_SESSION_SCAN_DAYS = 120


def strategy_instance(strategy: Any) -> BaseStrategy | None:
    """The registry strategy for a Strategy row (or test double); None if unknown."""
    strategy_type = getattr(strategy, "strategy_type", None)
    if not strategy_type:
        return None
    try:
        return get_strategy(strategy_type, getattr(strategy, "parameters", None) or {})
    except Exception:
        return None


def exit_policy_for(strategy: Any) -> ExitPolicy | None:
    instance = strategy_instance(strategy)
    return instance.exit_policy() if instance is not None else None


def symbol_scope_for(strategy: Any) -> frozenset[str] | None:
    instance = strategy_instance(strategy)
    return instance.symbol_scope() if instance is not None else None


def daily_bar_execution(strategy: Any) -> bool:
    instance = strategy_instance(strategy)
    return bool(instance is not None and instance.daily_bar_execution)


def in_symbol_scope(strategy: Any, symbol: str) -> bool:
    scope = symbol_scope_for(strategy)
    return scope is None or str(symbol).strip().upper() in scope


def exit_leg_percentages(policy: ExitPolicy | None, default_stop: Any, default_take: Any) -> tuple[Any, Any]:
    """Broker-side (stop, take-profit) percentages for an entry.

    A policy value of ``None`` falls back to the risk-rule default; ``0``
    disables that leg (stop-only entries become OTO orders).
    """
    if policy is None:
        return default_stop, default_take
    stop = default_stop if policy.stop_loss_pct is None else Decimal(str(policy.stop_loss_pct))
    take = default_take if policy.take_profit_pct is None else Decimal(str(policy.take_profit_pct))
    return stop, take


def sessions_held(entry_session: date, today: date) -> int:
    """NYSE sessions in ``[entry_session, today)``: completed sessions since entry."""
    if entry_session >= today:
        return 0
    start = max(entry_session, today - timedelta(days=_MAX_SESSION_SCAN_DAYS))
    count = 0
    day = start
    while day < today:
        if is_nyse_session(day):
            count += 1
        day += timedelta(days=1)
    return count


def _completed_frame(prices: Any, today: date) -> tuple[pd.DataFrame, pd.Series] | None:
    if not isinstance(prices, pd.DataFrame) or prices.empty or "date" not in prices or "close" not in prices:
        return None
    dates = pd.to_datetime(prices["date"], utc=True, errors="coerce").dt.date
    keep = (dates.notna() & (dates < today)).to_numpy()
    frame = prices.loc[keep].reset_index(drop=True)
    return frame, dates.loc[keep].reset_index(drop=True)


def evaluate_policy_exit(strategy: Any, symbol: str, prices: Any, opened_at: datetime,
                         now: datetime) -> dict | None:
    """Decide whether a held lot must exit under its strategy's exit policy.

    Returns ``None`` to keep holding, else ``{"reason": ..., ...details}``.
    ``prices`` is the trusted daily frame (completed bars).  The time stop is
    checked first, as in the backtester.  The indicator exit needs the entry
    session's bar to be complete, so it never fires in the entry session.
    """
    instance = strategy_instance(strategy)
    policy = instance.exit_policy() if instance is not None else None
    if policy is None or opened_at is None:
        return None
    today = now.astimezone(ET).date()
    entry_session = opened_at.astimezone(ET).date() if opened_at.tzinfo else opened_at.date()
    held = sessions_held(entry_session, today)
    details: dict = {"entry_session": entry_session.isoformat(), "sessions_held": held}
    if policy.max_holding_sessions:
        details["max_holding_sessions"] = policy.max_holding_sessions
        if held >= policy.max_holding_sessions:
            return {"reason": "time_stop", **details}
    if not policy.indicator_exit:
        return None
    completed = _completed_frame(prices, today)
    if completed is None:
        details["indicator"] = "daily bars unavailable"
        return None
    frame, dates = completed
    if frame.empty:
        return None
    details["last_completed_bar"] = dates.iloc[-1].isoformat()
    on_or_after_entry = [index for index, value in enumerate(dates) if value >= entry_session]
    if not on_or_after_entry:
        return None  # entry session's bar is not complete yet
    try:
        reason = instance.exit_signal(symbol, frame, on_or_after_entry[0])
    except Exception as exc:  # a broken indicator must not stop the cycle
        logger.warning("exit policy: %s exit_signal failed for %s: %s", instance.slug, symbol, exc)
        return None
    if isinstance(reason, str) and reason:
        return {"reason": f"indicator_exit:{reason}", **details}
    return None
