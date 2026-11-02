"""Bounded, journaled local short research. No broker order methods are used."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[4]
OUTPUT = ROOT / ".paper-training" / "local-short-session-20261030"
DATABASE = ROOT / ".paper-training" / "local-20261030.db"


def collection_needs_refresh(checked_at: str | None, now: datetime) -> bool:
    try:
        checked = datetime.fromisoformat(checked_at)
        if checked.tzinfo is None or now.tzinfo is None or checked > now:
            return True
        return checked.replace(second=0, microsecond=0) != now.replace(second=0, microsecond=0)
    except (TypeError, ValueError):
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--until", help="Stop today at HH:MM America/New_York; omit for one pass")
    parser.add_argument("--interval-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.interval_seconds < 60:
        parser.error("interval must be at least 60 seconds")
    now = datetime.now(ZoneInfo("America/New_York"))
    deadline = None
    if args.until:
        try:
            hour, minute = map(int, args.until.split(":"))
            deadline = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            parser.error("until must be HH:MM")
        if deadline <= now:
            parser.error("deadline has already passed")

    load_dotenv(ROOT / ".env")
    # Persist WAL only for the isolated local database. Concurrent API readers
    # must not hold up minute-bar imports during a long readiness request.
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE, timeout=10) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    # This launcher always owns its own SQLite database, never the published DB.
    os.environ.update(ENVIRONMENT="local", DATABASE_URL=f"sqlite:///{DATABASE}",
                      ALLOW_LIVE_TRADING="false", PAPER_WORKERS_ENABLED="false",
                      REDIS_URL="redis://127.0.0.1:56379/0", IEX_RESEARCH_ENABLED="true",
                      ACTIVE_MARKET_DATA_PROVIDER="alpaca_iex", IEX_FEATURE_REFRESH_ENABLED="false")
    from app.core.config import settings
    from app.db.session import SessionLocal
    from app.services.market_data import fetch_alpaca_iex_daily_prices, upsert_prices
    from app.services.stock_paper_ledger import AlpacaPaperClient
    from app.tasks.jobs import iex_research_collection_job
    from app.services.scalp_research import create_scalp_research_run, run_scalp_research
    import httpx

    if settings.paper_binding_source != "development" or settings.allow_live_trading:
        parser.error("a complete dedicated development paper binding is required")
    OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)

    def journal(event: str, **details) -> None:
        row = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **details}
        with (OUTPUT / "journal.jsonl").open("a") as handle:
            handle.write(json.dumps(row, allow_nan=False) + "\n")
        print(json.dumps(row, allow_nan=False), flush=True)

    def collect():
        # Reuse the collector's lease and test-then-train transaction path.
        # Feature-task publication is disabled above; no execution is queued.
        collected = iex_research_collection_job.run()
        journal("intraday_observed", status=collected["status"],
                rows=sum(row.get("rows_imported", 0) for row in collected.get("results", [])),
                failure_class=collected.get("failure_class"))
        journal("online_learning", results=collected.get("learning", {}).get("results", []),
                execution_eligible=False)
        return collected.get("checked_at")

    journal("started", paper_only=True, broker_execution=False,
            deadline=deadline.isoformat() if deadline else None)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    last_refresh = 0.0
    cycle = 0
    while not stop.is_set():
        if deadline and datetime.now(deadline.tzinfo) >= deadline:
            break
        try:
            broker = AlpacaPaperClient()
            account = broker.account()
            if account.get("id") != settings.dev_paper_broker_account_id.get_secret_value():
                raise RuntimeError("Development paper account binding mismatch")
            journal("paper_account", status=account.get("status"),
                    equity=account.get("equity"), shorting_enabled=account.get("shorting_enabled"),
                    positions=len(broker.positions()), orders=len(broker.orders(None)))
        except Exception as exc:
            journal("account_check_failed", error_type=type(exc).__name__)
            raise SystemExit(1) from None

        refresh = time.monotonic() - last_refresh >= 1800
        collection_at = None
        try:
            collection_at = collect()
        except Exception as exc:
            journal("intraday_failed", error_type=type(exc).__name__)
        with httpx.Client(base_url="http://127.0.0.1:8120", timeout=60) as client:
            try:
                health = client.get("/api/health")
                journal("health", http_status=health.status_code,
                        routes_ready=health.json().get("all_routes_ready"))
            except Exception as exc:
                journal("health_failed", error_type=type(exc).__name__)
            if cycle % 5 == 0:
                try:
                    response = client.post("/api/stock-paper/reconcile", headers={
                        "Authorization": f"Bearer {settings.auth_operator_key.get_secret_value()}",
                    })
                    journal("reconciliation", http_status=response.status_code,
                            ledger_status=response.json().get("status"),
                            broker_orders_submitted=0)
                except Exception as exc:
                    journal("reconciliation_failed", error_type=type(exc).__name__)
            for symbol in ("AAPL", "MSFT", "SPY", "QQQ"):
                if stop.is_set() or (deadline and datetime.now(deadline.tzinfo) >= deadline):
                    break
                try:
                    if refresh:
                        frame = fetch_alpaca_iex_daily_prices(symbol, "5y")
                        if frame.empty:
                            journal("history_unavailable", symbol=symbol)
                            continue
                        with SessionLocal() as db:
                            count = upsert_prices(db, symbol, frame, "alpaca_iex_daily")
                            db.commit()
                        journal("history_imported", symbol=symbol, rows=count,
                                first=str(frame.date.min()), last=str(frame.date.max()))
                    if cycle % 5 == 0:
                        # A batch can straddle a minute boundary. Fetch the
                        # newly required real bar instead of backdating the
                        # scalp clock or relaxing its completeness gate.
                        if collection_needs_refresh(collection_at, datetime.now(timezone.utc)):
                            journal("refresh_before_scalp", symbol=symbol)
                            collection_at = collect()
                        with SessionLocal() as db:
                            scalp = create_scalp_research_run(db, symbols=[symbol], actor="local-research-session")
                            db.commit()
                            outcome = run_scalp_research(db, scalp.run_id)
                            journal("scalp_research", symbol=symbol, run_id=scalp.run_id,
                                    status=outcome["status"],
                                    reason=scalp.metrics.get(symbol, {}).get("reason"),
                                    execution_eligible=False)
                    for entry in (90.0, 95.0):
                        response = client.post("/api/stock-paper/short-simulation",
                            headers={"Authorization": f"Bearer {settings.auth_researcher_key.get_secret_value()}"},
                            json={"symbol": symbol, "limit": 1500,
                                  "config": {"entry_rsi": entry}})
                        if response.status_code != 200:
                            journal("simulation_blocked", symbol=symbol, entry_rsi=entry,
                                    http_status=response.status_code, detail=response.json().get("detail"))
                            continue
                        report = response.json()
                        path = OUTPUT / f"{symbol}-{int(entry)}-{report['run_id']}.json"
                        if path.exists():
                            journal("simulation_unchanged", symbol=symbol, entry_rsi=entry,
                                    run_id=report["run_id"], new_evidence=False)
                            continue
                        path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
                        journal("simulation_completed", symbol=symbol, entry_rsi=entry,
                                run_id=report["run_id"], trades=report["number_of_trades"],
                                net_pnl=report["net_pnl"], max_drawdown=report["max_drawdown"],
                                costs_verified=report["costs_verified"], report=path.name)
                except Exception as exc:
                    journal("symbol_failed", symbol=symbol, error_type=type(exc).__name__)
        if refresh:
            last_refresh = time.monotonic()
        cycle += 1
        if not deadline:
            break
        remaining = (deadline - datetime.now(deadline.tzinfo)).total_seconds()
        if remaining > 0:
            stop.wait(min(args.interval_seconds, remaining))
    journal("completed", broker_orders_submitted=0)


if __name__ == "__main__":
    main()
