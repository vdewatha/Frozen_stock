from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import AuditLog, LiveBrokerLedgerEvent
from app.services.live_operations import live_operations_evidence, live_operations_snapshot


def _db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_live_operations_is_uncertain_or_blocked_without_live_evidence():
    db = _db()
    try:
        snapshot = live_operations_snapshot(db)
        assert snapshot["status"] == "blocked"
        assert snapshot["mode"] == "research"
        assert snapshot["live_orders_allowed"] is False
        assert snapshot["components"]["broker"]["status"] == "unknown"
        assert snapshot["components"]["data_freshness"]["status"] == "unknown"
        assert all(alert["state"] != "healthy" for alert in snapshot["alerts"])
    finally:
        db.close()


def test_live_operations_evidence_is_attributable_and_redacted():
    db = _db()
    try:
        db.add(
            AuditLog(
                event_type="live_broker",
                entity_type="live_order",
                entity_id=4,
                action="submit",
                status="requested",
                message="Order submitted",
                payload={"actor": "operator", "request_id": "req-1", "secret": "must-not-export"},
                event_sha256="a" * 64,
            )
        )
        db.add(
            LiveBrokerLedgerEvent(
                account_id=None,
                event_type="reconcile",
                status="halted",
                reason="Review required",
                actor="system",
                payload={"request_id": "req-2", "raw_payload": "must-not-export"},
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        result = live_operations_evidence(db)
        assert result["events"]
        assert result["events"][0]["actor"] in {"operator", "system"}
        assert "secret" not in str(result)
        assert "must-not-export" not in str(result)
        assert result["redaction"]
    finally:
        db.close()