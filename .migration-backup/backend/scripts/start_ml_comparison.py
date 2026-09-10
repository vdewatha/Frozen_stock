"""Launch a new isolated, pinned ML paper experiment; never replaces old evidence."""
import argparse
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess

NAME = 'trading-paper-comparison-ml'
IMAGE = 'trading-paper-comparison:20260906'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-model', type=Path, required=True)
    parser.add_argument('--stress-model', type=Path, required=True)
    parser.add_argument('--model-source-claim', required=True)
    args = parser.parse_args()
    if args.database.is_symlink() or not args.database.is_file() or args.output.is_symlink():
        parser.error('Require a regular source database and nonsymlink output')
    database, output = args.database.resolve(), args.output.resolve()
    if output == database.parent or output in database.parents:
        parser.error('Output must be separate from source state')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
        if connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            parser.error('Source must use DELETE journal mode for a single-file read-only mount')
    if subprocess.run(['docker', 'inspect', NAME], capture_output=True).returncode == 0:
        parser.error('ML comparison already exists; inspect it instead of duplicating it')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.stat().st_mode & 0o077 or any(output.iterdir()):
        parser.error('Require a fresh, empty, private output directory')
    mounts, options = [], []
    for profile in ('baseline', 'stress'):
        model = getattr(args, profile + '_model')
        if model.is_symlink() or not model.is_dir():
            parser.error('Model must be a nonsymlink artifact directory')
        model = model.resolve()
        manifest = model / 'manifest.json'
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 1048576:
            parser.error('Require a bounded regular manifest')
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        destination = '/models/' + profile + '/' + model.name
        mounts += ['--mount', f'type=bind,src={model},dst={destination},readonly']
        options += ['--' + profile + '-model', destination,
                    '--' + profile + '-manifest-sha256', digest]
    command = ['docker', 'run', '--detach', '--pull', 'never', '--name', NAME,
        '--restart', 'unless-stopped', '--network', 'none', '--read-only',
        '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--cpus', '1', '--memory', '1g',
        '--pids-limit', '128', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
        '--tmpfs', '/tmp', '--env', 'ALLOW_LIVE_TRADING=false',
        '--env', 'OPENBLAS_NUM_THREADS=1', '--env', 'OMP_NUM_THREADS=1',
        '--mount', f'type=bind,src={database},dst=/source/research.sqlite,readonly',
        '--mount', f'type=bind,src={output},dst=/comparison', *mounts,
        '--entrypoint', 'python', IMAGE, 'scripts/run_paper_comparison.py',
        '--source-db', '/source/research.sqlite', '--output', '/comparison',
        '--model-source-claim', args.model_source_claim, *options]
    subprocess.run(command, check=True, capture_output=True)
    print('Pinned ML paper comparison started; no live trading or original evidence changed.')


if __name__ == '__main__':
    main()
