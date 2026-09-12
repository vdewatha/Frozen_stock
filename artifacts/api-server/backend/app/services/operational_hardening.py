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
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    previous = None
    for row in rows:
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
            return _check("audit_chain", "breach", "Audit event digest chain does not verify.", row_id=row.id)
        previous = row.event_sha256
    return _check("audit_chain", "clear", "Audit event digest chain verifies.", event_count=len(rows))


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
    checks = [
        _database_check(db),
        _scheduler_check(),
        _worker_lease_check(db),
        _audit_chain_check(db),
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
    if breaches and notify:
        create_notification(
            db,
            category="operational_hardening",
            severity="critical",
            source="operational_hardening",
            title="Operational hardening breach",
            message="; ".join(item["message"] for item in breaches),
            entity_type="deployment",
            payload={"checks": breaches, "configuration_sha256": digest["sha256"], "incident_procedure": INCIDENT_PROCEDURE},
        )
    if notify:
        db.commit()
    return {
        "status": overall,
        "generated_at": _now(),
        "source": source,
        "checks": checks,
        "configuration_digest": digest,
        "incident_procedure": INCIDENT_PROCEDURE,
    }


def operational_hardening_snapshot(db: Session) -> dict:
    return run_operational_hardening(db, source="read", notify=False)