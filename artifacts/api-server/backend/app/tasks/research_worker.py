"""The research worker: a memory cap per task process and a record of lost jobs.

The hosted layout gives the ``learning`` queue a worker of its own
(``paper-research`` in ``src/production-server.ts``): a prefork pool of one
process that runs one task and is then replaced, so a heavy backtest hands
all of its memory back when it ends and can be stopped without touching the
workers that serve market data, signals and monitoring.

Two things live here, both hooked to Celery signals.

**The cap.** In a worker that consumes only ``learning``, each task process
checks its own resident memory once a second and stops itself with SIGKILL
when it passes ``PAPER_RESEARCH_MAX_MEMORY_MB``. Celery's own
``--max-memory-per-child`` only looks after a task has finished, which is too
late for a job that grows without bound. On Linux the task process also asks
the kernel to end it when its worker dies, so a killed worker never leaves a
research job running beside its replacement.

**The record.** A research job that dies with its process is not delivered
again: redelivering the job that just exhausted memory would exhaust it
again, and Celery redelivers a lost task without limit. Every such loss is
written down instead, with the run that covers it: the next firing of the
task's beat entry, and whether the after-close catch-up queues it sooner. The
worker's main process records a task process it lost (``WorkerLostError``,
or the hard time limit a prefork worker enforces); when the whole worker was
killed, its replacement finds the marker the dead one left in Redis and
records it at start-up. Tasks that are redelivered (``acks_late`` with
``reject_on_worker_lost``) are left to Celery.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import redis
from celery.signals import (
    celeryd_after_setup,
    task_failure,
    task_postrun,
    task_prerun,
    worker_process_init,
    worker_ready,
)

from app.core.config import settings
from app.core.memory import resident_mb

logger = logging.getLogger(__name__)

RESEARCH_QUEUE = "learning"
MAX_MEMORY_ENV = "PAPER_RESEARCH_MAX_MEMORY_MB"
# The hosted VM has 7,943 MiB and no swap, and the memory guard acts below 8%
# available (635 MiB), so about 7.3 GiB is usable. Without research the other
# processes have rested between 2.6 GiB (2026-11-16, after the readiness
# fixes) and about 3.9 GiB (2026-11-12, taking paper-core at 0.5 GiB instead
# of the 1.8 GiB it held with research inside it). 2 GiB on top of the higher
# figure leaves about 1.4 GiB for the API's spikes. The largest known job, the
# experiment engine, needs about 1.6 GiB; in the load scenario on Linux a job
# holding 1,200 MiB measured 1,384 to 1,393 MiB resident in its task process.
DEFAULT_MAX_MEMORY_MB = 2048
CAP_CHECK_SECONDS = 1.0
INFLIGHT_PREFIX = "celery:research:inflight:"
STOPPED_PREFIX = "celery:research:stopped:"
STOPPED_TTL_SECONDS = 60 * 60

# Set by the worker's main process for its task processes, which inherit the
# environment however they are started (fork on Linux, spawn on macOS).
RESEARCH_WORKER_ENV = "PAPER_RESEARCH_WORKER"

_worker_hostname: Optional[str] = None
_worker_started_at: Optional[float] = None
# The task this process is running (a research task process runs one at a time).
_current: dict[str, Any] = {}


def max_memory_mb(environ=None) -> int:
    """The cap in MiB; 0 switches it off. An unreadable value means the default."""
    raw = (os.environ if environ is None else environ).get(MAX_MEMORY_ENV, "")
    if not str(raw).strip():
        return DEFAULT_MAX_MEMORY_MB
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return DEFAULT_MAX_MEMORY_MB


def _client() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)


def _resident_now_mb() -> Optional[float]:
    """Resident memory of this process; off Linux, the peak (the best macOS offers cheaply)."""
    value = resident_mb()
    if value is not None:
        return value
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    except Exception:
        return None


def _die_with_parent() -> None:
    """Linux: have the kernel SIGKILL this task process when its worker dies."""
    if not sys.platform.startswith("linux"):
        return
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, int(signal.SIGKILL), 0, 0, 0)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def over_cap(resident: Optional[float], cap_mb: int) -> bool:
    return bool(cap_mb) and resident is not None and resident > cap_mb


def _stop_over_cap(resident: float, cap_mb: int) -> None:
    task = dict(_current)
    line = (
        f"[research-cap] stopping this task process: resident {resident:.0f} MiB is over the "
        f"{cap_mb} MiB cap ({MAX_MEMORY_ENV}); task={task.get('name')} id={task.get('id')}\n"
    )
    try:
        os.write(2, line.encode())
    except OSError:
        pass
    if task.get("id"):
        try:
            client = _client()
            client.set(
                f"{STOPPED_PREFIX}{task['id']}",
                json.dumps({"reason": "memory_cap", "resident_mb": round(resident, 1), "cap_mb": cap_mb}),
                ex=STOPPED_TTL_SECONDS,
            )
            client.close()
        except Exception:
            pass
    os.kill(os.getpid(), signal.SIGKILL)


def _watch_cap(cap_mb: int, interval: float = CAP_CHECK_SECONDS) -> None:
    while True:
        time.sleep(interval)
        resident = _resident_now_mb()
        if over_cap(resident, cap_mb):
            _stop_over_cap(float(resident), cap_mb)


def _consumed_queues(instance) -> set[str]:
    try:
        return {str(name) for name in instance.app.amqp.queues.consume_from}
    except Exception:
        return set()


def _on_worker_setup(sender=None, instance=None, **_kwargs) -> None:
    """In the worker's main process, before the pool forks: who am I, and am I the research worker."""
    global _worker_hostname, _worker_started_at
    _worker_hostname = str(sender) if sender else None
    _worker_started_at = time.time()
    os.environ[RESEARCH_WORKER_ENV] = "1" if _consumed_queues(instance) == {RESEARCH_QUEUE} else "0"


