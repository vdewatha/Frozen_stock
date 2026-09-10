"""Explicit, resumable 2025 public-trade backfill. Never imports a live database."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import signal
import sys
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.kraken_trade_backfill import open_backfill, fetch_page, ingest_page, export_hourly, MAX_DB


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--max-pages-this-run", type=int, default=1000)
    parser.add_argument('--gap-audit', type=Path, help='Explicit verified no-trade gap evidence; no candle filling')
    args = parser.parse_args()
    if not 1 <= args.max_pages_this_run <= 100000: parser.error("Run page budget must be 1..100000")
    root = args.output_directory
    if root.is_symlink(): parser.error("No symlink output directory")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077: parser.error("Private output directory required")
    lock = root / ".backfill.lock"
    if lock.is_symlink(): parser.error("Invalid lock")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): signal.signal(sig, lambda *_: stop.set())
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = open_backfill(root / "trade-backfill.sqlite")
        try:
            errors = 0
            for _ in range(args.max_pages_this_run):
                if stop.wait(1): return 0  # >=1s between requests, including restart.
                if (root / "trade-backfill.sqlite").stat().st_size > MAX_DB: raise ValueError("DB byte cap exceeded")
                state = db.execute("SELECT * FROM backfill_state WHERE id=1").fetchone()
                if state["complete"]:
                    print(json.dumps(export_hourly(db, root / "XBTUSD_60.csv", gap_audit=args.gap_audit)), flush=True)
                    return 0
                try:
                    result = ingest_page(db, state["cursor"], fetch_page(state["cursor"]))
                    errors = 0
                    print(json.dumps(result), flush=True)
                except Exception as error:
                    db.rollback()
                    db.execute("INSERT INTO backfill_errors(observed_at,reason) VALUES(?,?)", (datetime.now(timezone.utc).isoformat(), type(error).__name__))
                    db.commit()
                    errors += 1
                    print(json.dumps({"status": "blocked", "error_type": type(error).__name__}), flush=True)
                    if errors >= 5: return 1
                    if stop.wait(min(60, 2**errors)): return 0
            print(json.dumps({"status": "paused", "reason": "per_run_page_budget", "resume_supported": True}), flush=True)
            return 0
        finally: db.close()


if __name__ == "__main__": raise SystemExit(main())
