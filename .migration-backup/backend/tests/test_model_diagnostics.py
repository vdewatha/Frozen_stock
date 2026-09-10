import hashlib
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
from app.services.model_diagnostics import summarize, paired_block_interval, label_replay, publish_report, build_report


class DiagnosticsTests(unittest.TestCase):
    def test_summary(self):
        result = summarize([0, 1], [.2, .8])
        self.assertAlmostEqual(result['metrics']['brier_score'], .04)
        self.assertEqual(sum(b['count'] for b in result['reliability']), 2)
        self.assertIsNone(summarize([], [])['metrics'])
        for y, p in [([], [.2]), ([0], [np.nan]), ([[0]], [[.2]]), ([1], [1.1])]:
            with self.assertRaises(ValueError): summarize(y, p)

    def test_bootstrap(self):
        dates = pd.date_range('2025-01-01', periods=240, freq='h', tz='UTC')
        args = (dates, np.zeros(240), np.full(240, .2), np.full(240, .5))
        a = paired_block_interval(*args)
        self.assertEqual(a, paired_block_interval(*args))
        self.assertAlmostEqual(a['interval_95'][0], -.21)
        broken = dates[:10].append(dates[11:])
        self.assertIsNone(paired_block_interval(broken, np.zeros(239), np.zeros(239), np.zeros(239))['interval_95'])

    def test_nonoverlapping_replay(self):
        dates = pd.date_range('2025-01-01', periods=5, freq='h', tz='UTC')
        frame = pd.DataFrame({'date': dates, 'label_end': dates + pd.Timedelta(hours=2), 'net_return': .01})
        result = label_replay(frame, np.ones(5, dtype=bool))
        self.assertEqual(result['completed_hypothetical_roundtrips'], 3)
        self.assertAlmostEqual(result['net_fixed_stake_dollars'], 3)
        with self.assertRaises(ValueError): label_replay(frame, [True])

    def test_immutable_publication_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = {'report_id': 'a' * 64}
            result = publish_report(report, tmp, '# Report\n')
            self.assertTrue((result / 'files.json').is_file())
            with self.assertRaises(FileExistsError): publish_report(report, tmp, 'changed')
            with self.assertRaises(ValueError): publish_report({'report_id': '../outside'}, tmp, 'bad')
            self.assertEqual((result / 'report.md').read_text(), '# Report\n')

    def test_manifest_pin_checked_before_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'manifest.json').write_text('not json')
            with self.assertRaisesRegex(ValueError, 'Pinned manifest mismatch'):
                build_report(tmp, manifest_sha256=hashlib.sha256(b'other').hexdigest())
