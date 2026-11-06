"""Scheduled market-wide announcements, so paper BUYs are not opened right before them.

Three events move the whole market on a known date:

* ``fomc``: the Fed's rate decision, 14:00 ET on the meeting's second day.
  Dates come from the Fed's own calendar page; the 2026-2027 schedule below
  is the fallback when the page cannot be read.
* ``cpi`` and ``jobs``: the inflation and employment reports, 08:30 ET.
  Dates come from FRED's release calendar (needs ``FRED_API_KEY``).

Blackout, for BUYs only (exits are never blocked):

* strategies that hold overnight skip the whole session before each event,
  and the FOMC day until 14:30 ET (half an hour after the statement);
* intraday strategies close the same day, so only the FOMC morning applies.

The calendar is refreshed daily into the audit log (no table, no migration).
A missing calendar never blocks: it is reported as unknown on the result and
on the Learning scoreboard.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
import logging
import re
import threading
import time
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog
from app.services.audit import write_audit_log
from app.services.exchange_sessions import is_nyse_session

logger = logging.getLogger(__name__)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
EVENT_TYPE = "market_event_calendar"
RELEASE_TIME = {"fomc": dtime(14, 0), "cpi": dtime(8, 30), "jobs": dtime(8, 30)}
EVENT_NAMES = {"fomc": "Fed rate decision", "cpi": "CPI inflation report", "jobs": "jobs report"}
FOMC_SETTLE = timedelta(minutes=30)
HISTORY_DAYS = 400
# FRED release ids: 10 = Consumer Price Index, 50 = Employment Situation.
FRED_RELEASES = {"cpi": 10, "jobs": 50}
# Statement days (second day of each meeting), from federalreserve.gov,
# read 2026-11-06. Used only when the live page cannot be parsed.
FOMC_FALLBACK = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28",
    "2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15",
    "2027-10-27", "2027-12-08",
]
_MONTHS = {name: index for index, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"], start=1)}
_cache: dict = {"at": None, "events": None}
_cache_lock = threading.Lock()
_CACHE_SECONDS = 300


def parse_fomc_calendar(html: str) -> list[date]:
    """Statement days of scheduled meetings from the Fed's calendar page.

    Each year is a panel headed "2027 FOMC Meetings" with rows of a month
    (``Apr/May`` when a meeting spans two) and days (``27-28``, ``16-17*``).
    Unscheduled single-day calls have no range and are skipped.
    """
    headers = [(int(match.group(1)), match.start()) for match in re.finditer(r"(\d{4}) FOMC Meetings", html)]
    headers.sort(key=lambda item: item[1])
    days: set[date] = set()
    for index, (year, start) in enumerate(headers):
        end = headers[index + 1][1] if index + 1 < len(headers) else len(html)
        chunk = html[start:end]
        for month_text, day_text in re.findall(
                r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<', chunk, re.S):
            months = [_month(part) for part in month_text.split("/")]
            match = re.match(r"\s*(\d{1,2})\s*-\s*(\d{1,2})", day_text)
            if not match or None in months:
                continue
            first, second = int(match.group(1)), int(match.group(2))
            # "Apr/May 30-1": the statement day is in the later month.
            month = months[-1] if second < first else months[0]
            try:
                days.add(date(year, month, second))
            except ValueError:
                continue
    return sorted(days)


def _month(text: str) -> Optional[int]:
    text = text.strip().lower()[:3]
    return next((number for name, number in _MONTHS.items() if name.lower().startswith(text)), None) if text else None


def fetch_fomc_dates(*, client: Optional[httpx.Client] = None) -> list[date]:
    owned = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": "frozen-stock-research/1.0"})
    try:
        response = client.get(settings.market_events_fomc_url)
        response.raise_for_status()
        return parse_fomc_calendar(response.text)
    finally:
        if owned:
            client.close()


def fetch_release_dates(release_id: int, start: date, fetch: Optional[Callable] = None) -> list[date]:
    from app.services.economic_data import fred_get

    payload = (fetch or fred_get)("release/dates", {
        "release_id": release_id, "realtime_start": start.isoformat(), "realtime_end": "9999-12-31",
        "include_release_dates_with_no_data": "true", "sort_order": "asc", "limit": 1000,
    })
    out = set()
    for item in payload.get("release_dates") or []:
        try:
            out.add(date.fromisoformat(item["date"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def build_calendar(today: date, *, fomc: Optional[Callable] = None, fred: Optional[Callable] = None) -> dict:
    """Events from about a year back on (loss diagnosis looks back at old holds)."""
    from app.services.economic_data import fred_configured

    start = today - timedelta(days=HISTORY_DAYS)
    events: list[dict] = []
    sources: dict[str, str] = {}
    errors: dict[str, str] = {}
    try:
        fomc_days = (fomc or fetch_fomc_dates)()
        if not any(day >= today for day in fomc_days):
            raise ValueError("no upcoming meeting on the page")
        sources["fomc"] = "federalreserve.gov"
    except Exception as exc:
        errors["fomc"] = exc.__class__.__name__
        fomc_days = [date.fromisoformat(day) for day in FOMC_FALLBACK]
        sources["fomc"] = "built-in schedule"
    events += [{"kind": "fomc", "date": day.isoformat()} for day in fomc_days if day >= start]
    for kind, release_id in FRED_RELEASES.items():
        if fred is None and not fred_configured():
            errors[kind] = "FRED_API_KEY not set"
            continue
        try:
            days = fetch_release_dates(release_id, start, fetch=fred)
            events += [{"kind": kind, "date": day.isoformat()} for day in days if day >= start]
            sources[kind] = "FRED release calendar"
        except Exception as exc:
            errors[kind] = exc.__class__.__name__
    for event in events:
        event["time_et"] = RELEASE_TIME[event["kind"]].strftime("%H:%M")
    events.sort(key=lambda item: (item["date"], item["time_et"], item["kind"]))
    return {"events": events, "sources": sources, "errors": errors}


def refresh_market_events(db: Session, now: Optional[datetime] = None, **fetchers) -> dict:
    now = now or datetime.now(UTC)
    calendar = build_calendar(now.astimezone(ET).date(), **fetchers)
    missing = [kind for kind in RELEASE_TIME if kind not in calendar["sources"]]
    status = "complete" if not missing else "partial"
    payload = calendar | {"observed_at": now.astimezone(UTC).isoformat(), "status": status, "missing": missing}
    write_audit_log(
        db, event_type=EVENT_TYPE, entity_type="market_event_calendar", action="refresh", status=status,
        message=f"{len(calendar['events'])} scheduled events"
                + (f"; no dates for {', '.join(missing)}" if missing else "."),
        payload=payload,
    )
    db.commit()
    clear_cache()
    return payload


def latest_calendar(db: Session) -> dict:
    row = db.query(AuditLog).filter(AuditLog.event_type == EVENT_TYPE).order_by(AuditLog.id.desc()).first()
    return dict(row.payload or {}) if row is not None else {}


def cached_events(db: Session) -> Optional[list[dict]]:
    """The stored calendar's events (``None`` when never refreshed), cached briefly."""
    with _cache_lock:
        if _cache["at"] is not None and time.monotonic() - _cache["at"] < _CACHE_SECONDS:
            return _cache["events"]
    state = latest_calendar(db)
    events = state.get("events") if state else None
    if events is None and not state:
        # Never refreshed yet: the built-in FOMC schedule is still known.
        events = [{"kind": "fomc", "date": day, "time_et": "14:00"} for day in FOMC_FALLBACK]
    with _cache_lock:
        _cache.update(at=time.monotonic(), events=events)
    return events


