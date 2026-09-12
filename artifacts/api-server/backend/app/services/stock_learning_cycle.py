"""Durable coordinator for the governed stock learning cycle.

This service is intentionally orchestration-only.  Dataset creation, training,
holdout consumption, forward evaluation, lifecycle transitions, monitoring, and
recovery remain owned by their existing services.  A cycle records the links
and decisions between those systems without becoming a second model registry.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from typing import Any, Iterable

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.models import (
    StockDatasetSnapshot,
    StockLearningCycle,
    StockLearningCycleEvent,
    StockModelRegistry,
    StockPaperBindingState,
    StockPaperModelBinding,
    StockPaperPromotionReadinessReport,
    StockPaperRecoveryEvent,
    StockPaperRecoveryState,
    StockPaperTrial,
    StockMonitoringSnapshot,
    StockTrainingJob,
)
from app.services.intraday_data import NY, feed_status, session_bounds
from app.services.readiness import _scheduler_health
from app.services.stock_forward_trial import trial_feed_preflight
from app.services.stock_promotion_readiness import evaluate_promotion_readiness
from app.services.stock_training_jobs import (
    StockTrainingError,
    create_stock_training_job,
    transition_stock_model_lifecycle,
)

TERMINAL_STATUSES = {"complete", "demoted", "rolled_back", "failed"}
VALID_TRIGGERS = {"manual", "scheduled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _gate(status: str, *, reason: str | None = None, evidence: Any = None) -> dict:
    result: dict[str, Any] = {"status": status}
    if reason:
        result["reason"] = reason
    if evidence is not None:
        result["evidence"] = _safe(evidence)
    return result


def _readiness_history_gate(db: Session) -> dict:
    """Require the immutable promotion-readiness table and its write fence."""
    try:
        inspector = inspect(db.get_bind())
        if "stock_paper_promotion_readiness_reports" not in inspector.get_table_names():
            return _gate("unknown", reason="promotion-readiness history table is unavailable")
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            found = db.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'stock_paper_promotion_readiness_reports_immutable_%'"
            )).scalar()
        elif dialect == "postgresql":
            found = db.execute(text(
                "SELECT 1 FROM pg_trigger WHERE tgrelid = "
                "to_regclass('stock_paper_promotion_readiness_reports') "
                "AND tgname = 'stock_paper_promotion_readiness_reports_immutable'"
            )).scalar()
        else:
            found = None
        return (
            _gate("pass", evidence={"table": True, "immutable_fence": bool(found)})
            if found
            else _gate("unknown", reason="promotion-readiness history immutability is not verified")
        )
    except Exception as exc:
        return _gate("unknown", reason=f"promotion-readiness history protection unavailable: {exc.__class__.__name__}")


def evaluate_cycle_prerequisites(
    db: Session,
    *,
    symbols: Iterable[str],
    provider: str,
    now: datetime | None = None,
) -> dict[str, dict]:
    """Evaluate all gates required before a dataset or training job exists."""
    observed_at = now or _now()
    normalized = [str(symbol).strip().upper() for symbol in symbols]
    bounds = session_bounds(observed_at.astimezone(NY).date())
    in_session = bool(bounds and bounds[0] <= observed_at < bounds[1])
    feed_results: list[dict] = []
    for symbol in normalized:
        try:
            result = feed_status(db, symbol, now=observed_at)
            feed_results.append(_safe({
                "symbol": symbol,
                "status": result.get("status"),
                "entitlement_state": result.get("entitlement_state"),
                "exchange_timestamp": result.get("exchange_timestamp"),
                "ingestion_timestamp": result.get("ingestion_timestamp"),
                "missing_intervals": result.get("missing_intervals", []),
                "unavailable_reason": result.get("unavailable_reason"),
            }))
        except Exception as exc:
            feed_results.append({
                "symbol": symbol,
                "status": "unknown",
                "unavailable_reason": f"feed status unavailable: {exc.__class__.__name__}",
            })
    if not normalized:
        feed_gate = _gate("fail", reason="at least one stock symbol is required")
    elif not in_session:
        feed_gate = _gate("unknown", reason="regular-session verified feed preflight is required",
                          evidence={"regular_session": False, "symbols": feed_results})
    elif all(row.get("status") == "ready" and row.get("entitlement_state") == "verified" for row in feed_results):
        feed_gate = _gate("pass", evidence={"regular_session": True, "symbols": feed_results})
    else:
        feed_gate = _gate("fail", reason="feed entitlement, freshness, or session completeness is unavailable",
                          evidence={"regular_session": True, "symbols": feed_results})

    try:
        scheduler = _scheduler_health()
        scheduler_gate = _gate(
            "pass" if scheduler.get("healthy") and not scheduler.get("recent_unresolved_failures") else "fail",
            reason=None if scheduler.get("healthy") and not scheduler.get("recent_unresolved_failures")
            else "worker and exactly one scheduler heartbeat are required",
            evidence=scheduler,
        )
    except Exception as exc:
        scheduler_gate = _gate("unknown", reason=f"scheduler health unavailable: {exc.__class__.__name__}")

    from app.models.stock_paper import StockPaperAccount
    account = db.query(StockPaperAccount).filter_by(broker="alpaca_paper").one_or_none()
    ledger_ready = bool(
        account and account.status == "reconciled"
        and not account.reconciliation_required and account.accounting_verified
    )
    ledger_gate = _gate(
        "pass" if ledger_ready else "fail",
        reason=None if ledger_ready else "paper ledger reconciliation is unavailable",
        evidence={
            "account_status": account.status if account else "uninitialized",
            "reconciliation_required": account.reconciliation_required if account else None,
            "accounting_verified": account.accounting_verified if account else False,
        },
    )
    provider_gate = _gate(
        "pass" if provider in {"yfinance", "yahoo_chart"} else "fail",
        reason=None if provider in {"yfinance", "yahoo_chart"} else "provider provenance is not verified",
        evidence={"provider": provider},
    )
    protection_gate = _readiness_history_gate(db)
    return {
        "verified_feed": feed_gate,
        "scheduler_health": scheduler_gate,
        "paper_ledger": ledger_gate,
        "dataset_provenance": provider_gate,
        "readiness_history": protection_gate,
    }


def _all_pass(gates: dict[str, dict]) -> bool:
    return bool(gates) and all(item.get("status") == "pass" for item in gates.values())


def _event(
    db: Session,
    *,
    cycle: StockLearningCycle,
    stage: str,
    decision: str,
    actor: str,
    reason: str,
    evidence: dict | None = None,
) -> StockLearningCycleEvent:
    payload = _safe(evidence or {})
    identity = {
        "cycle_id": cycle.cycle_id,
        "stage": stage,
        "decision": decision,
        "actor": actor,
        "reason": reason.strip(),
        "evidence": payload,
    }
    digest = _digest(identity)
    existing = db.scalar(select(StockLearningCycleEvent).where(
        StockLearningCycleEvent.decision_sha256 == digest
    ))
    if existing:
        return existing
    row = StockLearningCycleEvent(
        cycle_id=cycle.cycle_id, stage=stage, decision=decision,
        actor=actor, reason=reason.strip(), decision_sha256=digest, evidence=payload,
    )
    db.add(row)
    db.flush()
    return row


def _projection_event(event: StockLearningCycleEvent) -> dict:
    return {
        "id": event.id, "stage": event.stage, "decision": event.decision,
        "actor": event.actor, "reason": event.reason,
        "decision_sha256": event.decision_sha256, "evidence": event.evidence,
        "created_at": event.created_at,
    }


def create_learning_cycle(
    db: Session,
    *,
    symbols: Iterable[str],
    cutoff_at: date,
    horizon_days: int,
    provider: str,
    actor: str,
    trigger: str = "manual",
    seed: int = 42,
) -> tuple[StockLearningCycle, bool]:
    normalized = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    if trigger not in VALID_TRIGGERS:
        raise StockTrainingError("Only manual or scheduled learning cycles are allowed")
    if not normalized:
        raise StockTrainingError("At least one stock symbol is required")
    request = {
        "symbols": normalized, "cutoff_date": cutoff_at.isoformat(),
        "horizon_days": horizon_days, "provider": provider,
        "trigger": trigger, "seed": seed,
    }
    request_sha256 = _digest(request)
    existing = db.scalar(select(StockLearningCycle).where(
        StockLearningCycle.request_sha256 == request_sha256
    ))
    if existing:
        return existing, True
    cycle = StockLearningCycle(
        cycle_id=request_sha256, request_sha256=request_sha256, trigger=trigger,
        status="blocked", stage="preflight", requested_by=actor, symbols=normalized,
        cutoff_date=cutoff_at, horizon_days=horizon_days, provider=provider, seed=seed,
        gates={}, evidence={"request": request}, last_reason="Preflight has not passed",
    )
    db.add(cycle)
    db.flush()
    gates = evaluate_cycle_prerequisites(db, symbols=normalized, provider=provider)
    cycle.gates = gates
    cycle.evidence = {"request": request, "preflight_at": _now().isoformat()}
    if not _all_pass(gates):
        deferred = trigger == "scheduled" and any(
            gate.get("status") == "unknown" for gate in gates.values()
        )
        cycle.status = "deferred" if deferred else "blocked"
        cycle.stage = "preflight"
        cycle.last_reason = "Scheduled cycle deferred pending prerequisite evidence" if deferred else next(
            (gate.get("reason") for gate in gates.values() if gate.get("status") != "pass"),
            "Required prerequisite gate is unavailable",
        )
        _event(
            db, cycle=cycle, stage="preflight",
            decision="deferred" if deferred else "blocked", actor=actor,
            reason=cycle.last_reason, evidence={"gates": gates},
        )
        return cycle, False
    _event(db, cycle=cycle, stage="preflight", decision="pass", actor=actor,
           reason="Verified feed, scheduler, paper ledger, provenance, and readiness-history gates passed",
           evidence={"gates": gates})
    try:
        job, duplicate = create_stock_training_job(
            db, symbols=normalized, horizon_bars=horizon_days, actor=actor,
            cutoff_at=cutoff_at, provider=provider, trigger=trigger, seed=seed,
        )
    except StockTrainingError as exc:
        cycle.status = "deferred" if trigger == "scheduled" else "blocked"
        cycle.last_reason = str(exc)
        _event(db, cycle=cycle, stage="dataset", decision="deferred" if trigger == "scheduled" else "blocked",
               actor=actor, reason=str(exc), evidence={"gates": gates})
        return cycle, False
    cycle.snapshot_id = job.snapshot_id
    cycle.training_job_id = job.id
    cycle.stage = "training"
    cycle.status = "deferred" if job.status == "deferred" else "queued"
    cycle.last_reason = job.queue_error or ("Existing deduplicated training intent" if duplicate else "Bounded challenger training queued")
    _event(db, cycle=cycle, stage="dataset", decision="pass", actor=actor,
           reason="Immutable verified dataset snapshot admitted", evidence={"snapshot_id": job.snapshot_id})
    _event(db, cycle=cycle, stage="training",
           decision="deferred" if job.status == "deferred" else "pass", actor=actor,
           reason=cycle.last_reason, evidence={"training_job_id": job.id, "deduplicated": duplicate})
    return cycle, duplicate


def sync_cycle_from_training_job(db: Session, job_id: str, *, actor: str = "training_worker") -> StockLearningCycle | None:
    cycle = db.scalar(select(StockLearningCycle).where(StockLearningCycle.training_job_id == job_id))
    if not cycle:
        return None
    job = db.get(StockTrainingJob, job_id)
    if not job:
        return cycle
    if job.status in {"queued", "running", "cancel_requested"}:
        cycle.status = "running" if job.status == "running" else "queued"
        cycle.stage = "training"
        return cycle
    if job.status != "succeeded" or not job.result_run_id:
        cycle.status = "failed" if job.status == "failed" else "blocked"
        cycle.stage = "training"
        cycle.last_reason = job.failure_detail or f"Training ended with status {job.status}"
        _event(db, cycle=cycle, stage="training", decision="fail", actor=actor,
               reason=cycle.last_reason, evidence={"job_id": job.id, "status": job.status})
        return cycle
    model = db.get(StockModelRegistry, job.result_run_id)
    if not model:
        cycle.status, cycle.stage, cycle.last_reason = "blocked", "validation", "Training completed without a model registry record"
        _event(db, cycle=cycle, stage="validation", decision="unknown", actor=actor,
               reason=cycle.last_reason, evidence={"job_id": job.id})
        return cycle
    manifest = model.training_metadata or {}
    validation_ok = bool(
        manifest.get("purged_expanding_walkforward")
        and int(manifest.get("final_holdout_evaluation_count", 0)) == 1
        and manifest.get("final_holdout_consumption")
        and int(manifest.get("embargo_days", -1)) >= 0
    )
    cycle.model_run_id = model.run_id
    cycle.stage = "forward_trial"
    cycle.status = "awaiting_forward_evidence"
    cycle.last_reason = "Awaiting a completed, aligned forward paper trial"
    _event(
        db, cycle=cycle, stage="validation",
        decision="pass" if validation_ok else "fail", actor=actor,
        reason="Purged walk-forward, embargo, and one-use holdout evidence verified"
        if validation_ok else "Registered model is missing leakage-safe validation or holdout proof",
        evidence={
            "model_run_id": model.run_id, "manifest_sha256": model.manifest_sha256,
            "purged_walkforward": bool(manifest.get("purged_expanding_walkforward")),
            "embargo_days": manifest.get("embargo_days"),
            "holdout_evaluation_count": manifest.get("final_holdout_evaluation_count"),
            "holdout_consumption": manifest.get("final_holdout_consumption"),
        },
    )
    if not validation_ok:
        cycle.status = "blocked"
        cycle.last_reason = "Leakage-safe validation and exactly-once holdout evidence are incomplete"
    return cycle


def review_learning_cycle(
    db: Session,
    cycle_id: str,
    *,
    actor: str,
    reason: str,
    trial_id: str | None = None,
) -> StockLearningCycle:
    cycle = db.get(StockLearningCycle, cycle_id)
    if not cycle:
        raise StockTrainingError("Learning cycle not found")
    if not reason.strip():
        raise StockTrainingError("A cycle review reason is required")
    if trial_id:
        trial = db.get(StockPaperTrial, trial_id)
        if not trial or trial.lineage.get("model_run_id") != cycle.model_run_id:
            raise StockTrainingError("Forward trial is not immutably linked to this cycle model")
        cycle.trial_id = trial_id
    model = db.get(StockModelRegistry, cycle.model_run_id) if cycle.model_run_id else None
    job = db.get(StockTrainingJob, cycle.training_job_id) if cycle.training_job_id else None
    validation = bool(
        model and job and job.status == "succeeded"
        and model.training_metadata.get("purged_expanding_walkforward")
        and model.training_metadata.get("final_holdout_consumption")
        and model.training_metadata.get("final_holdout_evaluation_count") == 1
    )
    forward: dict
    if cycle.trial_id:
        forward = evaluate_promotion_readiness(db, cycle.trial_id)
        forward_status = forward.get("decision", "unknown")
    else:
        forward = {"decision": "unknown", "reason": "No completed forward paper trial is linked to this cycle"}
        forward_status = "unknown"
    gates = {
        "validation": _gate("pass" if validation else "fail", reason=None if validation else "registered validation evidence is incomplete"),
        "forward_trial": _gate("pass" if forward_status == "pass" else "fail" if forward_status == "fail" else "unknown", evidence=forward),
        "paper_only": _gate("pass", evidence={"live_authorized": False}),
    }
    cycle.gates = gates
    cycle.evidence = {
        **(cycle.evidence or {}),
        "operator_review": {"actor": actor, "reason": reason.strip(), "reviewed_at": _now().isoformat()},
        "forward_evidence": forward,
    }
    cycle.stage = "operator_review"
    cycle.status = "operator_review" if all(gate["status"] == "pass" for gate in gates.values()) else "blocked"
    cycle.last_reason = (
        "All qualification evidence passed; explicit lifecycle action is still required"
        if cycle.status == "operator_review"
        else next(gate.get("reason", "Required evidence is incomplete") for gate in gates.values() if gate["status"] != "pass")
    )
    _event(db, cycle=cycle, stage="qualification",
           decision="pass" if cycle.status == "operator_review" else "blocked",
           actor=actor, reason=cycle.last_reason, evidence={"gates": gates})
    return cycle


def cycle_action(
    db: Session,
    cycle_id: str,
    *,
    action: str,
    actor: str,
    reason: str,
) -> StockLearningCycle:
    cycle = db.get(StockLearningCycle, cycle_id)
    if not cycle:
        raise StockTrainingError("Learning cycle not found")
    if action not in {"mark_eligible", "start_canary", "promote", "demote", "retire"}:
        raise StockTrainingError("Unsupported learning-cycle action")
    if action in {"mark_eligible", "start_canary", "promote"} and cycle.status != "operator_review":
        raise StockTrainingError("Cycle evidence must pass operator review before this action")
    if not cycle.model_run_id:
        raise StockTrainingError("Cycle has no registered model")
    transition_stock_model_lifecycle(
        db, model_run_id=cycle.model_run_id, action=action, actor=actor, reason=reason,
    )
    state = db.scalar(select(StockModelRegistry.lifecycle_state).where(
        StockModelRegistry.run_id == cycle.model_run_id
    ))
    cycle.active_binding_id = db.scalar(select(StockPaperBindingState.active_binding_id).where(
        StockPaperBindingState.id == 1
    ))
    cycle.status = "complete" if state == "champion" else "demoted" if state == "demoted" else cycle.status
    cycle.stage = "promotion" if action in {"mark_eligible", "start_canary", "promote"} else "recovery"
    cycle.last_reason = reason.strip()
    _event(db, cycle=cycle, stage=cycle.stage, decision="complete", actor=actor,
           reason=reason, evidence={"action": action, "lifecycle_state": state, "active_binding_id": cycle.active_binding_id})
    return cycle


def cycle_projection(db: Session, cycle: StockLearningCycle) -> dict:
    binding_state = db.get(StockPaperBindingState, 1)
    active_binding = db.get(StockPaperModelBinding, binding_state.active_binding_id) if binding_state else None
    monitor = db.get(StockMonitoringSnapshot, cycle.monitor_snapshot_id) if cycle.monitor_snapshot_id else None
    if monitor is None:
        monitor = db.query(StockMonitoringSnapshot).order_by(StockMonitoringSnapshot.generated_at.desc()).first()
    recovery = db.get(StockPaperRecoveryState, 1)
    recovery_event = db.query(StockPaperRecoveryEvent).order_by(StockPaperRecoveryEvent.created_at.desc()).first()
    return {
        "cycle_id": cycle.cycle_id, "status": cycle.status, "stage": cycle.stage,
        "trigger": cycle.trigger, "requested_by": cycle.requested_by,
        "symbols": cycle.symbols, "cutoff_date": cycle.cutoff_date,
        "horizon_days": cycle.horizon_days, "provider": cycle.provider,
        "snapshot_id": cycle.snapshot_id, "training_job_id": cycle.training_job_id,
        "model_run_id": cycle.model_run_id, "trial_id": cycle.trial_id,
        "active_binding_id": active_binding.id if active_binding else cycle.active_binding_id,
        "active_binding_model_run_id": active_binding.model_run_id if active_binding else None,
        "gates": cycle.gates, "evidence": cycle.evidence,
        "last_reason": cycle.last_reason, "paper_only": True, "live_authorized": False,
        "monitoring": {
            "snapshot_id": monitor.id if monitor else None,
            "status": monitor.status if monitor else "unknown",
            "generated_at": monitor.generated_at if monitor else None,
            "actions": monitor.actions if monitor else [],
        },
        "recovery": {
            "status": recovery.status if recovery else "unknown",
            "last_known_good_model_run_id": recovery.last_known_good_model_run_id if recovery else None,
            "last_known_good_binding_id": recovery.last_known_good_binding_id if recovery else None,
            "latest_event_id": recovery_event.id if recovery_event else None,
            "latest_event_action": recovery_event.action if recovery_event else None,
        },
        "events": [_projection_event(event) for event in db.scalars(
            select(StockLearningCycleEvent).where(StockLearningCycleEvent.cycle_id == cycle.cycle_id)
            .order_by(StockLearningCycleEvent.id)
        ).all()],
        "created_at": cycle.created_at,
        "updated_at": cycle.updated_at,
    }


def sync_cycle_observability(
    db: Session,
    *,
    monitor_snapshot_id: int | None = None,
    recovery_event_id: int | None = None,
    actor: str = "monitor",
) -> list[str]:
    """Durably attach monitoring/recovery evidence to affected active cycles."""
    monitor = db.get(StockMonitoringSnapshot, monitor_snapshot_id) if monitor_snapshot_id else None
    recovery_event = db.get(StockPaperRecoveryEvent, recovery_event_id) if recovery_event_id else None
    binding_state = db.get(StockPaperBindingState, 1)
    active_binding = db.get(StockPaperModelBinding, binding_state.active_binding_id) if binding_state else None
    model_run_id = active_binding.model_run_id if active_binding else None
    if not model_run_id and monitor:
        for action in monitor.actions or []:
            if action.get("model_run_id"):
                model_run_id = action["model_run_id"]
                break
    if not model_run_id and not recovery_event:
        return []
    statement = select(StockLearningCycle)
    if model_run_id:
        statement = statement.where(StockLearningCycle.model_run_id == model_run_id)
    else:
        statement = statement.where(StockLearningCycle.status.not_in(TERMINAL_STATUSES))
    cycles = db.scalars(statement).all()
    affected: list[str] = []
    for cycle in cycles:
        if monitor:
            cycle.monitor_snapshot_id = monitor.id
        if recovery_event:
            cycle.recovery_event_id = recovery_event.id
        if recovery_event and recovery_event.action in {"demote_model", "pause_stock_path", "flatten_positions"}:
            cycle.status = "demoted"
            cycle.stage = "recovery"
            cycle.last_reason = recovery_event.reason
            _event(
                db, cycle=cycle, stage="recovery", decision="complete", actor=actor,
                reason=recovery_event.reason,
                evidence={"monitor_snapshot_id": monitor.id if monitor else None, "recovery_event_id": recovery_event.id},
            )
        affected.append(cycle.cycle_id)
    return affected


def list_learning_cycles(db: Session, *, limit: int = 25) -> list[dict]:
    rows = db.scalars(select(StockLearningCycle).order_by(
        StockLearningCycle.created_at.desc()
    ).limit(limit)).all()
    return [cycle_projection(db, row) for row in rows]