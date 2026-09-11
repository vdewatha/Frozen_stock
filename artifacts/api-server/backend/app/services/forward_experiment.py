"""Frozen prospective research contract and append-only probability evidence."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np
from app.services.comparison_model import load_comparison_model
from app.services.paper_comparison import ComparisonConfig, canonical
from app.services.instruments import utc_timestamp

ARMS = ('champion_baseline', 'challenger_baseline', 'champion_stress', 'challenger_stress')


def implementation_hash():
    services = Path(__file__).parent
    scripts = services.parents[1] / 'scripts'
    paths = [services / name for name in ('forward_experiment.py', 'forward_evaluation.py',
        'paper_comparison.py', 'comparison_model.py', 'feature_pipeline.py', 'research_training_v2.py',
        'research_training_v3.py', 'crypto_collection.py')]
    paths.extend(scripts / name for name in ('run_forward_experiment.py', 'run_continuous_training.py'))
    return hashlib.sha256(b''.join(p.read_bytes() for p in paths)).hexdigest()


def freeze_experiment(root, model_pins, *, start_at, runtime_image_id, now=None):
    now = utc_timestamp(now or datetime.now(timezone.utc))
    start = utc_timestamp(start_at)
    if start <= now or start.minute or start.second or start.microsecond:
        raise ValueError('Start must be a future aligned UTC hour')
    if not isinstance(runtime_image_id, str) or len(runtime_image_id) != 71 or not runtime_image_id.startswith('sha256:') or any(c not in '0123456789abcdef' for c in runtime_image_id[7:]):
        raise ValueError('Exact Docker runtime image SHA-256 required')
    if set(model_pins) != set(ARMS):
        raise ValueError('Four champion/challenger cost arms required')
    arms = {}
    shared_history = None
    for name, pin in model_pins.items():
        directory = Path(pin['directory']).resolve(strict=True)
        profile = name.rsplit('_', 1)[1]
        fee, slip = ('.008', '.001') if profile == 'baseline' else ('.01', '.002')
        declared = json.loads((directory / 'manifest.json').read_text())
        manifest, spec = load_comparison_model(directory, manifest_sha256=pin['manifest_sha256'],
            fee_rate=fee, slippage_rate=slip, source_claim=declared['source_claim'])
        history = {key: manifest[key] for key in ('dataset_sha256', 'source_claim', 'feature_config_id', 'horizon_bars')}
        if shared_history is not None and shared_history != history:
            raise ValueError('All four arms must share identical dataset, source, features and horizon')
        shared_history = history
        if max(datetime.fromisoformat(manifest['final_test_end']), datetime.fromisoformat(spec['verified_dataset_end']) + timedelta(hours=1)) >= start:
            raise ValueError('Future window overlaps training history')
        if name.startswith('challenger') and (manifest['selected_model'] != 'random_forest' or not manifest.get('selection_policy')):
            raise ValueError('Challenger must be an explicitly fixed random forest')
        config = ComparisonConfig(ml_model_id=manifest['run_id'] + ':' + pin['manifest_sha256'],
            fee_profile=profile, fee_rate=fee, slippage_rate=slip, ml_horizon_hours=manifest['horizon_bars'])
        arms[name] = dict(ledger_path=f'evidence/{name}.sqlite', model_directory=str(directory),
            model_run_id=manifest['run_id'], manifest_sha256=pin['manifest_sha256'],
            source_claim=manifest['source_claim'], comparison_config=asdict(config), config_sha256=config.sha256)
    for profile in ('baseline', 'stress'):
        if arms['champion_' + profile]['model_run_id'] == arms['challenger_' + profile]['model_run_id']:
            raise ValueError('Challenger must be distinct from champion')
    contract = dict(version='forward-experiment-v1', frozen_at=now.isoformat(),
        policy=dict(start_at=start.isoformat(), end_at=(start + timedelta(days=84)).isoformat(),
            minimum_days=84, minimum_roundtrips=100, minimum_coverage=.95,
            maximum_drawdown=.10, minimum_profit_factor=1.2), arms=arms,
        code_sha256=implementation_hash(), runtime_image_id=runtime_image_id, shared_history=shared_history,
        selection_rationale='Fixed RF challenger motivated by exploratory development walk-forward; no tuning in this future window',
        forecast_target='24-hour next-hour-close net-return proxy; entry decision_close+1h, exit+24h; separate from actual simulated strategy exits',
        decision_policy='One fixed final checkpoint; daily health inspection only, no early success or parameter changes',
        live_authorized=False, eligible_for_qualification=False)
    contract['experiment_id'] = hashlib.sha256(canonical(contract).encode()).hexdigest()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    (root / 'evidence').mkdir(mode=0o700)
    with (root / 'experiment.json').open('x') as handle:
        handle.write(canonical(contract) + '\n')
    return contract


def load_contract(root, *, check_code=False):
    path = Path(root) / 'experiment.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2*1024*1024:
        raise ValueError('Regular bounded experiment contract required')
    contract = json.loads(path.read_text())
    identity = contract.copy()
    digest = identity.pop('experiment_id')
    if hashlib.sha256(canonical(identity).encode()).hexdigest() != digest or contract['version'] != 'forward-experiment-v1':
        raise ValueError('Experiment contract hash mismatch')
    if set(contract['arms']) != set(ARMS) or contract.get('live_authorized') is not False:
        raise ValueError('Invalid experiment arms or authority')
    policy = contract['policy']
    start, end = (utc_timestamp(datetime.fromisoformat(policy[k])) for k in ('start_at', 'end_at'))
    if (utc_timestamp(datetime.fromisoformat(contract['frozen_at'])) >= start or start.minute or start.second or start.microsecond
            or end-start != timedelta(days=84) or policy['minimum_days'] != 84
            or policy['minimum_roundtrips'] != 100 or policy['minimum_coverage'] != .95
            or policy['maximum_drawdown'] != .10 or policy['minimum_profit_factor'] != 1.2):
        raise ValueError('Invalid fixed prospective evaluation policy')
    for name, arm in contract['arms'].items():
        if (arm['ledger_path'] != f'evidence/{name}.sqlite' or ComparisonConfig(**arm['comparison_config']).sha256 != arm['config_sha256']
                or arm['comparison_config']['ml_model_id'] != arm['model_run_id'] + ':' + arm['manifest_sha256']):
            raise ValueError('Invalid frozen arm configuration')
    if check_code and implementation_hash() != contract['code_sha256']:
        raise ValueError('Frozen execution code changed; do not silently resume a different experiment')
    return contract


def record_forecasts(path, contract, candles, predictions, *, observed_at):
    """Future labels may mature later; never insert a retrospective prediction."""
    from app.services.crypto_collection import _check
    clock = utc_timestamp(observed_at)
    rows = list(candles)
    _check(rows, clock, 51)
    bar = datetime.fromisoformat(rows[-1].opened_at) + timedelta(hours=1)
    start, end = (datetime.fromisoformat(contract['policy'][k]) for k in ('start_at', 'end_at'))
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Forecast database must not be a symlink')
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {'identity', 'forecasts', 'outcomes'}:
            raise ValueError('Refusing unrelated database')
        db.execute('CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1), experiment_id TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS forecasts (arm TEXT, bar_close TEXT, payload TEXT NOT NULL, PRIMARY KEY(arm,bar_close))')
        db.execute('CREATE TABLE IF NOT EXISTS outcomes (arm TEXT, bar_close TEXT, payload TEXT NOT NULL, PRIMARY KEY(arm,bar_close))')
        existing = db.execute('SELECT experiment_id FROM identity WHERE id=1').fetchone()
        if existing and existing[0] != contract['experiment_id']:
            raise ValueError('Forecast experiment identity mismatch')
        db.execute('INSERT OR IGNORE INTO identity VALUES (1,?)', (contract['experiment_id'],))
        if predictions:
            if set(predictions) != set(ARMS) or not start <= bar < end or not 0 <= (clock-bar).total_seconds() <= 300:
                raise ValueError('Require all four on-time prospective predictions inside window')
            for arm, pred in predictions.items():
                pin = contract['arms'][arm]
                p = pred['ml_probability']
                if pred['ml_model_id'] != pin['model_run_id'] + ':' + pin['manifest_sha256'] or not np.isfinite(p) or not 0 <= p <= 1:
                    raise ValueError('Invalid pinned forecast')
                data = dict(observed_at=clock.isoformat(), probability=p, model_id=pred['ml_model_id'],
                    data_sha256=hashlib.sha256(canonical([r.content_sha256 for r in rows]).encode()).hexdigest(),
                    entry_at=(bar+timedelta(hours=1)).isoformat(),
                    exit_at=(bar+timedelta(hours=1+pin['comparison_config']['ml_horizon_hours'])).isoformat())
                old = db.execute('SELECT payload FROM forecasts WHERE arm=? AND bar_close=?', (arm, bar.isoformat())).fetchone()
                if old:
                    previous = json.loads(old[0])
                    if any(previous[k] != data[k] for k in data if k != 'observed_at'):
                        raise ValueError('Forecast evidence cannot be revised')
                else:
                    db.execute('INSERT INTO forecasts VALUES (?,?,?)', (arm, bar.isoformat(), canonical(data)))
        prices = {(datetime.fromisoformat(r.opened_at)+timedelta(hours=1)).isoformat(): r for r in rows}
        pending = db.execute('SELECT f.arm,f.bar_close,f.payload FROM forecasts f LEFT JOIN outcomes o ON f.arm=o.arm AND f.bar_close=o.bar_close WHERE o.arm IS NULL').fetchall()
        for arm, stamp, payload in pending:
            forecast = json.loads(payload)
            entry, exit_ = prices.get(forecast['entry_at']), prices.get(forecast['exit_at'])
            if entry is None or exit_ is None:
                continue
            cfg = contract['arms'][arm]['comparison_config']
            fee, slip = float(cfg['fee_rate']), float(cfg['slippage_rate'])
            net = float(exit_.close)*(1-slip)*(1-fee)/(float(entry.close)*(1+slip)*(1+fee))-1
            outcome = dict(scored_at=clock.isoformat(), target=int(net > 0), net_return=net,
                entry_close=str(entry.close), exit_close=str(exit_.close), entry_hash=entry.content_sha256, exit_hash=exit_.content_sha256)
            db.execute('INSERT INTO outcomes VALUES (?,?,?)', (arm, stamp, canonical(outcome)))


def forecast_summary(root):
    """Read probability evidence without opening a database for writes."""
    from app.services.model_diagnostics import summarize
    contract = load_contract(root)
    path = Path(root) / 'evidence/forecasts.sqlite'
    if not path.exists():
        return {'status': 'waiting_for_forward_predictions', 'eligible_for_qualification': False}
    if path.is_symlink():
        raise ValueError('Invalid forecast database')
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True) as db:
        if db.execute('SELECT experiment_id FROM identity WHERE id=1').fetchone()[0] != contract['experiment_id']:
            raise ValueError('Forecast identity mismatch')
        result = {}
        for arm in ARMS:
            pairs = db.execute('SELECT f.payload,o.payload FROM forecasts f JOIN outcomes o ON f.arm=o.arm AND f.bar_close=o.bar_close WHERE f.arm=? ORDER BY f.bar_close', (arm,)).fetchall()
            result[arm] = dict(recorded=db.execute('SELECT COUNT(*) FROM forecasts WHERE arm=?', (arm,)).fetchone()[0],
                scored=summarize([json.loads(o)['target'] for f,o in pairs], [json.loads(f)['probability'] for f,o in pairs]))
    return dict(arms=result, label_proxy=contract['forecast_target'], eligible_for_qualification=False)
