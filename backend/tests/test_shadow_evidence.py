from datetime import timedelta
import unittest
from sqlalchemy import select
import test_crypto_pipeline as fixtures
from app.models.shadow import ShadowDecision, ShadowModelBinding
from app.models.crypto_data import CryptoCandle
from app.models.models import ResearchModelRun
from app.services.crypto_pipeline import run_crypto_cycle
from app.services.instruments import Candle, Timeframe
from app.integrations.kraken import BTC_USD
from app.services.shadow_evidence import shadow_evidence_report


class ShadowEvidenceTests(unittest.TestCase):
    tearDown = fixtures.CryptoPipelineTests.tearDown

    def setUp(self):
        fixtures.CryptoPipelineTests.setUp(self)
        # Historical deterministic fixture: registration genuinely precedes its
        # synthetic clock's observations. Production timestamps are server-owned.
        with self.sessions.begin() as db:
            binding = db.get(ShadowModelBinding, self.binding_id)
            binding.created_at = (self.clock - timedelta(days=1)).replace(tzinfo=None)
            db.scalar(select(ResearchModelRun)).created_at = (self.clock - timedelta(days=2)).replace(tzinfo=None)

    def observe(self):
        run_crypto_cycle(self.sessions, client=self.client, as_of=self.clock)

    def advance(self):
        # The real training fixture uses a five-bar prediction horizon.
        for hour in range(5):
            self.client.ohlc.return_value.append(Candle(BTC_USD, Timeframe.HOUR,
                self.clock.replace(minute=0) + timedelta(hours=hour), 102, 110, 95, 105 + hour, 1500))
        self.clock += timedelta(hours=5)
        self.observe()

    def report(self, as_of=None):
        with self.sessions() as db:
            result = shadow_evidence_report(db, self.binding_id, as_of=as_of or self.clock)
            self.assertFalse(db.new)
            self.assertFalse(db.dirty)
            return result

    def test_actual_pipeline_metrics_and_exact_version(self):
        self.observe()
        self.advance()
        report = self.report()
        self.assertTrue(report["binding_integrity_passed"])
        self.assertEqual(report["counts"]["observed"], 2)
        self.assertEqual(report["counts"]["scored"], 1)
        self.assertEqual(report["counts"]["pending"], 1)
        self.assertEqual(report["observation_window"]["observed_span_seconds"], 18000)
        self.assertEqual(report["observation_window"]["missing_hourly_observations_within_span"], 4)
        self.assertIsNotNone(report["prediction_metrics"]["brier_score"])
        self.assertFalse(report["eligible_for_qualification"])
        self.assertFalse(report["live_authorized"])
        self.assertEqual(report["qualification_policy"]["required_completed_paper_trades"], 100)
        self.assertEqual(report["qualification_policy"]["required_observed_paper_days"], 84)

    def test_time_passing_never_manufactures_observation_days(self):
        self.observe()
        self.advance()
        report = self.report(as_of=self.clock + timedelta(days=100))
        self.assertEqual(report["observation_window"]["observed_span_days"], 5 / 24)
        self.assertEqual(report["counts"]["observed"], 2)
        self.assertEqual(report["counts"]["matured_unscored"], 1)
        self.assertFalse(report["eligible_for_qualification"])

    def test_backfilled_and_future_records_excluded(self):
        self.observe()
        self.advance()
        with self.sessions.begin() as db:
            first = db.scalar(select(ShadowDecision).order_by(ShadowDecision.id))
            first.backfilled = True
        report = self.report(as_of=self.clock - timedelta(minutes=30))
        self.assertEqual(report["counts"]["excluded_backfilled"], 1)
        self.assertEqual(report["counts"]["excluded_future"], 1)
        self.assertEqual(report["counts"]["observed"], 0)
        self.assertIsNone(report["prediction_metrics"])

    def test_tampered_outcome_excluded_from_metrics(self):
        self.observe()
        self.advance()
        with self.sessions.begin() as db:
            first = db.scalar(select(ShadowDecision).order_by(ShadowDecision.id))
            first.outcome = dict(first.outcome, brier_score=0)
        report = self.report()
        self.assertEqual(report["counts"]["invalid_outcomes"], 1)
        self.assertEqual(report["counts"]["scored"], 0)
        self.assertIsNone(report["prediction_metrics"])

    def test_tampered_candle_hash_and_binding_fail_closed(self):
        self.observe()
        with self.sessions.begin() as db:
            db.scalar(select(CryptoCandle)).content_sha256 = "0" * 64
        self.assertEqual(self.report()["counts"]["excluded_invalid"], 1)
        with self.sessions.begin() as db:
            binding = db.get(ShadowModelBinding, self.binding_id)
            binding.spec = dict(binding.spec, intercept=999)
        report = self.report()
        self.assertFalse(report["binding_integrity_passed"])
        self.assertEqual(report["status"], "blocked_invalid_binding")

    def test_empty_report_does_not_invent_metrics(self):
        report = self.report()
        self.assertEqual(report["counts"]["observed"], 0)
        self.assertIsNone(report["observation_window"]["first_observed_at"])
        self.assertIsNone(report["prediction_metrics"])

    def test_pre_registration_observation_cannot_be_forward_evidence(self):
        self.observe()
        with self.sessions.begin() as db:
            db.get(ShadowModelBinding, self.binding_id).created_at = (self.clock + timedelta(hours=1)).replace(tzinfo=None)
        report = self.report()
        self.assertEqual(report["counts"]["observed"], 0)
        self.assertEqual(report["counts"]["excluded_invalid"], 1)


if __name__ == "__main__":
    unittest.main()
