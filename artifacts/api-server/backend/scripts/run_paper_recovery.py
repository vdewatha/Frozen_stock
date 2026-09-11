"""Persistent reconciliation only: never submit orders or clear a kill switch."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=30)
    args = parser.parse_args()
    if not 10 <= args.interval_seconds <= 3600:
        parser.error("Interval must be 10..3600 seconds")
    if not os.environ.get("DATABASE_URL"):
        parser.error("Set DATABASE_URL explicitly; this command never migrates databases")
    from app.core.config import settings
    from app.db.schema import assert_schema_current
    from app.db.session import engine, SessionLocal
    from app.integrations.freqtrade_execution import FreqtradeDryRunClient
    from app.services.paper_recovery import run_recovery_cycle
    if settings.allow_live_trading or not settings.freqtrade_paper_execution_enabled:
        parser.error("Explicit paper-execution enablement and live-trading disabled required")
    assert_schema_current(engine)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        with FreqtradeDryRunClient(settings.freqtrade_url, settings.freqtrade_username,
                settings.freqtrade_password.get_secret_value()) as client:
            while not stop.is_set():
                result = run_recovery_cycle(SessionLocal, client)
                print(json.dumps(result, sort_keys=True), flush=True)
                if not args.continuous:
                    return 0 if result["status"] in ("success", "inactive") else 1
                stop.wait(args.interval_seconds)
        return 0
    except Exception:
        # A failed DB audit cannot be presented as a healthy running service.
        print(json.dumps({"status": "error", "reason": "recovery_service_failed"}), flush=True)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