def clear_cache() -> None:
    with _cache_lock:
        _cache.update(at=None, events=None)


def _next_session(day: date) -> date:
    day += timedelta(days=1)
    while not is_nyse_session(day):
        day += timedelta(days=1)
    return day


def event_moment(event: dict) -> datetime:
    hour, minute = (int(part) for part in str(event.get("time_et") or "08:30").split(":"))
    return datetime.combine(date.fromisoformat(event["date"]), dtime(hour, minute), ET)


def next_events(events: Optional[list[dict]], now: datetime, limit: int = 3) -> list[dict]:
    upcoming = [event for event in events or [] if event_moment(event) > now]
    return upcoming[:limit]


def event_blackout(events: Optional[list[dict]], now: datetime, *, holds_overnight: bool) -> Optional[dict]:
    """Skip result for a BUY right before a scheduled announcement, else ``None``."""
    if not settings.market_event_blackout_enabled or not events:
        return None
    local = now.astimezone(ET)
    today = local.date()
    for event in events:
        day = date.fromisoformat(event["date"])
        moment = event_moment(event)
        name = EVENT_NAMES.get(event["kind"], event["kind"])
        if event["kind"] == "fomc" and day == today and local < moment + FOMC_SETTLE:
            return {"status": "skipped", "event_blackout": event,
                    "reason": f"{name} at {moment.strftime('%H:%M')} ET today; no new BUYs until "
                              f"{(moment + FOMC_SETTLE).strftime('%H:%M')} ET"}
        if holds_overnight and day > today and _next_session(today) == day:
            return {"status": "skipped", "event_blackout": event,
                    "reason": f"{name} on {day.isoformat()} at {moment.strftime('%H:%M')} ET; an overnight "
                              "position would be held through it"}
    return None


def is_blackout_reason(reason: object) -> bool:
    text = str(reason or "")
    return "no new BUYs until" in text or "position would be held through it" in text


def events_between(events: Optional[list[dict]], start: datetime, end: datetime) -> Optional[list[str]]:
    """Kinds of announcements released inside ``(start, end]``; ``None`` when unknown."""
    if events is None:
        return None
    return [event["kind"] for event in events if start < event_moment(event) <= end]
