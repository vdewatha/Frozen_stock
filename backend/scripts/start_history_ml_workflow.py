"""Start a credential-free public-history and isolated paper-research workflow."""
import argparse
import os
from pathlib import Path
import sqlite3
import subprocess

NAME = 'trading-history-ml-workflow'
IMAGE = 'trading-paper-comparison:20260906-gaps'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-verified-gaps', action='store_true')
    args = parser.parse_args()
    if args.database.is_symlink() or not args.database.is_file() or args.output.is_symlink():
        parser.error('Require regular source database and nonsymlink output')
    database, output = args.database.resolve(), args.output.resolve()
    if output in database.parents:
        parser.error('Workflow output must be separate from source state')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
        if connection.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            parser.error('Single-file read-only source mount requires DELETE journal mode')
    if subprocess.run(['docker', 'inspect', NAME], capture_output=True).returncode == 0:
        parser.error('Workflow already exists; inspect it rather than duplicating it')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.stat().st_mode & 0o077:
        parser.error('Output must have private mode0700 permissions')
    command = ['docker', 'run', '--detach', '--pull', 'never', '--name', NAME,
        '--restart', 'on-failure:3', '--network', 'bridge', '--read-only',
        '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--cpus', '1', '--memory', '1g',
        '--pids-limit', '128', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
        '--tmpfs', '/tmp', '--env', 'ALLOW_LIVE_TRADING=false',
        '--env', 'OPENBLAS_NUM_THREADS=1', '--env', 'OMP_NUM_THREADS=1',
        '--mount', f'type=bind,src={database},dst=/source/research.sqlite,readonly',
        '--mount', f'type=bind,src={output},dst=/research',
        '--entrypoint', 'python', IMAGE, 'scripts/run_history_ml_workflow.py',
        '--source-db', '/source/research.sqlite', '--output', '/research',
        *(['--allow-verified-gaps'] if args.allow_verified_gaps else [])]
    subprocess.run(command, check=True, capture_output=True)
    print('Historical public-data workflow started. Only its research directory is writable; no exchange credentials.')


if __name__ == '__main__':
    main()
