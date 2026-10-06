import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from celery import Celery
from celery.contrib.testing.worker import start_worker
from celery.schedules import crontab
from sqlalchemy.exc import InternalError

from app.tasks.celery_app import celery_app, configured_schedule, OBSERVATION_TASKS
from app.tasks import jobs


class CeleryConfigurationTests(unittest.TestCase):
    def test_observation_schedule_excludes_trading_and_heavy_jobs(self):
        schedule = celery_app.conf.beat_schedule
        filtered = configured_schedule(schedule, observation_only=True)
        self.assertEqual({entry["task"] for entry in filtered.values()}, OBSERVATION_TASKS)
        self.assertIs(configured_schedule(schedule, observation_only=False), schedule)
        self.assertEqual(filtered["iex-research-collection"]["options"], {"expires": 55})
        self.assertNotIn("paper-trading-signal-job", filtered)
        self.assertNotIn("nightly-backtest-job", filtered)

    def test_research_cadence_is_minutely_expiring_and_locked(self):
        entry = celery_app.conf.beat_schedule["iex-research-collection"]
        self.assertEqual(entry["task"], "app.tasks.jobs.iex_research_collection_job")
        self.assertEqual(entry["options"], {"expires": 55})
        self.assertIsInstance(entry["schedule"], crontab)
        self.assertIn("iex_research_collection_job", jobs.REDIS_LOCKED_JOBS)
        self.assertEqual(jobs.iex_research_collection_job.time_limit, jobs.IEX_TASK_TIME_LIMIT_SECONDS)
        self.assertEqual(jobs.iex_research_collection_job.soft_time_limit, jobs.IEX_TASK_SOFT_TIME_LIMIT_SECONDS)
        self.assertEqual(jobs.IEX_JOB_LOCK_TTL_SECONDS, 180)
        self.assertGreater(jobs.IEX_JOB_LOCK_TTL_SECONDS, jobs.iex_research_collection_job.time_limit)
        boundary = datetime(2026, 10, 2, 18, 50, tzinfo=timezone.utc)
        with patch.object(entry["schedule"], "nowfun", return_value=boundary):
            due = entry["schedule"].is_due(boundary.replace(minute=49, second=57))
        self.assertTrue(due.is_due)
        self.assertEqual(due.next, 60)

    def test_iex_feature_refresh_is_throttled(self):
        class FakeRedis:
            def __init__(self, acquired):
                self.acquired = acquired
                self.deleted = []
                self.closed = False

            def set(self, key, value, nx, ex):
                self.args = (key, value, nx, ex)
                return self.acquired

            def delete(self, key):
                self.deleted.append(key)

            def close(self):
                self.closed = True

        client = FakeRedis(True)
        with patch.object(jobs.redis.Redis, "from_url", return_value=client), patch.object(
            jobs.daily_feature_generation, "apply_async", return_value=Mock(id="refresh-1")
        ) as queue:
            self.assertEqual(jobs._queue_feature_refresh(), "refresh-1")
        queue.assert_called_once_with(queue="market_data", expires=60 * 60)
        self.assertEqual(client.args, (jobs.FEATURE_REFRESH_THROTTLE_KEY, "1", True, jobs.FEATURE_REFRESH_THROTTLE_SECONDS))
        self.assertTrue(client.closed)

        client = FakeRedis(False)
        with patch.object(jobs.redis.Redis, "from_url", return_value=client), patch.object(
            jobs.daily_feature_generation, "apply_async"
        ) as queue:
            self.assertIsNone(jobs._queue_feature_refresh())
        queue.assert_not_called()
        self.assertTrue(client.closed)

    def test_intraday_import_has_a_dedicated_queue(self):
        queues = {queue.name for queue in celery_app.conf.task_queues}

        self.assertIn("market_data", queues)
        self.assertIn("intraday_market_data", queues)
        self.assertEqual(
            celery_app.conf.task_routes["app.tasks.jobs.intraday_market_data_import"],
            {"queue": "intraday_market_data"},
        )

    def test_jobs_module_is_registered_for_standalone_workers(self):
        self.assertIn("app.tasks.jobs.strategy_learning_scope_job", celery_app.tasks)
        self.assertIn("app.tasks.jobs.retry_failed_strategy_learning_scopes_job", celery_app.tasks)

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

    def test_model_realization_scoring_is_scheduled_on_learning_queue(self):
        routes = celery_app.conf.task_routes
        entry = celery_app.conf.beat_schedule["model-realization-scoring"]

        self.assertEqual(
            routes["app.tasks.jobs.model_realization_scoring_job"],
            {"queue": "learning"},
        )
        self.assertEqual(entry["task"], "app.tasks.jobs.model_realization_scoring_job")
        self.assertEqual(entry["schedule"], 6 * 60 * 60)

    def test_model_realization_scoring_is_research_only(self):
        db = Mock()
        with patch.object(jobs, "_run_job", side_effect=lambda _name, work: work(db)), patch.object(
            jobs,
            "score_realized_predictions",
            return_value={"checked": 3, "scored": 2, "scored_prediction_ids": [1, 2]},
        ) as score:
            result = jobs.model_realization_scoring_job()

        score.assert_called_once_with(db)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["scored"], 2)
        self.assertTrue(result["paper_only"])
        self.assertFalse(result["live_authorized"])

    def test_feature_generation_refreshes_on_learning_cadence(self):
        entry = celery_app.conf.beat_schedule["daily-feature-generation"]
        self.assertEqual(entry["task"], "app.tasks.jobs.daily_feature_generation")
        self.assertEqual(entry["schedule"], 6 * 60 * 60)

    def test_intraday_schedule_remains_expiring_each_minute(self):
        schedule = celery_app.conf.beat_schedule["intraday-market-data-import"]

        self.assertEqual(schedule["task"], "app.tasks.jobs.intraday_market_data_import")
        self.assertIsInstance(schedule["schedule"], crontab)
        self.assertEqual(schedule["options"], {"expires": 55})

    def test_intraday_schedule_does_not_inherit_startup_second(self):
        schedule = celery_app.conf.beat_schedule["intraday-market-data-import"]["schedule"]
        self.assertIsInstance(schedule, crontab)
        for second in (0, 15, 56, 59):
            with self.subTest(startup_second=second):
                last_run = datetime(2026, 9, 29, 18, 4, second, tzinfo=timezone.utc)
                boundary = datetime(2026, 9, 29, 18, 5, tzinfo=timezone.utc)
                with patch.object(schedule, "nowfun", return_value=boundary):
                    due = schedule.is_due(last_run)
                self.assertTrue(due.is_due)
                self.assertEqual(due.next, 60)
                with patch.object(schedule, "nowfun", return_value=boundary.replace(second=5)):
                    after_dispatch = schedule.is_due(boundary)
                self.assertFalse(after_dispatch.is_due)
                self.assertEqual(after_dispatch.next, 55)

    def test_intraday_task_time_limits_leave_room_for_lease_expiry(self):
        task = jobs.intraday_market_data_import

        self.assertEqual(task.soft_time_limit, jobs.INTRADAY_TASK_SOFT_TIME_LIMIT_SECONDS)
        self.assertEqual(task.time_limit, jobs.INTRADAY_TASK_TIME_LIMIT_SECONDS)
        self.assertGreater(
            jobs.INTRADAY_JOB_LOCK_TTL_SECONDS,
            task.time_limit,
        )
        self.assertTrue(task.acks_late)
        self.assertTrue(task.reject_on_worker_lost)

    def test_learning_tasks_report_progress_and_have_bounded_runtime(self):
        task = jobs.strategy_learning_scope_job

        self.assertTrue(celery_app.conf.task_track_started)
        self.assertTrue(celery_app.conf.worker_send_task_events)
        self.assertTrue(celery_app.conf.task_send_sent_event)
        self.assertEqual(task.soft_time_limit, jobs.LEARNING_TASK_SOFT_TIME_LIMIT_SECONDS)
        self.assertEqual(task.time_limit, jobs.LEARNING_TASK_TIME_LIMIT_SECONDS)
        self.assertTrue(task.acks_late)
        self.assertTrue(task.reject_on_worker_lost)

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
            research_lock = jobs._acquire_job_lock("iex_research_collection_job")
            other_lock = jobs._acquire_job_lock("daily_market_data_import")

        self.assertIsNotNone(intraday_lock)
        self.assertIsNotNone(research_lock)
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
                    "trading:scheduled-job:iex_research_collection_job",
                    jobs.IEX_JOB_LOCK_TTL_SECONDS,
                    False,
                ),
                (
                    "trading:scheduled-job:daily_market_data_import",
                    jobs.REDIS_JOB_LOCK_TTL_SECONDS,
                    False,
                ),
            ],
        )

    def test_redis_lease_retries_transient_startup_failure(self):
        class FakeLock:
            def acquire(self, blocking=False):
                return True

        class FakeRedis:
            def __init__(self):
                self.ping_attempts = 0

            def ping(self):
                self.ping_attempts += 1
                if self.ping_attempts < 3:
                    raise jobs.redis.RedisError("redis warming up")
                return True

            def lock(self, name, timeout, blocking=False):
                return FakeLock()

        client = FakeRedis()
        with patch.object(jobs.redis.Redis, "from_url", return_value=client), patch.object(
            jobs.time, "sleep"
        ) as sleep:
            lock = jobs._acquire_job_lock("iex_research_collection_job")

        self.assertIsNotNone(lock)
        self.assertEqual(client.ping_attempts, 3)
        sleep.assert_any_call(jobs.REDIS_LOCK_RETRY_DELAYS_SECONDS[0])
        sleep.assert_any_call(jobs.REDIS_LOCK_RETRY_DELAYS_SECONDS[1])

    def test_strategy_learning_scope_retries_database_failures(self):
        task = jobs.strategy_learning_scope_job

        self.assertEqual(task.max_retries, 2)
        self.assertTrue(task.retry_backoff)
        self.assertIn(InternalError, task.autoretry_for)

    def test_strategy_learning_failure_retry_is_scheduled(self):
        entry = celery_app.conf.beat_schedule["strategy-learning-failure-retry"]

        self.assertEqual(entry["task"], "app.tasks.jobs.retry_failed_strategy_learning_scopes_job")
        self.assertEqual(entry["schedule"], 60)

    def test_intraday_job_runs_while_shared_queue_job_is_busy(self):
        slow_job_started = threading.Event()
        release_slow_job = threading.Event()
        # Verify queue isolation, not a two-second host scheduling benchmark.
        # The blocked task must outlive every intraday result wait.
        result_timeout = 10

        def fake_run_job(job_name, work):
            if job_name == "daily_market_data_import":
                slow_job_started.set()
                if not release_slow_job.wait(timeout=30):
                    raise AssertionError("shared-queue task was not released by the test")
            return {"status": "complete", "job": job_name}

        test_app = Celery("queue-isolation", broker="memory://", backend="cache+memory://", set_as_current=False)
        test_app.conf.update(
            task_always_eager=False,
            task_routes=celery_app.conf.task_routes,
            worker_prefetch_multiplier=1,
        )
        daily_task = test_app.task(name=jobs.daily_market_data_import.name)(jobs.daily_market_data_import.run)
        intraday_task = test_app.task(name=jobs.intraday_market_data_import.name)(jobs.intraday_market_data_import.run)

        try:
            with patch.object(jobs, "_run_job", side_effect=fake_run_job):
                try:
                    with start_worker(
                        test_app,
                        pool="solo",
                        queues=("market_data",),
                        perform_ping_check=False,
                    ):
                        with start_worker(
                            test_app,
                            pool="solo",
                            queues=("intraday_market_data",),
                            perform_ping_check=False,
                        ):
                            daily_result = daily_task.delay()
                            self.assertTrue(
                                slow_job_started.wait(timeout=result_timeout),
                                "shared-queue task did not start in the general worker",
                            )

                            intraday_result = intraday_task.delay()
                            self.assertEqual(
                                intraday_result.get(timeout=result_timeout),
                                {"status": "complete", "job": "intraday_market_data_import"},
                            )
                            self.assertFalse(
                                daily_result.ready(),
                                "shared-queue task finished before the intraday task was consumed",
                            )

                            release_slow_job.set()
                            self.assertEqual(
                                daily_result.get(timeout=result_timeout),
                                {"status": "complete", "job": "daily_market_data_import"},
                            )
                finally:
                    release_slow_job.set()
        finally:
            test_app.close()
