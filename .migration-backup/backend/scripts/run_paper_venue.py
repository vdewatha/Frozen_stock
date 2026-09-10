"""Run pinned credential-free Freqtrade with durable paper state on localhost.

Foreground process: Ctrl-C stops the container, never deletes paper state.
Only three explicitly selected local API credentials are passed to the image.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.integrations.paper_venue import kraken_execution_config
from smoke_freqtrade import IMAGE

BOOTSTRAP = '''
import json, os
config=json.loads(os.environ.pop('PAPER_LOCAL_CONFIG'))
config['api_server']['listen_ip_address']='0.0.0.0'
fd=os.open('/tmp/paper-config.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as stream: json.dump(config,stream)
os.execvp('freqtrade',['freqtrade','trade','--dry-run','--config','/tmp/paper-config.json',
    '--userdir','/freqtrade/user_data','--db-url','sqlite:////paper-state/execution.dryrun.sqlite',
    '--strategy-path','/strategies','--strategy','ObservationOnly'])
'''


def launch_arguments(state_dir, port):
    if not 1024 <= port <= 65535:
        raise ValueError("Use an unprivileged localhost port")
    path = Path(state_dir).resolve(strict=True)
    if not path.is_dir() or path.name != ".paper-venue" or path.stat().st_mode & 0o077:
        raise ValueError("Use a private mode0700 directory named .paper-venue")
    strategy = Path(__file__).resolve().parents[1] / "freqtrade_strategies"
    return ["docker", "run", "--rm", "--init", "--pull", "never", "--stop-timeout", "30",
            "--entrypoint", "python", "--env", "PAPER_LOCAL_CONFIG", "--memory", "2g", "--cpus", "2",
            "--pids-limit", "256", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
            "--publish", f"127.0.0.1:{port}:8080", "--tmpfs", "/tmp",
            "--tmpfs", "/freqtrade/user_data:uid=1000,gid=1000,mode=0700",
            "--mount", f"type=bind,src={path},dst=/paper-state",
            "--mount", f"type=bind,src={strategy},dst=/strategies,readonly", IMAGE, "-c", BOOTSTRAP]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    try:
        config = kraken_execution_config(os.environ.get("FREQTRADE_USERNAME", ""),
            os.environ.get("FREQTRADE_PASSWORD", ""), os.environ.get("FREQTRADE_JWT_SECRET", ""))
        command = launch_arguments(args.state_dir, args.port)
    except (ValueError, OSError):
        print("Check private paper directory, localhost port and distinct local API secrets.", file=sys.stderr)
        return 1
    # Docker receives no FREQTRADE__ exchange/config environment overrides.
    environment = dict(os.environ, PAPER_LOCAL_CONFIG=json.dumps(config))
    return subprocess.call(command, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
