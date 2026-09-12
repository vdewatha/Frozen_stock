"""Authenticated operational views and explicit actions for stock learning cycles."""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import StockLearningCycle, StockTrainingJob
from app.services.audit import write_audit_log
from app.services.stock_learning_cycle import (
    StockTrainingError,
    create_learning_cycle,
    cycle_action,
    cycle_projection,
    list_learning_cycles,
    review_learning_cycle,
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


class ReviewCycleBody(StrictBody):
    reason: str = Field(min_length=3, max_length=1000)
    trial_id: str | None = Field(default=None, min_length=1, max_length=36)


class CycleActionBody(StrictBody):
    action: Literal["mark_eligible", "start_canary", "promote", "demote", "retire"]
    reason: str = Field(min_length=3, max_length=1000)


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


@router.get("/{cycle_id}")
def get_cycle(
    cycle_id: str = Path(..., pattern=r"^[0-9a-f]{64}$"),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(StockLearningCycle, cycle_id)
    if not row:
        raise HTTPException(404, "Learning cycle not found")
    return cycle_projection(db, row)


@router.post("")
def start_cycle(body: CreateCycleBody, request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        cycle, duplicate = create_learning_cycle(
            db, symbols=body.symbols, cutoff_at=body.cutoff_at,
            horizon_days=body.horizon_days, provider=body.provider,
            actor=request.state.actor, trigger=body.trigger, seed=body.seed,
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