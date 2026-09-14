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
    StockPaperAccount,
    StockDatasetSnapshot,
    StockLearningCycle,
    StockLearningCycleEvent,
    StockLearningScheduleControl,
    StockPaperPromotionDecision,
    StockModelRegistry,
    StockModelLifecycleState,
    StockPaperBindingState,
    StockPaperModelBinding,
    StockPaperPromotionReadinessReport,
    StockPaperRecoveryEvent,
    StockPaperRecoveryState,
    StockPaperTrial,
    StockMonitoringSnapshot,
    StockTrainingJob,
)
from app.services.audit import write_audit_log
from app.services.intraday_data import NY, feed_status, session_bounds
from app.services.readiness import _scheduler_health
from app.services.stock_forward_trial import trial_feed_preflight
from app.services.stock_promotion_readiness import evaluate_promotion_readiness
from app.services.stock_training_jobs import (
    StockTrainingError,
    _current_lifecycle,
    _lock_lifecycle_admission,
    _transition_model,
    create_stock_paper_binding,
    create_stock_training_job,
    transition_stock_model_lifecycle,
)
from app.services.stock_forward_trial import create_trial, start_trial, validate_trial_artifact

TERMINAL_STATUSES = {"complete", "promoted", "demoted", "rolled_back", "failed"}
VALID_TRIGGERS = {"manual", "scheduled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


SCHEDULE_CONTROL_ID = 1
SCHEDULE_PAUSE_ACTOR = "scheduled_learning_control"
SCHEDULE_PAUSED_REASON = "Scheduled paper learning is paused by operator"


def scheduled_learning_control_projection(db: Session) -> dict:
    control = db.get(StockLearningScheduleControl, SCHEDULE_CONTROL_ID)
    return {
        "paused": bool(control.paused) if control else False,
        "pause_reason": control.pause_reason if control and control.paused else None,
        "updated_by": control.updated_by if control else "system",
        "updated_at": control.updated_at if control else None,
        "paper_only": True,
        "live_authorized": False,
        "recovery_independent": True,
    }


def set_scheduled_learning_control(
    db: Session,
    *,
    paused: bool,
    actor: str,
    reason: str,
) -> StockLearningScheduleControl:
    reason = reason.strip()
    if len(reason) < 3:
        raise StockTrainingError("Scheduled learning control requires a reason")
    control = db.scalar(
        select(StockLearningScheduleControl)
        .where(StockLearningScheduleControl.id == SCHEDULE_CONTROL_ID)
        .with_for_update()
    )
    if control is None:
        control = StockLearningScheduleControl(id=SCHEDULE_CONTROL_ID)
        db.add(control)
        db.flush()
    control.paused = paused
    control.pause_reason = reason if paused else None
    control.updated_by = actor
    control.updated_at = _now()
    return control


def _scheduled_learning_pause_reason(db: Session) -> str | None:
    control = db.get(StockLearningScheduleControl, SCHEDULE_CONTROL_ID)
    if control and control.paused:
        return control.pause_reason or SCHEDULE_PAUSED_REASON
    return None


def _defer_scheduled_cycle_for_pause(
    db: Session,
    cycle: StockLearningCycle,
    *,
    reason: str,
    stage: str,
) -> None:
    if cycle.status in TERMINAL_STATUSES:
        return
    changed = cycle.status != "deferred" or cycle.stage != stage or cycle.last_reason != reason
    cycle.status = "deferred"
    cycle.stage = stage
    cycle.last_reason = reason
    if changed:
        _event(
            db,
            cycle=cycle,
            stage=stage,
            decision="deferred",
            actor=SCHEDULE_PAUSE_ACTOR,
            reason=reason,
            evidence={"scheduled_learning_paused": True},
        )


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
    cycle.stage = "admission"
    cycle.status = "awaiting_admission" if cycle.trigger == "scheduled" else "awaiting_forward_evidence"
    cycle.last_reason = (
        "Training completed; awaiting automatic paper-canary admission"
        if cycle.trigger == "scheduled"
        else "Awaiting a completed, aligned forward paper trial"
    )
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


HANDOFF_ACTOR = "paper_learning_automation"


def _handoff_failure(
    db: Session,
    cycle: StockLearningCycle,
    *,
    stage: str,
    status: str,
    reason: str,
    evidence: dict | None = None,
) -> StockLearningCycle:
    attempt_evidence = {
        **(evidence or {}),
        "attempted_at": _now().isoformat(),
    }
    cycle.stage = stage
    cycle.status = status
    cycle.last_reason = reason.strip()
    _event(
        db,
        cycle=cycle,
        stage=stage,
        decision="deferred" if status == "deferred" else "blocked",
        actor=HANDOFF_ACTOR,
        reason=cycle.last_reason,
        evidence=attempt_evidence,
    )
    write_audit_log(
        db,
        event_type="stock_learning_cycle",
        action="automatic_paper_trial_handoff",
        status=status,
        message=cycle.last_reason,
        entity_type="stock_learning_cycle",
        payload={
            "cycle_id": cycle.cycle_id,
            "binding_id": cycle.binding_id,
            "trial_id": cycle.trial_id,
            "paper_only": True,
            "live_authorized": False,
            **attempt_evidence,
        },
    )
    return cycle


def admit_scheduled_learning_cycle(
    db: Session,
    cycle_id: str,
    *,
    actor: str = HANDOFF_ACTOR,
) -> StockLearningCycle:
    """Admit one completed scheduled challenger into exactly one paper trial.

    The cycle-owned source keys on the binding and trial are the durable
    idempotency fence. Lifecycle transitions remain delegated to the existing
    binding service; this coordinator never mutates the immutable registry row.
    """
    cycle = db.get(StockLearningCycle, cycle_id)
    if not cycle:
        raise StockTrainingError("Learning cycle not found")
    if cycle.trigger != "scheduled":
        raise StockTrainingError("Automatic paper admission is limited to scheduled cycles")
    pause_reason = _scheduled_learning_pause_reason(db)
    if pause_reason:
        _defer_scheduled_cycle_for_pause(
            db, cycle, reason=pause_reason, stage="admission",
        )
        return cycle
    if cycle.binding_id and cycle.trial_id:
        return cycle
    if cycle.status in {"complete", "promoted", "demoted", "rolled_back", "failed"}:
        return cycle

    job = db.get(StockTrainingJob, cycle.training_job_id) if cycle.training_job_id else None
    if not job or job.status != "succeeded" or not job.result_run_id:
        return _handoff_failure(
            db, cycle, stage="admission", status="deferred",
            reason="Scheduled challenger is not yet a successfully completed training job",
            evidence={"training_job_id": cycle.training_job_id, "job_status": job.status if job else None},
        )
    if cycle.model_run_id != job.result_run_id:
        cycle.model_run_id = job.result_run_id
    validation = _automatic_validation_gate(db, cycle)
    cycle.gates = {**(cycle.gates or {}), "admission_validation": validation}
    if validation["status"] != "pass":
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked",
            reason=validation.get("reason", "Leakage-safe training validation is incomplete"),
            evidence={"validation": validation},
        )

    model = db.get(StockModelRegistry, cycle.model_run_id)
    snapshot = db.get(StockDatasetSnapshot, cycle.snapshot_id)
    if not model or not snapshot or model.snapshot_id != cycle.snapshot_id:
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked",
            reason="Completed challenger is missing its exact immutable model or dataset snapshot",
            evidence={"model_run_id": cycle.model_run_id, "snapshot_id": cycle.snapshot_id},
        )
    if snapshot.metadata_json.get("binding_eligible") is not True:
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked",
            reason=snapshot.metadata_json.get(
                "binding_eligibility_reason", "Dataset snapshot is not eligible for paper binding"
            ),
            evidence={"snapshot_id": snapshot.snapshot_id, "binding_eligible": False},
        )

    _lock_lifecycle_admission(db)
    binding = db.get(StockPaperModelBinding, cycle.binding_id) if cycle.binding_id else db.scalar(
        select(StockPaperModelBinding).where(
            StockPaperModelBinding.source_cycle_id == cycle.cycle_id,
        )
    )
    try:
        if binding is None:
            binding = create_stock_paper_binding(
                db,
                model_run_id=model.run_id,
                snapshot_id=snapshot.snapshot_id,
                actor=actor,
                purpose="scheduled paper trial",
                reason=f"Automatic admission for scheduled learning cycle {cycle.cycle_id}",
                source_cycle_id=cycle.cycle_id,
            )
        if (
            binding.model_run_id != model.run_id
            or binding.snapshot_id != snapshot.snapshot_id
            or binding.paper_only is not True
            or binding.live_authorized is not False
        ):
            raise StockTrainingError("Paper binding lineage or authorization policy is invalid")
        cycle.binding_id = binding.id
        cycle.active_binding_id = binding.id
        state = db.get(StockPaperBindingState, 1)
        if not state or state.active_binding_id != binding.id:
            raise StockTrainingError("Scheduled paper binding is not the active paper canary")
        lifecycle = db.get(StockModelLifecycleState, model.run_id)
        if not lifecycle or lifecycle.lifecycle_state != "paper_canary":
            raise StockTrainingError("Scheduled challenger did not reach the paper-canary lifecycle state")
        trial = db.get(StockPaperTrial, cycle.trial_id) if cycle.trial_id else db.scalar(
            select(StockPaperTrial).where(StockPaperTrial.source_cycle_id == cycle.cycle_id)
        )
        if trial is None:
            trial = create_trial(
                db, binding_id=binding.id, actor=actor, source_cycle_id=cycle.cycle_id,
            )
        if trial.binding_id != binding.id:
            raise StockTrainingError("Forward trial is linked to a different paper binding")
        cycle.trial_id = trial.id
    except StockTrainingError as exc:
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked", reason=str(exc),
            evidence={"model_run_id": model.run_id, "snapshot_id": snapshot.snapshot_id},
        )

    if trial.status == "blocked" and trial.blocked_reason:
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked",
            reason=trial.blocked_reason,
            evidence={"binding_id": binding.id, "trial_id": trial.id},
        )
    cycle.stage = "preflight"
    cycle.status = "awaiting_preflight"
    cycle.last_reason = "Paper canary and forward trial admitted; awaiting authenticated preflight"
    cycle.gates = {
        **(cycle.gates or {}),
        "paper_binding": _gate(
            "pass",
            evidence={
                "binding_id": binding.id,
                "model_run_id": binding.model_run_id,
                "snapshot_id": binding.snapshot_id,
                "paper_only": True,
                "live_authorized": False,
            },
        ),
        "forward_trial": _gate(
            "pass",
            evidence={"trial_id": trial.id, "binding_id": trial.binding_id, "policy": trial.policy},
        ),
    }
    cycle.evidence = {
        **(cycle.evidence or {}),
        "handoff": {
            "binding_id": binding.id,
            "trial_id": trial.id,
            "admitted_at": _now().isoformat(),
            "paper_only": True,
            "live_authorized": False,
        },
    }
    _event(
        db,
        cycle=cycle,
        stage="admission",
        decision="pass",
        actor=actor,
        reason=cycle.last_reason,
        evidence={"binding_id": binding.id, "trial_id": trial.id},
    )
    db.flush()
    return cycle


