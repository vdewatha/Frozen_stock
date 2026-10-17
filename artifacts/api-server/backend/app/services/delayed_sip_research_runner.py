"""Lease-coordinated delayed SIP collection for research-only hosted runs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from app.core.config import settings

LOCK_KEY = "trading:scheduled-job:delayed_sip_collection_job"
HEARTBEAT_KEY = "trading:research:delayed_sip:heartbeat"
LOCK_TTL_SECONDS = 360
DEFAULT_INTERVAL_SECONDS = 300
UTC = timezone.utc


def run_delayed_sip_collection_once(
    *,
    now: datetime | None = None,
    redis_client=None,
    db_factory: Callable | None = None,
) -> dict:
    """Collect bounded delayed SIP observations without enabling execution."""
    observed = now or datetime.now(UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("delayed SIP runner clock must be timezone aware")
    if settings.allow_live_trading:
        return {"status": "blocked_live_configuration", "ran": False, "execution_eligible": False}
    if not settings.delayed_sip_research_enabled:
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
        redis_client.set(HEARTBEAT_KEY, observed.isoformat(), ex=LOCK_TTL_SECONDS)
        lock = redis_client.lock(LOCK_KEY, timeout=LOCK_TTL_SECONDS, blocking=False)
        if not lock.acquire(blocking=False):
            return {"status": "lease_busy", "ran": False, "execution_eligible": False}
        acquired = True
        if db_factory is None:
            from app.db.session import SessionLocal

            db_factory = SessionLocal
        from app.services.delayed_sip_research import collect_delayed_sip

        db = db_factory()
        try:
            result = collect_delayed_sip(db, now=observed)
            result["execution_eligible"] = False
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


def run_delayed_sip_research_loop(*, stop_event=None, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> None:
    """Keep delayed historical coverage moving on a worker-less host."""
    import logging
    import threading

    if interval_seconds < 1:
        raise ValueError("interval_seconds must be positive")
    stop_event = stop_event or threading.Event()
    logger = logging.getLogger(__name__)
    while not stop_event.is_set():
        try:
            result = run_delayed_sip_collection_once()
            logger.info("delayed SIP research poll status=%s", result.get("status"))
        except Exception as exc:  # pragma: no cover - process supervisor path
            logger.warning("delayed SIP research poll unavailable: %s", exc.__class__.__name__)
        stop_event.wait(interval_seconds)


def delayed_sip_runner_status(*, now: datetime | None = None) -> dict:
    """Return non-secret liveness evidence for the supervised collector."""
    if not settings.delayed_sip_research_enabled:
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
