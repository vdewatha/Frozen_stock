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
from app.tasks.celery_app import (
    GENERAL_WORKER_QUEUES,
    INTRADAY_MARKET_DATA_QUEUE,
    RESEARCH_MARKET_DATA_QUEUE,
    SCALP_RESEARCH_QUEUE,
    celery_app,
)

REQUIRED_REGISTERED_TASKS = frozenset(
    {
        "app.tasks.jobs.strategy_learning_scope_job",
        "app.tasks.jobs.retry_failed_strategy_learning_scopes_job",
        "app.tasks.jobs.intraday_market_data_import",
        "app.tasks.jobs.model_realization_scoring_job",
        "app.tasks.jobs.paper_trading_signal_job",
        "app.tasks.jobs.scalp_research_job",
    }
)


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
        "model-realization-scoring",
        "deployment-monitor-job",
        "strategy-learning-batch-job",
        "strategy-learning-failure-retry",
        "paper-trading-signal-job",
        "paper-trade-reconciliation-job",
        "stock-paper-broker-reconciliation-job",
        "risk-monitor-job",
        "intraday-market-data-import",
        "stock-training-recovery-job",
        "scheduled-stock-challenger-retraining",
        "scheduled-stock-paper-trial-handoff",
        "scheduled-stock-paper-promotion",
        "stock-forward-trial-observe",
        "stock-forward-trial-reconcile",
    }
    direct_monitor = os.environ.get("PAPER_DIRECT_MONITOR_LOOP", "false").strip().lower() == "true"
    if not direct_monitor:
        required_jobs.add("stock-monitoring-job")
    missing = sorted(required_jobs - set(job_names))
    return {
        "name": "Scheduled job definitions",
        "status": _status(not missing),
        "message": "Required scheduled jobs are defined." if not missing else "Required scheduled job definitions are missing.",
        "details": {
            "configured_jobs": job_names,
            "missing_required_jobs": missing,
            "monitoring_mode": "direct_supervised_loop" if direct_monitor else "celery_beat_task",
            "direct_monitor_loop_enabled": direct_monitor,
        },
    }