def start_scheduled_learning_trial(
    db: Session,
    cycle_id: str,
    *,
    actor: str = HANDOFF_ACTOR,
) -> StockLearningCycle:
    """Retry only the admitted trial's safe, authenticated start gates."""
    cycle = admit_scheduled_learning_cycle(db, cycle_id, actor=actor)
    if _scheduled_learning_pause_reason(db):
        return cycle
    if not cycle.binding_id or not cycle.trial_id:
        return cycle
    if cycle.status in {"complete", "promoted", "demoted", "rolled_back", "failed"}:
        return cycle
    trial = db.get(StockPaperTrial, cycle.trial_id)
    binding_state = db.get(StockPaperBindingState, 1)
    if not trial:
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked",
            reason="Scheduled cycle points to a missing forward trial",
        )
    if not binding_state or binding_state.active_binding_id != cycle.binding_id:
        return _handoff_failure(
            db, cycle, stage="preflight", status="blocked",
            reason="The cycle paper binding is no longer the active paper canary",
            evidence={"binding_id": cycle.binding_id},
        )
    if trial.status == "running":
        cycle.stage, cycle.status = "forward_trial", "running_forward_trial"
        cycle.last_reason = "Forward paper trial is running"
        return cycle
    if trial.status == "completed":
        cycle.stage, cycle.status = "promotion", "awaiting_forward_evidence"
        cycle.last_reason = "Forward paper trial completed; awaiting immutable readiness evidence"
        return cycle

    recovery = db.get(StockPaperRecoveryState, 1)
    if recovery and recovery.status != "armed":
        return _handoff_failure(
            db, cycle, stage="preflight", status="deferred",
            reason="Paper recovery is paused or awaiting operator revalidation",
            evidence={"recovery_status": recovery.status},
        )
    try:
        validate_trial_artifact(db, trial)
    except StockTrainingError as exc:
        return _handoff_failure(
            db, cycle, stage="preflight", status="blocked", reason=str(exc),
            evidence={"trial_id": trial.id},
        )
    preflight = trial_feed_preflight(db, trial, authenticated_probe=True)
    cycle.gates = {**(cycle.gates or {}), "preflight": _gate(
        "pass" if preflight.get("ready") else "unknown",
        reason=None if preflight.get("ready") else preflight.get("reason") or "Authenticated paper preflight is incomplete",
        evidence=preflight,
    )}
    cycle.evidence = {**(cycle.evidence or {}), "preflight": preflight}
    if not preflight.get("ready") or not preflight.get("regular_session"):
        reason = preflight.get("reason") or "Regular-session authenticated preflight is required"
        return _handoff_failure(
            db, cycle, stage="preflight", status="deferred",
            reason=reason, evidence={"trial_id": trial.id, "preflight": preflight},
        )
    started = start_trial(db, trial.id, actor=actor)
    if started.status == "running":
        cycle.stage, cycle.status = "forward_trial", "running_forward_trial"
        cycle.last_reason = "Authenticated paper preflight passed; forward trial started"
        _event(
            db, cycle=cycle, stage="preflight", decision="pass", actor=actor,
            reason=cycle.last_reason,
            evidence={"trial_id": trial.id, "preflight": preflight},
        )
    else:
        reason = started.blocked_reason or started.pause_reason or "Forward trial did not start"
        return _handoff_failure(
            db, cycle, stage="preflight", status="deferred",
            reason=reason, evidence={"trial_id": trial.id, "preflight": preflight},
        )
    db.flush()
    return cycle


