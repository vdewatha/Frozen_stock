"""Independent exported-model parity checks without deserializing pickle."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from app.services.probabilistic_model import build_feature_frame, FEATURE_COLUMNS
from app.services.research_training import train_research_run
from app.services.research_registry import validate_research_run, IDENTITY_KEYS


class PortableModelTests(unittest.TestCase):
    def prices(self):
        x = np.arange(400)
        return pd.DataFrame({"date": pd.date_range("2020-01-01", periods=400, freq="h", tz="UTC"),
                             "close": 100+np.sin(x)*4+x/100, "volume": x+1000})

    def test_export_parity_integrity_and_binding(self):
        with TemporaryDirectory() as root, patch("joblib.load", side_effect=AssertionError("No pickle inference")):
            manifest = train_research_run(self.prices(), Path(root), symbol="BTC/USD", source="fixture",
                instrument_id="crypto_spot:KRAKEN:BTC:USD", timeframe_minutes=60)
            folder = Path(root).resolve()/manifest["run_id"]
            validated, _, _ = validate_research_run(folder)
            self.assertEqual(validated, manifest)
            portable = json.loads((folder/"logistic_regression.json").read_text())
            self.assertEqual(portable["run_id"], manifest["run_id"])
            self.assertEqual(portable["instrument"], manifest["instrument_id"])
            self.assertEqual(portable["timeframe_minutes"], 60)
            self.assertEqual(portable["feature_names"], FEATURE_COLUMNS)
            self.assertEqual(portable["training_cutoff"], manifest["train_label_end"])
            frame = build_feature_frame(self.prices())
            predictions = pd.read_csv(folder/"holdout_predictions.csv")
            lookup = frame.assign(date=pd.to_datetime(frame.date, utc=True)).set_index("date")
            features = lookup.loc[pd.to_datetime(predictions.date, utc=True), FEATURE_COLUMNS].to_numpy()
            logits = ((features-np.array(portable["mean"]))/np.array(portable["scale"])) @ np.array(portable["coef"]) + portable["intercept"]
            np.testing.assert_allclose(1/(1+np.exp(-logits)), predictions.logistic_regression, atol=1e-14)
            (folder/"logistic_regression.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "checksum"):
                validate_research_run(folder)

    def test_invalid_instrument_and_nonhourly_data(self):
        for kwargs, prices in [({"instrument_id": "other", "timeframe_minutes": 60}, self.prices()),
                               ({"instrument_id": "crypto_spot:KRAKEN:BTC:USD"}, self.prices()),
                               ({"instrument_id": "crypto_spot:KRAKEN:BTC:USD", "timeframe_minutes": 60}, self.prices().drop(index=3))]:
            with TemporaryDirectory() as root, self.assertRaises(ValueError):
                train_research_run(prices, Path(root), symbol="BTC/USD", source="fixture", **kwargs)

    def test_legacy_v1_registry_still_accepted(self):
        with TemporaryDirectory() as root:
            manifest = train_research_run(self.prices(), Path(root), symbol="TEST", source="fixture")
            old_folder = Path(root)/manifest["run_id"]
            manifest["format_version"] = 1
            del manifest["instrument_id"], manifest["timeframe_minutes"]
            identity = {key: manifest[key] for key in IDENTITY_KEYS}
            manifest["run_id"] = hashlib.sha256(json.dumps(identity, sort_keys=True, indent=2, allow_nan=False).encode()).hexdigest()
            folder = Path(root).resolve()/manifest["run_id"]
            old_folder.rename(folder)
            (folder/"manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
            self.assertEqual(validate_research_run(folder)[0], manifest)
