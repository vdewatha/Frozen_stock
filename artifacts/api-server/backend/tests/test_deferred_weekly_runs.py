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


def test_an_engine_run_where_nothing_could_run_is_not_a_completed_run():
    """2026-11-10/11: three runs ended in "no price history to replay" and each was stamped as the weekly run."""
    failed = {"status": "complete", "planned": 1, "experiments": [],
              "errors": [{"strategy_type": "book", "template": "entry_cluster_cap",
                          "error": "RuntimeError: no price history to replay: 0 of 45 symbols loaded"}]}
    worked = {"status": "complete", "planned": 2, "experiments": [{"strategy_type": "channel_breakout"}],
              "errors": [{"strategy_type": "book", "error": "RuntimeError: x"}]}
    nothing_planned = {"status": "complete", "planned": 0, "experiments": [], "errors": []}

    def run(result):
        with patch.object(jobs, "datetime") as clock, \
                patch.object(jobs, "_run_job", side_effect=lambda name, work: work(_Db())), \
                patch("app.services.experiment_engine.run_experiment_engine", return_value=dict(result)):
            clock.now.return_value = TUESDAY_1620
            return jobs.experiment_engine_job.run()

    out = run(failed)
    assert out["status"] == "deferred" and "0 of 45 symbols loaded" in out["reason"]
    client = FakeRedis()
    catchup.record_task_success(client, ENGINE, out)
    assert catchup._last_success(client, ENGINE) is None           # the after-close catch-up tries it again
    assert run(worked)["status"] == "complete" and run(nothing_planned)["status"] == "complete"


class _Db:
    def commit(self):
        pass


def test_the_widened_learning_set_gets_one_run_this_week():
    """The engine's first real run should not wait for Sunday 11-15 once the fix is deployed."""
    wednesday_close = datetime(2026, 11, 12, 16, 20, tzinfo=ET).astimezone(timezone.utc)
    ran_tuesday = datetime(2026, 11, 11, 16, 40, tzinfo=ET).astimezone(timezone.utc)   # before the window opened
    client = FakeRedis()
    for name in catchup.CATCHUP_ENTRIES:
        catchup.record_task_success(client, celery_app.conf.beat_schedule[name]["task"], {"status": "complete"},
                                    now=wednesday_close - timedelta(minutes=5))
    queued = catchup.queue_missed_jobs(client, celery_app.conf.beat_schedule, lambda *a, **k: None,
                                       now=wednesday_close, durable_last_success=lambda task: ran_tuesday)
    assert [(item["entry"], item["one_off"]) for item in queued if item.get("one_off")] == [
        ("experiment-engine", "20261111-learning-set")]
    # Once a real run is recorded inside the window, a restart never repeats it.
    again = catchup.queue_missed_jobs(FakeRedis(), celery_app.conf.beat_schedule, lambda *a, **k: None,
                                      now=wednesday_close + timedelta(days=1),
                                      durable_last_success=lambda task: wednesday_close + timedelta(minutes=30))
    assert not any(item.get("one_off") for item in again)