def sync_cycle_from_trial(
    db: Session,
    trial_id: str,
    *,
    report_id: int | None = None,
    actor: str = "forward_trial_worker",
) -> StockLearningCycle | None:
    """Attach worker progress to the exact originating scheduled cycle."""
    cycle = db.scalar(select(StockLearningCycle).where(StockLearningCycle.trial_id == trial_id))
    trial = db.get(StockPaperTrial, trial_id)
    if not cycle or not trial:
        return cycle
    if trial.status == "running":
        cycle.stage, cycle.status = "forward_trial", "running_forward_trial"
        cycle.last_reason = "Forward paper trial is running"
    elif trial.status == "completed":
        cycle.stage, cycle.status = "promotion", "awaiting_forward_evidence"
        cycle.last_reason = "Forward paper trial completed; awaiting immutable readiness evidence"
    elif trial.status in {"blocked", "paused"}:
        cycle.stage, cycle.status = "preflight", "awaiting_preflight"
        cycle.last_reason = trial.blocked_reason or trial.pause_reason or "Forward trial is waiting for a safe retry"
    if report_id:
        cycle.evidence = {
            **(cycle.evidence or {}),
            "forward_report": {"trial_id": trial_id, "report_id": report_id},
        }
    _event(
        db, cycle=cycle, stage=cycle.stage, decision="pass" if trial.status in {"running", "completed"} else "deferred",
        actor=actor, reason=cycle.last_reason, evidence={"trial_id": trial_id, "report_id": report_id},
    )
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


