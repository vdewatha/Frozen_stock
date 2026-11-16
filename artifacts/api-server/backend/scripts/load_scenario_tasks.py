"""Worker-side half of the load scenario (``scripts/run_load_scenario.sh``).

Loaded only by the scenario's own Celery workers (``--include``) and by its
driver. It registers scenario tasks that run the application's real job code
(the experiment sweep, the intraday ingest, the broker reconciliation, each
inside ``jobs._run_job`` with its real lease and advisory lock) against local
fakes at the provider boundary: a generated minute-bar feed and an in-memory
paper broker. No task here can reach a broker or a market-data provider.

It also installs the probes the report is built from. Each probe appends one
JSON line to a Redis list on the scenario's private Redis:

- ``sent``: a task was published (publisher side), with its queue;
- ``started``: ``task_prerun``, with the seconds it waited in its queue;
- ``finished``: ``task_postrun``, with its state and runtime;
- ``failed`` / ``revoked``: the exception type, or an expiry;
- ``db_error``: every database error by SQLSTATE (``55P03`` lock not
  available, ``40P01`` deadlock detected), including ones the job retries.
"""
from __future__ import annotations

import json
import os
import time
import zlib
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

EVENTS_KEY = "load-scenario:events"
SENT_HEADER = "load_scenario_sent_at"
SCHEMA_PREFIX = "load_scenario_"
ACCOUNT_ID = "load-scenario-paper-account"
# Daily bars must carry a trusted provenance label or the research code
# refuses them. The label is only ever written to a disposable schema.
DAILY_SOURCE = "alpaca_iex_daily"
RESEARCH_STRATEGY = "moving_average_crossover"
# Real heavy learning jobs. A research task borrows one of these names so it
# takes that job's advisory lock, exactly as the scheduled job would.
RESEARCH_JOB_NAMES = (
    "nightly_backtest_job",
    "strategy_discovery_job",
    "experiment_engine_job",
    "strategy_learning_batch_job",
)
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def require_disposable_environment(environ: Mapping[str, str] | None = None) -> str:
    """The scenario schema name, or an error when this is not a scenario process.

    The scenario writes generated prices under a trusted provenance label and
    a fake paper account, so it must never load against a schema, Redis or
    process that anything else uses.
    """
    env = os.environ if environ is None else environ
    if env.get("LOAD_SCENARIO") != "1":
        raise RuntimeError("Load scenario tasks load only inside scripts/run_load_scenario.sh (LOAD_SCENARIO=1).")
    schema = env.get("LOAD_SCENARIO_SCHEMA", "")
    if not schema.startswith(SCHEMA_PREFIX) or not schema.replace("_", "").isalnum():
        raise RuntimeError(f"The load scenario needs a disposable schema named {SCHEMA_PREFIX}*.")
    if f"search_path={schema}," not in env.get("PGOPTIONS", ""):
        raise RuntimeError("PGOPTIONS must put the disposable scenario schema first on the search path.")
    database_url = env.get("DATABASE_URL", "")
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
        raise RuntimeError("The load scenario runs on PostgreSQL only.")
    redis_host = env.get("REDIS_URL", "").split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
    if redis_host not in LOCAL_HOSTS:
        raise RuntimeError("The load scenario uses its own local Redis; REDIS_URL must point at this machine.")
    if env.get("ALLOW_LIVE_TRADING", "false").strip().lower() != "false":
        raise RuntimeError("The load scenario is paper only.")
    return schema


SCHEMA = require_disposable_environment()

import redis  # noqa: E402
from celery import current_task  # noqa: E402
from celery.signals import (  # noqa: E402
    before_task_publish,
    task_failure,
    task_postrun,
    task_prerun,
    task_revoked,
)
from sqlalchemy import event, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.memory import resident_mb  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.services import alpaca_research_data  # noqa: E402
from app.tasks import jobs  # noqa: E402
from app.tasks.celery_app import celery_app  # noqa: E402

UTC = timezone.utc
_events = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
_started: dict[str, float] = {}


def emit(kind: str, **fields) -> None:
    """Append one probe record; a probe never fails the work it observes."""
    try:
        _events.rpush(EVENTS_KEY, json.dumps({"kind": kind, "at": time.time(), "pid": os.getpid(), **fields}))
    except Exception:
        pass


def sqlstate_of(exc: BaseException | None) -> str | None:
    """The PostgreSQL SQLSTATE behind an exception, following SQLAlchemy's wrapper."""
    seen = 0
    while exc is not None and seen < 5:
        state = getattr(exc, "sqlstate", None)
        if state:
            return str(state)
        exc = getattr(exc, "orig", None) or exc.__cause__
        seen += 1
    return None


