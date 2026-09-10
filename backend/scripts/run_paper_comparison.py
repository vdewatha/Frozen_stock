"""Forward-only isolated comparisons with optional explicitly pinned research ML."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_continuous_training import readonly_source
from sqlalchemy.orm import Session
from app.services.crypto_collection import load_closed_history
from app.services.paper_comparison import ComparisonConfig, advance_comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--model-source-claim')
    for profile in ('baseline', 'stress'):
        parser.add_argument('--' + profile + '-model', type=Path)
        parser.add_argument('--' + profile + '-manifest-sha256')
    args = parser.parse_args()
    for profile in ('baseline', 'stress'):
        model = getattr(args, profile + '_model')
        digest = getattr(args, profile + '_manifest_sha256')
        if bool(model) != bool(digest) or (model and not args.model_source_claim):
            parser.error('Each model requires its manifest SHA256 and explicit source claim')
    if args.output.is_symlink():
        parser.error('Output must not be a symbolic link')
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.output.resolve() == args.source_db.resolve().parent:
        parser.error('Output must be separate from the source database directory')
    os.chmod(args.output, 0o700)
    if (args.output / '.worker.lock').is_symlink():
        parser.error('Lock must not be a symbolic link')
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with (args.output / '.worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pinned = {}
        for profile, fee, slip in [('baseline', '.008', '.001'), ('stress', '.01', '.002')]:
            model = getattr(args, profile + '_model')
            if model:
                from app.services.comparison_model import load_comparison_model
                digest = getattr(args, profile + '_manifest_sha256')
                manifest, _ = load_comparison_model(model, fee_rate=fee, slippage_rate=slip,
                    source_claim=args.model_source_claim, manifest_sha256=digest)
                pinned[profile] = dict(ml_model_id=manifest['run_id'] + ':' + digest,
                    ml_probability=None, ml_expected_net=None, horizon_hours=manifest['horizon_bars'])
        engine = readonly_source(args.source_db)
        try:
            while not stop.is_set():
                now = datetime.now(timezone.utc)
                failed = False
                try:
                    with Session(engine) as session:
                        candles = load_closed_history(session, as_of=now, minimum=51)
                    reports = {}
                    for profile, fee, slip in [('baseline', '.008', '.001'), ('stress', '.01', '.002')]:
                        prediction = dict(ml_model_id='unavailable', ml_probability=None,
                                          ml_expected_net=None, horizon_hours=24)
                        model = getattr(args, profile + '_model')
                        model_error = None
                        if model:
                            from app.services.comparison_model import comparison_prediction
                            prediction = pinned[profile].copy()
                            try:
                                prediction = comparison_prediction(model, candles, observed_at=now,
                                    fee_rate=fee, slippage_rate=slip, source_claim=args.model_source_claim,
                                    manifest_sha256=getattr(args, profile + '_manifest_sha256'))
                            except Exception as error:
                                model_error = type(error).__name__
                        reports[profile] = advance_comparison(args.output / (profile + '.sqlite'),
                            candles, observed_at=now, ml_probability=prediction['ml_probability'],
                            ml_expected_net=prediction['ml_expected_net'], config=ComparisonConfig(
                                ml_model_id=prediction['ml_model_id'], ml_horizon_hours=prediction['horizon_hours'],
                                fee_profile=profile, fee_rate=fee, slippage_rate=slip))
                        if model_error:
                            reports[profile]['ml_inference_error'] = model_error
                    print(json.dumps(reports, sort_keys=True), flush=True)
                    failed = any(report['status'] == 'blocked' or report.get('ml_inference_error') for report in reports.values())
                except Exception as error:
                    failed = True
                    print(json.dumps({'status': 'blocked', 'error_type': type(error).__name__}), flush=True)
                if args.once:
                    return 1 if failed else 0
                stop.wait(60)
        finally:
            engine.dispose()


if __name__ == '__main__':
    sys.exit(main())