AUTOMATIC_PROMOTION_ACTOR = "paper_learning_automation"
AUTOMATIC_PROMOTION_JOB = "scheduled_stock_paper_promotion_job"


def _automatic_validation_gate(db: Session, cycle: StockLearningCycle) -> dict:
    job = db.get(StockTrainingJob, cycle.training_job_id) if cycle.training_job_id else None
    model = db.get(StockModelRegistry, cycle.model_run_id) if cycle.model_run_id else None
    manifest = model.training_metadata if model else {}
    passed = bool(
        job and job.status == "succeeded" and job.result_run_id == cycle.model_run_id
        and model and model.snapshot_id == cycle.snapshot_id
        and bool(manifest.get("purged_expanding_walkforward"))
        and manifest.get("final_holdout_consumption")
        and manifest.get("final_holdout_evaluation_count") == 1
        and int(manifest.get("embargo_days", -1)) >= 0
    )
    return _gate(
        "pass" if passed else "fail",
        reason=None if passed else "completed model is missing exact leakage-safe validation or one-use holdout proof",
        evidence={
            "job_id": job.id if job else None,
            "job_status": job.status if job else None,
            "model_run_id": model.run_id if model else None,
            "snapshot_id": model.snapshot_id if model else None,
            "purged_expanding_walkforward": manifest.get("purged_expanding_walkforward"),
            "embargo_days": manifest.get("embargo_days"),
            "final_holdout_evaluation_count": manifest.get("final_holdout_evaluation_count"),
            "final_holdout_consumption": manifest.get("final_holdout_consumption"),
        },
    )


def _automatic_monitor_gate(db: Session) -> tuple[dict, int | None]:
    snapshot = db.query(StockMonitoringSnapshot).filter_by(
        monitor_key="stock_continuous_monitor",
    ).order_by(
        StockMonitoringSnapshot.generated_at.desc(),
        StockMonitoringSnapshot.id.desc(),
    ).first()
    if not snapshot:
        return _gate("unknown", reason="current monitoring evidence is unavailable"), None
    checks = snapshot.checks or []
    passed = snapshot.status == "clear" and bool(checks) and all(
        check.get("status") == "clear" for check in checks
    )
    return _gate(
        "pass" if passed else "fail",
        reason=None if passed else "current monitoring health is not clear",
        evidence={"snapshot_id": snapshot.id, "status": snapshot.status, "checks": checks},
    ), snapshot.id


def _automatic_trial_and_report(
    db: Session, cycle: StockLearningCycle, active_binding: StockPaperModelBinding | None,
) -> tuple[StockPaperTrial | None, dict | None]:
    # Never infer a scheduled trial from the current active binding. The cycle
    # owns one immutable trial link, and a later binding must not redirect old
    # evidence or promotion decisions.
    trial = db.get(StockPaperTrial, cycle.trial_id) if cycle.trial_id else None
    if trial is None:
        return None, None
    try:
        report = evaluate_promotion_readiness(db, trial.id)
    except Exception:
        # A malformed or unavailable report is evidence of uncertainty, never
        # permission to promote.
        report = None
    return trial, report


def _decision_projection(row: StockPaperPromotionDecision) -> dict:
    return {
        "id": row.id,
        "cycle_id": row.cycle_id,
        "trial_id": row.trial_id,
        "report_id": row.report_id,
        "model_run_id": row.model_run_id,
        "snapshot_id": row.snapshot_id,
        "decision": row.decision,
        "gates": row.gates,
        "lineage": row.lineage,
        "evidence": row.evidence,
        "actor": row.actor,
        "source_job": row.source_job,
        "correlation_id": row.correlation_id,
        "reason": row.reason,
        "before_binding_id": row.before_binding_id,
        "before_model_run_id": row.before_model_run_id,
        "after_binding_id": row.after_binding_id,
        "after_model_run_id": row.after_model_run_id,
        "decision_sha256": row.decision_sha256,
        "paper_only": row.paper_only,
        "live_authorized": row.live_authorized,
        "created_at": row.created_at,
    }


