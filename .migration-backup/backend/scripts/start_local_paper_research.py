"""Start the explicitly approved local paper pair; never initialize/reset accounts."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.integrations.paper_venue import kraken_execution_config, write_config
from app.integrations.freqtrade_execution import FreqtradeDryRunClient
from app.integrations.freqtrade import FreqtradeError
from run_paper_venue import launch_arguments

PROVIDER = "trading-paper-kraken"
WORKER = "trading-paper-research"


def worker_arguments(state, approval_id):
    return ["docker", "run", "--detach", "--pull", "never", "--name", WORKER, "--restart", "unless-stopped",
        "--network", "container:" + PROVIDER, "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--memory", "1g", "--cpus", "1", "--pids-limit", "128",
        "--tmpfs", "/tmp", "--mount", f"type=bind,src={state},dst=/paper-state",
        "--env", "DATABASE_URL=sqlite:////paper-state/research.sqlite", "--env", "ALLOW_LIVE_TRADING=false",
        "trading-paper-research:20260904", "--credentials", "/paper-state/provider.json",
        "--approval-id", str(approval_id), "--continuous", "--execute-paper"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--approval-id", type=int, required=True)
    args = parser.parse_args()
    state = args.state_dir.resolve(strict=True)
    if state.name != ".paper-venue" or state.stat().st_mode & 0o077:
        parser.error("Private mode0700 .paper-venue directory required")
    database = state / "research.sqlite"
    if database.is_symlink() or not database.is_file() or args.approval_id < 1:
        parser.error("Existing migrated research.sqlite and explicit trial approval required")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.schema import assert_schema_current
    from app.models.execution import PaperTrialApproval, PaperExecutionAccount
    engine = create_engine(f"sqlite:///{database}")
    try:
        assert_schema_current(engine)
        with Session(engine) as db:
            approval = db.get(PaperTrialApproval, args.approval_id)
            account = db.get(PaperExecutionAccount, 1)
            if not approval or not approval.active or not approval.nonqualifying or not account or account.kill_switch:
                parser.error("Active nonqualifying paper approval and explicitly enabled account required")
    finally:
        engine.dispose()
    for name in (PROVIDER, WORKER):
        if subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0:
            parser.error("Managed container already exists; inspect it instead of replacing state")
    credentials = state / "provider.json"
    config = kraken_execution_config("paper-research", secrets.token_urlsafe(40), secrets.token_urlsafe(40))
    write_config(credentials, config)  # Exclusive; never replaces existing secrets.
    provider_args = launch_arguments(state, 8080)
    provider_args.remove("--rm")
    provider_args[2:2] = ["--detach", "--name", PROVIDER, "--restart", "unless-stopped"]
    environment = dict(os.environ, PAPER_LOCAL_CONFIG=json.dumps(config))
    subprocess.run(provider_args, env=environment, check=True, capture_output=True)
    api = config["api_server"]
    with FreqtradeDryRunClient("http://127.0.0.1:8080", api["username"], api["password"]) as client:
        deadline = time.monotonic() + 120
        while True:
            try:
                client.verify()
                break
            except FreqtradeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Paper provider not ready; worker was not started") from None
                time.sleep(2)
    subprocess.run(worker_arguments(state, args.approval_id), check=True, capture_output=True)
    print("Paper provider and approved research worker started; no live funds or exchange keys.")


if __name__ == "__main__":
    main()
