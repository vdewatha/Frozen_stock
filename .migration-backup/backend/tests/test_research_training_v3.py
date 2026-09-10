import json
import hashlib
import io
import pandas as pd
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from app.services.research_training_v2 import prepare_frame, _model, _fit_predict, FEATURES
from app.services.research_training_v3 import partitions, train_research_v3, export_base, predict_portable
from test_research_training_v2 import prices


class V3Tests(unittest.TestCase):
    def test_verified_source_snapshot_is_preserved_and_mismatch_rejected(self):
        snapshot = prices(1800).to_csv(index=False).encode()
        parsed = pd.read_csv(io.BytesIO(snapshot))
        with tempfile.TemporaryDirectory() as temporary:
            manifest = train_research_v3(parsed, temporary, source="synthetic-test", horizon=5,
                fixed_model="random_forest", source_snapshot=snapshot)
            self.assertEqual(manifest["dataset_sha256"], hashlib.sha256(snapshot).hexdigest())
            self.assertEqual((Path(temporary) / manifest["run_id"] / "dataset.csv").read_bytes(), snapshot)
            changed = parsed.copy()
            changed.loc[0, "close"] += 1
            with self.assertRaisesRegex(ValueError, "snapshot differs"):
                train_research_v3(changed, temporary, source="synthetic-test", horizon=5,
                    fixed_model="random_forest", source_snapshot=snapshot)

    def test_fixed_challenger_has_distinct_identity_and_loads(self):
        from app.services.comparison_model import load_comparison_model
        import app.services.research_training_v3 as service
        with tempfile.TemporaryDirectory() as temp:
            automatic = train_research_v3(prices(1800), temp, source="synthetic-test", horizon=5)
            with patch.object(service, "split_plan", side_effect=AssertionError("No family selection allowed")):
                fixed = train_research_v3(prices(1800), temp, source="synthetic-test", horizon=5, fixed_model="random_forest")
            self.assertNotEqual(automatic["run_id"], fixed["run_id"])
            self.assertNotIn("selection_policy", automatic)
            self.assertEqual(fixed["selection_policy"], "fixed_random_forest_v1")
            self.assertEqual(fixed["walkforward_metrics"], {})
            directory = Path(temp) / fixed["run_id"]
            pin = hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
            manifest, spec = load_comparison_model(directory, manifest_sha256=pin,
                source_claim="synthetic-test", fee_rate=.01, slippage_rate=.001,
                expected_horizon=5, minimum_history=1800)
            self.assertEqual(spec["base"]["kind"], "random_forest")
            self.assertFalse(manifest["eligible_for_trading"])
            with self.assertRaises(FileExistsError):
                train_research_v3(prices(1800), temp, source="synthetic-test", horizon=5, fixed_model="random_forest")

    def test_fixed_challenger_final_labels_cannot_change_fitted_model(self):
        import app.services.research_training_v3 as service
        raw, frame = prepare_frame(prices(1800), horizon=5, fee_rate=.01, slippage_rate=.001)
        altered = frame.copy()
        altered.loc[altered.index[int(len(frame) * .8):], "target"] = 0
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            a = train_research_v3(raw, first, source="synthetic-test", horizon=5, fixed_model="random_forest")
            with patch.object(service, "prepare_frame", return_value=(raw, altered)):
                b = train_research_v3(raw, second, source="synthetic-test", horizon=5, fixed_model="random_forest")
            self.assertEqual((Path(first) / a["run_id"] / "calibrated_model.json").read_bytes(),
                (Path(second) / b["run_id"] / "calibrated_model.json").read_bytes())

    def test_unknown_fixed_family_is_rejected(self):
        with self.assertRaises(ValueError):
            train_research_v3(None, None, source="synthetic-test", fixed_model="unknown")

    def test_boundaries_are_strictly_purged(self):
        _, frame = prepare_frame(prices(1800), horizon=5, fee_rate=.01, slippage_rate=.001)
        train, calibration, final = partitions(frame)
        self.assertLess(train.label_end.max(), calibration.iloc[0].date)
        self.assertLess(calibration.label_end.max(), final.iloc[0].date)

    def test_numeric_exports_match_both_model_types(self):
        _, frame = prepare_frame(prices(1000), horizon=5, fee_rate=.01, slippage_rate=.001)
        for name in ("logistic_regression", "random_forest"):
            model = _model(name, 42)
            expected = _fit_predict(model, frame.iloc[:500], frame.iloc[500:])
            spec = {"base": export_base(model, name), "calibration": {"coef": 1., "intercept": 0.}}
            actual = predict_portable(spec, frame.iloc[500:][FEATURES])
            np.testing.assert_allclose(actual, np.clip(expected, 1e-6, 1 - 1e-6), atol=1e-12)

    def test_calibrated_artifacts_remain_ineligible(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = train_research_v3(prices(1800), temp, source="synthetic-test", horizon=5)
            spec = json.loads((Path(temp) / manifest["run_id"] / "calibrated_model.json").read_text())
            self.assertFalse(manifest["eligible_for_trading"])
            self.assertEqual(spec["payoff_estimate_source"], "calibration_only")
            self.assertTrue(spec["mean_win"] > 0)
            self.assertTrue(spec["mean_loss"] >= 0)
            self.assertEqual(sum(r["count"] for r in manifest["reliability_bins"]), manifest["final_test_rows"])

    def test_final_labels_cannot_change_selected_model_or_calibrator(self):
        import app.services.research_training_v3 as service
        raw, frame = prepare_frame(prices(1800), horizon=5, fee_rate=.01, slippage_rate=.001)
        altered = frame.copy()
        altered.loc[int(len(frame) * .8):, "target"] = 1 - altered.loc[int(len(frame) * .8):, "target"]
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            a = train_research_v3(raw, first, source="synthetic-test", horizon=5)
            with patch.object(service, "prepare_frame", return_value=(raw, altered)):
                b = train_research_v3(raw, second, source="synthetic-test", horizon=5)
            x = json.loads((Path(first) / a["run_id"] / "calibrated_model.json").read_text())
            y = json.loads((Path(second) / b["run_id"] / "calibrated_model.json").read_text())
            self.assertEqual(x, y)
            self.assertEqual(a["walkforward_metrics"], b["walkforward_metrics"])