def automate_paper_promotion(
    db: Session,
    cycle_id: str,
    *,
    source_job: str = AUTOMATIC_PROMOTION_JOB,
    actor: str = AUTOMATIC_PROMOTION_ACTOR,
) -> dict:
    """Evaluate and, only when every persisted gate passes, promote a paper canary.

    The coordinator deliberately requires an already-created paper canary and a
    completed forward trial. It never creates a trial, changes its policy, or
    treats a missing observation as evidence.
    """
    cycle = db.get(StockLearningCycle, cycle_id)
    if cycle is None:
        raise StockTrainingError("Learning cycle not found")
    if cycle.trigger != "scheduled":
        raise StockTrainingError("Automatic promotion is limited to scheduled paper cycles")
    pause_reason = _scheduled_learning_pause_reason(db)
    if pause_reason:
        _defer_scheduled_cycle_for_pause(
            db, cycle, reason=pause_reason, stage="promotion",
        )
        db.flush()
        return {
            "id": None,
            "cycle_id": cycle.cycle_id,
            "decision": "deferred",
            "reason": pause_reason,
            "paper_only": True,
            "live_authorized": False,
        }
    prior_decision = db.query(StockPaperPromotionDecision).filter_by(
        cycle_id=cycle.cycle_id,
    ).order_by(StockPaperPromotionDecision.id.desc()).first()
    if cycle.status in {"complete", "promoted"} and prior_decision and prior_decision.decision == "promoted":
        return _decision_projection(prior_decision)

    _lock_lifecycle_admission(db)
    binding_state = db.scalar(select(StockPaperBindingState).where(
        StockPaperBindingState.id == 1,
    ).with_for_update())
    active_binding = db.get(
        StockPaperModelBinding,
        binding_state.active_binding_id,
    ) if binding_state else None
    before_binding_id = active_binding.id if active_binding else None
    before_model_run_id = active_binding.model_run_id if active_binding else None
    trial, report = _automatic_trial_and_report(db, cycle, active_binding)
    validation = _automatic_validation_gate(db, cycle)
    monitor, monitor_snapshot_id = _automatic_monitor_gate(db)
    account = db.query(StockPaperAccount).filter_by(broker="alpaca_paper").one_or_none()
    recovery = db.get(StockPaperRecoveryState, 1)
    target_model = db.get(StockModelRegistry, cycle.model_run_id) if cycle.model_run_id else None
    target_state = _current_lifecycle(db, target_model, for_update=False) if target_model else None
    trial_gate = _gate(
        "pass" if trial and trial.status == "completed" else "unknown",
        reason=None if trial and trial.status == "completed"
        else "one completed forward paper trial must be linked to the scheduled cycle",
        evidence={"trial_id": trial.id if trial else None, "status": trial.status if trial else None},
    )
    validation_evidence = validation.get("evidence") or {}
    holdout_count = validation_evidence.get("final_holdout_evaluation_count")
    holdout_consumption = validation_evidence.get("final_holdout_consumption")
    holdout_gate = _gate(
        "pass" if holdout_count == 1 and isinstance(holdout_consumption, dict)
        and holdout_consumption.get("count") == 1 else "fail",
        reason=None if holdout_count == 1 and isinstance(holdout_consumption, dict)
        and holdout_consumption.get("count") == 1
        else "final holdout evidence is absent or was consumed more than once",
        evidence={"evaluation_count": holdout_count, "consumption": holdout_consumption},
    )
    report_gate = _gate(
        "pass" if report and report.get("decision") == "pass"
        and all(gate.get("status") == "pass" for gate in (report.get("gates") or {}).values())
        else "unknown" if report is None else "fail",
        reason=None if report and report.get("decision") == "pass"
        and all(gate.get("status") == "pass" for gate in (report.get("gates") or {}).values())
        else "promotion-readiness report is missing, incomplete, or not passing",
        evidence={
            "report_id": report.get("id") if report else None,
            "report_hash": report.get("report_hash") if report else None,
            "decision": report.get("decision") if report else None,
            "gates": report.get("gates") if report else None,
        },
    )
    binding_gate = _gate(
        "pass" if active_binding and cycle.model_run_id
        and active_binding.model_run_id == cycle.model_run_id
        and active_binding.paper_only is True
        and active_binding.live_authorized is False
        and target_state and target_state.lifecycle_state == "paper_canary"
        else "fail" if active_binding and active_binding.model_run_id != cycle.model_run_id
        else "unknown",
        reason=None if active_binding and cycle.model_run_id
        and active_binding.model_run_id == cycle.model_run_id
        and active_binding.paper_only is True
        and active_binding.live_authorized is False
        and target_state and target_state.lifecycle_state == "paper_canary"
        else "the scheduled model must already be the active paper canary",
        evidence={
            "active_binding_id": before_binding_id,
            "active_model_run_id": before_model_run_id,
            "target_model_run_id": cycle.model_run_id,
            "target_lifecycle_state": target_state.lifecycle_state if target_state else None,
        },
    )
    ledger_gate = _gate(
        "pass" if account and account.status == "reconciled"
        and not account.reconciliation_required and account.accounting_verified
        else "fail",
        reason=None if account and account.status == "reconciled"
        and not account.reconciliation_required and account.accounting_verified
        else "paper ledger reconciliation is not verified",
        evidence={
            "account_status": account.status if account else "uninitialized",
            "reconciliation_required": account.reconciliation_required if account else None,
            "accounting_verified": account.accounting_verified if account else False,
        },
    )
    recovery_gate = _gate(
        "pass" if recovery is None or recovery.status == "armed" else "fail",
        reason=None if recovery is None or recovery.status == "armed"
        else "paper recovery is paused or awaiting revalidation",
        evidence={"status": recovery.status if recovery else "uninitialized"},
    )
    paper_gate = _gate(
        "pass" if (not report or report.get("paper_only") is True)
        and (not report or report.get("live_authorized") is False)
        and (not active_binding or active_binding.paper_only is True)
        and (not active_binding or active_binding.live_authorized is False)
        else "fail",
        reason=None if (not report or report.get("paper_only") is True)
        and (not report or report.get("live_authorized") is False)
        and (not active_binding or active_binding.paper_only is True)
        and (not active_binding or active_binding.live_authorized is False)
        else "live authorization is not permitted",
        evidence={"paper_only": True, "live_authorized": False},
    )
    lineage = (report or {}).get("lineage") or {}
    lineage_gate = _gate(
        "pass" if report and lineage.get("model_run_id") == cycle.model_run_id
        and lineage.get("snapshot_id") == cycle.snapshot_id
        and trial and cycle.binding_id == trial.binding_id
        and trial.binding_id == before_binding_id
        else "unknown",
        reason=None if report and lineage.get("model_run_id") == cycle.model_run_id
        and lineage.get("snapshot_id") == cycle.snapshot_id
        and trial and cycle.binding_id == trial.binding_id
        and trial.binding_id == before_binding_id
        else "model, dataset, trial, and active binding lineage is incomplete or conflicting",
        evidence={
            "lineage": lineage,
            "cycle_snapshot_id": cycle.snapshot_id,
            "cycle_binding_id": cycle.binding_id,
            "active_binding_id": before_binding_id,
        },
    )
    report_gates = (report or {}).get("gates") or {}
    data_gate_names = ("regular_sessions", "historical_feed_health", "decision_coverage")
    data_gate_values = [report_gates.get(name) for name in data_gate_names]
    data_ready = bool(data_gate_values) and all(
        gate and gate.get("status") == "pass" for gate in data_gate_values
    )
    data_readiness_gate = _gate(
        "pass" if data_ready else "unknown" if report is None else "fail",
        reason=None if data_ready else "aligned forward data-quality evidence is not explicitly passing",
        evidence={name: report_gates.get(name) for name in data_gate_names},
    )
    gates = {
        "leakage_safe_validation": validation,
        "single_use_holdout": holdout_gate,
        "forward_trial": trial_gate,
        "promotion_readiness_report": report_gate,
        "immutable_lineage": lineage_gate,
        "paper_canary": binding_gate,
        "paper_ledger": ledger_gate,
        "data_readiness": data_readiness_gate,
        "monitor_health": monitor,
        "recovery_state": recovery_gate,
        "paper_only": paper_gate,
    }
    evidence = {
        "report_hash": report.get("report_hash") if report else None,
        "report_id": report.get("id") if report else None,
        "monitor_snapshot_id": monitor_snapshot_id,
        "trial_id": trial.id if trial else None,
        "gates": gates,
    }
    decision_basis = {
        "cycle_id": cycle.cycle_id,
        "model_run_id": cycle.model_run_id,
        "snapshot_id": cycle.snapshot_id,
        "trial_id": trial.id if trial else None,
        "report_hash": report.get("report_hash") if report else None,
        "gates": gates,
        "before_binding_id": before_binding_id,
        "before_model_run_id": before_model_run_id,
    }
    digest = _digest(decision_basis)
    existing = db.scalar(select(StockPaperPromotionDecision).where(
        StockPaperPromotionDecision.decision_sha256 == digest,
    ))
    if existing:
        return _decision_projection(existing)

    passed = _all_pass(gates)
    reason = (
        "Every immutable paper promotion gate passed"
        if passed
        else next(
            (gate.get("reason") for gate in gates.values() if gate.get("status") != "pass"),
            "Promotion evidence is incomplete",
        )
    )
    after_binding_id = before_binding_id if passed else None
    after_model_run_id = cycle.model_run_id if passed else None
    if passed:
        if target_model is None or target_state is None:
            raise StockTrainingError("Promotion target model is unavailable")
        champions = db.scalars(select(StockModelLifecycleState).where(
            StockModelLifecycleState.lifecycle_state == "champion",
            StockModelLifecycleState.model_run_id != target_model.run_id,
        ).with_for_update()).all()
        recovery_state = recovery or StockPaperRecoveryState(
            id=1, status="armed", flatten_policy="none", updated_by=actor,
        )
        if recovery is None:
            db.add(recovery_state)
            db.flush()
        for champion in champions:
            previous_binding = db.query(StockPaperModelBinding).filter_by(
                model_run_id=champion.model_run_id,
            ).order_by(StockPaperModelBinding.id.desc()).first()
            _transition_model(
                db,
                model=db.get(StockModelRegistry, champion.model_run_id),
                action="demote",
                actor=actor,
                reason=f"Automatic paper promotion replaced champion: {cycle.cycle_id}",
                binding_id=previous_binding.id if previous_binding else None,
            )
        _transition_model(
            db, model=target_model, action="promote", actor=actor,
            reason=f"Automatic paper promotion: {cycle.cycle_id}",
            binding_id=active_binding.id if active_binding else None,
        )
        recovery_state.last_known_good_model_run_id = (
            champions[0].model_run_id if champions else None
        )
        recovery_state.last_known_good_binding_id = (
            db.query(StockPaperModelBinding).filter_by(
                model_run_id=champions[0].model_run_id,
            ).order_by(StockPaperModelBinding.id.desc()).first().id
            if champions and db.query(StockPaperModelBinding).filter_by(
                model_run_id=champions[0].model_run_id,
            ).order_by(StockPaperModelBinding.id.desc()).first()
            else None
        )
        cycle.status, cycle.stage = "promoted", "promotion"
        cycle.monitor_snapshot_id = monitor_snapshot_id
        cycle.active_binding_id = after_binding_id
        cycle.last_reason = reason
        decision_name = "promoted"
    else:
        cycle.status, cycle.stage = "blocked", "promotion"
        cycle.monitor_snapshot_id = monitor_snapshot_id
        cycle.active_binding_id = before_binding_id
        cycle.last_reason = reason
        decision_name = "blocked"
    cycle.trial_id = trial.id if trial else cycle.trial_id
    cycle.gates = gates
    cycle.evidence = {**(cycle.evidence or {}), "automatic_promotion": evidence}
    decision_identity = {
        **decision_basis,
        "decision": decision_name,
        "reason": reason,
        "after_binding_id": after_binding_id,
        "after_model_run_id": after_model_run_id,
    }
    row = StockPaperPromotionDecision(
        cycle_id=cycle.cycle_id,
        trial_id=trial.id if trial else None,
        report_id=report.get("id") if report else None,
        model_run_id=cycle.model_run_id,
        snapshot_id=cycle.snapshot_id,
        decision=decision_name,
        gates=gates,
        lineage=lineage,
        evidence=evidence,
        actor=actor,
        source_job=source_job,
        correlation_id=cycle.cycle_id,
        reason=reason,
        before_binding_id=before_binding_id,
        before_model_run_id=before_model_run_id,
        after_binding_id=after_binding_id,
        after_model_run_id=after_model_run_id,
        decision_sha256=digest,
        paper_only=True,
        live_authorized=False,
    )
    db.add(row)
    _event(
        db, cycle=cycle, stage="promotion",
        decision="complete" if passed else "blocked", actor=actor,
        reason=reason,
        evidence={"automatic_decision": decision_name, **evidence, "before_binding_id": before_binding_id,
                  "after_binding_id": after_binding_id},
    )
    write_audit_log(
        db, event_type="stock_learning_cycle", action="automatic_paper_promotion",
        status="complete" if passed else "blocked", message=reason,
        entity_type="stock_learning_cycle", payload={
            "cycle_id": cycle.cycle_id, "actor": actor, "source_job": source_job,
            "correlation_id": cycle.cycle_id, "decision": decision_name,
            "model_run_id": cycle.model_run_id, "snapshot_id": cycle.snapshot_id,
            "trial_id": trial.id if trial else None, "report_id": report.get("id") if report else None,
            "gates": gates, "before_binding_id": before_binding_id, "after_binding_id": after_binding_id,
            "decision_sha256": row.decision_sha256, "paper_only": True, "live_authorized": False,
        },
    )
    db.flush()
    return _decision_projection(row)


