from __future__ import annotations

from datetime import datetime
import os

import redis
from fastapi.encoders import jsonable_encoder
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Notification
from app.services.audit import write_audit_log
from app.services.broker import broker_status
from app.services.notifications import create_notification
from app.services.readiness import readiness_snapshot
from app.tasks.celery_app import celery_app


def _status(ok: bool) -> str:
    return "ready" if ok else "blocked"


def _check_database(db: Session) -> dict:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        return {
            "name": "Database",
            "status": "blocked",
            "message": "Database connection failed.",
            "details": {"error_type": exc.__class__.__name__},
        }
    return {"name": "Database", "status": "ready", "message": "Database connection succeeded.", "details": {}}


def _check_redis() -> dict:
    configured = bool(os.environ.get("REDIS_URL", "").strip())
    if not configured:
        return {
            "name": "Redis",
            "status": "blocked",
            "message": "Redis is not configured. Workers and scheduled jobs cannot be trusted.",
            "details": {"configured": False, "reachable": False},
        }
    try:
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        pong = bool(client.ping())
    except Exception as exc:
        return {
            "name": "Redis",
            "status": "blocked",
            "message": "Redis connection failed. Workers and scheduled jobs cannot be trusted.",
            "details": {"configured": True, "reachable": False, "error_type": exc.__class__.__name__},
        }
    return {
        "name": "Redis",
        "status": _status(pong),
        "message": "Redis connection succeeded." if pong else "Redis did not respond to ping.",
        "details": {"configured": True, "reachable": pong},
    }


def _check_market_readiness(readiness: dict) -> dict:
    market_check = next((check for check in readiness["checks"] if check["name"] == "Market data"), None)
    if not market_check:
        return {
            "name": "Trusted market data",
            "status": "blocked",
            "message": "Readiness did not include a market data check.",
            "details": {},
        }
    details = market_check.get("details", {})
    untrusted = details.get("untrusted_assets", [])
    missing = details.get("missing_assets", [])
    stale = details.get("stale_assets", [])
    ok = market_check["status"] == "ready" and not untrusted and not missing and not stale
    return {
        "name": "Trusted market data",
        "status": _status(ok),
        "message": "Active assets have recent trusted market data." if ok else "Active assets need a fresh trusted market import before deployment can be trusted.",
        "details": details,
    }


def _check_celery_schedule() -> dict:
    job_names = sorted(celery_app.conf.beat_schedule.keys())
    required_jobs = {
        "daily-market-data-import",
        "deployment-monitor-job",
        "strategy-learning-batch-job",
        "paper-trading-signal-job",
        "paper-trade-reconciliation-job",
        "risk-monitor-job",
        "stock-monitoring-job",
    }
    missing = sorted(required_jobs - set(job_names))
    return {
        "name": "Scheduled job definitions",
        "status": _status(not missing),
        "message": "Required scheduled jobs are defined." if not missing else "Required scheduled job definitions are missing.",
        "details": {"configured_jobs": job_names, "missing_required_jobs": missing},
    }


def _check_celery_workers() -> dict:
    """Require an actual Celery broadcast response, never configuration alone."""
    try:
        responses = celery_app.control.inspect(timeout=2).ping() or {}
        workers = sorted(responses)
    except Exception as exc:
        return {
            "name": "Celery workers",
            "status": "blocked",
            "message": "Celery worker ping failed; worker availability is unconfirmed.",
            "details": {"workers": [], "error_type": exc.__class__.__name__},
        }
    return {
        "name": "Celery workers",
        "status": _status(bool(workers)),
        "message": "Celery workers responded to ping." if workers else "No Celery workers responded to ping.",
        "details": {"workers": workers, "worker_count": len(workers)},
    }


def _check_celery_beat() -> dict:
    """A configured beat schedule is not proof that exactly one beat is running."""
    try:
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        keys = sorted(
            key.decode() if isinstance(key, bytes) else key
            for pattern in ("celery:beat:heartbeat*", "celery:beat:lease*", "celerybeat-heartbeat*")
            for key in client.scan_iter(match=pattern)
        )
        keys = sorted(set(keys))
    except Exception as exc:
        return {
            "name": "Celery beat scheduler",
            "status": "blocked",
            "message": "Celery beat heartbeat/lease is unavailable.",
            "details": {"evidence": [], "error_type": exc.__class__.__name__},
        }
    status = "ready" if len(keys) == 1 else "blocked"
    message = (
        "Exactly one Celery beat scheduler is evidenced by a Redis lease/heartbeat."
        if len(keys) == 1
        else "Exactly one Celery beat scheduler could not be evidenced by a Redis lease/heartbeat."
    )
    return {"name": "Celery beat scheduler", "status": status, "message": message, "details": {"evidence": keys, "count": len(keys)}}