def _on_publish(headers=None, routing_key=None, **_kwargs) -> None:
    if headers is None or not str(headers.get("task", "")).startswith("load_scenario."):
        return
    sent_at = time.time()
    headers[SENT_HEADER] = sent_at
    emit("sent", id=headers.get("id"), task=headers.get("task"), queue=routing_key, sent_at=sent_at)


def _on_prerun(task_id=None, task=None, **_kwargs) -> None:
    request = getattr(task, "request", None)
    sent_at = getattr(request, SENT_HEADER, None)
    started_at = time.time()
    _started[str(task_id)] = started_at
    emit(
        "started", id=task_id, task=getattr(task, "name", None),
        queue=(getattr(request, "delivery_info", None) or {}).get("routing_key"),
        worker=getattr(request, "hostname", None), sent_at=sent_at, started_at=started_at,
        queue_delay_seconds=None if sent_at is None else round(max(0.0, started_at - float(sent_at)), 4),
    )


def _on_postrun(task_id=None, task=None, state=None, retval=None, **_kwargs) -> None:
    started_at = _started.pop(str(task_id), None)
    emit(
        "finished", id=task_id, task=getattr(task, "name", None), state=state,
        result_status=retval.get("status") if isinstance(retval, dict) else None,
        worker=getattr(getattr(task, "request", None), "hostname", None),
        runtime_seconds=None if started_at is None else round(time.time() - started_at, 4),
        resident_mb=resident_mb(),
    )


def _on_failure(task_id=None, exception=None, sender=None, **_kwargs) -> None:
    emit("failed", id=task_id, task=getattr(sender, "name", None),
         error_type=type(exception).__name__, sqlstate=sqlstate_of(exception))


def _on_revoked(request=None, sender=None, expired=False, terminated=False, **_kwargs) -> None:
    emit("revoked", id=getattr(request, "id", None), task=getattr(sender, "name", None),
         expired=bool(expired), terminated=bool(terminated))


def _on_db_error(context) -> None:
    state = sqlstate_of(context.original_exception)
    if state:
        task = current_task
        emit("db_error", sqlstate=state, task=getattr(task, "name", None) if task else None)


before_task_publish.connect(_on_publish, weak=False)
task_prerun.connect(_on_prerun, weak=False)
task_postrun.connect(_on_postrun, weak=False)
task_failure.connect(_on_failure, weak=False)
task_revoked.connect(_on_revoked, weak=False)
event.listen(engine, "handle_error", _on_db_error)


def _delay(variable: str, default_ms: int) -> None:
    milliseconds = max(0, int(os.environ.get(variable, default_ms)))
    if milliseconds:
        time.sleep(milliseconds / 1000)


def fake_minute_bars(symbols: list[str], start: datetime, end: datetime, *, client=None) -> dict[str, list[dict]]:
    """Generated one-minute bars in Alpaca's shape; the same bar for the same minute, always."""
    _delay("LOAD_FEED_LATENCY_MS", 100)
    pages: dict[str, list[dict]] = {}
    for symbol in symbols:
        minute = start.astimezone(UTC).replace(second=0, microsecond=0)
        rows = []
        while minute < end:
            stamp = minute.isoformat().replace("+00:00", "Z")
            base = 50 + (zlib.crc32(f"{symbol}:{stamp}".encode()) % 5000) / 100
            rows.append({"t": stamp, "o": base, "h": base + 0.05, "l": base - 0.05, "c": base + 0.01, "v": 1000})
            minute += timedelta(minutes=1)
        pages[symbol] = rows
    return pages


# Every intraday fetch in the application goes through these two functions.
alpaca_research_data.fetch_iex_bars = fake_minute_bars
alpaca_research_data.fetch_sip_bars = fake_minute_bars


class FakePaperBroker:
    """An unchanging, flat paper account. It accepts no order."""

    supports_bracket_orders = True

    def account(self) -> dict:
        _delay("LOAD_BROKER_LATENCY_MS", 150)
        return {"id": ACCOUNT_ID, "currency": "USD", "status": "ACTIVE", "cash": "100000.00",
                "buying_power": "200000.00", "equity": "100000.00", "last_equity": "100000.00"}

    def positions(self) -> list[dict]:
        _delay("LOAD_BROKER_LATENCY_MS", 150)
        return []

    def orders(self, after=None) -> list[dict]:
        _delay("LOAD_BROKER_LATENCY_MS", 150)
        return []

    def fills(self, after=None) -> list[dict]:
        _delay("LOAD_BROKER_LATENCY_MS", 150)
        return []

    def submit_order(self, payload: dict) -> dict:
        raise RuntimeError("The load scenario places no orders")

    def cancel_order(self, broker_order_id: str) -> None:
        raise RuntimeError("The load scenario cancels no orders")

    def order_by_client_id(self, client_order_id: str) -> dict | None:
        return None


