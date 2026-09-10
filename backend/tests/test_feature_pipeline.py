import unittest

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal, assert_series_equal

from app.services.feature_pipeline import DEFAULT_FEATURES, FeatureSpec, feature_config_id, generate_features


def prices(n=250):
    x = np.arange(n)
    return pd.DataFrame({"date": pd.date_range("2020-01-01", periods=n).date,
                         "close": 100 + x / 20 + 3 * np.sin(x), "volume": 1000 + x})


class FeaturePipelineTests(unittest.TestCase):
    def test_prefix_invariance_and_input_unchanged(self):
        source = prices()
        saved = source.copy(deep=True)
        columns = [s.name for s in DEFAULT_FEATURES]
        assert_frame_equal(generate_features(source)[columns].iloc[:170], generate_features(source.iloc[:170])[columns])
        assert_frame_equal(source, saved)

    def test_legacy_values(self):
        source = prices()
        result = generate_features(source)
        assert_series_equal(result.return_20d, source.close.pct_change(20).clip(-5, 5), check_names=False)
        assert_series_equal(result.volatility_20d, source.close.pct_change().rolling(20).std().clip(-5, 5), check_names=False)
        self.assertEqual([s.name for s in DEFAULT_FEATURES], list(result.columns[3:]))

    def test_configuration_and_identity(self):
        specs = (FeatureSpec("return_3", "return", 3),)
        result = generate_features(prices(), specs)
        self.assertIn("return_3", result)
        self.assertNotEqual(feature_config_id(specs), feature_config_id())
        self.assertEqual(feature_config_id(specs), result.attrs["feature_config_id"])

    def test_rejects_unsafe_configuration(self):
        for spec in (FeatureSpec("x", "arbitrary.import"), FeatureSpec("x", "return", -1), FeatureSpec("close", "return")):
            with self.assertRaises(ValueError):
                generate_features(prices(), (spec,))

    def test_missing_values_not_forward_filled(self):
        source = prices()
        source.loc[70, "close"] = np.nan
        result = generate_features(source)
        self.assertTrue(pd.isna(result.loc[90, "return_20d"]))

    def test_duplicate_dates_rejected(self):
        with self.assertRaises(ValueError):
            generate_features(pd.concat([prices(), prices().iloc[:1]]))


if __name__ == "__main__":
    unittest.main()
