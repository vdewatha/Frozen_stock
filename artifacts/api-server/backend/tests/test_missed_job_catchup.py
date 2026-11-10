"""Missed daily/weekly jobs are caught up once after downtime (2026-11-08/09 outage)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.tasks import catchup
from app.tasks.celery_app import celery_app

ET = ZoneInfo("America/New_York")
# Deployment back up Monday 2026-11-09 09:57 ET after being frozen since Sunday 11-08 10:17 ET.
BOOT = datetime(2026, 11, 9, 9, 57, tzinfo=ET).astimezone(timezone.utc)
DOWN_SINCE = datetime(2026, 11, 8, 10, 17, tzinfo=ET).astimezone(timezone.utc)


class FakeRedis:
    def __init__(self):
        self.hashes, self.keys = {}, {}

    def hset(self, name, key, value):
        self.hashes.setdefault(name, {})[key] = value

    def hget(self, name, key):
        return self.hashes.get(name, {}).get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


def _seed_all_ran_before_outage(client):
    for name in catchup.CATCHUP_ENTRIES:
        task = celery_app.conf.beat_schedule[name]["task"]
        catchup.record_task_success(client, task, {"status": "complete"}, now=DOWN_SINCE - timedelta(minutes=1))


def test_weekend_outage_catches_up_the_weekly_jobs_once_and_staggered():
    client, sent = FakeRedis(), []
    _seed_all_ran_before_outage(client)
    queued = catchup.queue_missed_jobs(
        client, celery_app.conf.beat_schedule, lambda task, **kw: sent.append((task, kw)), now=BOOT,
    )
    names = [item["entry"] for item in queued]
    # Sunday's runs after 10:17 ET and Monday's pre-open dailies were missed.
    for missed in ("signal-autopsy", "market-events", "daily-economic-data-import"):
        assert missed in names
    # The experiment engine was missed too, but it defers itself in-session:
    # the after-close pass queues it instead (see test_deferred_weekly_runs).
    assert "experiment-engine" not in names
    # Saturday's weekly jobs and Sunday 10:00 discovery ran before the freeze.
    for ran in ("yardstick", "weekly-performance-report", "strategy-discovery"):
        assert ran not in names
    autopsy = next(item for item in queued if item["entry"] == "signal-autopsy")
    assert datetime.fromisoformat(autopsy["missed"]).astimezone(ET) == datetime(2026, 11, 8, 11, 30, tzinfo=ET)
    # Staggered, never a burst.
    assert [kw["countdown"] for _, kw in sent] == [i * catchup.STAGGER_SECONDS for i in range(len(sent))]
    # Trading jobs are never caught up.
    assert not any("target_weight_rebalance" in task for task, _ in sent)
    # A second beat start in the same window queues nothing again.
    again = catchup.queue_missed_jobs(client, celery_app.conf.beat_schedule, lambda *a, **k: None, now=BOOT + timedelta(minutes=5))
    assert again == []


def test_jobs_that_ran_on_schedule_are_not_rerun():
    client = FakeRedis()
    for name in catchup.CATCHUP_ENTRIES:
        task = celery_app.conf.beat_schedule[name]["task"]
        catchup.record_task_success(client, task, {"status": "complete"}, now=BOOT - timedelta(minutes=1))
    assert catchup.queue_missed_jobs(client, celery_app.conf.beat_schedule, lambda *a, **k: None, now=BOOT) == []


def test_a_job_due_again_soon_waits_for_its_own_slot():
    client = FakeRedis()
    _seed_all_ran_before_outage(client)
    # At 06:30 ET the 06:45 market-events run is minutes away: no catch-up.
    early = datetime(2026, 11, 9, 6, 30, tzinfo=ET).astimezone(timezone.utc)
    names = [item["entry"] for item in catchup.queue_missed_jobs(
        client, celery_app.conf.beat_schedule, lambda *a, **k: None, now=early)]
    assert "market-events" not in names


def test_failed_results_do_not_count_as_a_run():
    client = FakeRedis()
    catchup.record_task_success(client, "app.tasks.jobs.yardstick_job", {"status": "failed"})
    assert client.hget(catchup.LAST_SUCCESS_KEY, "app.tasks.jobs.yardstick_job") is None
    catchup.record_task_success(client, "app.tasks.jobs.yardstick_job", {"status": "skipped"})
    assert client.hget(catchup.LAST_SUCCESS_KEY, "app.tasks.jobs.yardstick_job") is not None


def test_a_job_with_no_recorded_run_catches_up_its_latest_firing_within_eight_days():
    # The yardstick and dividend check were deployed only on 11-09, so they
    # have never run: their Saturday 11-07 firing is caught up once.
    client = FakeRedis()
    queued = catchup.queue_missed_jobs(client, celery_app.conf.beat_schedule, lambda *a, **k: None, now=BOOT)
    yardstick = next(item for item in queued if item["entry"] == "yardstick")
    assert datetime.fromisoformat(yardstick["missed"]).astimezone(ET) == datetime(2026, 11, 7, 13, 50, tzinfo=ET)