def _on_task_process_start(**_kwargs) -> None:
    """In each prefork task process of the research worker: tie it to its worker and start the cap."""
    if os.environ.get(RESEARCH_WORKER_ENV) != "1":
        return
    _die_with_parent()
    cap_mb = max_memory_mb()
    if cap_mb:
        threading.Thread(target=_watch_cap, args=(cap_mb,), name="research-memory-cap", daemon=True).start()


def _redelivered_on_loss(task) -> bool:
    return bool(getattr(task, "acks_late", False) and getattr(task, "reject_on_worker_lost", False))


def _delivered_queue(task) -> Optional[str]:
    request = getattr(task, "request", None)
    return (getattr(request, "delivery_info", None) or {}).get("routing_key")


def _hostname_of(task) -> Optional[str]:
    return getattr(getattr(task, "request", None), "hostname", None) or _worker_hostname


def _on_prerun(task_id=None, task=None, **_kwargs) -> None:
    if task is None or not task_id or _delivered_queue(task) != RESEARCH_QUEUE or _redelivered_on_loss(task):
        return
    _current.clear()
    _current.update({"id": str(task_id), "name": getattr(task, "name", None)})
    hostname = _hostname_of(task)
    if not hostname:
        return
    try:
        client = _client()
        client.hset(f"{INFLIGHT_PREFIX}{hostname}", str(task_id), json.dumps({
            "task": getattr(task, "name", None), "started_at": time.time(), "pid": os.getpid(),
        }))
        client.close()
    except Exception:
        pass  # the marker is evidence, never a reason to fail the job


def _on_postrun(task_id=None, task=None, **_kwargs) -> None:
    if task is None or not task_id or _delivered_queue(task) != RESEARCH_QUEUE or _redelivered_on_loss(task):
        return
    if _current.get("id") == str(task_id):
        _current.clear()
    hostname = _hostname_of(task)
    if not hostname:
        return
    try:
        client = _client()
        client.hdel(f"{INFLIGHT_PREFIX}{hostname}", str(task_id))
        client.close()
    except Exception:
        pass


def _seconds(schedule) -> Optional[float]:
    if isinstance(schedule, (int, float)):
        return float(schedule)
    if isinstance(schedule, timedelta):
        return schedule.total_seconds()
    run_every = getattr(schedule, "run_every", None)
    return run_every.total_seconds() if isinstance(run_every, timedelta) else None


