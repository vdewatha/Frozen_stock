"""Start only the frozen isolated forward paper worker; no exchange access."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.forward_experiment import load_contract

NAME = 'trading-forward-rf-experiment'


def launch(root, source, *, runner=subprocess.run):
    if Path(root).is_symlink() or Path(source).is_symlink():
        raise ValueError('Regular experiment/source paths required')
    root, source = Path(root).resolve(strict=True), Path(source).resolve(strict=True)
    contract = load_contract(root, check_code=True)
    image = contract['runtime_image_id']
    if not image.startswith('sha256:') or len(image) != 71 or any(c not in '0123456789abcdef' for c in image[7:]):
        raise ValueError('Exact frozen image digest required')
    if datetime.now(timezone.utc) >= datetime.fromisoformat(contract['policy']['start_at']):
        raise ValueError('Launch before frozen start; do not silently shorten the experiment')
    evidence = root / 'evidence'
    if evidence.is_symlink() or not evidence.is_dir() or any(evidence.iterdir()) or evidence.stat().st_mode & 0o077:
        raise ValueError('First launch requires a fresh private evidence directory')
    if source.is_relative_to(root) or root.is_relative_to(source.parent):
        raise ValueError('Separate source state and experiment required')
    with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as db:
        if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            raise ValueError('Read-only single-file source mount requires DELETE journal mode')
    if runner(['docker', 'inspect', NAME], capture_output=True).returncode == 0:
        raise ValueError('Forward experiment container already exists; do not duplicate/reset it')
    mounts = []
    for name, arm in contract['arms'].items():
        model = Path(arm['model_directory'])
        if model.is_symlink() or not model.is_dir():
            raise ValueError('Regular pinned model directory required')
        mounts += ['--mount', f'type=bind,src={model},dst=/models/{name}/{arm["model_run_id"]},readonly']
    command = ['docker', 'run', '--detach', '--pull', 'never', '--name', NAME,
        '--restart', 'unless-stopped', '--network', 'none', '--read-only',
        '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--cpus', '1', '--memory', '1g', '--pids-limit', '128',
        '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3', '--tmpfs', '/tmp',
        '--env', 'ALLOW_LIVE_TRADING=false', '--env', 'OPENBLAS_NUM_THREADS=1', '--env', 'OMP_NUM_THREADS=1',
        '--mount', f'type=bind,src={source},dst=/source/research.sqlite,readonly',
        '--mount', f'type=bind,src={root / "experiment.json"},dst=/experiment/experiment.json,readonly',
        '--mount', f'type=bind,src={evidence},dst=/experiment/evidence', *mounts,
        '--entrypoint', 'python', image, 'scripts/run_forward_experiment.py',
        '--root', '/experiment', '--source-db', '/source/research.sqlite', '--models-root', '/models']
    runner(command, check=True, capture_output=True)
    return {'container': NAME, 'experiment_id': contract['experiment_id'], 'status': 'started', 'live_authorized': False}


if __name__ == '__main__':
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source-db', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(launch(args.root, args.source_db), sort_keys=True))
