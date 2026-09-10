import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from datetime import timedelta
from app.services.research_training_v3 import train_research_v3
from app.services.comparison_model import load_comparison_model, comparison_prediction, validate_numeric
from test_research_training_v2 import prices
import test_paper_comparison as comparison_fixtures


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        manifest = train_research_v3(prices(1800), self.temp.name, source="synthetic-test", horizon=5)
        self.directory = Path(self.temp.name) / manifest["run_id"]
        self.pin = hashlib.sha256((self.directory / "manifest.json").read_bytes()).hexdigest()
        self.kwargs = dict(fee_rate=".01", slippage_rate=".001", source_claim="synthetic-test", manifest_sha256=self.pin, expected_horizon=5, minimum_history=1800)
    def tearDown(self): self.temp.cleanup()

    def test_hash_and_cost_and_provenance_pins(self):
        manifest, spec = load_comparison_model(self.directory, **self.kwargs)
        self.assertFalse(manifest["eligible_for_trading"])
        for changes in ({"fee_rate": ".008"}, {"source_claim": "different"}, {"manifest_sha256": "0" * 64}, {"minimum_history": 8760}):
            with self.assertRaises(ValueError): load_comparison_model(self.directory, **(self.kwargs | changes))
        (self.directory / "calibrated_model.json").write_text("{}")
        with self.assertRaises(ValueError): load_comparison_model(self.directory, **self.kwargs)

    def test_numeric_tree_rejects_cycles_and_nonfinite(self):
        _, spec = load_comparison_model(self.directory, **self.kwargs)
        spec["base"] = {"kind": "random_forest", "trees": [{"left": [0], "right": [0], "feature": [0], "threshold": [0.], "probability": [.5]}]}
        with self.assertRaises(ValueError): validate_numeric(spec)
        spec["mean_loss"] = float("nan")
        with self.assertRaises(ValueError): validate_numeric(spec)

    def test_forward_inference_and_late_gate(self):
        fixture = comparison_fixtures.ComparisonTests()
        fixture.setUp()
        try:
            rows = fixture.rows()
            now = fixture.start + timedelta(hours=60, minutes=1)
            result = comparison_prediction(self.directory, rows, observed_at=now, **self.kwargs)
            self.assertTrue(0 <= result["ml_probability"] <= 1)
            self.assertTrue(result["ml_model_id"].endswith(self.pin))
            self.assertFalse(result["eligible_for_trading"])
            with self.assertRaises(ValueError):
                comparison_prediction(self.directory, rows, observed_at=now + timedelta(minutes=10), **self.kwargs)
        finally: fixture.tearDown()
