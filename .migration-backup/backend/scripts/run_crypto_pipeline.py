"""Credential-free persistent Kraken observations and research-only shadow runs."""
import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description="Collect Kraken and run shadow models; never sends orders")
    parser.add_argument("--continuous", action="store_true", help="Repeat until SIGINT/SIGTERM; otherwise run once")
    parser.add_argument("--interval-seconds", type=int, default=60, help="60..3600 seconds; default 60")
    args = parser.parse_args()
    if not 60 <= args.interval_seconds <= 3600:
        parser.error("Interval must be between 60 and 3600 seconds")
    if not os.environ.get("DATABASE_URL"):
        parser.error("Set DATABASE_URL explicitly; this command never migrates a database")
    from app.db.schema import assert_schema_current
    from app.db.session import engine, SessionLocal
    from app.services.crypto_pipeline import run_crypto_cycle
    assert_schema_current(engine)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO)
    try:
        while not stop.is_set():
            try:
                result = run_crypto_cycle(SessionLocal)
            except Exception:
                # Preserve no credentials/SQL/URLs in process output.
                result = {"status": "error", "reason": "pipeline_stage_failed"}
            print(json.dumps(result, sort_keys=True), flush=True)
            if not args.continuous:
                return 0 if result["status"] == "success" else 1
            stop.wait(args.interval_seconds)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
