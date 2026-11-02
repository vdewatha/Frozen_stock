"""Upcoming earnings dates for the paper entry blackout.

Earnings gaps are how mean-reversion and momentum lots jump past their stop,
so new paper BUYs are skipped when a report falls inside the hold window.
Dates come from yfinance and are cached per process. Funds have no earnings
(an empty, known answer). A provider failure is "unknown" and does not block:
a data outage must not stop paper trading, and the result records it.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, timedelta, timezone
import logging
import threading
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services.exchange_sessions import is_nyse_session

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
FRESH_FOR = timedelta(hours=12)
RETRY_AFTER = timedelta(minutes=30)
FETCH_TIMEOUT_SECONDS = 10

_cache: dict[str, tuple[datetime, list[date] | None]] = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="earnings")


def _fetch(symbol: str) -> list[date]:
    import yfinance as yf

    calendar = yf.Ticker(symbol).calendar
    values = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
    dates = set()
    for value in values or []:
        if isinstance(value, datetime):
            dates.add(value.date())
        elif isinstance(value, date):
            dates.add(value)
    return sorted(dates)


def upcoming_earnings(symbol: str, now: datetime, fetch=None) -> list[date] | None:
    """Known earnings dates (``[]`` for funds), or ``None`` when unavailable."""
    symbol = symbol.upper()
    with _lock:
        cached = _cache.get(symbol)
    if cached is not None:
        fetched_at, dates = cached
        if now - fetched_at < (FRESH_FOR if dates is not None else RETRY_AFTER):
            return dates
    try:
        dates = _pool.submit(fetch or _fetch, symbol).result(timeout=FETCH_TIMEOUT_SECONDS)
    except FutureTimeout:
        logger.info("earnings calendar timed out for %s", symbol)
        dates = None
    except Exception as exc:  # provider errors must not stop the cycle
        logger.info("earnings calendar unavailable for %s: %s", symbol, exc.__class__.__name__)
        dates = None
    with _lock:
        _cache[symbol] = (now, dates)
    return dates


def _session_horizon(today: date, sessions: int) -> date:
    day, counted = today, 0
    while counted < sessions:
        day += timedelta(days=1)
        if is_nyse_session(day):
            counted += 1
    return day


def earnings_blackout(symbol: str, now: datetime, sessions: int | None = None, fetch=None) -> dict | None:
    """Skip result for a BUY when earnings fall within ``sessions`` sessions, else ``None``.

    Today counts (a report before the open or after the close both gap the
    lot). ``0`` disables the blackout.
    """
    sessions = settings.paper_earnings_blackout_sessions if sessions is None else sessions
    if sessions <= 0:
        return None
    dates = upcoming_earnings(symbol, now, fetch=fetch)
    if dates is None:
        return None
    today = now.astimezone(ET).date()
    horizon = _session_horizon(today, sessions)
    upcoming = [day for day in dates if today <= day <= horizon]
    if not upcoming:
        return None
    return {
        "status": "skipped",
        "reason": f"earnings on {upcoming[0].isoformat()} fall within the {sessions}-session hold window",
        "earnings_date": upcoming[0].isoformat(),
    }


def earnings_known(symbol: str) -> bool:
    with _lock:
        cached = _cache.get(symbol.upper())
    return cached is not None and cached[1] is not None


def clear_cache() -> None:
    with _lock:
        _cache.clear()
