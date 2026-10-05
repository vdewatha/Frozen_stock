"""Version-pinned NYSE sessions, including exceptional historical closures."""
from datetime import date, datetime, timedelta
from functools import lru_cache

import exchange_calendars as xcals


@lru_cache(maxsize=16)
def _calendar(year: int):
    # Explicit bounds avoid the library's moving, wall-clock-relative defaults.
    return xcals.get_calendar(
        "XNYS", start=f"{year - 1}-01-01", end=f"{year + 1}-12-31"
    )


def is_nyse_session(day: date) -> bool:
    return bool(_calendar(day.year).is_session(day.isoformat()))


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    calendar = _calendar(day.year)
    label = day.isoformat()
    if not calendar.is_session(label):
        return None
    return (
        calendar.session_open(label).to_pydatetime(),
        calendar.session_close(label).to_pydatetime(),
    )


@lru_cache(maxsize=16)
def _closures(year: int) -> frozenset[date]:
    calendar = _calendar(year)
    day = date(year, 1, 1)
    closures = set()
    while day.year == year:
        if day.weekday() < 5 and not calendar.is_session(day.isoformat()):
            closures.add(day)
        day += timedelta(days=1)
    return frozenset(closures)


def nyse_holidays(year: int) -> set[date]:
    return set(_closures(year))