def run_scheduled_paper_trial_handoff_job(
    db: Session,
    *,
    source_job: str = "scheduled_stock_paper_trial_handoff_job",
) -> dict:
    """Advance completed scheduled challengers through admission and start.

    Every candidate is evaluated independently so one unavailable feed or
    account does not erase the durable reason for another cycle. Re-running
    this job is safe because the cycle, binding, and trial source keys are
    unique.
    """
    pause_reason = _scheduled_learning_pause_reason(db)
    rows = db.scalars(select(StockLearningCycle).where(
        StockLearningCycle.trigger == "scheduled",
        StockLearningCycle.training_job_id.is_not(None),
        StockLearningCycle.status.not_in((
            "blocked", "complete", "promoted", "demoted", "rolled_back", "failed",
        )),
    ).order_by(StockLearningCycle.created_at, StockLearningCycle.cycle_id)).all()
    if pause_reason:
        for cycle in rows:
            _defer_scheduled_cycle_for_pause(
                db, cycle, reason=pause_reason, stage="handoff",
            )
        db.commit()
        return {
            "status": "paused",
            "job": source_job,
            "deferred": [
                {"cycle_id": cycle.cycle_id, "reason": pause_reason}
                for cycle in rows
            ],
            "reason": pause_reason,
            "paper_only": True,
            "live_authorized": False,
        }
    results: list[dict] = []
    for cycle in rows:
        try:
            sync_cycle_from_training_job(db, cycle.training_job_id, actor=HANDOFF_ACTOR)
            cycle = start_scheduled_learning_trial(db, cycle.cycle_id, actor=HANDOFF_ACTOR)
            db.commit()
            results.append({
                "cycle_id": cycle.cycle_id,
                "binding_id": cycle.binding_id,
                "trial_id": cycle.trial_id,
                "status": cycle.status,
                "stage": cycle.stage,
                "reason": cycle.last_reason,
                "paper_only": True,
                "live_authorized": False,
            })
        except Exception as exc:
            db.rollback()
            cycle = db.get(StockLearningCycle, cycle.cycle_id)
            if cycle:
                _handoff_failure(
                    db,
                    cycle,
                    stage="admission",
                    status="deferred",
                    reason=f"Scheduled paper handoff retry unavailable: {exc.__class__.__name__}",
                    evidence={"error_type": exc.__class__.__name__},
                )
                db.commit()
                results.append({
                    "cycle_id": cycle.cycle_id,
                    "binding_id": cycle.binding_id,
                    "trial_id": cycle.trial_id,
                    "status": cycle.status,
                    "stage": cycle.stage,
                    "reason": cycle.last_reason,
                    "paper_only": True,
                    "live_authorized": False,
                })
    return {
        "status": "complete",
        "job": source_job,
        "evaluated": len(results),
        "results": results,
        "paper_only": True,
        "live_authorized": False,
    }


