"""One-shot stopped Freqtrade probe; disposable container, no host ports or orders."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.integrations.paper_venue import kraken_observation_config, kraken_execution_config

IMAGE = "freqtradeorg/freqtrade@sha256:7031bca43ed7668ebf421725dd5016acade6ef88b0771db3e08c96e6d19a42db"
PROBE = '''
import json, base64, urllib.request, importlib.util
config=json.load(open('/tmp/probe-config.json'))
auth=config['api_server']
header='Basic '+base64.b64encode((auth['username']+':'+auth['password']).encode()).decode()
def get(endpoint):
    request=urllib.request.Request('http://127.0.0.1:8080/api/v1/'+endpoint, headers={'Authorization':header})
    with urllib.request.urlopen(request, timeout=3) as response: return json.load(response)
assert get('ping')['status']=='pong'
settings=get('show_config')
assert settings['dry_run'] is True
assert settings['state']=='stopped'
assert settings['exchange']=='kraken'
assert settings['strategy']=='ObservationOnly'
trades=get('status')
assert isinstance(trades,list) and len(trades)==0
spec=importlib.util.spec_from_file_location('project_adapter','/probe_client.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with module.FreqtradeClient('http://127.0.0.1:8080',auth['username'],auth['password']) as client:
    assert client.ping() is True
    assert client.status()['open_trade_count']==0
print(json.dumps({'ok':True,'adapter_verified':True,'dry_run':True,'state':'stopped','exchange':'kraken','open_trades':0,'version':settings.get('version')}))
'''
BOOTSTRAP = '''
import json, os
config=json.loads(os.environ.pop('PAPER_PROBE_CONFIG'))
fd=os.open('/tmp/probe-config.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as stream: json.dump(config,stream)
os.execvp('freqtrade',['freqtrade','trade','--dry-run','--config','/tmp/probe-config.json',
    '--userdir','/freqtrade/user_data','--db-url','sqlite:////tmp/probe.dryrun.sqlite',
    '--strategy-path','/strategies','--strategy','ObservationOnly'])
'''


def command(args, timeout=20, env=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)


def main(execution=False):
    name = "trading-observation-smoke-" + uuid4().hex[:12]
    password, jwt = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    factory = kraken_execution_config if execution else kraken_observation_config
    environment = dict(os.environ, PAPER_PROBE_CONFIG=json.dumps(factory("observer", password, jwt)))
    strategy = Path(__file__).resolve().parents[1] / "freqtrade_strategies"
    adapter = Path(__file__).resolve().parents[1] / "app" / "integrations" / "freqtrade.py"
    application = Path(__file__).resolve().parents[1] / "app"
    probe_code = PROBE
    if execution:
        probe_code = PROBE.replace("'stopped'", "'running'") + '''
import sys
sys.path.insert(0,'/project')
from app.integrations.freqtrade_execution import FreqtradeDryRunClient
with FreqtradeDryRunClient('http://127.0.0.1:8080',auth['username'],auth['password']) as client:
    client.verify()
    assert client.trades()==[]
print('Execution profile/adapter verified; no orders submitted.')
'''
    try:
        result = command(["docker", "run", "-d", "--pull", "never", "--name", name,
                "--entrypoint", "python", "--env", "PAPER_PROBE_CONFIG",
                "--memory", "2g", "--cpus", "2", "--pids-limit", "256",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
                "--tmpfs", "/tmp", "--tmpfs", "/freqtrade/user_data:uid=1000,gid=1000,mode=0700",
                "--mount", f"type=bind,src={strategy},dst=/strategies,readonly",
                "--mount", f"type=bind,src={adapter},dst=/probe_client.py,readonly",
                "--mount", f"type=bind,src={application},dst=/project/app,readonly",
                IMAGE, "-c", BOOTSTRAP], timeout=30, env=environment)
        if result.returncode:
            print("Could not start isolated Freqtrade probe; check Docker and pull the pinned image.")
            print(result.stderr[-1200:].replace(password, "[redacted]").replace(jwt, "[redacted]"))
            return 1
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state = command(["docker", "inspect", "--format", "{{.State.Running}}", name])
            if state.stdout.strip() != "true":
                break
            probe = command(["docker", "exec", name, "python", "-c", probe_code])
            if probe.returncode == 0:
                print(probe.stdout.strip())
                return 0
            time.sleep(3)
        logs = command(["docker", "logs", "--tail", "80", name])
        errors = (logs.stdout + logs.stderr).splitlines()
        for line in errors[-25:]:
            print(line.replace(password, "[redacted]").replace(jwt, "[redacted]"))
        print("Stopped dry-run health probe failed; no model or trading enabled.")
        return 1
    finally:
        # This exact randomly named probe has no user data volumes.
        command(["docker", "rm", "--force", "--volumes", name])


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-profile", action="store_true", help="Verify running dry-run API, still submit no orders")
    raise SystemExit(main(parser.parse_args().execution_profile))
