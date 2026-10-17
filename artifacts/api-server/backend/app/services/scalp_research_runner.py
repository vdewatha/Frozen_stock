"""Lease-coordinated, paper-only scalp research polling."""

from __future__ import annotations

from datetime import datetime, time as clock_time, timezone
import json
from typing import Callable
from zoneinfo import ZoneInfo

from app.core.config import settings

LOCK_TTL_SECONDS = 120
HEARTBEAT_TTL_SECONDS = 180
STATUS_TTL_SECONDS = 180
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
ALLOWED_SYMBOLS = frozenset({"AAPL", "MSFT", "SPY", "QQQ"})


def _status_key(symbol: str) -> str:
    return f"trading:research:scalp:{symbol}:status"


def _record_poll_status(redis_client, symbol: str, *, observed: datetime, result: dict) -> None:
    """Keep the last bounded poll outcome beside the liveness heartbeat."""
    payload = {
        "checked_at": observed.isoformat(),
        "status": result.get("status"),
        "run_id": result.get("run_id"),
        "error": result.get("error"),
        "execution_eligible": False,
    }
    redis_client.set(_status_key(symbol), json.dumps(payload, separators=(",", ":")), ex=STATUS_TTL_SECONDS)


def _is_market_window(now: datetime) -> bool:
    observed = now.astimezone(NY)
    return observed.weekday() < 5 and clock_time(9, 30) <= observed.time() < clock_time(16, 1)


def run_scalp_research_once(
    symbol: str,
    *,
    now: datetime | None = None,
    redis_client=None,
    db_factory: Callable | None = None,
) -> dict:
    """Run one isolated symbol research batch; never submit an order."""
    symbol = symbol.strip().upper()
    observed = now or datetime.now(UTC)
    if symbol not in ALLOWED_SYMBOLS:
        return {"status": "unsupported_symbol", "symbol": symbol, "execution_eligible": False}
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("scalp runner clock must be timezone aware")
    if settings.allow_live_trading:
        return {"status": "blocked_live_configuration", "symbol": symbol, "execution_eligible": False}
    if not settings.iex_research_enabled:
        return {"status": "disabled", "symbol": symbol, "execution_eligible": False}

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
        redis_client.set(
            f"trading:research:scalp:{symbol}:heartbeat",
            observed.isoformat(),
            ex=HEARTBEAT_TTL_SECONDS,
        )
        if not _is_market_window(observed):
            return {"status": "outside_market_hours", "symbol": symbol, "execution_eligible": False}
        lock = redis_client.lock(
            f"trading:scheduled-job:scalp_research:{symbol}",
            timeout=LOCK_TTL_SECONDS,
            blocking=False,
        )
        if not lock.acquire(blocking=False):
            return {"status": "lease_busy", "symbol": symbol, "execution_eligible": False}
        acquired = True
        if db_factory is None:
            from app.db.session import SessionLocal

            db_factory = SessionLocal
        from app.services.scalp_research import create_scalp_research_run, run_scalp_research

        db = db_factory()
        try:
            row = create_scalp_research_run(db, symbols=[symbol], actor="hosted-research-worker")
            db.commit()
            result = run_scalp_research(db, row.run_id)
            result.setdefault("execution_eligible", False)
            result["symbol"] = symbol
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


def run_scalp_research_loop(symbol: str, *, stop_event=None, interval_seconds: int = 60) -> None:
    import logging
    import threading

    if interval_seconds < 1:
        raise ValueError("interval_seconds must be positive")
    stop_event = stop_event or threading.Event()
    logger = logging.getLogger(__name__)
    while not stop_event.is_set():
        observed = datetime.now(UTC)
        try:
            result = run_scalp_research_once(symbol)
            # The heartbeat is intentionally separate from this outcome: a
            # supervised process can be alive while its database/data gate is
            # rejecting work. Persist only non-secret operational metadata.
            import redis

            client = redis.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
                health_check_interval=15,
            )
            try:
                _record_poll_status(client, symbol, observed=observed, result=result)
            finally:
                client.close()
            logger.info("scalp research poll symbol=%s status=%s", symbol, result.get("status"))
        except Exception as exc:  # pragma: no cover - process supervisor path
            try:
                import redis

                client = redis.Redis.from_url(
                    settings.redis_url,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                    health_check_interval=15,
                )
                try:
                    _record_poll_status(
                        client,
                        symbol,
                        observed=observed,
                        result={"status": "error", "error": exc.__class__.__name__},
                    )
                finally:
                    client.close()
            except Exception:
                pass
            logger.warning("scalp research poll symbol=%s unavailable: %s", symbol, exc.__class__.__name__)
        stop_event.wait(interval_seconds)


def scalp_research_runner_status(*, now: datetime | None = None, symbols=None) -> dict:
    """Return non-secret heartbeat evidence for each research-only scalp lane."""
    configured = symbols or sorted(ALLOWED_SYMBOLS)
    if not settings.iex_research_enabled:
        return {"enabled": False, "alive": False, "symbols": {symbol: {"alive": False} for symbol in configured}}
    observed = now or datetime.now(UTC)
    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            health_check_interval=15,
        )
        try:
            result = {}
            for symbol in configured:
                raw = client.get(f"trading:research:scalp:{symbol}:heartbeat")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                heartbeat = datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None
                age = (observed - heartbeat).total_seconds() if heartbeat else None
                try:
                    status_raw = client.get(_status_key(symbol))
                    if isinstance(status_raw, bytes):
                        status_raw = status_raw.decode("utf-8")
                    try:
                        last_poll = json.loads(status_raw) if isinstance(status_raw, str) else None
                    except json.JSONDecodeError:
                        last_poll = {"status": "unparseable"}
                except Exception:
                    # Older deployments may have only the heartbeat key. A
                    # missing diagnostic must not turn a healthy lane red.
                    last_poll = None
                result[symbol] = {
                    "alive": age is not None and 0 <= age <= HEARTBEAT_TTL_SECONDS,
                    "heartbeat_at": heartbeat.isoformat() if heartbeat else None,
                    "last_poll": last_poll,
                }
            return {"enabled": True, "alive": all(item["alive"] for item in result.values()), "symbols": result}
        finally:
            client.close()
    except Exception as exc:  # pragma: no cover - Redis outage path
        return {
            "enabled": True,
            "alive": False,
            "symbols": {symbol: {"alive": False} for symbol in configured},
            "error": exc.__class__.__name__,
        }
