"""A thin book symbol gets the whole session to print a fresh bar (SGOV, 2026-11-10 10:30 ET)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.tasks.catchup import next_firing
from app.tasks.celery_app import celery_app

ET = ZoneInfo("America/New_York")


def _runs_on(day):
    times = set()
    for name in ("target-weight-rebalance", "target-weight-rebalance-topup"):
        schedule = celery_app.conf.beat_schedule[name]["schedule"]
        cursor = datetime(day.year, day.month, day.day, 0, 0, tzinfo=ET).astimezone(timezone.utc)
        while True:
            fire = next_firing(schedule, cursor)
            local = fire.astimezone(ET)
            if local.date() != day:
                break
            times.add(local.strftime("%H:%M"))
            cursor = fire
    return sorted(times)


def test_the_tranche_day_retries_through_the_session():
    tuesday = datetime(2026, 11, 17, tzinfo=ET).date()
    assert _runs_on(tuesday) == ["10:30", "11:30", "12:30", "13:00", "13:30", "14:30"]


def test_every_run_stays_clear_of_the_auctions():
    for hhmm in _runs_on(datetime(2026, 11, 17, tzinfo=ET).date()):
        assert "10:00" <= hhmm <= "15:30"
