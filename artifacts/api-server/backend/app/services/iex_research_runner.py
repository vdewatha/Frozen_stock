"""Lease-coordinated, research-only IEX polling.

This module is deliberately independent of FastAPI and Celery.  It can run
inside the hosted API as a best-effort helper or as the entrypoint of a
dedicated, always-on research worker.  It never imports an execution task and
it refuses to run when live trading is enabled.
"""

from __future__ import annotations

from datetime import datetime, time as clock_time, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from app.core.config import settings

LOCK_KEY = "trading:scheduled-job:iex_research_collection_job"
HEARTBEAT_KEY = "trading:research:iex:heartbeat"
LOCK_TTL_SECONDS = 180
DEFAULT_INTERVAL_SECONDS = 60
NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def _is_market_window(now: datetime) -> bool:
    observed = now.astimezone(NY)
    return observed.weekday() < 5 and clock_time(9, 30) <= observed.time() < clock_time(16, 1)


def run_iex_collection_once(
    *,
    now: datetime | None = None,
    redis_client=None,
    db_factory: Callable | None = None,
) -> dict:
    """Collect one bounded IEX observation if the NYSE is open.

    ``redis_client`` and ``db_factory`` are injectable so lease and commit
    behavior can be tested without a broker request.  The returned status is
    operational metadata only; it never changes execution eligibility.
    """
    observed = now or datetime.now(UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("IEX runner clock must be timezone aware")
    if settings.allow_live_trading:
        return {"status": "blocked_live_configuration", "ran": False, "execution_eligible": False}
    if not settings.iex_research_enabled:
        return {"status": "disabled", "ran": False, "execution_eligible": False}

    owned_redis = redis_client is None
    if redis_client is None:
        import redis

        redis_client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=15,
        )
    lock = None
    acquired = False
    try:
        redis_client.ping()
        # A heartbeat is separate from collection freshness: it proves the
        # supervised runner is alive even when the exchange is closed.
        redis_client.set(HEARTBEAT_KEY, observed.isoformat(), ex=LOCK_TTL_SECONDS)
        if not _is_market_window(observed):
            return {"status": "outside_market_hours", "ran": False, "execution_eligible": False}
        lock = redis_client.lock(LOCK_KEY, timeout=LOCK_TTL_SECONDS, blocking=False)
        if not lock.acquire(blocking=False):
            return {"status": "lease_busy", "ran": False, "execution_eligible": False}
        acquired = True

        if db_factory is None:
            from app.db.session import SessionLocal

            db_factory = SessionLocal
        from app.services.alpaca_research_data import collect_iex_research
        from app.services.online_research import advance_online_research

        db = db_factory()
        try:
            result = collect_iex_research(db, now=observed)
            if result.get("status") == "observed":
                result["learning"] = advance_online_research(db)
            db.commit()
            return result
        finally:
            db.close()
    finally:
        if acquired and lock is not None:
            try:
                lock.release()
            except Exception:
                pass
        if owned_redis:
            try:
                redis_client.close()
            except Exception:
                pass


def research_runner_status(*, now: datetime | None = None) -> dict:
    """Return non-secret liveness evidence for the supervised runner."""
    from app.core.config import settings

    if not settings.iex_research_enabled:
        return {"enabled": False, "alive": False, "heartbeat_at": None}
    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            health_check_interval=15,
        )
        try:
            raw = client.get(HEARTBEAT_KEY)
        finally:
            client.close()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        heartbeat = datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None
        observed = now or datetime.now(UTC)
        age = (observed - heartbeat).total_seconds() if heartbeat else None
        return {
            "enabled": True,
            "alive": age is not None and 0 <= age <= LOCK_TTL_SECONDS,
            "heartbeat_at": heartbeat.isoformat() if heartbeat else None,
        }
    except Exception as exc:  # pragma: no cover - Redis outage path
        return {"enabled": True, "alive": False, "heartbeat_at": None, "error": exc.__class__.__name__}


def run_iex_research_loop(*, stop_event=None, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> None:
    """Run the safe polling loop until ``stop_event`` is set.

    Errors are logged only by class name and do not widen the loop's scope.
    A caller should supervise this process and restart it after termination.
    """
    import logging
    import threading

    if interval_seconds < 1:
        raise ValueError("interval_seconds must be positive")
    stop_event = stop_event or threading.Event()
    logger = logging.getLogger(__name__)
    while not stop_event.is_set():
        try:
            result = run_iex_collection_once()
            logger.info("IEX research poll status=%s", result.get("status"))
        except Exception as exc:  # pragma: no cover - process supervisor path
            logger.warning("IEX research poll unavailable: %s", exc.__class__.__name__)
        stop_event.wait(interval_seconds)
