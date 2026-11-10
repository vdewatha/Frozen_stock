"""The experiment engine never ran (2026-11-10).

Its Sunday 11-08 12:00 run was lost to the freeze. The 08:58 ET boot's
catch-up queued it, it landed inside the session and skipped, and the
catch-up recorded the skip as a run, so it would not run before Sunday 11-15.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.tasks import catchup, jobs
from app.tasks.celery_app import celery_app

ET = ZoneInfo("America/New_York")
ENGINE_TASK = celery_app.conf.beat_schedule["experiment-engine"]["task"]
AFTER_PUBLISH = datetime(2026, 11, 10, 16, 10, tzinfo=ET).astimezone(timezone.utc)


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

    def close(self):
        pass


def _all_ran_recently(client, now):
    for name in catchup.CATCHUP_ENTRIES:
        task = celery_app.conf.beat_schedule[name]["task"]
        catchup.record_task_success(client, task, {"status": "complete"}, now=now - timedelta(minutes=5))


def test_a_deferral_is_not_recorded_as_a_run():
    client = FakeRedis()
    catchup.record_task_success(client, ENGINE_TASK, {"status": "deferred"})
    assert catchup._last_success(client, ENGINE_TASK) is None
    catchup.record_task_success(client, ENGINE_TASK, {"status": "skipped"})  # lease held elsewhere: a run
    assert catchup._last_success(client, ENGINE_TASK) is not None


def test_the_lost_engine_run_is_queued_once_after_the_publish():
    client = FakeRedis()
    # Today's in-session skip was recorded as a run, like every other job.
    _all_ran_recently(client, AFTER_PUBLISH)
    sent = []
    queued = catchup.queue_missed_jobs(client, celery_app.conf.beat_schedule,
                                       lambda task, **kw: sent.append(task), now=AFTER_PUBLISH)
    assert [item["entry"] for item in queued] == ["experiment-engine"]
    assert sent == [ENGINE_TASK]
    # A later restart (ordinary dailies may be due again by then) never repeats it.
    again = catchup.queue_missed_jobs(client, celery_app.conf.beat_schedule,
                                      lambda task, **kw: sent.append(task), now=AFTER_PUBLISH + timedelta(hours=1))
    assert not any(item.get("one_off") or item["entry"] == "experiment-engine" for item in again)
    assert sent.count(ENGINE_TASK) == 1


@pytest.mark.parametrize("now", [
    datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc),   # before the lost run
    datetime(2026, 11, 15, 17, 0, tzinfo=timezone.utc),  # its own Sunday slot
    datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
])
def test_the_one_off_stays_inside_its_window(now):
    client = FakeRedis()
    _all_ran_recently(client, now)
    queued = catchup.queue_missed_jobs(client, celery_app.conf.beat_schedule, lambda *a, **k: None, now=now)
    assert all(item.get("one_off") is None for item in queued)


def _in_session(now):
    return (now - timedelta(hours=1), now + timedelta(hours=2))


@pytest.mark.parametrize("job,task_name", [
    (jobs.experiment_engine_job, "experiment_engine_job"),
    (jobs.strategy_discovery_job, "strategy_discovery_job"),
])
def test_an_in_session_weekly_job_requeues_itself_once_after_the_close(job, task_name):
    client = FakeRedis()
    now = datetime.now(timezone.utc)
    bounds = _in_session(now)
    with patch("app.services.intraday_data.session_bounds", return_value=bounds), \
            patch.object(jobs.redis.Redis, "from_url", return_value=client), \
            patch.object(job, "apply_async") as apply_async:
        first = job()
        second = job()
    assert first["status"] == "deferred" and first["requeued"] is True
    assert second["status"] == "deferred" and second["requeued"] is False
    apply_async.assert_called_once()
    kwargs = apply_async.call_args.kwargs
    assert kwargs["eta"] == bounds[1] + jobs.DEFER_PAST_CLOSE
    assert kwargs["queue"] == "learning"
    # The catch-up does not count the deferral as a run.
    recorder = FakeRedis()
    catchup.record_task_success(recorder, f"app.tasks.jobs.{task_name}", first)
    assert catchup._last_success(recorder, f"app.tasks.jobs.{task_name}") is None


def test_a_redis_outage_still_defers_without_raising():
    now = datetime.now(timezone.utc)
    with patch("app.services.intraday_data.session_bounds", return_value=_in_session(now)), \
            patch.object(jobs.redis.Redis, "from_url", side_effect=ConnectionError("down")), \
            patch.object(jobs.experiment_engine_job, "apply_async") as apply_async:
        result = jobs.experiment_engine_job()
    assert result["status"] == "deferred" and result["requeued"] is False
    apply_async.assert_not_called()