def covering_run(task_name: str, beat_schedule: dict, now: datetime, client=None) -> dict:
    """The scheduled run that covers a lost run of ``task_name``.

    ``covered`` is False when no beat entry runs the task: nothing queues it
    again. ``catchup`` says whether the catch-up pass (boot, and weekdays
    after the close) queues one run before the next firing: ``pending``,
    ``used`` (the firing's one catch-up was already queued, most likely the
    run that was lost), ``next_run_close`` or ``not_listed``.
    """
    from app.tasks import catchup

    best: Optional[dict] = None
    for name, entry in (beat_schedule or {}).items():
        if entry.get("task") != task_name:
            continue
        schedule = entry["schedule"]
        interval = _seconds(schedule)
        if interval is not None:
            found = {"entry": name, "next_run_at": now + timedelta(seconds=interval), "every_seconds": interval,
                     "catchup": "not_listed"}
        else:
            try:
                next_run = catchup.next_firing(schedule, now)
            except Exception:
                continue
            state = "not_listed"
            if name in catchup.CATCHUP_ENTRIES:
                firing = catchup.missed_firing(schedule, None, now)
                if next_run - now <= catchup.NEXT_RUN_GRACE:
                    state = "next_run_close"
                elif firing is None:
                    state = "not_listed"
                else:
                    used = False
                    if client is not None:
                        try:
                            used = bool(client.exists(f"{catchup.CATCHUP_MARKER_PREFIX}{name}:{firing.isoformat()}"))
                        except Exception:
                            used = False
                    state = "used" if used else "pending"
            found = {"entry": name, "next_run_at": next_run, "every_seconds": None, "catchup": state}
        if best is None or found["next_run_at"] < best["next_run_at"]:
            best = found
    if best is None:
        return {"covered": False, "entry": None, "next_run_at": None, "every_seconds": None, "catchup": "not_listed"}
    return {"covered": True, **best, "next_run_at": best["next_run_at"].astimezone(timezone.utc).isoformat()}


def _eastern(iso: str) -> str:
    """An ISO instant as New York wall-clock time, the beat schedule's own clock."""
    from zoneinfo import ZoneInfo

    try:
        local = datetime.fromisoformat(iso).astimezone(ZoneInfo("America/New_York"))
    except (TypeError, ValueError):
        return str(iso)
    return local.strftime("%A %Y-%m-%d %H:%M ET")


def loss_message(job: str, cause: str, cover: dict, ran_seconds: Optional[float]) -> str:
    ran = "" if ran_seconds is None else f" after {ran_seconds:.0f} s"
    text = f"The process running {job} was stopped{ran} ({cause}). The job was not delivered again."
    if not cover.get("covered"):
        return f"{text} No beat entry runs this task, so nothing queues it again."
    when = _eastern(cover["next_run_at"])
    if cover.get("every_seconds"):
        text += f" Its schedule ({cover['entry']}) runs it again within {cover['every_seconds']:.0f} s."
    else:
        text += f" Its next scheduled run ({cover['entry']}) is {when}."
    if cover.get("catchup") == "pending":
        text += " The catch-up pass (at boot, and weekdays after the close) queues one run before then."
    elif cover.get("catchup") == "used":
        text += " The catch-up for this firing was already used, so it waits for that run."
    return text


def _cause(task_id: str, fallback: str, client) -> tuple[str, dict]:
    try:
        raw = client.get(f"{STOPPED_PREFIX}{task_id}") if client is not None else None
        detail = json.loads(raw) if raw else {}
    except Exception:
        detail = {}
    if detail.get("reason") == "memory_cap":
        return (f"memory cap: {detail.get('resident_mb')} MiB resident, cap {detail.get('cap_mb')} MiB "
                f"({MAX_MEMORY_ENV})"), detail
    return fallback, detail


def record_lost_job(task_name: str, task_id: str, *, fallback_cause: str, started_at: Optional[float] = None,
                    hostname: Optional[str] = None, now: Optional[datetime] = None, client=None,
                    session_factory=None) -> Optional[dict]:
    """Log one lost research job and store it as a warning notification. Never raises."""
    now = now or datetime.now(timezone.utc)
    job = task_name.rsplit(".", 1)[-1]
    try:
        from app.tasks.celery_app import celery_app

        cover = covering_run(task_name, celery_app.conf.beat_schedule, now, client)
    except Exception:
        cover = {"covered": False, "entry": None, "next_run_at": None, "every_seconds": None, "catchup": "not_listed"}
    cause, detail = _cause(task_id, fallback_cause, client)
    ran_seconds = None if started_at is None else max(0.0, now.timestamp() - float(started_at))
    loss = {
        "job": job, "task": task_name, "task_id": task_id, "worker": hostname, "cause": cause,
        "ran_seconds": None if ran_seconds is None else round(ran_seconds, 1),
        "lost_at": now.isoformat(), "redelivered": False, "covering_run": cover, **({"stop": detail} if detail else {}),
    }
    message = loss_message(job, cause, cover, ran_seconds)
    logger.warning("Research job lost with its process: %s", message)
    try:
        from app.services.notifications import LOST_RESEARCH_JOB_ENTITY, create_notification

        with (session_factory or _one_off_session)() as db:
            # A warning: a critical scheduled-job alert would hold paper orders
            # (notifications.trading_blocking_critical_filter), and a lost
            # research job is no reason to stop trading.
            create_notification(
                db, category="scheduled_job", severity="warning", source=job,
                title=f"Research job lost: {job}", message=message, entity_type=LOST_RESEARCH_JOB_ENTITY, payload=loss,
            )
            db.commit()
    except Exception as exc:
        logger.error("Could not store the lost research job: job=%s error_type=%s", job, type(exc).__name__)
    return loss


