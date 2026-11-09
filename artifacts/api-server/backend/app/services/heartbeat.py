"""Cheap, public liveness evidence for an outside uptime check.

``/api/ready`` builds the full readiness snapshot and can take longer than a
minute on the hosted VM, so it cannot be polled from outside. This module reads
only short-lived Redis keys and one primary-key row:

- a Celery worker heartbeat key (written every few seconds, 90 s expiry);
- the beat lease (renewed by the single beat process, 15 s expiry);
- the monitor and watchdog heartbeats on the recovery state, written by the
  five-minute monitoring jobs. A stale stamp means work stopped flowing even
  if the processes are still up.

It also reports whether the recovery state is tradable and, when it is not, how
long it has been seen halted, so the outside check can tell a short automatic
pause from a halt that needs the owner.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import redis
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import StockPaperRecoveryState


WORKER_HEARTBEAT_PATTERN = "celery:worker:heartbeat:*"
BEAT_LEASE_KEY = "celery:beat:lease:primary"
HALT_SEEN_KEY = "trading:heartbeat:halt-seen-at"
HALT_SEEN_TTL_SECONDS = 7 * 24 * 60 * 60
# Matches stock_recovery.HEARTBEAT_TIMEOUT: past this the watchdog re-pauses.
WORK_STALE_SECONDS = 10 * 60
TRADABLE_STATES = frozenset({"armed", "resumable"})
REDIS_TIMEOUT_SECONDS = 0.5
# The route is unauthenticated, so repeated calls share one result.
CACHE_SECONDS = 10.0

_cache_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


def _age_seconds(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    stamp = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return round((now - stamp).total_seconds(), 1)


def _redis_evidence(tradable: bool | None, now: datetime) -> dict:
    evidence = {"worker_alive": False, "beat_alive": False, "redis_error": None, "halted_for_seconds": None}
    client = None
    try:
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
        )
        evidence["worker_alive"] = next(iter(client.scan_iter(match=WORKER_HEARTBEAT_PATTERN, count=100)), None) is not None
        evidence["beat_alive"] = bool(client.exists(BEAT_LEASE_KEY))
        if tradable is False:
            client.set(HALT_SEEN_KEY, now.isoformat(), nx=True, ex=HALT_SEEN_TTL_SECONDS)
            raw = client.get(HALT_SEEN_KEY)
            raw = raw.decode() if isinstance(raw, bytes) else raw
            try:
                evidence["halted_for_seconds"] = _age_seconds(datetime.fromisoformat(raw), now) if raw else 0.0
            except ValueError:
                evidence["halted_for_seconds"] = 0.0
        elif tradable is True:
            client.delete(HALT_SEEN_KEY)
    except Exception as exc:  # pragma: no cover - exercised through the error field
        evidence["redis_error"] = exc.__class__.__name__
    finally:
        if client is not None:
            client.close()
    return evidence


def heartbeat_snapshot(db: Session, *, now: datetime | None = None) -> dict:
    """Return liveness evidence; ``ok`` is false when the bot has stopped working."""
    now = now or datetime.now(timezone.utc)
    problems: list[str] = []
    state = None
    try:
        state = db.get(StockPaperRecoveryState, 1)
    except Exception as exc:
        problems.append(f"database:{exc.__class__.__name__}")
    monitor_age = _age_seconds(state.last_monitor_heartbeat_at, now) if state else None
    watchdog_age = _age_seconds(state.last_watchdog_heartbeat_at, now) if state else None
    recovery_status = state.status if state else None
    tradable = None
    if state is not None:
        tradable = state.status in TRADABLE_STATES and not state.accounting_review_required

    evidence = _redis_evidence(tradable, now)
    if evidence["redis_error"]:
        problems.append(f"redis:{evidence['redis_error']}")
    else:
        if not evidence["worker_alive"]:
            problems.append("no_worker_heartbeat")
        if not evidence["beat_alive"]:
            problems.append("no_beat_lease")
    if state is not None:
        for label, age in (("monitor", monitor_age), ("watchdog", watchdog_age)):
            if age is None or age > WORK_STALE_SECONDS:
                problems.append(f"{label}_heartbeat_stale")

    return {
        "ok": not problems,
        "problems": problems,
        "worker_alive": evidence["worker_alive"],
        "beat_alive": evidence["beat_alive"],
        "monitor_age_seconds": monitor_age,
        "watchdog_age_seconds": watchdog_age,
        "work_stale_after_seconds": WORK_STALE_SECONDS,
        "recovery_status": recovery_status,
        "tradable": tradable,
        "halted_for_seconds": evidence["halted_for_seconds"],
        "generated_at": now.isoformat(),
        "paper_only": True,
        "live_trading": False,
    }


def cached_heartbeat_snapshot(db: Session) -> dict:
    global _cache
    with _cache_lock:
        if _cache is not None and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        snapshot = heartbeat_snapshot(db)
        _cache = (time.monotonic(), snapshot)
        return snapshot


def reset_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None
