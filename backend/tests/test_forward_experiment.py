from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import test_paper_comparison as fixture
from app.services.paper_comparison import ComparisonConfig, canonical
from app.services.forward_experiment import ARMS, load_contract, record_forecasts, forecast_summary, freeze_experiment
from run_forward_experiment import cycle


class ForwardExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'evidence').mkdir()
        self.fixture = fixture.ComparisonTests()
        self.fixture.setUp()
        self.start = self.fixture.start + timedelta(hours=60)
        arms = {}
        for name in ARMS:
            config = ComparisonConfig(ml_model_id='a'*64+':'+'b'*64)
            arms[name] = dict(ledger_path=f'evidence/{name}.sqlite', model_run_id='a'*64,
                manifest_sha256='b'*64, comparison_config=asdict(config), config_sha256=config.sha256)
        self.contract = dict(version='forward-experiment-v1', frozen_at=(self.start-timedelta(hours=1)).isoformat(),
            policy=dict(start_at=self.start.isoformat(), end_at=(self.start+timedelta(days=84)).isoformat(),
                minimum_days=84, minimum_roundtrips=100, minimum_coverage=.95, maximum_drawdown=.10, minimum_profit_factor=1.2),
            arms=arms, forecast_target='future price label proxy', live_authorized=False)
        self.contract['experiment_id'] = hashlib.sha256(canonical(self.contract).encode()).hexdigest()
        (self.root / 'experiment.json').write_text(canonical(self.contract))
        self.predictions = {name: dict(ml_probability=.8, ml_model_id=arms[name]['comparison_config']['ml_model_id']) for name in ARMS}

    def tearDown(self):
        self.fixture.tearDown()
        self.temp.cleanup()

    def test_contract_tamper(self):
        self.assertEqual(load_contract(self.root), self.contract)
        changed = self.contract | {'live_authorized': True}
        (self.root / 'experiment.json').write_text(canonical(changed))
        with self.assertRaises(ValueError): load_contract(self.root)

    def test_forecast_idempotency_future_outcomes(self):
        path = self.root / 'evidence/forecasts.sqlite'
        now = self.start+timedelta(minutes=1)
        record_forecasts(path, self.contract, self.fixture.rows(), self.predictions, observed_at=now)
        record_forecasts(path, self.contract, self.fixture.rows(), self.predictions, observed_at=now+timedelta(seconds=30))
        before = forecast_summary(self.root)
        self.assertTrue(all(a['recorded']==1 and a['scored']['rows']==0 for a in before['arms'].values()))
        record_forecasts(path, self.contract, self.fixture.rows(85), {}, observed_at=self.start+timedelta(hours=25, minutes=1))
        after = forecast_summary(self.root)
        self.assertTrue(all(a['scored']['rows']==1 for a in after['arms'].values()))
        with sqlite3.connect(path) as db:
            forecast = json.loads(db.execute('SELECT payload FROM forecasts LIMIT 1').fetchone()[0])
        self.assertGreater(forecast['entry_at'], forecast['observed_at'])
        altered = {name: p | {'ml_probability': .9} for name,p in self.predictions.items()}
        with self.assertRaisesRegex(ValueError, 'revised'):
            record_forecasts(path, self.contract, self.fixture.rows(), altered, observed_at=now)

    def test_late_partial_and_outside_window_rejected(self):
        path = self.root / 'evidence/forecasts.sqlite'
        for preds, now in [(self.predictions, self.start+timedelta(minutes=6)),
                           ({ARMS[0]: self.predictions[ARMS[0]]}, self.start+timedelta(minutes=1))]:
            with self.assertRaises(ValueError): record_forecasts(path, self.contract, self.fixture.rows(), preds, observed_at=now)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0], 0)

    def test_worker_time_gates_do_not_access_source(self):
        with patch('run_forward_experiment.Session', side_effect=AssertionError('must not read source')):
            self.assertEqual(cycle(self.root,self.contract,None,now=self.start-timedelta(seconds=1))['status'], 'scheduled')
            self.assertEqual(cycle(self.root,self.contract,None,now=self.start+timedelta(minutes=6))['status'], 'waiting')
            self.assertEqual(cycle(self.root,self.contract,None,now=self.start+timedelta(days=84,hours=26))['status'], 'completed')

    def test_freeze_requires_identical_source_and_future_window(self):
        pins = {}
        for index, name in enumerate(ARMS):
            directory = self.root / ('model' + str(index))
            directory.mkdir()
            manifest = dict(run_id=str(index)*64, selected_model='random_forest' if name.startswith('challenger') else 'logistic_regression',
                selection_policy='fixed_random_forest_v1', source_claim='same-source', dataset_sha256='a'*64,
                feature_config_id='features', horizon_bars=24, final_test_end=self.fixture.start.isoformat())
            (directory/'manifest.json').write_text(json.dumps(manifest))
            pins[name] = dict(directory=str(directory), manifest_sha256='b'*64)
        def loader(directory, **kwargs):
            return json.loads((directory/'manifest.json').read_text()), {'verified_dataset_end': self.fixture.start.isoformat()}
        with patch('app.services.forward_experiment.load_comparison_model', side_effect=loader):
            result = freeze_experiment(self.root/'new', pins, start_at=self.start, now=self.start-timedelta(hours=1), runtime_image_id='sha256:'+'a'*64)
            self.assertEqual(load_contract(self.root/'new')['experiment_id'], result['experiment_id'])
            target = Path(pins['challenger_baseline']['directory'])/'manifest.json'
            value = json.loads(target.read_text()) | {'dataset_sha256': 'different'}
            target.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'identical dataset'):
                freeze_experiment(self.root/'bad', pins, start_at=self.start, now=self.start-timedelta(hours=1), runtime_image_id='sha256:'+'a'*64)
            self.assertFalse((self.root/'bad').exists())
