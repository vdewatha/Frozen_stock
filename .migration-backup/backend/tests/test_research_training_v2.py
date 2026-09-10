import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from app.services.research_training_v2 import prepare_frame, split_plan, train_research_v2


def prices(n=900):
    rng = np.random.default_rng(51)
    close = 100 * np.exp(np.cumsum(rng.normal(0, .018, n)))
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
        "open": close, "close": close * (1 + rng.normal(0, .001, n)), "volume": rng.uniform(10, 100, n)})


class V2TrainingTests(unittest.TestCase):
    def test_input_index_does_not_change_label_alignment(self):
        raw = prices()
        shifted = raw.copy()
        shifted.index = np.arange(1000, 1000 + len(raw))
        a, labels_a = prepare_frame(raw, horizon=5, fee_rate=.01, slippage_rate=.001)
        b, labels_b = prepare_frame(shifted, horizon=5, fee_rate=.01, slippage_rate=.001)
        pd.testing.assert_frame_equal(a, b)
        pd.testing.assert_frame_equal(labels_a, labels_b)

    def test_next_open_cost_labels_and_purge(self):
        raw = prices()
        _, frame = prepare_frame(raw, horizon=5, fee_rate=.01, slippage_rate=.001)
        row = frame.iloc[0]
        index = raw.index[raw.date == row.date][0]
        expected = raw.open.iloc[index + 6] * .999 * .99 / (raw.open.iloc[index + 1] * 1.001 * 1.01) - 1
        self.assertAlmostEqual(row.net_return, expected)
        self.assertEqual(row.label_end, raw.date.iloc[index + 6])
        development, final, splits = split_plan(frame)
        self.assertLess(development.label_end.max(), final.iloc[0].date)
        for train, validation in splits:
            self.assertLess(train.label_end.max(), validation.iloc[0].date)

    def test_gaps_and_invalid_costs_fail_closed(self):
        with self.assertRaises(ValueError):
            prepare_frame(prices().drop(index=200), horizon=5, fee_rate=.01, slippage_rate=.001)
        with self.assertRaises(ValueError):
            prepare_frame(prices(), horizon=5, fee_rate=float("nan"), slippage_rate=.001)

    def test_nonfinite_net_labels_fail_closed(self):
        raw = prices()
        raw.loc[100, "open"] = 1e-300
        raw.loc[105, "open"] = 1e300
        with self.assertRaises((ValueError, FloatingPointError)):
            prepare_frame(raw, horizon=5, fee_rate=.01, slippage_rate=.001)

    def test_immutable_artifacts_nonqualifying_and_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            result = train_research_v2(prices(), temp, source="synthetic-test-only", horizon=5)
            self.assertFalse(result["eligible_for_trading"])
            self.assertEqual(result["fee_rate_per_side"], .01)
            path = Path(temp) / result["run_id"]
            for name, digest in result["files"].items():
                self.assertEqual(hashlib.sha256((path / name).read_bytes()).hexdigest(), digest)
            with self.assertRaises(FileExistsError):
                train_research_v2(prices(), temp, source="synthetic-test-only", horizon=5)

    def test_final_labels_not_used_for_model_selection(self):
        import app.services.research_training_v2 as service
        raw = prices()
        _, original = prepare_frame(raw, horizon=5, fee_rate=.01, slippage_rate=.001)
        altered = original.copy()
        boundary = int(len(original) * .8)
        altered.loc[boundary:, "target"] = 1 - altered.loc[boundary:, "target"]
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            a = train_research_v2(raw, first, source="synthetic-test-only", horizon=5)
            with patch.object(service, "prepare_frame", return_value=(raw, altered)):
                b = train_research_v2(raw, second, source="synthetic-test-only", horizon=5)
            self.assertEqual(a["selected_model"], b["selected_model"])
            self.assertEqual(a["walkforward_metrics"], b["walkforward_metrics"])

    def test_numerical_failure_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as temp, patch("app.services.research_training_v2._fit_predict", side_effect=RuntimeWarning()):
            with self.assertRaises(RuntimeWarning):
                train_research_v2(prices(), temp, source="synthetic-test-only", horizon=5)
            self.assertEqual(list(Path(temp).iterdir()), [])
