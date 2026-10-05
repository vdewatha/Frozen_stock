"""Authenticated operational views and explicit actions for stock learning cycles."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.core.config import settings
from app.models import StockLearningCycle, StockPaperPromotionDecision, StockTrainingJob
from app.services.audit import write_audit_log
from app.services.stock_learning_cycle import (
    _decision_projection,
    StockTrainingError,
    create_learning_cycle,
    cycle_action,
    cycle_projection,
    classify_launch_prerequisites,
    create_paper_run_approval,
    evaluate_cycle_prerequisites,
    evaluate_launch_admission_prerequisites,
    launch_preflight_eligibility,
    list_learning_cycles,
    paper_run_approval_projection,
    review_learning_cycle,
    scheduled_learning_control_projection,
    set_scheduled_learning_control,
)
from app.services.stock_training_jobs import enqueue_stock_training_job

router = APIRouter(prefix="/stock/learning-cycles", tags=["stock learning cycles"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class CreateCycleBody(StrictBody):
    symbols: list[str] = Field(min_length=1, max_length=25)
    cutoff_at: date
    horizon_days: int = Field(default=5, ge=1, le=252)
    provider: Literal["yfinance", "yahoo_chart"] = "yfinance"
    trigger: Literal["manual", "scheduled"] = "manual"
    seed: int = Field(default=42, ge=0, le=2_147_483_647)
    paper_session_date: date | None = None


class ReviewCycleBody(StrictBody):
    reason: str = Field(min_length=3, max_length=1000)
    trial_id: str | None = Field(default=None, min_length=1, max_length=36)

    @field_validator("reason")
    @classmethod
    def require_non_blank_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("reason must contain at least three non-whitespace characters")
        return value


class CycleActionBody(StrictBody):
    action: Literal["mark_eligible", "start_canary", "promote", "demote", "retire"]
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def require_non_blank_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("reason must contain at least three non-whitespace characters")
        return value


class ScheduleControlBody(StrictBody):
    action: Literal["pause", "resume"]
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def require_non_blank_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("reason must contain at least three non-whitespace characters")
        return value


class PaperRunApprovalBody(StrictBody):
    environment: Literal["paper"]
    execution_provider: str = Field(min_length=1, max_length=64)
    provider_switch: dict[str, str] | None = None
    symbols: list[str] = Field(min_length=1, max_length=25)
    exposure_limits: dict[str, str | int | float] = Field(min_length=1, max_length=16)
    loss_limits: dict[str, str | int | float] = Field(min_length=1, max_length=8)
    duration_sessions: int = Field(ge=1, le=252)
    schedule: dict[str, str] = Field(min_length=1, max_length=16)
    stop_conditions: list[str] = Field(min_length=1, max_length=16)
    stop_authority: str = Field(min_length=1, max_length=64)
    pending_order_treatment: str = Field(min_length=1, max_length=32)
    remaining_position_policy: str = Field(min_length=1, max_length=32)
    approving_actors: list[str] = Field(default_factory=list, max_length=10)


def _audit(db: Session, request: Request, action: str, cycle_id: str, payload: dict) -> None:
    write_audit_log(
        db, event_type="stock_learning_cycle", action=action, status="success",
        message="Authenticated paper-only learning-cycle operation",
        entity_type="stock_learning_cycle", payload={
            "cycle_id": cycle_id, "actor": request.state.actor,
            "request_id": request.state.request_id, **payload,
        },
    )


@router.get("")
def get_cycles(
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[dict]:
    return list_learning_cycles(db, limit=limit)


@router.get("/schedule-control")
def get_schedule_control(db: Session = Depends(get_db)) -> dict:
    return scheduled_learning_control_projection(db)


@router.get("/launch-prerequisites")
def get_launch_prerequisites(
    cycle_id: str | None = Query(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """Read the latest launch prerequisites without changing execution state."""
    cycle = db.get(StockLearningCycle, cycle_id) if cycle_id else None
    if cycle_id and cycle is None:
        raise HTTPException(404, "Learning cycle not found")

    symbols = (
        [str(symbol).strip().upper() for symbol in cycle.symbols]
        if cycle is not None
        else [
            str(symbol).strip().upper()
            for symbol in settings.stock_learning_default_symbols
            if str(symbol).strip()
        ]
    )
    provider = (
        cycle.provider
        if cycle is not None
        else settings.stock_learning_default_provider
    )
    checked_at = datetime.now(timezone.utc)
    gates = evaluate_cycle_prerequisites(
        db,
        symbols=symbols,
        provider=provider,
        now=checked_at,
    )
    gates = {
        **gates,
        **evaluate_launch_admission_prerequisites(db, now=checked_at),
    }
    status, gate_reason = classify_launch_prerequisites(gates)
    eligible, eligibility_reason = launch_preflight_eligibility(
        cycle,
        prerequisites_ready=status == "ready",
        # Current read-only evidence supersedes saved cycle snapshots.  Keep
        # lineage/admission gates that have no current equivalent, but never
        # let a repaired current gate remain blocked by historical evidence.
        cycle_gates=(
            {
                **(cycle.gates or {}),
                **gates,
            }
            if cycle is not None else None
        ),
    )
    reason = eligibility_reason if status == "ready" and not eligible else gate_reason
    return {
        "cycle_id": cycle.cycle_id if cycle is not None else None,
        "symbols": symbols,
        "provider": provider,
        "checked_at": checked_at,
        "status": status,
        "eligible_for_approval": eligible,
        "reason": reason,
        "gates": gates,
    }


@router.post("/schedule-control")
def update_schedule_control(
    body: ScheduleControlBody,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    try:
        control = set_scheduled_learning_control(
            db,
            paused=body.action == "pause",
            actor=request.state.actor,
            reason=body.reason,
        )
        write_audit_log(
            db,
            event_type="stock_learning_cycle",
            action=f"scheduled_learning_{body.action}",
            status="success",
            message=body.reason,
            entity_type="stock_learning_schedule_control",
            payload={
                "actor": request.state.actor,
                "request_id": request.state.request_id,
                "paused": control.paused,
                "paper_only": True,
                "live_authorized": False,
            },
        )
        db.commit()
        return scheduled_learning_control_projection(db)
    except StockTrainingError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None


@router.get("/{cycle_id}")
def get_cycle(
    cycle_id: str = Path(..., pattern=r"^[0-9a-f]{64}$"),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(StockLearningCycle, cycle_id)
    if not row:
        raise HTTPException(404, "Learning cycle not found")
    return cycle_projection(db, row)


@router.get("/{cycle_id}/approval")
def get_paper_run_approval(
    cycle_id: str = Path(..., pattern=r"^[0-9a-f]{64}$"),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(StockLearningCycle, cycle_id)
    if not row:
        raise HTTPException(404, "Learning cycle not found")
    return paper_run_approval_projection(db, row)


@router.get("/{cycle_id}/automatic-promotion")
def get_automatic_promotion(
    cycle_id: str = Path(..., pattern=r"^[0-9a-f]{64}$"),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(StockLearningCycle, cycle_id)
    if not row:
        raise HTTPException(404, "Learning cycle not found")
    decision = db.query(StockPaperPromotionDecision).filter_by(
        cycle_id=cycle_id,
    ).order_by(StockPaperPromotionDecision.id.desc()).first()
    return {
        "cycle_id": cycle_id,
        "decision": _decision_projection(decision) if decision else None,
        "paper_only": True,
        "live_authorized": False,
    }


@router.post("")
def start_cycle(body: CreateCycleBody, request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        cycle, duplicate = create_learning_cycle(
            db, symbols=body.symbols, cutoff_at=body.cutoff_at,
            horizon_days=body.horizon_days, provider=body.provider,
            actor=request.state.actor, trigger=body.trigger, seed=body.seed,
            paper_session_date=body.paper_session_date,
        )
        _audit(db, request, "start_cycle", cycle.cycle_id, {"deduplicated": duplicate})
        db.commit()
        if cycle.training_job_id and cycle.status == "queued" and not duplicate:
            job = db.get(StockTrainingJob, cycle.training_job_id)
            if job:
                enqueue_stock_training_job(db, job)
                db.commit()
        return cycle_projection(db, cycle) | {"deduplicated": duplicate}
    except StockTrainingError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None


@router.post("/{cycle_id}/approval")
def approve_paper_run(
    body: PaperRunApprovalBody,
    cycle_id: str = Path(..., pattern=r"^[0-9a-f]{64}$"),
    request: Request = None,
    db: Session = Depends(get_db),
) -> dict:
    try:
        approval = create_paper_run_approval(
            db,
            cycle_id,
            actor=request.state.actor,
            environment=body.environment,
            execution_provider=body.execution_provider,
            provider_switch=body.provider_switch,
            symbols=body.symbols,
            exposure_limits=body.exposure_limits,
            loss_limits=body.loss_limits,
            duration_sessions=body.duration_sessions,
            schedule=body.schedule,
            stop_conditions=body.stop_conditions,
            stop_authority=body.stop_authority,
            pending_order_treatment=body.pending_order_treatment,
            remaining_position_policy=body.remaining_position_policy,
            approving_actors=body.approving_actors,
        )
        db.commit()
        cycle = db.get(StockLearningCycle, cycle_id)
        return cycle_projection(db, cycle) if cycle else {"approval_id": approval.id}
    except StockTrainingError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None


@router.post("/{cycle_id}/review")
def review_cycle(
    body: ReviewCycleBody,
    cycle_id: str = Path(..., pattern=r"^[0-9a-f]{64}$"),
    request: Request = None,
    db: Session = Depends(get_db),
) -> dict:
    try:
        row = review_learning_cycle(
            db, cycle_id, actor=request.state.actor,
            reason=body.reason, trial_id=body.trial_id,
        )
        _audit(db, request, "review_cycle", cycle_id, {"trial_id": body.trial_id})
        db.commit()
        return cycle_projection(db, row)
    except (StockTrainingError, ValueError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None


@router.post("/{cycle_id}/action")
def act_on_cycle(
    body: CycleActionBody,
    cycle_id: str = Path(..., pattern=r"^[0-9a-f]{64}$"),
    request: Request = None,
    db: Session = Depends(get_db),
) -> dict:
    try:
        row = cycle_action(
            db, cycle_id, action=body.action,
            actor=request.state.actor, reason=body.reason,
        )
        _audit(db, request, f"cycle_{body.action}", cycle_id, {"action": body.action})
        db.commit()
        return cycle_projection(db, row)
    except StockTrainingError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None
