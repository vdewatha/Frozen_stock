import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import warnings
from unittest.mock import patch

import numpy as np
import pandas as pd

from app.services.research_training import train_research_run


class ResearchTrainingTests(unittest.TestCase):
    def prices(self):
        x = np.arange(400)
        return pd.DataFrame({"date": pd.date_range("2020-01-01", periods=400),
                             "close": 100 + np.sin(x) * 4 + x / 100, "volume": x + 1000})

    def test_artifacts_integrity_purged_holdout_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = train_research_run(self.prices(), Path(root), symbol="TEST", source="fixture")
            folder = Path(root) / manifest["run_id"]
            self.assertEqual(json.loads((folder / "manifest.json").read_text()), manifest)
            self.assertLess(manifest["train_label_end"], manifest["holdout_start"])
            self.assertFalse(manifest["eligible_for_trading"])
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), digest)
            with self.assertRaises(FileExistsError):
                train_research_run(self.prices(), Path(root), symbol="TEST", source="fixture")

    def test_reproducible_independent_runs(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            left = train_research_run(self.prices(), Path(a), symbol="TEST", source="fixture")
            right = train_research_run(self.prices(), Path(b), symbol="TEST", source="fixture")
            self.assertEqual(left, right)

    def test_invalid_observations_and_short_history(self):
        for prices in (self.prices().iloc[:50], self.prices().iloc[::-1],
                       self.prices().assign(close=np.nan), self.prices().assign(volume=-1)):
            with tempfile.TemporaryDirectory() as root, self.assertRaises(ValueError):
                train_research_run(prices, Path(root), symbol="TEST", source="fixture")

    def test_subsecond_observations_preserved(self):
        prices = self.prices()
        prices["date"] = pd.date_range("2020-01-01", periods=len(prices), freq="100ms", tz="UTC")
        with tempfile.TemporaryDirectory() as root:
            manifest = train_research_run(prices, Path(root), symbol="TEST", source="fixture")
            saved = pd.read_csv(Path(root) / manifest["run_id"] / "dataset.csv")
            self.assertEqual(saved.date.nunique(), len(prices))
            self.assertLess(pd.Timestamp(manifest["train_label_end"]), pd.Timestamp(manifest["holdout_start"]))

    def test_numerical_warning_does_not_publish(self):
        def failed_fit(*args, **kwargs):
            warnings.warn("invalid numerical operation", RuntimeWarning)
        with tempfile.TemporaryDirectory() as root, patch("app.services.research_training.LogisticRegression.fit", side_effect=failed_fit):
            with self.assertRaisesRegex(ValueError, "Numerical validation failed"):
                train_research_run(self.prices(), Path(root), symbol="TEST", source="fixture")
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_prediction_warning_and_invalid_probability_do_not_publish(self):
        def warning(*args, **kwargs):
            warnings.warn("invalid matrix operation", RuntimeWarning)
        def invalid(model, x):
            return np.full((len(x), 2), np.nan)
        for replacement in (warning, invalid):
            with tempfile.TemporaryDirectory() as root, patch("app.services.research_training.LogisticRegression.predict_proba", replacement):
                with self.assertRaises(ValueError):
                    train_research_run(self.prices(), Path(root), symbol="TEST", source="fixture")
                self.assertEqual(list(Path(root).iterdir()), [])
