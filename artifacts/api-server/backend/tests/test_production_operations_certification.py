"""Isolated acceptance drills for production operations and recovery.

These tests use disposable SQLite state and mocked infrastructure probes.  They
prove the control-plane contracts without contacting a live broker or restoring
over an active database.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import AuditLog, Notification
from app.services import deployment_monitor
from app.services.live_operations import live_operations_evidence, live_operations_snapshot
from app.services.operational_hardening import run_operational_hardening


ROOT = Path(__file__).parents[1]


def _db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_live_operations_health_contract_distinguishes_unknown_and_blocked_trading():
    db = _db()
    try:
        result = live_operations_snapshot(db)
        assert result["health"]["liveness"]["status"] == "healthy"
        assert result["health"]["readiness"]["status"] == "blocked"
        assert result["health"]["operation"]["status"] == "blocked"
        assert result["health"]["trading"]["status"] == "blocked"
        assert result["health"]["evidence"]["status"] == "unknown"
        assert "broker" in result["health"]["evidence"]["unknown_components"]
        assert result["live_orders_allowed"] is False
    finally:
        db.close()


def test_bounded_redacted_evidence_keeps_traceability_without_payload_values():
    db = _db()
    try:
        for index in range(75):
            db.add(
                AuditLog(
                    event_type="operations_probe",
                    entity_type="deployment",
                    entity_id=index,
                    action="observe",
                    status="blocked",
                    message=f"Probe {index}",
                    payload={
                        "actor": "reviewer",
                        "request_id": f"request-{index}",
                        "api_secret": "must-not-export",
                    },
                    created_at=datetime.now(timezone.utc),
                    event_sha256=f"{index:064x}",
                )
            )
        db.commit()
        result = live_operations_evidence(db, limit=200)
        assert len(result["events"]) == 50
        assert result["events"][0]["proof_digest"]
        assert result["events"][0]["payload_keys"] == ["actor", "request_id"]
        assert "must-not-export" not in str(result)
        assert "api_secret" not in str(result)
        assert "bounded" in result["retention"]
    finally:
        db.close()


def test_blocked_monitor_is_traced_to_one_notification_and_audit_event():
    db = _db()
    snapshot = {
        "deployable": False,
        "blockers": ["Redis"],
        "readiness_status": "blocked",
        "readiness_blockers": ["Scheduler health"],
        "checks": [],
    }
    with patch.object(deployment_monitor, "deployment_monitor_snapshot", return_value=snapshot):
        result = deployment_monitor.run_deployment_monitor(db, source="acceptance_drill")
    try:
        assert result["status"] == "blocked"
        assert result["notification_action"] == "created"
        notification = db.scalar(select(Notification))
        audit = db.scalar(select(AuditLog))
        assert notification is not None
        assert notification.severity == "critical"
        assert audit is not None
        assert audit.event_type == "deployment_monitor"
        assert audit.status == "blocked"
        assert "acceptance_drill" in (notification.payload or {}).get("source", "")
        assert "secret" not in str(notification.payload)
    finally:
        db.close()


def test_operational_hardening_is_unknown_on_disposable_sqlite_and_preserves_incident_procedure():
    db = _db()
    try:
        with patch(
            "redis.Redis.from_url",
            side_effect=ConnectionError("redis unavailable"),
        ), patch.object(
            __import__("app.services.operational_hardening", fromlist=["settings"]).settings,
            "database_url",
            "sqlite:///disposable.db",
        ):
            result = run_operational_hardening(db, source="acceptance_drill", notify=False)
        assert result["status"] == "unknown"
        statuses = {check["key"]: check["status"] for check in result["checks"]}
        assert statuses["postgresql"] == "unknown"
        assert statuses["scheduler_ownership"] == "unknown"
        assert statuses["backup_restore_tools"] == "unknown"
        assert "disposable target" in result["incident_procedure"]
        assert "redis unavailable" not in str(result)
    finally:
        db.close()


def test_watchdog_exception_returns_fail_closed_unknown_result():
    spec = importlib.util.spec_from_file_location(
        "stock_watchdog",
        ROOT / "scripts" / "run_stock_watchdog.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    db = MagicMock()
    with patch.object(module, "SessionLocal", return_value=db), patch.object(
        module, "run_stock_watchdog", side_effect=RuntimeError("probe failed")
    ):
        result = module.run_once()
    assert result == {
        "status": "unknown",
        "reasons": ["watchdog_evaluation_failed"],
        "fail_closed": True,
    }
    db.rollback.assert_called_once_with()
    db.close.assert_called_once_with()


def test_backup_restore_requires_explicit_disposable_target():
    spec = importlib.util.spec_from_file_location(
        "backup_restore",
        ROOT / "scripts" / "verify_postgres_backup_restore.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    with patch.dict(
        "os.environ",
        {
            "DATABASE_URL": "postgresql://source.example/database",
            "RESTORE_DATABASE_URL": "postgresql://target.example/database",
        },
        clear=True,
    ), patch.object(sys, "argv", ["verify_postgres_backup_restore.py"]):
        try:
            module.main()
        except SystemExit as exc:
            assert exc.code == "RESTORE_TARGET_DISPOSABLE=true is required for a restore target"
        else:
            raise AssertionError("restore guard did not reject an unmarked target")