from __future__ import annotations

from hashlib import sha256
import json
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import AuditLog


def lock_audit_chain(db: Session) -> None:
    """Take the audit writers' transaction lock (PostgreSQL; a no-op elsewhere).

    ``write_audit_log`` takes it for every row.  A writer that must look
    before it writes (a once-only record) takes it first, so a second writer
    waits and then sees the first one's row.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(781928344203)"))


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
    # Serialize audit writers so the previous digest is unambiguous under
    # PostgreSQL concurrency. SQLite remains deterministic for local tests.
    lock_audit_chain(db)
    # A transaction can write several audit events before it commits. Flush
    # earlier events in this session so the next event links to the actual
    # latest row rather than branching from the last committed row.
    db.flush()
    previous = db.scalar(
        select(AuditLog.event_sha256).order_by(AuditLog.id.desc()).limit(1)
    )
    normalized_payload = payload or {}
    digest_payload = {
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action": action,
        "status": status,
        "message": message,
        "payload": normalized_payload,
        "previous_event_sha256": previous,
    }
    digest = sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    row = AuditLog(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        status=status,
        message=message,
        payload=normalized_payload,
        previous_event_sha256=previous,
        event_sha256=digest,
    )
    db.add(row)
    return row
