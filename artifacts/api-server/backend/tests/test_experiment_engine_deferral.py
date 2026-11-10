"""The experiment engine never ran (2026-11-10).

Its Sunday 11-08 12:00 run was lost to the freeze. The 09:01 ET boot's
catch-up queued it, it landed inside the session and skipped, and the
catch-up recorded the skip as a run. #68 stops that happening again; this
one-off replaces the run that stamp still hides (otherwise none before 11-15).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.tasks import catchup
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


def test_the_one_off_waits_for_the_close():
    in_session = datetime(2026, 11, 10, 14, 0, tzinfo=ET).astimezone(timezone.utc)
    client = FakeRedis()
    _all_ran_recently(client, in_session)
    queued = catchup.queue_missed_jobs(client, celery_app.conf.beat_schedule, lambda *a, **k: None, now=in_session)
    assert all(item.get("one_off") is None for item in queued)
    # The marker is untouched, so the after-close pass still runs it.
    after = catchup.queue_missed_jobs(client, celery_app.conf.beat_schedule, lambda *a, **k: None, now=AFTER_PUBLISH)
    assert [item["entry"] for item in after if item.get("one_off")] == ["experiment-engine"]
