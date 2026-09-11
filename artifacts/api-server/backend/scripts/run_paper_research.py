"""Real-time paper research worker; no live money or automatic model promotion."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run_cycle(factory, client, approval_id):
    from app.services.crypto_pipeline import run_crypto_cycle
    from app.services.paper_trial_cycle import run_paper_trial_cycle
    from app.services.paper_recovery import run_recovery_cycle
    client.verify()
    try:
        data = run_crypto_cycle(factory)
    except Exception:
        data = {"status": "error", "reason": "data_stage_failed"}
    trial = (run_paper_trial_cycle(factory, client, approval_id)
             if data["status"] == "success" else run_recovery_cycle(factory, client))
    return {"data": data, "trial": trial}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--approval-id", type=int, required=True)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--execute-paper", action="store_true", help="Explicitly run the approved nonqualifying trial")
    args = parser.parse_args()
    if not args.execute_paper or args.approval_id < 1 or not os.environ.get("DATABASE_URL"):
        parser.error("Explicit database, paper approval and --execute-paper required")
    from app.core.config import settings
    from app.db.session import engine, SessionLocal
    from app.db.schema import assert_schema_current
    from app.integrations.freqtrade_execution import FreqtradeDryRunClient
    if settings.allow_live_trading:
        parser.error("Live trading must remain disabled")
    assert_schema_current(engine)
    config = json.loads(args.credentials.read_text())
    if (config.get("dry_run") is not True or config.get("exchange", {}).get("key") != ""
            or config.get("exchange", {}).get("secret") != ""):
        parser.error("Credential-free dry-run provider configuration required")
    api = config["api_server"]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        with FreqtradeDryRunClient("http://127.0.0.1:8080", api["username"], api["password"]) as client:
            while not stop.is_set():
                # Exit on provider loss so the container supervisor reconnects
                # the shared network namespace after a provider restart.
                # Reconciliation is required even when collection fails; the
                # trial service separately rejects old observations for orders.
                print(json.dumps(run_cycle(SessionLocal, client, args.approval_id), sort_keys=True), flush=True)
                if not args.continuous:
                    return 0
                stop.wait(60)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
