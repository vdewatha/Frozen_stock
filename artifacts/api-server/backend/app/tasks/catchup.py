"""Catch up daily and weekly jobs whose scheduled run was missed.

Celery beat fires crontab entries only at their scheduled minute and never
backfills. When the deployment was down from Saturday 2026-11-08 10:17 ET to
Monday 09:57 ET, the whole weekend's weekly jobs (yardstick, dividend check,
weekly report, cost calibration, strategy discovery, signal autopsy,
experiment engine, ...) and Monday's pre-open dailies simply never ran.

Two pieces:

* ``record_task_success`` (connected to Celery's ``task_success`` signal)
  stores each task's last successful completion in a Redis hash.
* ``queue_missed_jobs`` runs when the beat launcher starts and again each
  weekday after the close (``missed_job_catchup_job``). For each entry in
  ``CATCHUP_ENTRIES`` whose crontab had a firing since the recorded last
  success, it queues the task once, staggered, so a restart never fires a
  burst of heavy jobs at the same moment.

Strategy discovery and the experiment engine refuse to run during the
session. On 2026-11-10 a 09:01 ET restart queued both in-session; they
returned "skipped" and that counted as a successful run, so neither would
have run until the next Sunday (the engine had never completed a run). Those
runs now report "deferred", which is not a success, they are not queued
during the session, and the after-close pass picks them up.

Only report, research and bookkeeping jobs are listed. Trading jobs (the
target-weight rebalance, the paper cycle) must never run outside their
window, and the heavy retraining/promotion runs simply wait for their next
slot.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

LAST_SUCCESS_KEY = "celery:task:last-success"
CATCHUP_MARKER_PREFIX = "celery:catchup:queued:"
CATCHUP_MARKER_TTL_SECONDS = 6 * 60 * 60
# A missed run older than this is not caught up (the next run supersedes it).
MAX_MISSED_AGE = timedelta(days=8)
# If the next scheduled run is this close, let it run on schedule instead.
NEXT_RUN_GRACE = timedelta(hours=3)
STAGGER_SECONDS = 120
# The beat schedule's wall clock (celery_app.conf.timezone). Firings are
# computed here from the crontab fields rather than through the crontab's own
# time zone, which follows whichever Celery app is current (UTC in a bare
# test process), so the result never depends on import order.
SCHEDULE_TZ = ZoneInfo("America/New_York")
# "skipped" is a run too: usually another worker held the job's lease.
FAILED_STATUSES = {"failed", "error"}
# A job that declined to run at this hour has not run.
NOT_A_RUN_STATUSES = FAILED_STATUSES | {"deferred"}
# Entries whose task defers itself from 30 minutes before the open to the close.
SESSION_DEFERRED_ENTRIES = frozenset({"strategy-discovery", "experiment-engine"})

# Beat entry names (celery_app.conf.beat_schedule keys), in dependency order.
CATCHUP_ENTRIES: tuple[str, ...] = (
    # Pre-open dailies.
    "event-log-daily",
    "market-events",
    "daily-economic-data-import",
    "exploration-universe",
    "daily-news-import",
    "target-weight-daily",
    "defensive-sleeve-daily",
    "market-gauges",
    # After-close dailies.
    "feed-hygiene",
    "loss-feedback",
    "paper-trade-scorecard-snapshot",
    "experiment-verdict",
    "sleeve-guardrail",
    "gate-cost",
    "setup-shadow",
    "alert-daily-digest",
    "daily-market-regime-detection",
    # Weekly (Saturday, then Sunday).
    "paper-cost-calibration",
    "dividend-check",
    "yardstick",
    "weekly-performance-report",
    "engine-audit",
    "event-log-weekly",
    "strategy-discovery",
    "signal-autopsy",
    "experiment-engine",
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def record_task_success(client, task_name: str, result: Any, now: Optional[datetime] = None) -> None:
    """Store the task's completion time unless its result reports a failure or skip."""
    if isinstance(result, dict) and str(result.get("status", "")).lower() in NOT_A_RUN_STATUSES:
        return
    stamp = _utc(now or datetime.now(timezone.utc)).isoformat()
    client.hset(LAST_SUCCESS_KEY, task_name, stamp)


