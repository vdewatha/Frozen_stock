"""2026-11-10: the experiment engine had never completed a run.

A 09:01 ET restart's catch-up queued strategy discovery and the experiment
engine in-session; both declined ("skipped") and the catch-up counted that as
a successful run, so neither would have run until the next Sunday.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.tasks import catchup, jobs
from app.tasks.celery_app import celery_app
from tests.test_missed_job_catchup import FakeRedis

ET = ZoneInfo("America/New_York")
TUESDAY_0949 = datetime(2026, 11, 10, 9, 49, tzinfo=ET).astimezone(timezone.utc)
TUESDAY_1620 = datetime(2026, 11, 10, 16, 20, tzinfo=ET).astimezone(timezone.utc)
ENGINE = "app.tasks.jobs.experiment_engine_job"
DISCOVERY = "app.tasks.jobs.strategy_discovery_job"


def _all_ran_last_week(client):
    for name in catchup.CATCHUP_ENTRIES:
        task = celery_app.conf.beat_schedule[name]["task"]
        catchup.record_task_success(client, task, {"status": "complete"},
                                    now=datetime(2026, 11, 1, 18, 0, tzinfo=ET).astimezone(timezone.utc))


def test_a_deferred_run_is_not_a_success():
    client = FakeRedis()
    catchup.record_task_success(client, ENGINE, {"status": "deferred", "reason": "in session"})
    assert catchup._last_success(client, ENGINE) is None
    catchup.record_task_success(client, ENGINE, {"status": "skipped", "reason": "lease held"})
    assert catchup._last_success(client, ENGINE) is not None  # a lease skip still counts


def test_session_deferred_jobs_wait_for_the_after_close_pass():
    client = FakeRedis()
    _all_ran_last_week(client)
    morning = [item["entry"] for item in catchup.queue_missed_jobs(
        client, celery_app.conf.beat_schedule, lambda *a, **k: None, now=TUESDAY_0949)]
    assert "experiment-engine" not in morning and "strategy-discovery" not in morning
    assert "signal-autopsy" in morning  # runs any time
    sent = []
    evening = [item["entry"] for item in catchup.queue_missed_jobs(
        client, celery_app.conf.beat_schedule, lambda task, **kw: sent.append(task), now=TUESDAY_1620)]
    assert "experiment-engine" in evening and "strategy-discovery" in evening
    assert ENGINE in sent and DISCOVERY in sent


def test_jobs_report_deferred_in_session():
    with patch.object(jobs, "datetime") as clock:
        clock.now.return_value = TUESDAY_0949
        assert jobs.experiment_engine_job.run()["status"] == "deferred"
        assert jobs.strategy_discovery_job.run()["status"] == "deferred"


def test_after_close_catchup_is_scheduled_and_never_caught_up_itself():
    entry = celery_app.conf.beat_schedule["missed-job-catchup"]
    assert entry["task"] == "app.tasks.jobs.missed_job_catchup_job"
    assert 16 in entry["schedule"].hour and "missed-job-catchup" not in catchup.CATCHUP_ENTRIES