class _one_off_session:
    """A session on a connection of its own, closed for good afterwards.

    The losses are recorded in the worker's main process, which forks the
    task processes. A pooled connection left open there would be inherited by
    every later task process.
    """

    def __enter__(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from sqlalchemy.pool import NullPool

        from app.db.session import connect_args

        self.engine = create_engine(settings.database_url, connect_args=connect_args, poolclass=NullPool)
        self.session = Session(self.engine)
        return self.session

    def __exit__(self, *_exc) -> None:
        try:
            self.session.close()
        finally:
            self.engine.dispose()


def _is_research_task(sender) -> bool:
    try:
        from app.tasks.celery_app import celery_app

        route = (celery_app.conf.task_routes or {}).get(getattr(sender, "name", ""), {})
        return route.get("queue") == RESEARCH_QUEUE
    except Exception:
        return False


def _process_loss_cause(exception) -> Optional[str]:
    """Why the task's process is gone, or None when the task merely raised."""
    from billiard.exceptions import TimeLimitExceeded, WorkerLostError

    if isinstance(exception, WorkerLostError):
        return str(exception).split(" Job:")[0].strip() or "worker process lost"
    if isinstance(exception, TimeLimitExceeded):
        # A prefork worker kills the process of a task that passes its hard limit.
        limit = exception.args[0] if exception.args else None
        return "its hard time limit" + (f" of {limit:g} s" if isinstance(limit, (int, float)) else "")
    return None


def _on_failure(sender=None, task_id=None, exception=None, **_kwargs) -> None:
    """Main process: a task process died under a task that Celery will not deliver again."""
    cause = _process_loss_cause(exception)
    if cause is None or not task_id or sender is None or _redelivered_on_loss(sender):
        return
    client, marker = None, None
    try:
        client = _client()
        if _worker_hostname:
            raw = client.hget(f"{INFLIGHT_PREFIX}{_worker_hostname}", str(task_id))
            marker = json.loads(raw) if raw else None
            client.hdel(f"{INFLIGHT_PREFIX}{_worker_hostname}", str(task_id))
    except Exception:
        marker = None
    try:
        if marker is None and not _is_research_task(sender):
            return
        record_lost_job(
            str(getattr(sender, "name", "")), str(task_id),
            fallback_cause=cause,
            started_at=(marker or {}).get("started_at"), hostname=_worker_hostname, client=client,
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def record_jobs_lost_with_worker(hostname: str, started_at: float, client=None, **record_kwargs) -> list[dict]:
    """Start-up of a worker: record the research jobs its killed predecessor was running."""
    own = client is None
    recorded: list[dict] = []
    try:
        client = client or _client()
        key = f"{INFLIGHT_PREFIX}{hostname}"
        for raw_id, raw in (client.hgetall(key) or {}).items():
            task_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
            try:
                marker = json.loads(raw)
            except (TypeError, ValueError):
                marker = {}
            # A marker newer than this worker belongs to a task it is running now.
            if float(marker.get("started_at") or 0) >= started_at:
                continue
            client.hdel(key, task_id)
            loss = record_lost_job(
                str(marker.get("task") or "unknown"), task_id,
                fallback_cause="its worker was killed and restarted",
                started_at=marker.get("started_at"), hostname=hostname, client=client, **record_kwargs,
            )
            if loss:
                recorded.append(loss)
    except Exception as exc:
        logger.error("Could not read the lost research job markers: error_type=%s", type(exc).__name__)
    finally:
        if own and client is not None:
            try:
                client.close()
            except Exception:
                pass
    return recorded


def _on_worker_ready(sender=None, **_kwargs) -> None:
    hostname = getattr(sender, "hostname", None) or _worker_hostname
    if hostname:
        record_jobs_lost_with_worker(str(hostname), _worker_started_at or time.time())


celeryd_after_setup.connect(_on_worker_setup, weak=False)
worker_process_init.connect(_on_task_process_start, weak=False)
task_prerun.connect(_on_prerun, weak=False)
task_postrun.connect(_on_postrun, weak=False)
task_failure.connect(_on_failure, weak=False)
worker_ready.connect(_on_worker_ready, weak=False)