def _touch(megabytes: int) -> bytearray | None:
    """Hold ``megabytes`` of resident memory for the caller's lifetime."""
    if megabytes <= 0:
        return None
    block = bytearray(megabytes * 1024 * 1024)
    block[::4096] = b"\x01" * len(block[::4096])
    return block


@celery_app.task(name="load_scenario.research")
def research(job_name: str, symbols: list[str], sweeps: int, ballast_mb: int = 0) -> dict:
    """A heavy learning job: ``sweeps`` real experiment sweeps over generated daily history."""
    if job_name not in RESEARCH_JOB_NAMES:
        raise ValueError(f"Unknown research job name: {job_name}")

    def work(db):
        from app.services.experiments import run_strategy_experiments

        ballast = _touch(ballast_mb)
        created = 0
        for index in range(sweeps):
            result = run_strategy_experiments(
                db, symbol=symbols[index % len(symbols)], strategy_slug=RESEARCH_STRATEGY,
                max_candidates=3, apply_promotions=False,
            )
            db.commit()
            created += len(result["experiments"])
        return {"status": "complete", "job": job_name, "sweeps": sweeps, "experiments": created,
                "ballast_mb": 0 if ballast is None else ballast_mb}

    return jobs._run_job(job_name, work)


@celery_app.task(
    name="load_scenario.intraday_import",
    soft_time_limit=jobs.INTRADAY_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=jobs.INTRADAY_TASK_TIME_LIMIT_SECONDS,
    acks_late=True,
    reject_on_worker_lost=True,
)
def intraday_import(symbols: list[str], simulated_now: str) -> dict:
    """The minutely import: the real ingest for one simulated session minute."""
    def work(db):
        from app.services.intraday_data import ingest_intraday

        result = ingest_intraday(db, symbols, now=datetime.fromisoformat(simulated_now))
        db.commit()
        return {"status": "complete", "job": "intraday_market_data_import", "symbols": len(symbols),
                "simulated_now": simulated_now,
                "ingest_statuses": sorted({str(row.get("status")) for row in result["results"]})}

    return jobs._run_job("intraday_market_data_import", work)


@celery_app.task(name="load_scenario.broker_reconciliation")
def broker_reconciliation() -> dict:
    """The five-minute reconciliation: the real pass, holding the paper account row."""
    def work(db):
        from app.services.stock_exit_rearm import rearm_broker_exits
        from app.services.stock_paper_ledger import reconcile_stock_paper_account

        broker = FakePaperBroker()
        result = reconcile_stock_paper_account(db, broker)
        rearm = rearm_broker_exits(db, broker)
        return {"status": "complete", "job": "stock_paper_broker_reconciliation_job", "paper_only": True,
                "account_status": result.get("account_status"), "exit_rearm": rearm.get("status")}

    return jobs._run_job("stock_paper_broker_reconciliation_job", work)


@celery_app.task(name="load_scenario.signal_pass")
def signal_pass(hold_ms: int, lock_timeout_ms: int) -> dict:
    """An order reservation's lock footprint: the paper account row, as the signal pass takes it.

    It waits for a running reconciliation and gives up with SQLSTATE 55P03
    after ``lock_timeout_ms``. It reserves nothing.
    """
    def work(db):
        from app.services.stock_paper_ledger import active_paper_account

        db.execute(text(f"SET LOCAL lock_timeout = '{int(lock_timeout_ms)}ms'"))
        account = active_paper_account(db, for_update=True)
        time.sleep(max(0, int(hold_ms)) / 1000)
        db.commit()
        return {"status": "complete", "job": "paper_trading_signal_job", "account": account is not None}

    return jobs._run_job("paper_trading_signal_job", work)


@celery_app.task(name="load_scenario.probe")
def probe() -> dict:
    """A short job with one database round trip, for queues the scenario does not otherwise load."""
    with SessionLocal() as db:
        db.execute(text("SELECT 1")).scalar()
    return {"status": "complete", "job": "load_scenario.probe"}
