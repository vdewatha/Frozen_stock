import unittest
import pandas as pd
from app.services.research_training_v2 import prepare_frame
from app.services.research_walkforward import evaluate_walkforward
from test_research_training_v2 import prices


class WalkforwardTests(unittest.TestCase):
    def frame(self):
        return prepare_frame(prices(2600), horizon=5, fee_rate=.01, slippage_rate=.001)[1]

    def test_purged_nonoverlap_and_development_only(self):
        result = evaluate_walkforward(self.frame())
        self.assertFalse(result["new_untouched_test"])
        self.assertFalse(result["eligible_for_trading"])
        self.assertEqual(len(result["pooled_metrics"]), 4)
        for audit in result["audit"]:
            self.assertLess(audit["training_label_end"], audit["calibration_start"])
            self.assertLess(audit["calibration_label_end"], audit["evaluation_start"])
            self.assertLessEqual(audit["evaluation_end"], result["original_development_end"])
        dates = [row["date"] for row in result["predictions"]]
        self.assertEqual(len(dates), len(set(dates)))

    def test_later_labels_do_not_change_earlier_fold_forecasts(self):
        frame = self.frame()
        original = evaluate_walkforward(frame)
        cutoff = pd.Timestamp(original["audit"][0]["evaluation_end"])
        changed = frame.copy()
        changed.loc[changed.date > cutoff, "target"] = 1 - changed.loc[changed.date > cutoff, "target"]
        second = evaluate_walkforward(changed)
        first_prob = [row for row in original["predictions"] if row["fold"] == 0]
        second_prob = [row for row in second["predictions"] if row["fold"] == 0]
        self.assertEqual(first_prob, second_prob)

    def test_single_class_past_fails_closed(self):
        frame = self.frame()
        frame.loc[:600, "target"] = 0
        with self.assertRaises(ValueError): evaluate_walkforward(frame)

    def test_original_calibration_and_final_labels_not_consulted(self):
        frame = self.frame()
        first = evaluate_walkforward(frame)
        changed = frame.copy()
        changed.loc[int(len(frame) * .6):, "target"] = 0
        self.assertEqual(first, evaluate_walkforward(changed))
