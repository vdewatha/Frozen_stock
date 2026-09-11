from datetime import datetime, timedelta, timezone
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from app.services.forward_evaluation import evaluate_experiment, render_markdown
from app.services.paper_comparison import ComparisonConfig, canonical, _connect, advance_comparison
from app.models.crypto_data import CryptoCandle
from app.integrations.kraken import BTC_USD
from app.services.crypto_collection import _values, _hash
from app.services.instruments import Candle, Timeframe


class ForwardEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'evidence').mkdir()
        self.start = datetime(2026,1,1,tzinfo=timezone.utc)
        self.config = ComparisonConfig(ml_model_id='frozen-model:'+('a'*64))
        self.manifest = dict(version='forward-experiment-v1', frozen_at=(self.start-timedelta(hours=1)).isoformat(), live_authorized=False, policy=dict(
            start_at=self.start.isoformat(), end_at=(self.start+timedelta(days=84)).isoformat(),
            minimum_days=84, minimum_roundtrips=100, minimum_coverage=.95, maximum_drawdown=.1,
            minimum_profit_factor=1.2), arms={
            f'{role}_{cost}': dict(ledger_path=f'evidence/{role}_{cost}.sqlite', model_run_id='frozen-model', comparison_config=asdict(self.config),
                                  config_sha256=self.config.sha256, manifest_sha256='a'*64)
            for role in ('champion','challenger') for cost in ('baseline','stress')})
        self.write_manifest()

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self):
        self.manifest.pop('experiment_id', None)
        self.manifest['experiment_id'] = hashlib.sha256(canonical(self.manifest).encode()).hexdigest()
        (self.root/'experiment.json').write_text(canonical(self.manifest))

    def test_missing_evidence_is_accumulating_not_pass(self):
        report = evaluate_experiment(self.root, self.start+timedelta(hours=2,minutes=6))
        self.assertEqual(report['status'], 'accumulating')
        self.assertEqual(report['arms']['champion_baseline']['missing_hours'], 3)
        self.assertFalse(report['eligible_for_live_trading'])
        self.assertIn('Frozen checks', render_markdown(report))

    def test_complete_missing_evidence_is_inconclusive(self):
        report = evaluate_experiment(self.root, self.start+timedelta(days=85))
        self.assertEqual(report['status'], 'inconclusive')
        self.assertEqual(report['arms']['champion_baseline']['missing_hours'], 84*24)

    def test_grace_period_and_prestart_not_counted_missing(self):
        for clock in (self.start-timedelta(days=1), self.start+timedelta(minutes=4)):
            report = evaluate_experiment(self.root, clock)
            self.assertEqual(report['arms']['champion_baseline']['missing_hours'], 0)

    def test_manifest_tampering_rejected(self):
        self.manifest['policy']['minimum_roundtrips'] = 0
        (self.root/'experiment.json').write_text(canonical(self.manifest))
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            evaluate_experiment(self.root, self.start)

    def test_path_escape_rejected(self):
        self.manifest['arms']['champion_baseline']['ledger_path'] = '../other.sqlite'
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'frozen arm'):
            evaluate_experiment(self.root, self.start)

    def test_config_binding_rejected_and_read_only(self):
        path = self.root/'evidence/champion_baseline.sqlite'
        connection = _connect(path, ComparisonConfig(ml_model_id='wrong-model'))
        connection.commit()
        connection.close()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'configuration/model'):
            evaluate_experiment(self.root, self.start)
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_empty_initialized_ledger(self):
        connection = _connect(self.root/'evidence/champion_baseline.sqlite', self.config)
        connection.commit()
        connection.close()
        report = evaluate_experiment(self.root, self.start+timedelta(hours=1))
        arm = report['arms']['champion_baseline']
        self.assertEqual(arm['status'], 'empty')
        self.assertEqual(arm['strategies']['cost_ml']['roundtrips'], 0)
        self.assertEqual(arm['strategies']['cash']['equity'], 10000)

    def fill_ledgers(self):
        self.config = ComparisonConfig(ml_model_id='frozen-model:'+('a'*64), ml_horizon_hours=1)
        for arm in self.manifest['arms'].values():
            arm['config_sha256'] = self.config.sha256
            arm['comparison_config'] = asdict(self.config)
        self.write_manifest()
        for step in range(4):
            rows = []
            for offset in range(-60, step):
                opened = self.start+timedelta(hours=offset)
                price = 100+offset/10
                candle = Candle(BTC_USD, Timeframe.HOUR, opened, price, price+2, price-2, price, 3)
                values = _values(candle)
                rows.append(CryptoCandle(**values, content_sha256=_hash(values),
                    observed_at=(self.start+timedelta(hours=step,minutes=1)).isoformat()))
            for arm in self.manifest['arms'].values():
                advance_comparison(self.root/arm['ledger_path'], rows,
                    observed_at=self.start+timedelta(hours=step,minutes=1), config=self.config,
                    ml_probability=.9, ml_expected_net=.02)

    def test_real_ledger_roundtrip_fee_accounting_and_read_only(self):
        self.fill_ledgers()
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.glob('evidence/*.sqlite')}
        report = evaluate_experiment(self.root, self.start+timedelta(hours=3,minutes=6))
        ml = report['arms']['challenger_baseline']['strategies']['cost_ml']
        self.assertEqual(ml['roundtrips'], 1)
        self.assertLess(ml['realized_net_pnl'], 0)
        self.assertGreater(ml['open_quantity'], 0)
        self.assertLess(ml['net_equity_pnl'], ml['realized_net_pnl'])
        self.assertGreater(ml['fees_paid'], 0)
        self.assertEqual(report['arms']['challenger_baseline']['coverage'], 1)
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.glob('evidence/*.sqlite')})

    def test_account_snapshot_tampering_rejected(self):
        self.fill_ledgers()
        with sqlite3.connect(self.root/'evidence/champion_baseline.sqlite') as connection:
            state = json.loads(connection.execute("SELECT state FROM comparison_accounts WHERE strategy='cash'").fetchone()[0])
            state['cash'] = '20000'
            connection.execute("UPDATE comparison_accounts SET state=? WHERE strategy='cash'", (canonical(state),))
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            evaluate_experiment(self.root, self.start+timedelta(hours=4))

    def test_fabricated_realized_pnl_rejected(self):
        self.fill_ledgers()
        with sqlite3.connect(self.root/'evidence/champion_baseline.sqlite') as connection:
            row = connection.execute('SELECT bar_close,report FROM comparison_steps ORDER BY bar_close LIMIT 1').fetchone()
            report = json.loads(row[1])
            report['accounts'][0]['state']['cash'] = '20000'
            connection.execute('UPDATE comparison_steps SET report=? WHERE bar_close=?', (canonical(report), row[0]))
        with self.assertRaisesRegex(ValueError, 'balances'):
            evaluate_experiment(self.root, self.start+timedelta(hours=4))

    def test_future_and_outside_window_evidence_rejected(self):
        self.fill_ledgers()
        with self.assertRaisesRegex(ValueError, 'Future'):
            evaluate_experiment(self.root, self.start)
        self.manifest['policy']['start_at'] = (self.start+timedelta(hours=1)).isoformat()
        self.manifest['policy']['end_at'] = (self.start+timedelta(days=84,hours=1)).isoformat()
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'outside frozen window'):
            evaluate_experiment(self.root, self.start+timedelta(hours=4))


if __name__ == '__main__':
    unittest.main()
