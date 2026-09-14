import threading
import unittest
from unittest.mock import patch

from celery.contrib.testing.worker import start_worker

from app.tasks.celery_app import celery_app
from app.tasks import jobs


class CeleryConfigurationTests(unittest.TestCase):
    def test_intraday_import_has_a_dedicated_queue(self):
        queues = {queue.name for queue in celery_app.conf.task_queues}

        self.assertIn("market_data", queues)
        self.assertIn("intraday_market_data", queues)
        self.assertEqual(
            celery_app.conf.task_routes["app.tasks.jobs.intraday_market_data_import"],
            {"queue": "intraday_market_data"},
        )

    def test_other_market_data_jobs_stay_on_shared_queue(self):
        routes = celery_app.conf.task_routes

        self.assertEqual(
            routes["app.tasks.jobs.daily_market_data_import"],
            {"queue": "market_data"},
        )
        self.assertEqual(
            routes["app.tasks.jobs.daily_feature_generation"],
            {"queue": "market_data"},
        )

    def test_intraday_schedule_remains_expiring_each_minute(self):
        schedule = celery_app.conf.beat_schedule["intraday-market-data-import"]

        self.assertEqual(schedule["task"], "app.tasks.jobs.intraday_market_data_import")
        self.assertEqual(schedule["schedule"], 60)
        self.assertEqual(schedule["options"], {"expires": 55})

    def test_intraday_task_time_limits_leave_room_for_lease_expiry(self):
        task = jobs.intraday_market_data_import

        self.assertEqual(task.soft_time_limit, jobs.INTRADAY_TASK_SOFT_TIME_LIMIT_SECONDS)
        self.assertEqual(task.time_limit, jobs.INTRADAY_TASK_TIME_LIMIT_SECONDS)
        self.assertGreater(
            jobs.INTRADAY_JOB_LOCK_TTL_SECONDS,
            task.time_limit,
        )

    def test_intraday_lease_uses_short_dedicated_ttl(self):
        class FakeLock:
            def acquire(self, blocking=False):
                return True

        class FakeRedis:
            def __init__(self):
                self.lock_calls = []

            def ping(self):
                return True

            def lock(self, name, timeout, blocking=False):
                self.lock_calls.append((name, timeout, blocking))
                return FakeLock()

        client = FakeRedis()
        with patch.object(jobs.redis.Redis, "from_url", return_value=client):
            intraday_lock = jobs._acquire_job_lock("intraday_market_data_import")
            other_lock = jobs._acquire_job_lock("daily_market_data_import")

        self.assertIsNotNone(intraday_lock)
        self.assertIsNotNone(other_lock)
        self.assertEqual(
            client.lock_calls,
            [
                (
                    "trading:scheduled-job:intraday_market_data_import",
                    jobs.INTRADAY_JOB_LOCK_TTL_SECONDS,
                    False,
                ),
                (
                    "trading:scheduled-job:daily_market_data_import",
                    jobs.REDIS_JOB_LOCK_TTL_SECONDS,
                    False,
                ),
            ],
        )

    def test_intraday_job_runs_while_shared_queue_job_is_busy(self):
        slow_job_started = threading.Event()
        release_slow_job = threading.Event()

        def fake_run_job(job_name, work):
            if job_name == "daily_market_data_import":
                slow_job_started.set()
                if not release_slow_job.wait(timeout=5):
                    raise AssertionError("shared-queue task was not released by the test")
            return {"status": "complete", "job": job_name}

        original_broker_url = celery_app.conf.broker_url
        original_result_backend = celery_app.conf.result_backend
        celery_app.conf.update(
            broker_url="memory://",
            result_backend="cache+memory://",
            task_always_eager=False,
        )

        try:
            with patch.object(jobs, "_run_job", side_effect=fake_run_job):
                try:
                    with start_worker(
                        celery_app,
                        pool="solo",
                        queues=("market_data",),
                        perform_ping_check=False,
                    ):
                        with start_worker(
                            celery_app,
                            pool="solo",
                            queues=("intraday_market_data",),
                            perform_ping_check=False,
                        ):
                            daily_result = jobs.daily_market_data_import.delay()
                            self.assertTrue(
                                slow_job_started.wait(timeout=5),
                                "shared-queue task did not start in the general worker",
                            )

                            intraday_result = jobs.intraday_market_data_import.delay()
                            self.assertEqual(
                                intraday_result.get(timeout=2),
                                {"status": "complete", "job": "intraday_market_data_import"},
                            )
                            self.assertFalse(
                                daily_result.ready(),
                                "shared-queue task finished before the intraday task was consumed",
                            )

                            release_slow_job.set()
                            self.assertEqual(
                                daily_result.get(timeout=2),
                                {"status": "complete", "job": "daily_market_data_import"},
                            )
                finally:
                    release_slow_job.set()
        finally:
            celery_app.conf.update(
                broker_url=original_broker_url,
                result_backend=original_result_backend,
            )