def run_automatic_paper_promotion_job(
    db: Session,
    *,
    source_job: str = AUTOMATIC_PROMOTION_JOB,
) -> dict:
    """Admit/start scheduled trials, then evaluate exact completed evidence."""
    handoff = run_scheduled_paper_trial_handoff_job(db, source_job=source_job)
    if handoff.get("status") == "paused":
        return {
            "status": "paused",
            "job": source_job,
            "handoff": handoff,
            "evaluated": 0,
            "results": [],
            "reason": handoff.get("reason"),
            "paper_only": True,
            "live_authorized": False,
        }
    rows = db.scalars(select(StockLearningCycle).where(
        StockLearningCycle.trigger == "scheduled",
        StockLearningCycle.model_run_id.is_not(None),
        StockLearningCycle.stage.in_(("forward_trial", "operator_review", "promotion")),
        StockLearningCycle.status.not_in(("complete", "promoted", "demoted", "rolled_back", "failed")),
    ).order_by(StockLearningCycle.created_at, StockLearningCycle.cycle_id)).all()
    results: list[dict] = []
    for cycle in rows:
        try:
            result = automate_paper_promotion(db, cycle.cycle_id, source_job=source_job)
            db.commit()
            results.append({
                "cycle_id": cycle.cycle_id,
                "decision": result["decision"],
                "reason": result["reason"],
                "decision_id": result["id"],
            })
        except Exception as exc:
            db.rollback()
            cycle = db.get(StockLearningCycle, cycle.cycle_id)
            if cycle is not None:
                reason = f"Automatic promotion evaluation unavailable: {exc.__class__.__name__}"
                cycle.status, cycle.stage, cycle.last_reason = "deferred", "promotion", reason
                _event(
                    db,
                    cycle=cycle,
                    stage="promotion",
                    decision="deferred",
                    actor=AUTOMATIC_PROMOTION_ACTOR,
                    reason=reason,
                    evidence={"source_job": source_job, "error_type": exc.__class__.__name__},
                )
                write_audit_log(
                    db,
                    event_type="stock_learning_cycle",
                    action="automatic_paper_promotion",
                    status="deferred",
                    message=reason,
                    entity_type="stock_learning_cycle",
                    payload={
                        "cycle_id": cycle.cycle_id,
                        "actor": AUTOMATIC_PROMOTION_ACTOR,
                        "source_job": source_job,
                        "paper_only": True,
                        "live_authorized": False,
                    },
                )
                db.commit()
            results.append({
                "cycle_id": cycle.cycle_id,
                "decision": "deferred",
                "reason": reason,
            })
    return {
        "status": "complete",
        "job": source_job,
        "handoff": handoff,
        "evaluated": len(results),
        "results": results,
        "paper_only": True,
        "live_authorized": False,
    }


