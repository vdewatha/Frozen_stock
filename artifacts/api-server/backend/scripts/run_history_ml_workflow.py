"""One fixed 2025 backfill -> two fixed-cost candidates -> isolated paper research.

No exchange credentials, broker, original-account writes, or model replacement.
Training failures are persisted and require review instead of endless retries.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.kraken_history import read_kraken_csv
from app.services.research_training_v3 import train_research_v3
from app.services.comparison_model import load_comparison_model
from run_continuous_training import published_manifest


def prepare_candidates(root, *, trainer=train_research_v3, allow_verified_gaps=False):
    """Train only on fully covered immutable data; return fixed artifact pins."""
    root = Path(root)
    history = root / 'backfill' / 'XBTUSD_60.csv'
    provenance = history.with_name('provenance.json')
    if provenance.is_symlink() or not provenance.is_file() or provenance.stat().st_size > 1048576:
        raise ValueError('Expected bounded backfill provenance')
    provenance_bytes = provenance.read_bytes()
    metadata = json.loads(provenance_bytes)
    missing = []
    if allow_verified_gaps:
        from app.services.kraken_gap_audit import verify_audit
        database = root / 'backfill/trade-backfill.sqlite'
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            missing, audit_hash = verify_audit(db, root / 'backfill/gap-audit.json')
        if (metadata.get('gap_policy') != 'segment' or metadata.get('missing_hours') != missing
                or metadata.get('calendar_hours') != 8760 or metadata.get('gap_audit_sha256') != audit_hash):
            raise ValueError('Explicit segmented provenance or gap evidence mismatch')
    frame, digest = read_kraken_csv(history, start='2025-01-01T00:00:00+00:00',
        end='2026-01-01T00:00:00+00:00', **({'allowed_missing_hours': missing} if allow_verified_gaps else {}))
    if len(frame) + len(missing) != 8760:
        raise ValueError('Expected complete calendar accounting for 2025')
    if (metadata.get('csv_sha256') != digest or metadata.get('rows') != len(frame)
            or metadata.get('source_url') != 'https://api.kraken.com/0/public/Trades'
            or metadata.get('instrument') != 'crypto_spot:KRAKEN:BTC:USD'
            or metadata.get('start') != 1735689600 or metadata.get('end') != 1767225600
            or metadata.get('format') != ('kraken-public-trades-backfill-v2' if allow_verified_gaps else 'kraken-public-trades-backfill-v1')
            or metadata.get('eligible_for_trading') is not False):
        raise ValueError('Backfill provenance mismatch')
    source = 'kraken-public-trades-backfill:' + hashlib.sha256(provenance_bytes).hexdigest()
    with sqlite3.connect(root / 'workflow.sqlite') as state:
        state.execute('CREATE TABLE IF NOT EXISTS training_attempts(profile TEXT PRIMARY KEY, attempted INTEGER NOT NULL)')
        results = {}
        for profile, fee, slip in [('baseline', .008, .001), ('stress', .01, .002)]:
            output = root / 'candidates' / profile
            manifest = published_manifest(output)
            if manifest is None:
                attempt = state.execute('SELECT attempted FROM training_attempts WHERE profile=?', (profile,)).fetchone()
                if attempt:
                    raise ValueError('Unfinished training attempt requires review; no automatic retraining')
                state.execute('INSERT INTO training_attempts VALUES (?,1)', (profile,))
                state.commit()
                manifest = trainer(frame, output, source=source, fee_rate=fee, slippage_rate=slip,
                    **({'gap_policy': 'segment'} if allow_verified_gaps else {}))
            directory = output / manifest['run_id']
            pin = hashlib.sha256((directory / 'manifest.json').read_bytes()).hexdigest()
            load_comparison_model(directory, fee_rate=fee, slippage_rate=slip,
                source_claim=source, manifest_sha256=pin)
            results[profile] = {'directory': str(directory), 'manifest_sha256': pin,
                                'run_id': manifest['run_id'], 'metrics': manifest['final_test_metrics']}
    return {'source_claim': source, 'profiles': results, 'eligible_for_trading': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--allow-verified-gaps', action='store_true')
    args = parser.parse_args()
    root = args.output
    if root.is_symlink():
        parser.error('No symlink output')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077 or root.resolve() in args.source_db.resolve().parents:
        parser.error('Require private output separate from source state')
    if (root / '.workflow.lock').is_symlink():
        parser.error('No symlink lock')
    with (root / '.workflow.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        scripts = Path(__file__).parent
        print(json.dumps({'phase': 'historical_backfill', 'live_authorized': False}), flush=True)
        subprocess.run([sys.executable, str(scripts / 'backfill_kraken_trades.py'),
            '--output-directory', str(root / 'backfill'), '--max-pages-this-run', '100000',
            *(['--gap-audit', str(root / 'backfill/gap-audit.json')] if args.allow_verified_gaps else [])], check=True)
        # The downloader verifies completion and exports only after every hour exists.
        print(json.dumps({'phase': 'training'}), flush=True)
        result = prepare_candidates(root, allow_verified_gaps=args.allow_verified_gaps)
        selection = root / 'pinned_models.json'
        payload = json.dumps(result, sort_keys=True, indent=2, allow_nan=False).encode()
        if selection.exists():
            if selection.is_symlink() or selection.read_bytes() != payload:
                raise ValueError('Pinned selection changed; review required')
        else:
            with selection.open('xb') as handle:
                handle.write(payload)
        command = [sys.executable, str(scripts / 'run_paper_comparison.py'),
                   '--source-db', str(args.source_db), '--output', str(root / 'comparison'),
                   '--model-source-claim', result['source_claim']]
        for profile, info in result['profiles'].items():
            command += ['--' + profile + '-model', info['directory'],
                        '--' + profile + '-manifest-sha256', info['manifest_sha256']]
        print(json.dumps({'phase': 'pinned_paper_comparison', 'models': result, 'live_authorized': False}), flush=True)
        # Keep the workflow lock across exec; the comparison also has its own lock.
        os.set_inheritable(lock.fileno(), True)
        os.execv(sys.executable, command)


if __name__ == '__main__':
    main()
