import unittest

import numpy as np
import pandas as pd

from app.services.probabilistic_model import FEATURE_COLUMNS, HORIZONS, _walk_forward_for_horizon, build_feature_frame, predict_probabilities


class ProbabilisticValidationTests(unittest.TestCase):
    def frame(self):
        x = np.arange(320)
        return build_feature_frame(pd.DataFrame({"date": pd.date_range("2020-01-01", periods=320).date,
                                                "close": 100 + np.sin(x) * 4 + x / 100, "volume": 1000 + x}))

    def test_unknown_tail_labels_remain_unknown(self):
        frame = self.frame()
        for horizon in HORIZONS:
            self.assertTrue(frame[f"target_up_{horizon}d"].iloc[-horizon:].isna().all())
            self.assertTrue(frame[f"target_up_{horizon}d"].iloc[:-horizon].notna().all())

    def test_train_outcomes_precede_test_after_missing_rows(self):
        frame = self.frame().dropna(subset=FEATURE_COLUMNS)
        frame = frame.drop(frame.index[::7])
        for horizon in HORIZONS:
            folds = _walk_forward_for_horizon(frame, horizon, train_window=100, test_window=20)
            self.assertTrue(folds)
            for fold in folds:
                self.assertLess(pd.Timestamp(fold["train_label_end"]), pd.Timestamp(fold["test_start"]))
                self.assertGreaterEqual(fold["purged_train_rows"], 0)
                self.assertEqual(fold["sample_size"], 20)
            if horizon == 20:
                self.assertTrue(all(f["purged_train_rows"] > 0 for f in folds))

    def test_no_history_produces_no_folds(self):
        self.assertEqual(_walk_forward_for_horizon(self.frame().iloc[:30], 20), [])

    def test_prediction_contract(self):
        source = self.frame()[["date", "close", "volume"]]
        result = predict_probabilities("TEST", source, "test_fixture")
        self.assertEqual(result["prediction_date"], str(source.iloc[-1].date))
        self.assertEqual(set(result["latest_features"]), set(FEATURE_COLUMNS))
        self.assertEqual([p["horizon_days"] for p in result["predictions"]], HORIZONS)
        for prediction in result["predictions"]:
            self.assertAlmostEqual(prediction["probability_up"] + prediction["probability_down"], 1)


if __name__ == "__main__":
    unittest.main()
