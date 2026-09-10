"""Run a frozen, four-arm forward simulation with no broker or network access."""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
from pathlib import Path
import signal
import sys
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_continuous_training import readonly_source
from sqlalchemy.orm import Session
from app.services.crypto_collection import load_closed_history
from app.services.comparison_model import comparison_prediction
from app.services.paper_comparison import advance_comparison, ComparisonConfig
from app.services.forward_experiment import load_contract, record_forecasts


def cycle(root, contract, engine, *, now, models_root=None):
    start, end = (datetime.fromisoformat(contract['policy'][k]) for k in ('start_at', 'end_at'))
    if now < start:
        return {'status': 'scheduled', 'start_at': start.isoformat()}
    if now >= end + timedelta(hours=26):
        return {'status': 'completed', 'reason': 'fixed_window_and_label_maturation_elapsed', 'live_authorized': False}
    if now.minute >= 5:
        return {'status': 'waiting', 'reason': 'next_hour_observation_window'}
    with Session(engine) as session:
        candles = load_closed_history(session, as_of=now, minimum=51)
    predictions, reports = {}, {}
    if now < end:
        # Validate every arm before writing any new comparison step. A failure
        # cannot silently give one family more favorable observation coverage.
        for name, arm in contract['arms'].items():
            model = Path(models_root) / name / arm['model_run_id'] if models_root else Path(arm['model_directory'])
            cfg = arm['comparison_config']
            predictions[name] = comparison_prediction(model, candles, observed_at=now,
                fee_rate=cfg['fee_rate'], slippage_rate=cfg['slippage_rate'],
                source_claim=arm['source_claim'], manifest_sha256=arm['manifest_sha256'])
        record_forecasts(Path(root) / 'evidence/forecasts.sqlite', contract, candles, predictions, observed_at=now)
        for name, arm in contract['arms'].items():
            pred = predictions[name]
            reports[name] = advance_comparison(Path(root) / arm['ledger_path'], candles, observed_at=now,
                ml_probability=pred['ml_probability'], ml_expected_net=pred['ml_expected_net'],
                config=ComparisonConfig(**arm['comparison_config']))
        return {'status': 'observed', 'bar_close': reports[next(iter(reports))]['bar_close'],
            'arms': {name: {'probability': predictions[name]['ml_probability'],
                'expected_net': predictions[name]['ml_expected_net']} for name in reports}, 'live_authorized': False}
    record_forecasts(Path(root) / 'evidence/forecasts.sqlite', contract, candles, {}, observed_at=now)
    return {'status': 'maturing_labels', 'live_authorized': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--models-root', type=Path)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    contract = load_contract(args.root, check_code=True)
    lockpath = args.root / 'evidence/.worker.lock'
    if lockpath.is_symlink():
        parser.error('Invalid worker lock')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with lockpath.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        engine = readonly_source(args.source_db)
        try:
            while not stop.is_set():
                now = datetime.now(timezone.utc)
                try:
                    result = cycle(args.root, contract, engine, now=now, models_root=args.models_root)
                except Exception as exc:
                    result = {'status': 'blocked', 'error_type': type(exc).__name__, 'reason': str(exc)[:160]}
                print(json.dumps(result | {'observed_at': now.isoformat()}, sort_keys=True), flush=True)
                if args.once:
                    return int(result['status'] == 'blocked')
                if result['status'] == 'completed':
                    # Remain quiescent so restart-unless-stopped can survive
                    # daemon restarts without restarting a finished experiment.
                    stop.wait()
                    return 0
                stop.wait(30)
        finally:
            engine.dispose()


if __name__ == '__main__':
    sys.exit(main())
