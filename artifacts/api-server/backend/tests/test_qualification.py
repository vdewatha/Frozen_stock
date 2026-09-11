import unittest
from datetime import datetime, timezone
from app.services.qualification import QualificationPolicy, evaluate_qualification as evaluate

def evaluate_qualification(evidence):
    return evaluate(evidence, evaluated_at=datetime(2026, 3, 26, tzinfo=timezone.utc))


def evidence():
    return dict(model_id="model-1", model_artifact_sha256="a"*64, dataset_sha256="b"*64,
                ledger_snapshot_sha256="c"*64, mode="paper", evidence_origin="observed_ledger",
                leakage_checks_passed=True, feature_parity_passed=True, costs_included=True,
                reconciliation_passed=True, trusted_data_only=True, critical_incidents=0,
                manual_trades=0, synthetic_trades=0, backfilled_decisions=0, completed_trades=100,
                profit_factor=1.2, net_expectancy=.01, max_drawdown=.1, brier_score=.2,
                sharpe_ratio=1.1, benchmark_sharpe_ratio=.9, regimes=["bull", "bear"],
                training_cutoff="2025-01-01T00:00:00Z", test_start="2025-01-02T00:00:00Z",
                test_end="2025-12-31T00:00:00Z", paper_start="2026-01-01T00:00:00Z",
                paper_end="2026-03-26T00:00:00Z", last_eligible_bar="2026-03-25T00:00:00Z",
                observed_at="2026-03-26T00:00:00Z")


class QualificationTests(unittest.TestCase):
    def test_candidate_never_authorizes_live(self):
        result = evaluate_qualification(evidence())
        self.assertEqual(result["status"], "live_pilot_candidate")
        self.assertFalse(result["live_authorized"])

    def test_every_missing_field_blocks(self):
        for name in evidence():
            candidate = evidence(); del candidate[name]
            with self.subTest(field=name):
                self.assertEqual(evaluate_qualification(candidate)["status"], "blocked")

    def test_thresholds_nonfinite_and_regressions(self):
        failures = dict(completed_trades=99, profit_factor=1.199, net_expectancy=0, max_drawdown=.101,
                        brier_score=.251, critical_incidents=1, regimes=["bull", "bull"],
                        paper_end="2026-03-25T00:00:00Z", test_start="2024-01-01T00:00:00Z",
                        mode="live", evidence_origin="backtest", manual_trades=1)
        for name, value in failures.items():
            self.assertEqual(evaluate_qualification(evidence() | {name: value})["status"], "blocked", name)
        for field in ("net_expectancy", "profit_factor", "max_drawdown", "brier_score", "completed_trades"):
            for value in (float("nan"), float("inf"), True, "1"):
                self.assertEqual(evaluate_qualification(evidence() | {field: value})["status"], "blocked")

    def test_changed_threshold_changes_policy_identity(self):
        first = QualificationPolicy()
        self.assertNotEqual(first.fingerprint, QualificationPolicy(minimum_days=100).fingerprint)
        with self.assertRaises(ValueError): QualificationPolicy(minimum_trades=1)

    def test_review_regressions(self):
        for name in ("minimum_trades", "minimum_days", "minimum_regimes"):
            for value in (float("nan"), float("inf"), 100.5, True):
                with self.assertRaises(ValueError): QualificationPolicy(**{name: value})
        for version in (" ", 123, None):
            with self.assertRaises(ValueError): QualificationPolicy(version=version)
        for regimes in (["bull", "BULL"], ["bull", "bull "]):
            self.assertEqual(evaluate_qualification(evidence() | {"regimes": regimes})["status"], "blocked")
        self.assertEqual(evaluate_qualification(evidence() | {"observed_at": "2027-01-01T00:00:00Z"})["status"], "blocked")
