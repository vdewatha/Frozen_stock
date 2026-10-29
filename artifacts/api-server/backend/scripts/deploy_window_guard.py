"""Refuse a deployment build during NYSE market hours.

Replit publish runs the [deployment] build command before anything restarts,
so this script runs first in that command: between 09:25 and 16:05
America/New_York on an NYSE session day it exits 1 with a message and the
publish fails while the running paper bot keeps trading. Set
PAPER_ALLOW_MARKET_HOURS_DEPLOY=true to override.

Pure standard library (no app imports, database or network) so it can never
break an off-hours build. The session calendar is the regular NYSE holiday
rules; unscheduled closures are treated as session days (the safe side).

Usage: python3 deploy_window_guard.py [--now 2026-10-29T10:00:00-04:00]
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import os
import sys

OVERRIDE_ENV = "PAPER_ALLOW_MARKET_HOURS_DEPLOY"
WINDOW_START = time(9, 25)
WINDOW_END = time(16, 5)
UTC = timezone.utc


def _eastern_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:
        return None


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _to_eastern_fallback(moment: datetime) -> datetime:
    """US Eastern time without tz data (DST: 2nd Sunday Mar to 1st Sunday Nov, 02:00 local)."""
    utc = moment.astimezone(UTC).replace(tzinfo=None)
    year = utc.year
    dst_start = datetime.combine(_nth_weekday(year, 3, 6, 2), time(7))  # 02:00 EST in UTC
    dst_end = datetime.combine(_nth_weekday(year, 11, 6, 1), time(6))  # 02:00 EDT in UTC
    offset = -4 if dst_start <= utc < dst_end else -5
    return (utc + timedelta(hours=offset)).replace(tzinfo=timezone(timedelta(hours=offset)))


def to_eastern(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    tz = _eastern_tz()
    return moment.astimezone(tz) if tz is not None else _to_eastern_fallback(moment)


def _easter(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> set[date]:
    holidays = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    new_year = date(year, 1, 1)
    # NYSE does not close the preceding Friday when Jan 1 is a Saturday.
    if new_year.weekday() != 5:
        holidays.add(_observed(new_year))
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    return holidays


def is_session_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def in_blocked_window(moment: datetime) -> bool:
    local = to_eastern(moment)
    return is_session_day(local.date()) and WINDOW_START <= local.time() < WINDOW_END


def check(moment: datetime, environ: dict | None = None) -> tuple[int, str]:
    environ = os.environ if environ is None else environ
    local = to_eastern(moment)
    stamp = local.strftime("%Y-%m-%d %H:%M %Z").strip()
    if not in_blocked_window(moment):
        return 0, f"deploy window guard: {stamp} is outside NYSE market hours; build allowed."
    if str(environ.get(OVERRIDE_ENV, "")).strip().lower() == "true":
        return 0, f"deploy window guard: {stamp} is during market hours; {OVERRIDE_ENV}=true override accepted."
    return 1, (
        f"deploy window guard: refusing to deploy at {stamp}. Publishing restarts the paper bot; "
        f"deploys are blocked {WINDOW_START:%H:%M}-{WINDOW_END:%H:%M} America/New_York on NYSE session days. "
        f"Publish after {WINDOW_END:%H:%M} ET, or set {OVERRIDE_ENV}=true to override."
    )


def main(argv: list[str]) -> int:
    moment = datetime.now(UTC)
    if "--now" in argv:
        try:
            moment = datetime.fromisoformat(argv[argv.index("--now") + 1])
        except (IndexError, ValueError):
            print("deploy window guard: --now needs an ISO-8601 timestamp", file=sys.stderr)
            return 2
    try:
        code, message = check(moment)
    except Exception as exc:  # pragma: no cover - defensive
        # A guard bug must not break an off-hours build.
        print(f"deploy window guard: check unavailable ({type(exc).__name__}); build allowed.", file=sys.stderr)
        return 0
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
