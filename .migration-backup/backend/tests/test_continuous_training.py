import importlib.util
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("continuous_training", Path(__file__).parents[1] / "scripts/run_continuous_training.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class ContinuousTrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)
        self.state = worker.open_state(self.output)
        self.now = datetime(2026, 9, 5, 4, tzinfo=timezone.utc)
        self.loader = Mock(return_value=("2026-09-05T03:00:00+00:00", object()))
        self.trainer = Mock(side_effect=self.publish)

    @staticmethod
    def publish(frame, output, **kwargs):
        directory = output / "candidate"
        directory.mkdir(parents=True)
        (directory / "dataset.csv").write_bytes(b"data")
        manifest = {"run_id": "candidate", "eligible_for_trading": False, "status": "experimental",
                    "files": {"dataset.csv": hashlib.sha256(b"data").hexdigest()}}
        (directory / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def cycle(self, **kwargs):
        return worker.training_cycle(self.state, self.output, None, self.now,
                                     loader=self.loader, trainer=self.trainer, **kwargs)

    def test_restart_skips_completed_cutoff_and_trains_new_cutoff(self):
        result = self.cycle()
        self.assertFalse(result["binding_changed"])
        self.assertFalse(result["live_authorized"])
        self.state.close()
        self.state = worker.open_state(self.output)
        self.assertEqual(self.cycle()["reason"], "cutoff_already_trained")
        self.assertEqual(self.trainer.call_count, 1)
        self.loader.return_value = ("2026-09-05T04:00:00+00:00", object())
        self.assertEqual(self.cycle()["status"], "completed")
        self.assertEqual(self.trainer.call_count, 2)

    def test_failure_retries_are_bounded(self):
        self.trainer.side_effect = ValueError("sensitive details never emitted")
        for _ in range(3):
            self.assertEqual(self.cycle()["error_type"], "ValueError")
        self.assertEqual(self.cycle()["reason"], "retry_limit")
        self.assertEqual(self.trainer.call_count, 3)

    def test_completed_artifact_tampering_is_detected(self):
        self.cycle()
        artifact = next(self.output.glob("*/candidate/dataset.csv"))
        artifact.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.cycle()
        self.assertEqual(self.trainer.call_count, 1)

    def test_crashed_attempt_counts_toward_limit(self):
        self.state.execute("INSERT INTO candidates VALUES (?,3,'running',NULL,NULL)", (self.loader.return_value[0],))
        self.state.commit()
        self.assertEqual(self.cycle()["reason"], "retry_limit")
        self.trainer.assert_not_called()

    def test_minimum_interval(self):
        self.cycle()
        self.loader.return_value = ("2026-09-05T04:00:00+00:00", object())
        self.assertEqual(self.cycle(interval_hours=2)["reason"], "training_interval")
        self.assertEqual(self.trainer.call_count, 1)

    def test_recovers_atomic_publication_without_retraining(self):
        directory = self.output / hashlib.sha256(self.loader.return_value[0].encode()).hexdigest() / "candidate"
        directory.mkdir(parents=True)
        (directory / "dataset.csv").write_bytes(b"data")
        (directory / "manifest.json").write_text(json.dumps({"run_id": "candidate",
            "eligible_for_trading": False, "status": "experimental",
            "files": {"dataset.csv": hashlib.sha256(b"data").hexdigest()}}))
        self.assertEqual(self.cycle()["status"], "completed")
        self.trainer.assert_not_called()

    def test_corrupt_publication_never_retrains_or_reports_completion(self):
        directory = self.output / hashlib.sha256(self.loader.return_value[0].encode()).hexdigest() / "candidate"
        directory.mkdir(parents=True)
        (directory / "dataset.csv").write_bytes(b"changed")
        (directory / "manifest.json").write_text(json.dumps({"run_id": "candidate",
            "eligible_for_trading": False, "status": "experimental",
            "files": {"dataset.csv": hashlib.sha256(b"data").hexdigest()}}))
        with self.assertRaises(ValueError):
            self.cycle()
        self.trainer.assert_not_called()

    def test_stale_or_corrupt_data_never_trains(self):
        self.loader.side_effect = ValueError("stale")
        with self.assertRaises(ValueError):
            self.cycle()
        self.trainer.assert_not_called()

    def test_source_is_read_only_and_is_not_created(self):
        source = self.output / "source.sqlite"
        with sqlite3.connect(source) as db:
            db.execute("CREATE TABLE data (value INTEGER)")
        engine = worker.readonly_source(source)
        try:
            with engine.connect() as connection:
                with self.assertRaises(Exception):
                    connection.exec_driver_sql("INSERT INTO data VALUES (1)")
        finally:
            engine.dispose()
        with self.assertRaises(FileNotFoundError):
            worker.readonly_source(self.output / "missing.sqlite")


if __name__ == "__main__":
    unittest.main()
