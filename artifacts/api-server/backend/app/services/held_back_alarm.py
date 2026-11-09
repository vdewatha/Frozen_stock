"""Alarm on a session in which the bot wanted to trade, was held back, and sent nothing.

On 2026-11-09 every autonomous order was refused all session (launch admission
failed on research-job alerts, the slow book had no fresh VOO/SGOV price) and
nothing said so: 279 signal cycles logged ``submitted=0``, the slow book's
tranche and every cost-probe tick were refused, and it was found the next
evening by reading the logs. A day with no orders is often fine (no signal, no
tranche due, the week's probes done); a day on which something *wanted* to
trade and a system gate stopped it is not.

The signal cycle, the slow book's rebalance and the cost probes record each
held-back attempt here (Redis, per ET day; advisory, never raises). During the
session ``check_held_back_session`` opens one warning per day when held-back
attempts exist and no autonomous or probe order reached the broker, and
resolves it once one does. A warning, not critical: a critical alert would
itself fail the launch-admission notifications gate.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
KEY_PREFIX = "held-back:"
KEY_TTL_SECONDS = 3 * 24 * 60 * 60
NOTIFICATION_SOURCE = "held_back_session"
NOTIFICATION_CATEGORY = "paper_execution"
# Give the first rebalance (10:30 ET) and probe ticks a chance before judging.
FIRST_CHECK_AFTER_OPEN = timedelta(minutes=60)
SOURCE_LABELS = {
    "signals": "strategy signals",
    "target_weight": "slow book (VOO/SGOV)",
    "cost_probe": "cost probes",
}
# Signal results refused by a system gate rather than by a strategy rule.
SIGNAL_GATE_PREFIXES = (
    "Scheduled paper execution blocked by launch admission",
    "Stock paper exposure is blocked until reconciled",
)
# Slow-book skips that are calendar or configuration, not a held-back tranche.
TARGET_WEIGHT_HELD_PREFIXES = (
    "engine is paused",
    "stock paper account is not",
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _day(now: datetime) -> date:
    return _utc(now).astimezone(ET).date()


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


# ------------------------------------------------------------ classification

def signal_cycle_held_back(result: Mapping) -> tuple[int, Optional[str]]:
    """(count, commonest reason) of a signal cycle's gate refusals."""
    if result.get("status") == "skipped":
        reason = str(result.get("reason") or "")
        return (1, reason) if reason.startswith("stock paper account is not reconciled") else (0, None)
    reasons = Counter(
        str(item.get("reason") or "")
        for item in result.get("results") or []
        if isinstance(item, Mapping) and item.get("status") == "blocked"
        and str(item.get("reason") or "").startswith(SIGNAL_GATE_PREFIXES)
    )
    if not reasons:
        return 0, None
    reason, _ = reasons.most_common(1)[0]
    return sum(reasons.values()), reason


def target_weight_held_back(result: Mapping) -> Optional[str]:
    status = result.get("status")
    reason = str(result.get("reason") or "")
    if status == "blocked":
        return reason or "blocked"
    if status == "skipped" and reason.startswith(TARGET_WEIGHT_HELD_PREFIXES):
        return reason
    return None


# ------------------------------------------------------------ recording

def record_held_back(source: str, reason: Optional[str], *, count: int = 1,
                     now: Optional[datetime] = None, client: Any = None) -> None:
    """Count a held-back attempt for today's session; advisory, never raises."""
    if count <= 0:
        return
    now = _utc(now or datetime.now(UTC))
    key = f"{KEY_PREFIX}{_day(now).isoformat()}"
    owned = client is None
    try:
        client = client or _redis()
        pipe = client.pipeline()
        pipe.hincrby(key, f"{source}:count", int(count))
        pipe.hsetnx(key, f"{source}:first_at", now.isoformat())
        pipe.hset(key, f"{source}:last_at", now.isoformat())
        pipe.hset(key, f"{source}:reason", str(reason or "")[:500])
        pipe.expire(key, KEY_TTL_SECONDS)
        pipe.execute()
    except Exception as exc:  # pragma: no cover - visibility only
        logger.warning("held-back attempt not recorded: source=%s error=%s", source, exc.__class__.__name__)
    finally:
        if owned and client is not None:
            try:
                client.close()
            except Exception:
                pass


