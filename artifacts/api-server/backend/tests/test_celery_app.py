import unittest

from app.tasks.celery_app import celery_app


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