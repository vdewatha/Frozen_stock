import unittest
import numpy as np
import pandas as pd
from app.services.feature_pipeline import generate_features, feature_config_id


class FeatureV2Tests(unittest.TestCase):
    def frame(self, close):
        return pd.DataFrame({"date": pd.date_range("2020-01-01", periods=len(close), freq="h"), "close": close, "volume": 100.})

    def test_rsi_extremes_and_flat(self):
        for close, expected in [(np.arange(100., 180.), 1.), (np.arange(180.,100.,-1), -1.), (np.full(80,100.),0.)]:
            self.assertAlmostEqual(generate_features(self.frame(close), version="2").rsi_14.iloc[-1], expected)

    def test_price_scale_and_causality(self):
        frame = self.frame(100 + np.sin(np.arange(100)) * 5 + np.arange(100) / 10)
        first = generate_features(frame, version="2")
        scaled = frame.copy(); scaled.close *= 1000
        second = generate_features(scaled, version="2")
        np.testing.assert_allclose(first.macd_histogram, second.macd_histogram, atol=1e-12)
        prefix = generate_features(frame.iloc[:80], version="2")
        pd.testing.assert_frame_equal(first.iloc[:80], prefix)
        self.assertGreater(first.rsi_14.dropna().nunique(), 2)

    def test_legacy_default_and_version_identity(self):
        frame = self.frame(100 + np.sin(np.arange(100)))
        pd.testing.assert_frame_equal(generate_features(frame), generate_features(frame, version="1"))
        self.assertNotEqual(feature_config_id(), feature_config_id(version="2"))
        with self.assertRaises(ValueError): generate_features(frame, version="3")
