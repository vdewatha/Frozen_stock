#!/usr/bin/env python3
"""Seed, drive and sample the load scenario (``scripts/run_load_scenario.sh``).

``seed`` fills the disposable schema: strategies, generated daily history for
the research symbols and a flat paper account read from the fake broker.

``run`` publishes the workload on a fixed timetable, samples the database,
the processes and Redis once a second, kills one worker part-way through and
times its recovery. It writes ``samples.jsonl``, ``events.jsonl``,
``drill.json`` and ``meta.json`` into the run directory; the report script
reads only those files.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import platform
import random
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

from scripts import load_scenario_tasks as scenario

from app.core.config import settings  # noqa: E402
from app.core.memory import resident_mb  # noqa: E402
from app.db.session import SessionLocal, application_name  # noqa: E402
from app.services.heartbeat import WORKER_HEARTBEAT_PATTERN  # noqa: E402
from app.tasks.celery_app import (  # noqa: E402
    BROKER_RECONCILIATION_QUEUE,
    INTRADAY_MARKET_DATA_QUEUE,
    celery_app,
)

UTC = timezone.utc
SAMPLER_APPLICATION = "load-scenario-sampler"
LOCK_WAITS = text(
    "SELECT pid, wait_event, state, "
    "EXTRACT(EPOCH FROM (clock_timestamp() - COALESCE(state_change, query_start))) AS waited, "
    "left(query, 120) AS query "
    "FROM pg_stat_activity WHERE datname = current_database() "
    "AND application_name = :application AND wait_event_type = 'Lock'"
)
SESSION_STATES = text(
    "SELECT count(*) AS sessions, "
    "count(*) FILTER (WHERE state LIKE 'idle in transaction%') AS idle_in_transaction "
    "FROM pg_stat_activity WHERE datname = current_database() AND application_name = :application"
)
DEADLOCKS = text("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def config_from_env() -> dict:
    """Every knob of the run, so the report records what produced its numbers."""
    return {
        "duration_seconds": _env_int("LOAD_DURATION_SECONDS", 180),
        "drain_seconds": _env_int("LOAD_DRAIN_SECONDS", 15),
        "research_jobs": _env_int("LOAD_RESEARCH_JOBS", 10),
        "research_sweeps_per_job": _env_int("LOAD_RESEARCH_SWEEPS", 12),
        "research_symbols": _env_int("LOAD_RESEARCH_SYMBOLS", 6),
        "research_ballast_mb": _env_int("LOAD_RESEARCH_BALLAST_MB", 0),
        "intraday_interval_seconds": _env_float("LOAD_INTRADAY_INTERVAL_SECONDS", 5),
        "intraday_symbols": _env_int("LOAD_INTRADAY_SYMBOLS", 10),
        "reconcile_interval_seconds": _env_float("LOAD_RECONCILE_INTERVAL_SECONDS", 10),
        "signal_interval_seconds": _env_float("LOAD_SIGNAL_INTERVAL_SECONDS", 5),
        "signal_hold_ms": _env_int("LOAD_SIGNAL_HOLD_MS", 50),
        "signal_lock_timeout_ms": _env_int("LOAD_SIGNAL_LOCK_TIMEOUT_MS", 3000),
        "probe_interval_seconds": _env_float("LOAD_PROBE_INTERVAL_SECONDS", 5),
        "probe_queues": [q for q in os.environ.get(
            "LOAD_PROBE_QUEUES", "learning_fast,market_data,research_market_data").split(",") if q],
        "feed_latency_ms": _env_int("LOAD_FEED_LATENCY_MS", 100),
        "broker_latency_ms": _env_int("LOAD_BROKER_LATENCY_MS", 150),
        "kill_worker": os.environ.get("LOAD_KILL_WORKER", "paper-core"),
        "kill_after_seconds": _env_int("LOAD_KILL_AFTER_SECONDS", 60),
        "restart_delay_seconds": _env_float("LOAD_RESTART_DELAY_SECONDS", 2),
        "sample_interval_seconds": _env_float("LOAD_SAMPLE_INTERVAL_SECONDS", 1),
    }


def research_symbols(count: int) -> list[str]:
    return sorted(settings.paper_execution_symbols)[:count]


def intraday_symbols(count: int) -> list[str]:
    return sorted(settings.paper_execution_symbols)[:count]


def _weekdays_before(day, count: int) -> list:
    days = []
    while len(days) < count:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            days.append(day)
    return list(reversed(days))


def seed(config: dict) -> dict:
    from app.models import Asset, MarketPrice
    from app.services.experiments import BENCHMARK_SYMBOL
    from app.services.seed import seed_defaults
    from app.services.stock_paper_ledger import active_paper_account, initialize_stock_paper_account

    os.environ["LOAD_BROKER_LATENCY_MS"] = "0"
    symbols = sorted({*research_symbols(config["research_symbols"]), BENCHMARK_SYMBOL})
    days = _weekdays_before(datetime.now(UTC).date(), 520)
    with SessionLocal() as db:
        if db.execute(text("SELECT current_schema()")).scalar() != scenario.SCHEMA:
            raise SystemExit("Refusing to seed: the session is not in the disposable scenario schema.")
        seed_defaults(db)
        db.commit()
        for symbol in symbols:
            if db.query(Asset).filter_by(symbol=symbol).one_or_none() is None:
                db.add(Asset(symbol=symbol, name=symbol, is_active=True))
            if db.query(MarketPrice).filter_by(symbol=symbol, source=scenario.DAILY_SOURCE).count():
                continue
            generator, price = random.Random(symbol), 100.0
            for day in days:
                opened = price
                price = max(5.0, price * (1 + generator.gauss(0.0004, 0.012)))
                high = max(opened, price) * (1 + abs(generator.gauss(0, 0.003)))
                low = min(opened, price) * (1 - abs(generator.gauss(0, 0.003)))
                db.add(MarketPrice(
                    symbol=symbol, price_date=day, open=round(opened, 4), high=round(high, 4), low=round(low, 4),
                    close=round(price, 4), adjusted_close=round(price, 4),
                    volume=1_000_000 + generator.randint(0, 500_000), source=scenario.DAILY_SOURCE,
                ))
        db.commit()
        if active_paper_account(db) is None:
            initialize_stock_paper_account(db, scenario.FakePaperBroker())
            db.commit()
    return {"daily_symbols": symbols, "daily_bars_per_symbol": len(days)}


def simulated_session_start() -> datetime:
    """Forty-five minutes into the last completed regular session before today."""
    from app.services.exchange_sessions import session_bounds
    from app.services.intraday_data import NY, _previous_session

    bounds = session_bounds(_previous_session(datetime.now(UTC).astimezone(NY).date()))
    assert bounds is not None
    return bounds[0] + timedelta(minutes=45)


def _pids(run_dir: Path) -> dict[str, int]:
    pids = {}
    for path in sorted((run_dir / "pids").glob("*.pid")):
        try:
            pids[path.stem] = int(path.read_text().strip())
        except (OSError, ValueError):
            continue
    return pids


_libproc = None


def _macos_footprint_mb(pid: int) -> float | None:
    """A process's physical footprint on macOS (what Activity Monitor shows), or None.

    macOS compresses idle pages, so ``ps`` there reports a resident size far
    below what the process really holds; the footprint counts those pages.
    """
    global _libproc
    if sys.platform != "darwin":
        return None
    try:
        if _libproc is None:
            _libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "libproc.dylib")
        # struct rusage_info_v0: a 16-byte uuid, then uint64 fields; the
        # eighth of them is ri_phys_footprint.
        usage = (ctypes.c_uint64 * 12)()
        if _libproc.proc_pid_rusage(int(pid), 0, ctypes.byref(usage)) != 0:
            return None
        return round(usage[9] / (1024 * 1024), 1)
    except (OSError, AttributeError):
        return None


def process_memory_mb(pids: dict[str, int]) -> tuple[dict[str, float | None], str]:
    """MiB per labelled process: ``resident_mb`` (/proc), else the macOS footprint, else ``ps``."""
    memory = {label: resident_mb(pid) for label, pid in pids.items()}
    if not pids or any(value is not None for value in memory.values()):
        return memory, "proc"
    memory = {label: _macos_footprint_mb(pid) for label, pid in pids.items()}
    if any(value is not None for value in memory.values()):
        return memory, "macos_phys_footprint"
    try:
        output = subprocess.run(
            ["ps", "-o", "pid=,rss=", "-p", ",".join(str(pid) for pid in pids.values())],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return memory, "unavailable"
    kib = {int(fields[0]): int(fields[1]) for fields in (line.split() for line in output.splitlines()) if len(fields) == 2}
    return {label: round(kib[pid] / 1024, 1) if pid in kib else None for label, pid in pids.items()}, "ps"


class Sampler(threading.Thread):
    def __init__(self, run_dir: Path, started: float, interval: float, client):
        super().__init__(daemon=True)
        self.run_dir, self.started, self.interval, self.client = run_dir, started, interval, client
        self.stop = threading.Event()
        self.memory_source = "unavailable"
        self.engine = create_engine(
            settings.database_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True,
            connect_args={"application_name": SAMPLER_APPLICATION, "connect_timeout": 5},
        )
        self.queues = sorted(queue.name for queue in celery_app.conf.task_queues)

    def deadlocks(self) -> int | None:
        try:
            with self.engine.connect() as connection:
                return int(connection.execute(DEADLOCKS).scalar() or 0)
        except Exception:
            return None

    def sample(self) -> dict:
        now = time.time()
        row: dict = {"at": now, "elapsed_seconds": round(now - self.started, 2)}
        try:
            with self.engine.connect() as connection:
                waits = connection.execute(LOCK_WAITS, {"application": application_name()}).mappings().all()
                states = connection.execute(SESSION_STATES, {"application": application_name()}).mappings().one()
            row["lock_waits"] = [
                {"pid": wait["pid"], "wait_event": wait["wait_event"], "state": wait["state"],
                 "waited_seconds": round(float(wait["waited"] or 0), 3), "query": wait["query"]}
                for wait in waits
            ]
            row["sessions"] = int(states["sessions"])
            row["idle_in_transaction"] = int(states["idle_in_transaction"])
        except Exception as exc:
            row["database_error"] = type(exc).__name__
        row["memory_mb"], self.memory_source = process_memory_mb(_pids(self.run_dir))
        try:
            row["queue_depth"] = {queue: int(self.client.llen(queue)) for queue in self.queues}
            row["heartbeat_ttl_ms"] = {
                key.decode().rsplit(":", 1)[-1]: int(self.client.pttl(key))
                for key in self.client.scan_iter(match=WORKER_HEARTBEAT_PATTERN, count=100)
            }
        except Exception as exc:
            row["redis_error"] = type(exc).__name__
        return row

    def run(self) -> None:
        with (self.run_dir / "samples.jsonl").open("a", encoding="utf-8") as handle:
            while not self.stop.is_set():
                tick = time.monotonic()
                handle.write(json.dumps(self.sample()) + "\n")
                handle.flush()
                self.stop.wait(max(0.05, self.interval - (time.monotonic() - tick)))


def recovery_drill(run_dir: Path, client, label: str, result: dict, timeout: float = 120.0) -> None:
    """SIGKILL one worker and time the return of its heartbeat key.

    The key (``celery_app._record_worker_heartbeat``) is rewritten every two
    seconds with a 90-second expiry and outlives a dead worker, so "restored"
    is the first time its remaining life goes up again after the kill.
    """
    pid = _pids(run_dir).get(label)
    keys = [key for key in client.scan_iter(match=f"celery:worker:heartbeat:{label}@*", count=100)]
    if pid is None or not keys:
        result.update({"worker": label, "status": "not_run", "reason": "worker pid or heartbeat key not found"})
        return
    key = keys[0]
    last_ttl = int(client.pttl(key))
    os.kill(pid, signal.SIGKILL)
    killed = time.time()
    result.pop("reason", None)
    result.update({
        "worker": label, "status": "killed", "signal": "SIGKILL", "killed_pid": pid, "killed_at": killed,
        "heartbeat_key": key.decode(), "heartbeat_ttl_at_kill_ms": last_ttl,
        "process_restarted_seconds": None, "heartbeat_restored_seconds": None, "new_pid": None,
    })
    while time.time() - killed < timeout:
        time.sleep(0.05)
        if result["new_pid"] is None:
            current = _pids(run_dir).get(label)
            if current is not None and current != pid:
                result["new_pid"] = current
                result["process_restarted_seconds"] = round(time.time() - killed, 2)
        try:
            ttl = int(client.pttl(key))
        except Exception:
            continue
        if ttl > last_ttl + 1000:
            result["heartbeat_restored_seconds"] = round(time.time() - killed, 2)
            result["status"] = "recovered"
            return
        if ttl >= 0:
            last_ttl = ttl
    result["status"] = "not_recovered"
    result["reason"] = f"no heartbeat within {int(timeout)} seconds of the kill"


def _send(name: str, queue: str, args: list, expires: float | None = None) -> None:
    celery_app.send_task(f"load_scenario.{name}", args=args, queue=queue, expires=expires)


def run(config: dict, run_dir: Path) -> dict:
    import redis

    client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    client.delete(scenario.EVENTS_KEY)
    started = time.time()
    sampler = Sampler(run_dir, started, config["sample_interval_seconds"], client)
    deadlocks_before = sampler.deadlocks()
    sampler.start()

    daily, minute = research_symbols(config["research_symbols"]), intraday_symbols(config["intraday_symbols"])
    session_start = simulated_session_start()
    for index in range(config["research_jobs"]):
        _send("research", "learning", [
            scenario.RESEARCH_JOB_NAMES[index % len(scenario.RESEARCH_JOB_NAMES)],
            daily[index % len(daily):] + daily[:index % len(daily)],
            config["research_sweeps_per_job"], config["research_ballast_mb"],
        ])

    # Each stream keeps production's ratio of expiry to cadence: a tick that
    # waited past the next one is dropped, not run late.
    intraday_tick = [0]

    def send_intraday() -> None:
        stamp = session_start + timedelta(minutes=intraday_tick[0])
        intraday_tick[0] += 1
        _send("intraday_import", INTRADAY_MARKET_DATA_QUEUE, [minute, stamp.isoformat()],
              expires=config["intraday_interval_seconds"] * 55 / 60)

    streams = [
        [0.0, config["intraday_interval_seconds"], send_intraday],
        [0.0, config["reconcile_interval_seconds"], lambda: _send(
            "broker_reconciliation", BROKER_RECONCILIATION_QUEUE, [],
            expires=config["reconcile_interval_seconds"] * 240 / 300)],
        [0.0, config["signal_interval_seconds"], lambda: _send(
            "signal_pass", "paper_trading", [config["signal_hold_ms"], config["signal_lock_timeout_ms"]],
            expires=config["signal_interval_seconds"] * 55 / 60)],
        [0.0, config["probe_interval_seconds"], lambda: [
            _send("probe", queue, []) for queue in config["probe_queues"]]],
    ]
    drill: dict = {"worker": config["kill_worker"], "status": "not_run", "reason": "the run ended before the kill"}
    drill_thread = None
    deadline = started + config["duration_seconds"]
    while time.time() < deadline:
        elapsed = time.time() - started
        for stream in streams:
            if stream[1] > 0 and elapsed >= stream[0]:
                stream[2]()
                stream[0] += stream[1]
        if drill_thread is None and config["kill_after_seconds"] > 0 and elapsed >= config["kill_after_seconds"]:
            drill_thread = threading.Thread(
                target=recovery_drill, args=(run_dir, client, config["kill_worker"], drill), daemon=True)
            drill_thread.start()
        time.sleep(0.05)
    publishing_stopped = time.time()
    time.sleep(config["drain_seconds"])
    if drill_thread is not None:
        drill_thread.join(timeout=1)
    sampler.stop.set()
    sampler.join(timeout=10)
    ended = time.time()

    events = [json.loads(raw) for raw in client.lrange(scenario.EVENTS_KEY, 0, -1)]
    (run_dir / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    (run_dir / "drill.json").write_text(json.dumps(drill, indent=2) + "\n", encoding="utf-8")
    layout_path = run_dir / "layout.json"
    with SessionLocal() as db:
        server_version = db.execute(text("SHOW server_version")).scalar()
    meta = {
        "schema": scenario.SCHEMA,
        "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
        "started_epoch": started, "publishing_stopped_epoch": publishing_stopped, "ended_epoch": ended,
        "config": config,
        "layout": json.loads(layout_path.read_text()) if layout_path.exists() else {},
        "simulated_session_start": session_start.isoformat(),
        "postgres_deadlocks_before": deadlocks_before,
        "postgres_deadlocks_after": sampler.deadlocks(),
        "memory_source": sampler.memory_source,
        "environment": {
            "platform": platform.platform(), "python": platform.python_version(),
            "cpu_count": os.cpu_count(), "postgres": server_version,
        },
        "paper_only": True, "live_trading": False,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    sampler.engine.dispose()
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("seed", "run"))
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    config = config_from_env()
    if args.command == "seed":
        print(json.dumps(seed(config)))
        return 0
    meta = run(config, args.run_dir)
    print(f"load_scenario_events={args.run_dir / 'events.jsonl'} duration={meta['ended_epoch'] - meta['started_epoch']:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