def _last_success(client, task_name: str) -> Optional[datetime]:
    raw = client.hget(LAST_SUCCESS_KEY, task_name)
    if not raw:
        return None
    try:
        return _utc(datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw))
    except ValueError:
        return None


def next_firing(schedule, after: datetime, tz: ZoneInfo = SCHEDULE_TZ) -> datetime:
    """The crontab's first firing strictly after ``after`` (UTC), in ``tz`` wall-clock time."""
    local_after = _utc(after).astimezone(tz)
    hours, minutes = sorted(schedule.hour), sorted(schedule.minute)
    day = local_after.date()
    for _ in range(400):
        # Celery numbers weekdays from Sunday = 0; Python from Monday = 0.
        if ((day.weekday() + 1) % 7 in schedule.day_of_week
                and day.day in schedule.day_of_month and day.month in schedule.month_of_year):
            for hour in hours:
                for minute in minutes:
                    fire = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                    if fire > local_after:
                        return _utc(fire)
        day += timedelta(days=1)
    raise ValueError("crontab has no firing within 400 days")


def missed_firing(schedule, last_success: Optional[datetime], now: datetime) -> Optional[datetime]:
    """The latest scheduled firing after ``last_success`` (or within ``MAX_MISSED_AGE``), if any."""
    floor = now - MAX_MISSED_AGE
    since = max(last_success, floor) if last_success else floor
    # Daily/weekly entries fire a handful of times in eight days.
    latest, cursor = None, since
    for _ in range(64):
        fire = next_firing(schedule, cursor)
        if fire > now:
            break
        latest = cursor = fire
    return latest


def in_session_deferral_window(now: datetime) -> bool:
    """From 30 minutes before the regular open to the close (the jobs' own rule)."""
    from app.services.intraday_data import session_bounds

    now = _utc(now)
    bounds = session_bounds(now.astimezone(SCHEDULE_TZ).date())
    return bounds is not None and bounds[0] - timedelta(minutes=30) <= now < bounds[1]


def queue_missed_jobs(
    client,
    beat_schedule: dict,
    send_task: Callable[..., Any],
    *,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Queue each allowlisted entry with a missed firing once; return what was queued."""
    now = _utc(now or datetime.now(timezone.utc))
    queued: list[dict] = []
    for name in CATCHUP_ENTRIES:
        entry = beat_schedule.get(name)
        if not entry:
            continue  # excluded on this host (PAPER_BEAT_EXCLUDE) or renamed
        schedule = entry["schedule"]
        if not all(hasattr(schedule, field) for field in ("hour", "minute", "day_of_week", "day_of_month", "month_of_year")):
            continue
        task = entry["task"]
        missed = missed_firing(schedule, _last_success(client, task), now)
        if missed is None:
            continue
        if next_firing(schedule, now) - now <= NEXT_RUN_GRACE:
            continue
        if name in SESSION_DEFERRED_ENTRIES and in_session_deferral_window(now):
            continue  # it would only defer itself; the after-close pass queues it
        # One catch-up per entry per window, even if beat restarts repeatedly.
        if not client.set(f"{CATCHUP_MARKER_PREFIX}{name}", missed.isoformat(), nx=True, ex=CATCHUP_MARKER_TTL_SECONDS):
            continue
        countdown = len(queued) * STAGGER_SECONDS
        options = dict(entry.get("options") or {})
        expires = options.pop("expires", None)
        send_task(
            task,
            args=entry.get("args", ()),
            kwargs=entry.get("kwargs", {}),
            countdown=countdown,
            expires=countdown + (expires if isinstance(expires, (int, float)) else 2 * 60 * 60),
            **{key: value for key, value in options.items() if key in {"queue", "routing_key", "priority"}},
        )
        queued.append({"entry": name, "task": task, "missed": missed.isoformat(), "countdown": countdown})
    return queued
