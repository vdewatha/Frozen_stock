"""Train hourly research candidates, never promote models or authorize orders."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import json
from pathlib import Path
import signal
import sqlite3
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def readonly_source(path):
    """Use SQLite mode=ro (not immutable: the collector uses a live WAL)."""
    from sqlalchemy import create_engine, event
    path = Path(path).resolve(strict=True)
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(
        path.as_uri() + "?mode=ro", uri=True, timeout=30))
    @event.listens_for(engine, "connect")
    def protect(connection, _):
        connection.execute("PRAGMA query_only=ON")
    return engine


def load_snapshot(engine, now):
    import pandas as pd
    from sqlalchemy.orm import Session
    from app.services.crypto_collection import load_closed_history
    with Session(engine) as session:
        rows = load_closed_history(session, as_of=now, minimum=300)
        cutoff = rows[-1].opened_at
        frame = pd.DataFrame([{"date": row.opened_at, "close": str(row.close),
                               "volume": str(row.volume)} for row in rows])
    return cutoff, frame


def published_manifest(directory):
    manifests = list(directory.glob("*/manifest.json"))
    if not manifests:
        return None
    if len(manifests) != 1:
        raise ValueError("ambiguous_candidate_artifacts")
    path = manifests[0]
    manifest = json.loads(path.read_text())
    if manifest.get("eligible_for_trading") is not False or manifest.get("status") != "experimental":
        raise ValueError("invalid_candidate_manifest")
    files = manifest.get("files", {})
    if not files or path.parent.name != manifest.get("run_id"):
        raise ValueError("invalid_candidate_manifest")
    for name, digest in files.items():
        if Path(name).name != name or (path.parent / name).is_symlink():
            raise ValueError("invalid_candidate_path")
        if hashlib.sha256((path.parent / name).read_bytes()).hexdigest() != digest:
            raise ValueError("candidate_integrity_failure")
    return manifest


def training_cycle(state, output, engine, now, *, interval_hours=1, max_attempts=3,
                   loader=load_snapshot, trainer=None):
    if interval_hours < 1 or max_attempts < 1:
        raise ValueError("invalid_training_policy")
    if trainer is None:
        from app.services.research_training import train_research_run
        trainer = train_research_run
    cutoff, frame = loader(engine, now)
    row = state.execute("SELECT attempts, status, run_id FROM candidates WHERE cutoff=?", (cutoff,)).fetchone()
    directory = output / hashlib.sha256(cutoff.encode()).hexdigest()
    manifest = published_manifest(directory)
    if row and row[1] == "completed":
        if manifest is None or manifest["run_id"] != row[2]:
            raise ValueError("completed_candidate_missing_or_changed")
        return {"status": "waiting", "reason": "cutoff_already_trained", "cutoff": cutoff}
    if manifest is None:
        if row and row[0] >= max_attempts:
            return {"status": "blocked", "reason": "retry_limit", "cutoff": cutoff}
        last = state.execute("SELECT MAX(cutoff) FROM candidates WHERE status='completed'").fetchone()[0]
        if last and datetime.fromisoformat(cutoff) < datetime.fromisoformat(last) + timedelta(hours=interval_hours):
            return {"status": "waiting", "reason": "training_interval"}
        state.execute("INSERT INTO candidates(cutoff,attempts,status) VALUES(?,1,'running') "
                      "ON CONFLICT(cutoff) DO UPDATE SET attempts=attempts+1,status='running'", (cutoff,))
        state.commit()  # Crash counts as an attempt, preventing endless restart retries.
        try:
            manifest = trainer(frame, directory, symbol="BTC/USD", source="durable-kraken-hourly-collector",
                               horizon=1, instrument_id="crypto_spot:KRAKEN:BTC:USD", timeframe_minutes=60)
        except Exception as error:
            state.execute("UPDATE candidates SET status='failed',error=? WHERE cutoff=?",
                          (type(error).__name__, cutoff))
            state.commit()
            return {"status": "failed", "cutoff": cutoff, "error_type": type(error).__name__}
    state.execute("INSERT INTO candidates(cutoff,attempts,status,run_id) VALUES(?,0,'completed',?) "
                  "ON CONFLICT(cutoff) DO UPDATE SET status='completed',run_id=excluded.run_id,error=NULL",
                  (cutoff, manifest["run_id"]))
    state.commit()
    return {"status": "completed", "cutoff": cutoff, "run_id": manifest["run_id"],
            "metrics": manifest.get("metrics"), "eligible_for_trading": False,
            "binding_changed": False, "live_authorized": False}


def open_state(output):
    state = sqlite3.connect(output / "training-state.sqlite")
    state.execute("CREATE TABLE IF NOT EXISTS candidates (cutoff TEXT PRIMARY KEY, attempts INTEGER NOT NULL, "
                  "status TEXT NOT NULL, run_id TEXT, error TEXT)")
    state.commit()
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", "--database", dest="source_db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-hours", type=int, default=1)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--continuous", action="store_true", help="Continuous is the default; explicit launcher compatibility")
    args = parser.parse_args()
    if args.once and args.continuous:
        parser.error("Choose either --once or --continuous")
    if args.interval_hours < 1 or not 60 <= args.poll_seconds <= 3600 or not 1 <= args.max_attempts <= 10:
        parser.error("Require interval >= 1 hour, poll 60..3600 seconds, attempts 1..10")
    args.output.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with (args.output / ".worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        engine = readonly_source(args.source_db)
        from app.db.schema import assert_schema_current
        assert_schema_current(engine)
        state = open_state(args.output)
        try:
            while not stop.is_set():
                now = datetime.now(timezone.utc)
                try:
                    result = training_cycle(state, args.output, engine, now,
                        interval_hours=args.interval_hours, max_attempts=args.max_attempts)
                except Exception as error:
                    result = {"status": "blocked", "error_type": type(error).__name__}
                result["observed_at"] = now.isoformat()
                print(json.dumps(result, sort_keys=True), flush=True)
                if args.once:
                    break
                stop.wait(args.poll_seconds)
        finally:
            state.close()
            engine.dispose()


if __name__ == "__main__":
    main()
