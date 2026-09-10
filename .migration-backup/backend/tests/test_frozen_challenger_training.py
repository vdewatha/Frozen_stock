import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.train_frozen_challenger import train_challenger


class FrozenTrainingTests(unittest.TestCase):
    def test_source_pin_and_output_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source'
            source.mkdir()
            (source / 'manifest.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'outside'):
                train_challenger(source, '', source / 'new')
            with self.assertRaisesRegex(ValueError, 'hash'):
                train_challenger(source, '0' * 64, Path(temporary) / 'output')

    def test_verified_snapshot_and_original_settings_are_forwarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source'
            source.mkdir()
            data = b'date,open,close,volume\n2025-01-01,100,101,10\n'
            manifest = dict(source_claim='test', fee_rate_per_side=.008,
                slippage_rate_per_side=.001, horizon_bars=24, seed=42,
                gap_policy='segment', dataset_sha256=hashlib.sha256(data).hexdigest())
            payload = json.dumps(manifest).encode()
            (source / 'manifest.json').write_bytes(payload)
            (source / 'dataset.csv').write_bytes(data)
            pin = hashlib.sha256(payload).hexdigest()
            with patch('scripts.train_frozen_challenger.load_comparison_model', return_value=(manifest, {})) as loader, patch('scripts.train_frozen_challenger.train_research_v3', return_value={'run_id': 'new'}) as trainer:
                self.assertEqual(train_challenger(source, pin, Path(temporary) / 'output'), {'run_id': 'new'})
                self.assertEqual(loader.call_args.kwargs['manifest_sha256'], pin)
                self.assertEqual(trainer.call_args.kwargs, dict(source='test', horizon=24,
                    fee_rate=.008, slippage_rate=.001, seed=42, gap_policy='segment', fixed_model='random_forest', source_snapshot=data))
                (source / 'dataset.csv').write_bytes(data + b'changed')
                with self.assertRaisesRegex(ValueError, 'dataset changed'):
                    train_challenger(source, pin, Path(temporary) / 'output')
