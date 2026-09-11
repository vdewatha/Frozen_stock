from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models import AuditLog


def write_audit_log(
    db: Session,
    *,
    event_type: str,
    action: str,
    status: str,
    message: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    payload: Optional[dict] = None,
) -> AuditLog:
    row = AuditLog(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        status=status,
        message=message,
        payload=payload or {},
    )
    db.add(row)
    return row