def _check_celery_workers() -> dict:
    """Require ping and queue-subscription evidence, never configuration alone."""
    try:
        inspector = celery_app.control.inspect(timeout=2)
        responses = inspector.ping() or {}
        workers = sorted(
            name for name, response in responses.items()
            if isinstance(response, dict) and response.get("ok") == "pong"
        )
        active_queues = inspector.active_queues() or {}
        registered_responses = inspector.registered() or {}
    except Exception as exc:
        return {
            "name": "Celery workers",
            "status": "blocked",
            "message": "Celery worker or queue inspection failed; worker availability is unconfirmed.",
            "details": {
                "workers": [],
                "worker_count": 0,
                "intraday_workers": [],
                "intraday_worker_count": 0,
                "dedicated_intraday_workers": [],
                "dedicated_intraday_worker_count": 0,
                "general_workers": [],
                "general_worker_count": 0,
                "research_workers": [],
                "research_worker_count": 0,
                "dedicated_research_workers": [],
                "dedicated_research_worker_count": 0,
                "scalp_research_workers": [],
                "scalp_research_worker_count": 0,
                "dedicated_scalp_research_workers": [],
                "dedicated_scalp_research_worker_count": 0,
                "worker_queues": {},
                "missing_general_queues": sorted(GENERAL_WORKER_QUEUES),
                "required_research_queue": RESEARCH_MARKET_DATA_QUEUE,
                "missing_research_queue": True,
                "required_scalp_research_queue": SCALP_RESEARCH_QUEUE,
                "missing_scalp_research_queue": True,
                "registered_tasks": {},
                "missing_registered_tasks": sorted(REQUIRED_REGISTERED_TASKS),
                "error_type": exc.__class__.__name__,
            },
        }

    worker_queues = {}
    for worker in workers:
        queue_names = {
            entry.get("name")
            for entry in (active_queues.get(worker) or [])
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
        worker_queues[worker] = sorted(queue_names)

    intraday_workers = sorted(
        worker
        for worker, queues in worker_queues.items()
        if INTRADAY_MARKET_DATA_QUEUE in queues
    )
    dedicated_intraday_workers = sorted(
        worker
        for worker, queues in worker_queues.items()
        if queues == [INTRADAY_MARKET_DATA_QUEUE]
    )
    research_workers = sorted(
        worker
        for worker, queues in worker_queues.items()
        if RESEARCH_MARKET_DATA_QUEUE in queues
    )
    dedicated_research_workers = sorted(
        worker
        for worker, queues in worker_queues.items()
        if queues == [RESEARCH_MARKET_DATA_QUEUE]
    )
    scalp_research_workers = sorted(
        worker
        for worker, queues in worker_queues.items()
        if SCALP_RESEARCH_QUEUE in queues
    )
    dedicated_scalp_research_workers = sorted(
        worker
        for worker, queues in worker_queues.items()
        if queues == [SCALP_RESEARCH_QUEUE]
    )
    general_workers = sorted(
        worker
        for worker, queues in worker_queues.items()
        if GENERAL_WORKER_QUEUES.intersection(queues)
    )
    served_queues = {queue for queues in worker_queues.values() for queue in queues}
    missing_general_queues = sorted(GENERAL_WORKER_QUEUES - served_queues)
    missing_research_queue = not dedicated_research_workers
    missing_scalp_research_queue = not dedicated_scalp_research_workers
    registered_tasks = {
        worker: sorted(set(tasks or []))
        for worker, tasks in registered_responses.items()
        if isinstance(tasks, (list, tuple, set))
    } if isinstance(registered_responses, dict) else {}
    registered_union = {
        task
        for tasks in registered_tasks.values()
        for task in tasks
    }
    registration_evidence_available = isinstance(registered_responses, dict) and bool(registered_responses)
    missing_registered_tasks = sorted(
        REQUIRED_REGISTERED_TASKS - registered_union
    ) if registration_evidence_available else []
    if not dedicated_intraday_workers:
        message = (
            "No dedicated Celery worker is listening exclusively to the intraday "
            "market-data queue; "
            "one-minute market polling is blocked."
        )
    elif missing_research_queue:
        message = (
            "No dedicated Celery worker is listening exclusively to the research "
            "market-data queue; delayed observations are blocked."
        )
    elif missing_scalp_research_queue:
        message = (
            "No dedicated Celery worker is listening exclusively to the scalp "
            "research queue; scalp research is blocked."
        )
    elif missing_general_queues:
        message = (
            "Required Celery queues have no responding consumer: "
            + ", ".join(missing_general_queues)
        )
    elif missing_registered_tasks:
        message = (
            "Required Celery task registrations are missing: "
            + ", ".join(missing_registered_tasks)
        )
    else:
        message = "Dedicated intraday and general Celery workers are listening to their queues."

    return {
        "name": "Celery workers",
        "status": _status(
            bool(workers)
            and bool(dedicated_intraday_workers)
            and not missing_research_queue
            and not missing_scalp_research_queue
            and not missing_general_queues
            and not missing_registered_tasks
        ),
        "message": message if workers else "No Celery workers responded to ping.",
        "details": {
            "workers": workers,
            "worker_count": len(workers),
            "intraday_workers": intraday_workers,
            "intraday_worker_count": len(intraday_workers),
            "dedicated_intraday_workers": dedicated_intraday_workers,
            "dedicated_intraday_worker_count": len(dedicated_intraday_workers),
            "general_workers": general_workers,
            "general_worker_count": len(general_workers),
            "research_workers": research_workers,
            "research_worker_count": len(research_workers),
            "dedicated_research_workers": dedicated_research_workers,
            "dedicated_research_worker_count": len(dedicated_research_workers),
            "scalp_research_workers": scalp_research_workers,
            "scalp_research_worker_count": len(scalp_research_workers),
            "dedicated_scalp_research_workers": dedicated_scalp_research_workers,
            "dedicated_scalp_research_worker_count": len(dedicated_scalp_research_workers),
            "worker_queues": worker_queues,
            "required_intraday_queue": INTRADAY_MARKET_DATA_QUEUE,
            "required_general_queues": sorted(GENERAL_WORKER_QUEUES),
            "missing_general_queues": missing_general_queues,
            "required_research_queue": RESEARCH_MARKET_DATA_QUEUE,
            "missing_research_queue": missing_research_queue,
            "required_scalp_research_queue": SCALP_RESEARCH_QUEUE,
            "missing_scalp_research_queue": missing_scalp_research_queue,
            "registered_tasks": registered_tasks,
            "missing_registered_tasks": missing_registered_tasks,
        },
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

    # Readiness warnings can be explicitly acceptable for paper operation
    # (for example, incomplete broker cost metadata).  They must not turn a
    # paper-safe deployment into a critical incident when no blocking check
    # exists.  `paper_trading_allowed` already excludes unexpected warnings
    # and every blocked check, while live safety remains independently locked.
    deployable = not blockers and readiness["paper_trading_allowed"]
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
