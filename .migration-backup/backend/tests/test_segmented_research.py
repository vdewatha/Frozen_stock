import hashlib
from pathlib import Path
import tempfile
import unittest
import pandas as pd
from app.services.research_training_v2 import prepare_frame, FEATURES
from app.services.research_training_v3 import train_research_v3
from app.services.comparison_model import load_comparison_model
from test_research_training_v2 import prices


class SegmentTests(unittest.TestCase):
    def test_no_feature_or_label_crosses_gap_and_defaults_reject(self):
        full = prices(1800)
        raw = full.drop(index=[700, 701, 1200])
        with self.assertRaises(ValueError): prepare_frame(raw, horizon=5, fee_rate=.01, slippage_rate=.001)
        cleaned, frame = prepare_frame(raw, horizon=5, fee_rate=.01, slippage_rate=.001, gap_policy="segment")
        self.assertEqual(len(cleaned), 1797)
        for missing in full.date.iloc[[700, 701, 1200]]:
            self.assertFalse(((frame.date < missing) & (frame.label_end >= missing)).any())
        _, tail = prepare_frame(full.iloc[1201:], horizon=5, fee_rate=.01, slippage_rate=.001)
        actual = frame.loc[frame.date >= tail.iloc[0].date].reset_index(drop=True)
        pd.testing.assert_frame_equal(actual[FEATURES], tail[FEATURES])

    def test_more_than_24_missing_hours_blocked(self):
        with self.assertRaises(ValueError):
            prepare_frame(prices(1800).drop(index=range(600, 625)), horizon=5, fee_rate=.01, slippage_rate=.001, gap_policy="segment")

    def test_segment_manifest_and_loader_calendar_coverage(self):
        raw = prices(1800).drop(index=[500, 1000])
        with tempfile.TemporaryDirectory() as temp:
            manifest = train_research_v3(raw, temp, source="synthetic-segment-test", horizon=5, gap_policy="segment")
            path = Path(temp) / manifest["run_id"]
            pin = hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest()
            self.assertEqual(len(manifest["missing_hours"]), 2)
            self.assertEqual(manifest["calendar_hours"], 1800)
            accepted, _ = load_comparison_model(path, fee_rate=.01, slippage_rate=.001, source_claim="synthetic-segment-test",
                manifest_sha256=pin, expected_horizon=5, minimum_history=1800)
            self.assertFalse(accepted["eligible_for_trading"])
