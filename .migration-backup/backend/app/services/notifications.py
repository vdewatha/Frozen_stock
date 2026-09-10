from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Notification


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
