import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import pandas as pd

scripts = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location('history_workflow', scripts / 'run_history_ml_workflow.py')
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class WorkflowTests(unittest.TestCase):
    def setup_root(self, temp):
        root = Path(temp)
        (root / 'backfill').mkdir()
        (root / 'backfill/provenance.json').write_text(json.dumps({
            'csv_sha256': 'd' * 64, 'rows': 8760,
            'source_url': 'https://api.kraken.com/0/public/Trades',
            'instrument': 'crypto_spot:KRAKEN:BTC:USD', 'start': 1735689600, 'end': 1767225600,
            'format': 'kraken-public-trades-backfill-v1', 'eligible_for_trading': False}))
        return root

    def test_completed_candidates_not_retrained(self):
        calls = []
        def trainer(frame, output, **kwargs):
            calls.append(kwargs)
            run = hashlib.sha256(output.name.encode()).hexdigest()
            directory = output / run
            directory.mkdir(parents=True)
            (directory / 'model.json').write_text('{}')
            manifest = {'run_id': run, 'eligible_for_trading': False, 'status': 'experimental',
                        'final_test_metrics': {}, 'files': {'model.json': hashlib.sha256(b'{}').hexdigest()}}
            (directory / 'manifest.json').write_text(json.dumps(manifest))
            return manifest
        with tempfile.TemporaryDirectory() as temp:
            root = self.setup_root(temp)
            with patch.object(workflow, 'read_kraken_csv', return_value=(pd.DataFrame(index=range(8760)), 'd' * 64)), \
                 patch.object(workflow, 'load_comparison_model', return_value=({}, {})):
                first = workflow.prepare_candidates(root, trainer=trainer)
                second = workflow.prepare_candidates(root, trainer=trainer)
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 2)
            self.assertEqual([c['fee_rate'] for c in calls], [.008, .01])
            self.assertFalse(first['eligible_for_trading'])

    def test_failed_training_requires_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.setup_root(temp)
            with patch.object(workflow, 'read_kraken_csv', return_value=(pd.DataFrame(index=range(8760)), 'd' * 64)):
                with self.assertRaises(RuntimeError):
                    workflow.prepare_candidates(root, trainer=lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
                with self.assertRaisesRegex(ValueError, 'requires review'):
                    workflow.prepare_candidates(root)
