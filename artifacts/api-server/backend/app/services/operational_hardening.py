"""Deployment-time checks for concurrency, leases, audit integrity, and recovery."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog, Notification, StockTrainingJob
from app.services.audit import write_audit_log
from app.services.notifications import create_notification

UTC = timezone.utc
BEAT_LEASE_KEY = "celery:beat:lease:primary"
INCIDENT_PROCEDURE = (
    "On breach: stop new paper entries with the kill switch, preserve the audit/event "
    "chain, reconcile broker state, verify the deployment/configuration digest, restore "
    "only into a disposable target, then resume only after operator review and fresh evidence."
)


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def configuration_digest() -> dict:
    backend = Path(__file__).resolve().parents[2]
    tracked = sorted(
        path for root in (backend / "app", backend / "alembic", backend / "scripts")
        if root.exists() for path in root.rglob("*.py")
    )
    file_hashes = {
        str(path.relative_to(backend)): sha256(path.read_bytes()).hexdigest()
        for path in tracked
    }
    public_config = {
        "allow_live_trading": settings.allow_live_trading,
        "redis_url": settings.redis_url.split("@")[-1],
        "alpaca_data_url": settings.alpaca_data_url,
        "alpaca_feed": settings.alpaca_feed,
        "environment": settings.environment,
        "file_hashes": file_hashes,
    }
    return {
        "sha256": sha256(_canonical(public_config)).hexdigest(),
        "inputs": {
            "allow_live_trading": settings.allow_live_trading,
            "redis_endpoint": settings.redis_url.split("@")[-1],
            "alpaca_data_url": settings.alpaca_data_url,
            "alpaca_feed": settings.alpaca_feed,
            "environment": settings.environment,
            "file_count": len(file_hashes),
        },
    }


def _check(key: str, status: str, message: str, **details: Any) -> dict:
    return {"key": key, "status": status, "message": message, "details": details}


def _database_check(db: Session) -> dict:
    dialect = db.get_bind().dialect.name
    if dialect != "postgresql":
        return _check("postgresql", "unknown", "The deployment database is not PostgreSQL.", dialect=dialect)
    isolation = db.scalar(text("SELECT current_setting('transaction_isolation')"))
    locked = db.scalar(text("SELECT pg_try_advisory_lock(781928344204)"))
    if locked:
        db.execute(text("SELECT pg_advisory_unlock(781928344204)"))
    return _check(
        "postgresql",
        "clear" if locked and isolation in {"read committed", "repeatable read", "serializable"} else "breach",
        "PostgreSQL locking probe completed.",
        transaction_isolation=isolation,
        advisory_lock_probe=bool(locked),
    )


def _scheduler_check() -> dict:
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        ttl = client.ttl(BEAT_LEASE_KEY)
        owner_present = bool(client.exists(BEAT_LEASE_KEY))
        if not owner_present:
            return _check("scheduler_ownership", "unknown", "No scheduler lease is currently visible.", ttl=ttl)
        return _check(
            "scheduler_ownership",
            "clear" if ttl > 0 else "breach",
            "Redis scheduler lease is visible.",
            ttl=ttl,
        )
    except Exception as exc:
        return _check("scheduler_ownership", "unknown", "Scheduler lease probe failed.", error_type=type(exc).__name__)


def _worker_lease_check(db: Session) -> dict:
    now = _now().replace(tzinfo=None)
    active = db.scalars(
        select(StockTrainingJob).where(StockTrainingJob.status.in_(["running", "leased"]))
    ).all()
    expired = [
        job.id for job in active
        if job.lease_expires_at is not None and job.lease_expires_at < now
    ]
    return _check(
        "worker_leases",
        "breach" if expired else "clear",
        "Worker lease records are within their expiry window." if not expired else "Expired worker leases require recovery.",
        active_count=len(active),
        expired_job_ids=expired,
    )


def _audit_chain_check(db: Session) -> dict:
    previous = None
    previous_row_id = None
    event_count = 0
    # Verify the full chain without retaining every payload in each API request.
    with db.scalars(select(AuditLog).order_by(AuditLog.id).execution_options(yield_per=100)) as rows:
        for row in rows:
            event_count += 1
            expected = sha256(_canonical({
                "event_type": row.event_type,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "action": row.action,
                "status": row.status,
                "message": row.message,
                "payload": row.payload or {},
                "previous_event_sha256": previous,
            })).hexdigest()
            if row.previous_event_sha256 != previous or row.event_sha256 != expected:
                previous_mismatch = row.previous_event_sha256 != previous
                return _check(
                    "audit_chain",
                    "breach",
                    "Audit event digest chain does not verify.",
                    incident_scope="historical_audit_integrity",
                    classification="historical_fork" if previous_mismatch else "event_digest_mismatch",
                    first_broken_row_id=row.id,
                    preceding_row_id=previous_row_id,
                    expected_previous_event_sha256=previous,
                    observed_previous_event_sha256=row.previous_event_sha256,
                    expected_event_sha256=expected,
                    observed_event_sha256=row.event_sha256,
                    event_type=row.event_type,
                    action=row.action,
                    event_status=row.status,
                    created_at=row.created_at.isoformat() if row.created_at else None,
                    original_rows_preserved=True,
                    runtime_health_scope="current runtime probes remain separately evaluated",
                )
            previous = row.event_sha256
            previous_row_id = row.id
    return _check(
        "audit_chain",
        "clear",
        "Audit event digest chain verifies.",
        event_count=event_count,
        incident_scope="historical_audit_integrity",
        original_rows_preserved=True,
    )


def _audit_chain_incident_fingerprint(check: dict) -> str:
    details = check.get("details") or {}
    return sha256(_canonical({
        "classification": details.get("classification"),
        "first_broken_row_id": details.get("first_broken_row_id"),
        "preceding_row_id": details.get("preceding_row_id"),
        "expected_previous_event_sha256": details.get("expected_previous_event_sha256"),
        "observed_previous_event_sha256": details.get("observed_previous_event_sha256"),
        "expected_event_sha256": details.get("expected_event_sha256"),
        "observed_event_sha256": details.get("observed_event_sha256"),
    })).hexdigest()


def _quarantine_audit_chain_incident(db: Session, check: dict, *, source: str) -> dict:
    """Record an immutable decision without rewriting the breached history."""
    details = dict(check.get("details") or {})
    fingerprint = _audit_chain_incident_fingerprint(check)
    details["incident_fingerprint"] = fingerprint
    existing = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.event_type == "operational_hardening",
            AuditLog.action == "quarantine_audit_chain",
        )
        .order_by(AuditLog.id.desc())
    ).all()
    for row in existing:
        if (row.payload or {}).get("incident_fingerprint") == fingerprint:
            return details | {"decision": "already_quarantined", "decision_audit_id": row.id}
    row = write_audit_log(
        db,
        event_type="operational_hardening",
        action="quarantine_audit_chain",
        status="blocked",
        message="Historical audit-chain integrity incident quarantined; original audit rows are preserved.",
        entity_type="audit_chain",
        entity_id=details.get("first_broken_row_id"),
        payload={
            "incident_fingerprint": fingerprint,
            "classification": details.get("classification"),
            "first_broken_row_id": details.get("first_broken_row_id"),
            "preceding_row_id": details.get("preceding_row_id"),
            "expected_previous_event_sha256": details.get("expected_previous_event_sha256"),
            "observed_previous_event_sha256": details.get("observed_previous_event_sha256"),
            "expected_event_sha256": details.get("expected_event_sha256"),
            "observed_event_sha256": details.get("observed_event_sha256"),
            "source": source,
            "original_rows_preserved": True,
            "resume_gate": "blocked_until_operator_review_and_fresh_evidence",
        },
    )
    db.flush()
    return details | {"decision": "quarantined", "decision_audit_id": row.id}


def _upsert_audit_chain_notification(
    db: Session,
    *,
    check: dict,
    incident: dict,
    configuration_sha256: str,
) -> Notification:
    payload = {
        "incident_scope": "historical_audit_integrity",
        "classification": incident.get("classification"),
        "incident_fingerprint": incident.get("incident_fingerprint"),
        "first_broken_row_id": incident.get("first_broken_row_id"),
        "preceding_row_id": incident.get("preceding_row_id"),
        "original_rows_preserved": True,
        "runtime_health_scope": "reported by separate operational checks",
        "checks": [check],
        "configuration_sha256": configuration_sha256,
        "incident_procedure": INCIDENT_PROCEDURE,
    }
    candidates = db.scalars(
        select(Notification)
        .where(
            Notification.category == "operational_hardening",
            Notification.source == "operational_hardening",
            Notification.status.in_(["open", "acknowledged"]),
        )
        .order_by(Notification.id.desc())
    ).all()
    notification = next(
        (
            row for row in candidates
            if row.title == "Operational hardening breach"
            or (row.payload or {}).get("incident_scope") == "historical_audit_integrity"
        ),
        None,
    )
    message = (
        "Historical audit-chain integrity incident quarantined at retained row "
        f"{incident.get('first_broken_row_id')}; original audit rows are preserved. "
        "Current runtime health remains reported by separate checks."
    )
    if notification is None:
        notification = create_notification(
            db,
            category="operational_hardening",
            severity="critical",
            source="operational_hardening",
            title="Historical audit-chain integrity incident quarantined",
            message=message,
            entity_type="audit_chain",
            entity_id=incident.get("first_broken_row_id"),
            payload=payload,
        )
    else:
        notification.title = "Historical audit-chain integrity incident quarantined"
        notification.message = message
        notification.entity_type = "audit_chain"
        notification.entity_id = incident.get("first_broken_row_id")
        notification.payload = payload
        notification.updated_at = _now()
    return notification


def _backup_check() -> dict:
    if settings.database_url.startswith("sqlite"):
        return _check("backup_restore_tools", "unknown", "Backup/restore proof requires a PostgreSQL deployment target.")
    tools = {"pg_dump": shutil.which("pg_dump"), "pg_restore": shutil.which("pg_restore")}
    return _check(
        "backup_restore_tools",
        "clear" if all(tools.values()) else "breach",
        "PostgreSQL backup and restore tools are available." if all(tools.values()) else "pg_dump and pg_restore are required.",
        tools=tools,
    )


def run_operational_hardening(db: Session, *, source: str = "operator", notify: bool = True) -> dict:
    audit_check = _audit_chain_check(db)
    checks = [
        _database_check(db),
        _scheduler_check(),
        _worker_lease_check(db),
        audit_check,
        _backup_check(),
        _check(
            "live_trading_guard",
            "clear" if settings.allow_live_trading is False else "breach",
            "Live trading is explicitly disabled." if settings.allow_live_trading is False else "Live trading must remain disabled.",
        ),
    ]
    digest = configuration_digest()
    statuses = [item["status"] for item in checks]
    overall = "breach" if "breach" in statuses else "unknown" if "unknown" in statuses else "clear"
    breaches = [item for item in checks if item["status"] == "breach"]
    audit_incident = None
    if audit_check["status"] == "breach" and notify:
        audit_incident = _quarantine_audit_chain_incident(db, audit_check, source=source)
    if breaches and notify:
        if audit_check["status"] == "breach":
            _upsert_audit_chain_notification(
                db,
                check=audit_check,
                incident=audit_incident or {},
                configuration_sha256=digest["sha256"],
            )
        else:
            create_notification(
                db,
                category="operational_hardening",
                severity="critical",
                source="operational_hardening",
                title="Operational hardening breach",
                message="; ".join(item["message"] for item in breaches),
                entity_type="deployment",
                payload={
                    "checks": breaches,
                    "configuration_sha256": digest["sha256"],
                    "incident_procedure": INCIDENT_PROCEDURE,
                    "incident_scope": "current_runtime_health",
                },
            )
    if notify:
        db.commit()
    runtime_checks = [item for item in checks if item["key"] != "audit_chain"]
    runtime_statuses = [item["status"] for item in runtime_checks]
    runtime_status = (
        "breach"
        if "breach" in runtime_statuses
        else "unknown"
        if "unknown" in runtime_statuses
        else "clear"
    )
    return {
        "status": overall,
        "generated_at": _now(),
        "source": source,
        "checks": checks,
        "configuration_digest": digest,
        "incident_procedure": INCIDENT_PROCEDURE,
        "audit_chain_incident": audit_incident,
        "runtime_health": {
            "status": runtime_status,
            "checks": runtime_checks,
        },
    }


def operational_hardening_snapshot(db: Session) -> dict:
    return run_operational_hardening(db, source="read", notify=False)
