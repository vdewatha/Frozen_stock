from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Notification

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
# Research-only jobs: a failure stays a critical alert (it is retried and shown)
# but cannot block paper trading. strategy_learning_scope_job backtests
# parameters with apply_promotions=False and never places or sizes an order.
# On 2026-11-09 its failures on deactivated symbols (ALGN, ABNB) kept 200+
# critical alerts open and blocked every autonomous order, cost probes and the
# slow book's rebalance included.
RESEARCH_ONLY_JOB_SOURCE_PREFIXES = ("strategy_learning_scope_job:",)


def trading_blocking_critical_filter():
    """SQL condition for open critical notifications that should block paper trading."""
    research_only = or_(*(Notification.source.like(f"{prefix}%") for prefix in RESEARCH_ONLY_JOB_SOURCE_PREFIXES))
    return and_(
        Notification.status != "resolved",
        Notification.severity == "critical",
        Notification.category != "deployment_monitor",
        # NULL-safe: a row without a source is never treated as research-only.
        or_(Notification.category != "scheduled_job", Notification.source.is_(None), ~research_only),
    )


def create_notification(
    db: Session,
    *,
    category: str,
    severity: str,
    source: str,
    title: str,
    message: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    payload: Optional[dict] = None,
    commit: bool = False,
) -> Notification:
    notification = Notification(
        category=category,
        severity=severity,
        status="open",
        source=source,
        title=title,
        message=message,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload or {},
    )
    db.add(notification)
    if SEVERITY_ORDER.get(str(severity).lower(), 0) >= SEVERITY_ORDER.get(str(settings.alert_min_severity).lower(), 1):
        # Warning/critical alerts also go to the external delivery outbox.
        from app.services.alert_delivery import queue_notification

        queue_notification(db, notification)
    if commit:
        db.commit()
        db.refresh(notification)
    return notification


def list_notifications(
    db: Session,
    *,
    status: Optional[str] = None,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 50,
) -> list[Notification]:
    query = db.query(Notification)
    if status:
        query = query.filter(Notification.status == status)
    if category:
        query = query.filter(Notification.category == category)
    if severity:
        query = query.filter(Notification.severity == severity)
    return query.order_by(Notification.created_at.desc()).limit(min(limit, 200)).all()


def acknowledge_notification(db: Session, notification_id: int) -> Notification:
    notification = db.query(Notification).filter(Notification.id == notification_id).one_or_none()
    if not notification:
        raise ValueError(f"Unknown notification: {notification_id}")
    if notification.status == "open":
        notification.status = "acknowledged"
        notification.acknowledged_at = datetime.utcnow()
        notification.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(notification)
    return notification


def resolve_notification(db: Session, notification_id: int) -> Notification:
    notification = db.query(Notification).filter(Notification.id == notification_id).one_or_none()
    if not notification:
        raise ValueError(f"Unknown notification: {notification_id}")
    notification.status = "resolved"
    if not notification.acknowledged_at:
        notification.acknowledged_at = datetime.utcnow()
    notification.resolved_at = datetime.utcnow()
    notification.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(notification)
    return notification


def resolve_successful_job_notifications(db: Session, source: str) -> int:
    """Close prior critical incidents only after the same job succeeds again."""
    notifications = (
        db.query(Notification)
        .filter(
            Notification.category == "scheduled_job",
            Notification.severity == "critical",
            Notification.status == "open",
            Notification.source == source,
        )
        .all()
    )
    if not notifications:
        return 0
    now = datetime.utcnow()
    for notification in notifications:
        notification.status = "resolved"
        notification.acknowledged_at = notification.acknowledged_at or now
        notification.resolved_at = now
        notification.updated_at = now
    return len(notifications)


LEARNING_SCOPE_SOURCE_PREFIX = "strategy_learning_scope_job:"


def resolve_obsolete_learning_scope_alerts(db: Session) -> dict:
    """Close scope-job alerts that can never clear by a successful rerun.

    A scope alert closes when the same scope succeeds again. Two kinds never
    will: target-weight rows with no indicator class (``passive_arm``,
    ``defensive_tsmom_iv``; about 216 alerts on 2026-11-09 before PR #54 skipped
    them) and symbols deactivated after the scope was planned (ALGN, ABNB, BDX).
    Their scopes are now skipped without a rerun, so the old alerts stayed open
    and held the readiness "Scheduler health" check blocked for a day. The
    caller commits.
    """
    from app.models import Asset
    from app.services.strategies.registry import is_known_strategy

    rows = (
        db.query(Notification)
        .filter(
            Notification.category == "scheduled_job",
            Notification.status != "resolved",
            Notification.source.like(f"{LEARNING_SCOPE_SOURCE_PREFIX}%"),
        )
        .all()
    )
    if not rows:
        return {"resolved": 0, "unknown_strategy": 0, "inactive_symbol": 0}
    active = {symbol for (symbol,) in db.query(Asset.symbol).filter(Asset.is_active.is_(True)).all()}
    now = datetime.utcnow()
    counts = {"unknown_strategy": 0, "inactive_symbol": 0}
    for row in rows:
        parts = str(row.source)[len(LEARNING_SCOPE_SOURCE_PREFIX):].split(":", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            continue
        symbol, slug = parts[0].strip().upper(), parts[1].strip()
        if not is_known_strategy(slug):
            counts["unknown_strategy"] += 1
        elif symbol not in active:
            counts["inactive_symbol"] += 1
        else:
            continue
        row.status = "resolved"
        row.acknowledged_at = row.acknowledged_at or now
        row.resolved_at = now
        row.updated_at = now
    return {"resolved": counts["unknown_strategy"] + counts["inactive_symbol"], **counts}