def cycle_projection(db: Session, cycle: StockLearningCycle) -> dict:
    binding_state = db.get(StockPaperBindingState, 1)
    active_binding = db.get(StockPaperModelBinding, binding_state.active_binding_id) if binding_state else None
    binding = db.get(StockPaperModelBinding, cycle.binding_id) if cycle.binding_id else None
    trial = db.get(StockPaperTrial, cycle.trial_id) if cycle.trial_id else None
    latest_report = db.scalar(select(StockPaperPromotionReadinessReport).where(
        StockPaperPromotionReadinessReport.trial_id == cycle.trial_id,
    ).order_by(
        StockPaperPromotionReadinessReport.report_version.desc().nullslast(),
        StockPaperPromotionReadinessReport.id.desc(),
    )) if cycle.trial_id else None
    monitor = db.get(StockMonitoringSnapshot, cycle.monitor_snapshot_id) if cycle.monitor_snapshot_id else None
    if monitor is None:
        monitor = db.query(StockMonitoringSnapshot).order_by(StockMonitoringSnapshot.generated_at.desc()).first()
    recovery = db.get(StockPaperRecoveryState, 1)
    recovery_event = db.query(StockPaperRecoveryEvent).order_by(StockPaperRecoveryEvent.created_at.desc()).first()
    automatic_decision = db.query(StockPaperPromotionDecision).filter_by(
        cycle_id=cycle.cycle_id,
    ).order_by(StockPaperPromotionDecision.id.desc()).first()
    return {
        "cycle_id": cycle.cycle_id, "status": cycle.status, "stage": cycle.stage,
        "trigger": cycle.trigger, "requested_by": cycle.requested_by,
        "symbols": cycle.symbols, "cutoff_date": cycle.cutoff_date,
        "horizon_days": cycle.horizon_days, "provider": cycle.provider,
        "snapshot_id": cycle.snapshot_id, "training_job_id": cycle.training_job_id,
        "model_run_id": cycle.model_run_id, "binding_id": cycle.binding_id,
        "trial_id": cycle.trial_id,
        "active_binding_id": active_binding.id if active_binding else cycle.active_binding_id,
        "active_binding_model_run_id": active_binding.model_run_id if active_binding else None,
        "handoff": {
            "stage": cycle.stage,
            "status": cycle.status,
            "binding_id": binding.id if binding else None,
            "trial_id": trial.id if trial else None,
            "trial_status": trial.status if trial else None,
            "preflight": (cycle.gates or {}).get("preflight"),
            "reason": cycle.last_reason,
            "report_id": latest_report.id if latest_report else None,
            "report_decision": latest_report.decision if latest_report else None,
        },
        "gates": cycle.gates, "evidence": cycle.evidence,
        "last_reason": cycle.last_reason, "paper_only": True, "live_authorized": False,
        "scheduled_learning_paused": bool(_scheduled_learning_pause_reason(db)),
        "automatic_promotion": _decision_projection(automatic_decision) if automatic_decision else None,
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
        if recovery_event and recovery_event.action in {"pause", "demote_model", "pause_stock_path", "flatten_positions"}:
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