def held_back_on(day: date, *, client: Any = None) -> dict[str, dict]:
    owned = client is None
    try:
        client = client or _redis()
        raw = client.hgetall(f"{KEY_PREFIX}{day.isoformat()}") or {}
    finally:
        if owned and client is not None:
            try:
                client.close()
            except Exception:
                pass
    out: dict[str, dict] = {}
    for field, value in raw.items():
        source, _, name = _text(field).rpartition(":")
        if not source:
            continue
        entry = out.setdefault(source, {})
        entry[name] = int(_text(value)) if name == "count" else _text(value)
    return {source: entry for source, entry in out.items() if int(entry.get("count", 0)) > 0}


# ------------------------------------------------------------ the alarm

def orders_sent_since(db: Session, since: datetime) -> int:
    from app.models.stock_paper import StockPaperOrder
    from app.services.stock_paper_ledger import AUTONOMOUS_ORDER_SOURCES, PROBE_SOURCE

    return (
        db.query(StockPaperOrder)
        .filter(
            StockPaperOrder.source.in_(sorted(AUTONOMOUS_ORDER_SOURCES | {PROBE_SOURCE})),
            StockPaperOrder.submitted_at.isnot(None),
            StockPaperOrder.submitted_at >= since,
        )
        .count()
    )


def _open_alarm(db: Session, day: date):
    from app.models import Notification

    return (
        db.query(Notification)
        .filter(
            Notification.source == NOTIFICATION_SOURCE,
            Notification.status != "resolved",
            Notification.entity_type == "session",
            Notification.entity_id == int(day.strftime("%Y%m%d")),
        )
        .order_by(Notification.id.desc())
        .first()
    )


def describe(held: Mapping[str, Mapping]) -> str:
    parts = []
    for source in sorted(held, key=lambda name: list(SOURCE_LABELS).index(name) if name in SOURCE_LABELS else 99):
        entry = held[source]
        label = SOURCE_LABELS.get(source, source)
        reason = entry.get("reason") or "no reason recorded"
        parts.append(f"{label}: {entry.get('count', 0)}x, last: {reason}")
    return "; ".join(parts)


def check_held_back_session(db: Session, *, now: Optional[datetime] = None, client: Any = None) -> dict:
    """Open (or resolve) today's held-back warning. The caller commits."""
    from app.services.exchange_sessions import session_bounds
    from app.services.notifications import create_notification

    now = _utc(now or datetime.now(UTC))
    today = _day(now)
    base = {"job": "held_back_session_alarm_job", "session": today.isoformat(), "paper_only": True}
    bounds = session_bounds(today)
    if bounds is None:
        return base | {"status": "skipped", "reason": "not a trading session"}
    opened = _utc(bounds[0])
    if now < opened + FIRST_CHECK_AFTER_OPEN:
        return base | {"status": "skipped", "reason": "too early in the session"}
    sent = orders_sent_since(db, opened)
    existing = _open_alarm(db, today)
    if sent:
        if existing is not None:
            existing.status = "resolved"
            existing.resolved_at = now.replace(tzinfo=None)
            existing.updated_at = now.replace(tzinfo=None)
        return base | {"status": "complete", "orders_sent": sent, "resolved_alarm": existing is not None}
    held = held_back_on(today, client=client)
    if not held:
        return base | {"status": "complete", "orders_sent": 0, "held_back": {}}
    message = (
        "No autonomous or cost-probe paper order has reached the broker this session, "
        f"and these wanted to trade but were held back: {describe(held)}."
    )
    payload = {"session": today.isoformat(), "held_back": held, "checked_at": now.isoformat()}
    if existing is not None:
        existing.message = message
        existing.payload = payload
        existing.updated_at = now.replace(tzinfo=None)
        return base | {"status": "complete", "orders_sent": 0, "held_back": held, "alarm": "updated"}
    create_notification(
        db,
        category=NOTIFICATION_CATEGORY,
        severity="warning",
        source=NOTIFICATION_SOURCE,
        title=f"No paper orders today: held back ({', '.join(SOURCE_LABELS.get(s, s) for s in held)})",
        message=message,
        entity_type="session",
        entity_id=int(today.strftime("%Y%m%d")),
        payload=payload,
    )
    logger.warning("held-back session: no orders sent; %s", describe(held))
    return base | {"status": "complete", "orders_sent": 0, "held_back": held, "alarm": "opened"}