def _check_live_trading_safety() -> dict:
    broker = broker_status()
    ok = bool(broker.get("paper_trading_enabled")) and bool(broker.get("live_trading_blocked")) and not settings.allow_live_trading
    return {
        "name": "Live trading safety",
        "status": _status(ok),
        "message": "Live trading is disabled and paper trading is the only allowed broker mode."
        if ok
        else "Live trading safety is not locked down.",
        "details": {"allow_live_trading": settings.allow_live_trading, "broker": broker},
    }


def deployment_monitor_snapshot(db: Session) -> dict:
    readiness = readiness_snapshot(db)
    checks = [
        _check_database(db),
        _check_redis(),
        _check_market_readiness(readiness),
        _check_celery_schedule(),
        _check_celery_workers(),
        _check_celery_beat(),
        _check_live_trading_safety(),
    ]
    blockers = [check["name"] for check in checks if check["status"] == "blocked"]
    warnings = [check["name"] for check in checks if check["status"] == "warning"]
    readiness_blockers = [check["name"] for check in readiness["checks"] if check["status"] == "blocked"]
    readiness_warnings = [check["name"] for check in readiness["checks"] if check["status"] == "warning"]

    deployable = not blockers and readiness["overall_status"] == "ready"
    return {
        "generated_at": datetime.utcnow(),
        "environment": settings.environment,
        "deployable": deployable,
        "status": "ready" if deployable else "blocked",
        "paper_trading_allowed": readiness["paper_trading_allowed"],
        "live_trading_allowed": False,
        "message": "Deployment checks are ready for paper-only operation."
        if deployable
        else "Deployment is not ready for trusted paper operation.",
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "readiness_status": readiness["overall_status"],
        "readiness_blockers": readiness_blockers,
        "readiness_warnings": readiness_warnings,
        "readiness": readiness,
    }


def run_deployment_monitor(db: Session, *, source: str = "manual") -> dict:
    snapshot = deployment_monitor_snapshot(db)
    snapshot_payload = jsonable_encoder(snapshot)
    blocker_text = ", ".join(snapshot["blockers"]) or "none"
    readiness_text = ", ".join(snapshot["readiness_blockers"]) or "none"
    status = "ready" if snapshot["deployable"] else "blocked"
    message = (
        "Deployment monitor is ready for paper-only operation."
        if snapshot["deployable"]
        else f"Deployment monitor blocked: {blocker_text}. Readiness blockers: {readiness_text}."
    )
    notification = (
        db.query(Notification)
        .filter(
            Notification.category == "deployment_monitor",
            Notification.source == "deployment_monitor",
            Notification.entity_type == "deployment",
            Notification.status != "resolved",
        )
        .order_by(Notification.created_at.desc())
        .first()
    )

    notification_action = "none"
    notification_id = notification.id if notification else None
    if snapshot["deployable"]:
        if notification:
            notification.status = "resolved"
            if not notification.acknowledged_at:
                notification.acknowledged_at = datetime.utcnow()
            notification.resolved_at = datetime.utcnow()
            notification.updated_at = datetime.utcnow()
            notification_action = "resolved"
            notification_id = notification.id
    elif notification:
        notification.severity = "critical"
        notification.title = "Deployment monitor blocked"
        notification.message = message
        notification.payload = {"source": source, "snapshot": snapshot_payload}
        notification.updated_at = datetime.utcnow()
        notification_action = "updated"
        notification_id = notification.id
    else:
        notification = create_notification(
            db,
            category="deployment_monitor",
            severity="critical",
            source="deployment_monitor",
            title="Deployment monitor blocked",
            message=message,
            entity_type="deployment",
            payload={"source": source, "snapshot": snapshot_payload},
        )
        notification_action = "created"
        db.flush()
        notification_id = notification.id

    write_audit_log(
        db,
        event_type="deployment_monitor",
        action=source,
        status=status,
        message=message,
        entity_type="deployment",
        entity_id=notification_id,
        payload={
            "snapshot": snapshot_payload,
            "notification_action": notification_action,
            "notification_id": notification_id,
        },
    )
    db.commit()
    if notification and notification_id is None:
        db.refresh(notification)
        notification_id = notification.id
    return {
        "status": status,
        "source": source,
        "deployable": snapshot["deployable"],
        "blockers": snapshot["blockers"],
        "readiness_status": snapshot["readiness_status"],
        "readiness_blockers": snapshot["readiness_blockers"],
        "notification_action": notification_action,
        "notification_id": notification_id,
        "snapshot": snapshot,
    }
