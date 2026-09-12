"""Authenticated durable controls for verified stock-model training."""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import (
    StockModelLifecycleEvent, StockModelRegistry, StockPaperBindingState,
    StockPaperModelBinding, StockTrainingJob,
)
from app.services.audit import write_audit_log
from app.services.stock_training_jobs import (
    StockTrainingError, create_stock_paper_binding, create_stock_training_job,
    enqueue_stock_training_job, model_projection, recover_stock_training_jobs,
    report_projection, request_stock_training_cancel, snapshot_projection, summarize_job,
    get_stock_model_lifecycle_state, transition_stock_model_lifecycle,
    validate_registered_stock_model, _dataset_from_record,
)

router = APIRouter(prefix="/stock/training", tags=["stock training"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class StartJobBody(StrictBody):
    symbols: list[str] = Field(min_length=1, max_length=25)
    cutoff_at: date
    horizon_bars: int = Field(default=5, ge=1, le=252)
    provider: Literal["yfinance", "yahoo_chart"] = "yfinance"
    trigger: Literal["manual", "scheduled"] = "manual"
    seed: int = Field(default=42, ge=0, le=2_147_483_647)


class BindingBody(StrictBody):
    model_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The UI may display this label, but the server derives authority from the
    # immutable model id and does not trust the caller-provided value.
    model_version: str | None = Field(default=None, max_length=128)
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["PAPER_ONLY_FROZEN_BINDING"]
    reason: str = Field(min_length=3, max_length=1000)
    purpose: str = Field(default="forward_paper_evaluation", min_length=3, max_length=64)


class LifecycleBody(StrictBody):
    action: Literal["mark_eligible", "start_canary", "promote", "demote", "retire"]
    reason: str = Field(min_length=3, max_length=1000)


def _audit(db: Session, request: Request, action: str, payload: dict, entity_id: int | None = None) -> None:
    write_audit_log(
        db, event_type="stock_training", action=action, status="success",
        message="Authenticated paper-only stock training operation", entity_type="stock_training",
        entity_id=entity_id, payload={"actor": request.state.actor, "request_id": request.state.request_id, **payload},
    )


@router.post("/jobs")
def start_job(body: StartJobBody, request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        job, duplicate = create_stock_training_job(
            db, symbols=body.symbols, cutoff_at=body.cutoff_at, horizon_bars=body.horizon_bars,
            provider=body.provider, trigger=body.trigger, seed=body.seed, actor=request.state.actor,
        )
        _audit(db, request, "start_job_deduplicated" if duplicate else "start_job", {"job_id": job.id})
        db.commit()  # Persist job/snapshot/reservation before the non-durable broker call.
        if not duplicate:
            enqueue_stock_training_job(db, job)
            db.commit()
        db.refresh(job)
        return summarize_job(job) | {"deduplicated": duplicate}
    except StockTrainingError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None


@router.get("/jobs")
def list_jobs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(select(StockTrainingJob).order_by(StockTrainingJob.created_at.desc()).offset(offset).limit(limit)).all()
    return {"items": [summarize_job(row) for row in rows], "total": db.scalar(select(func.count()).select_from(StockTrainingJob)), "limit": limit, "offset": offset}


@router.post("/jobs/recover")
def recover_jobs(request: Request, db: Session = Depends(get_db)) -> dict:
    rows = recover_stock_training_jobs(db)
    _audit(db, request, "recover_jobs", {"job_ids": [row.id for row in rows]})
    db.commit()
    return {"recovered_job_ids": [row.id for row in rows], "paper_only": True, "live_authorized": False}


@router.get("/jobs/{job_id}")
def get_job(job_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"), db: Session = Depends(get_db)) -> dict:
    row = db.get(StockTrainingJob, job_id)
    if row is None:
        raise HTTPException(404, "Stock training job not found")
    if row.result_run_id:
        from app.models import StockDatasetSnapshot
        model, snapshot = db.get(StockModelRegistry, row.result_run_id), db.get(StockDatasetSnapshot, row.snapshot_id)
        try:
            if model is None or snapshot is None:
                raise StockTrainingError("Job result registry references are missing")
            validate_registered_stock_model(model, _dataset_from_record(snapshot))
        except StockTrainingError as exc:
            raise HTTPException(409, f"Stock training report integrity failure: {exc}") from None
    return summarize_job(row, db, detail=True)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str = Path(..., pattern=r"^[0-9a-f-]{36}$"), request: Request = None, db: Session = Depends(get_db)) -> dict:
    try:
        row = request_stock_training_cancel(db, job_id)
        _audit(db, request, "cancel_job", {"job_id": row.id})
        db.commit()
        return summarize_job(row)
    except StockTrainingError as exc:
        db.rollback()
        raise HTTPException(404 if "not found" in str(exc) else 409, str(exc)) from None


@router.get("/runs/{run_id}/report")
def model_report(run_id: str = Path(..., pattern=r"^[0-9a-f]{64}$"), db: Session = Depends(get_db)) -> dict:
    job = db.scalar(select(StockTrainingJob).where(StockTrainingJob.result_run_id == run_id))
    model = db.get(StockModelRegistry, run_id)
    if job is None or model is None:
        raise HTTPException(404, "Stock training report not found")
    from app.models import StockDatasetSnapshot
    snapshot = db.get(StockDatasetSnapshot, job.snapshot_id)
    try:
        if snapshot is None:
            raise StockTrainingError("Report references no verified dataset snapshot")
        validate_registered_stock_model(model, _dataset_from_record(snapshot))
    except StockTrainingError as exc:
        raise HTTPException(409, f"Stock training report integrity failure: {exc}") from None
    return report_projection(job, snapshot, model, get_stock_model_lifecycle_state(db, run_id))


def _binding(row: StockPaperModelBinding, *, integrity_error: str | None = None) -> dict:
    return {
        "id": row.id, "binding_id": row.id, "model_run_id": row.model_run_id, "model_id": row.model_run_id,
        "snapshot_id": row.snapshot_id, "binding_sha256": row.binding_sha256, "binding_hash": row.binding_sha256,
        "purpose": row.purpose, "paper_only": True, "live_authorized": False, "frozen": True,
        "active": integrity_error is None, "integrity_valid": integrity_error is None, "integrity_error": integrity_error,
        "bound_by": row.bound_by, "bound_at": row.created_at, "created_at": row.created_at, "reason": row.reason,
    }


@router.get("/binding")
def active_binding(db: Session = Depends(get_db)) -> dict | None:
    # Binding rows are append-only audit events; the singleton pointer is the
    # authoritative current-binding selection.
    state = db.get(StockPaperBindingState, 1)
    row = db.get(StockPaperModelBinding, state.active_binding_id) if state else None
    if row is None:
        return None
    try:
        from app.models import StockDatasetSnapshot
        model, snapshot = db.get(StockModelRegistry, row.model_run_id), db.get(StockDatasetSnapshot, row.snapshot_id)
        if model is None or snapshot is None:
            raise StockTrainingError("Bound registry references are missing")
        validate_registered_stock_model(model, _dataset_from_record(snapshot))
        return _binding(row) | {"lifecycle_state": get_stock_model_lifecycle_state(db, model.run_id)}
    except StockTrainingError as exc:
        # The append-only evidence remains visible, but is explicitly not
        # presented as a usable/active frozen binding.
        return _binding(row, integrity_error=str(exc))


@router.get("/models/{run_id}/lifecycle")
def model_lifecycle(
    run_id: str = Path(..., pattern=r"^[0-9a-f]{64}$"),
    db: Session = Depends(get_db),
) -> dict:
    model = db.get(StockModelRegistry, run_id)
    if model is None:
        raise HTTPException(404, "Stock model not found")
    active = db.get(StockPaperBindingState, 1)
    events = db.scalars(
        select(StockModelLifecycleEvent)
        .where(StockModelLifecycleEvent.model_run_id == run_id)
        .order_by(StockModelLifecycleEvent.id)
    ).all()
    return {
        "model_id": model.run_id,
        "lifecycle_state": get_stock_model_lifecycle_state(db, model.run_id),
        "active_binding_id": active.active_binding_id if active else None,
        "events": [
            {
                "id": event.id,
                "binding_id": event.binding_id,
                "from_state": event.from_state,
                "to_state": event.to_state,
                "action": event.action,
                "actor": event.actor,
                "reason": event.reason,
                "event_sha256": event.event_sha256,
                "created_at": event.created_at,
            }
            for event in events
        ],
        "paper_only": True,
        "live_authorized": False,
    }


@router.post("/models/{run_id}/lifecycle")
def change_model_lifecycle(
    body: LifecycleBody,
    run_id: str = Path(..., pattern=r"^[0-9a-f]{64}$"),
    request: Request = None,
    db: Session = Depends(get_db),
) -> dict:
    try:
        model = transition_stock_model_lifecycle(
            db, model_run_id=run_id, action=body.action,
            actor=request.state.actor, reason=body.reason,
        )
        event = db.scalar(
            select(StockModelLifecycleEvent)
            .where(StockModelLifecycleEvent.model_run_id == run_id)
            .order_by(StockModelLifecycleEvent.id.desc())
        )
        _audit(
            db, request, f"stock_model_{body.action}",
            {
                "model_id": run_id,
                "from_state": event.from_state if event else None,
                "to_state": get_stock_model_lifecycle_state(db, model.run_id),
                "event_id": event.id if event else None,
            },
            event.id if event else None,
        )
        db.commit()
        lifecycle_state = get_stock_model_lifecycle_state(db, model.run_id)
        return model_projection(model, lifecycle_state=lifecycle_state) | {
            "lifecycle_state": lifecycle_state,
            "lifecycle_event_id": event.id if event else None,
            "paper_only": True,
            "live_authorized": False,
        }
    except StockTrainingError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None


@router.post("/binding")
def bind_paper_model(body: BindingBody, request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        row = create_stock_paper_binding(
            db, model_run_id=body.model_id, snapshot_id=body.snapshot_id, actor=request.state.actor,
            purpose=body.purpose, reason=body.reason,
        )
        _audit(db, request, "bind_frozen_paper_model", {"model_id": body.model_id, "snapshot_id": body.snapshot_id, "binding_id": row.id}, row.id)
        db.commit()
        model = db.get(StockModelRegistry, row.model_run_id)
        return _binding(row) | {
            "active_binding_id": row.id,
            "lifecycle_state": get_stock_model_lifecycle_state(db, model.run_id) if model else None,
        }
    except StockTrainingError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None