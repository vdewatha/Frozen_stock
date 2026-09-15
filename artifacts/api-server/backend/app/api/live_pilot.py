from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.trading import (
    LivePilotActionRequest,
    LivePilotActivationRequest,
    LivePilotLimitChangeRequest,
    LivePilotPromotionRequest,
)
from app.services.live_pilot import (
    LivePilotError,
    activate_live_pilot,
    live_pilot_status,
    promote_live_pilot,
    review_live_pilot,
    rollback_live_pilot,
    stop_live_pilot,
    update_live_pilot_limits,
)

router = APIRouter(prefix="/system/live-pilot", tags=["live-pilot"])


def _actor(request: Request) -> str:
    return str(getattr(request.state, "actor", "unknown"))


@router.get("")
def status(db: Session = Depends(get_db)) -> dict:
    return live_pilot_status(db)


@router.post("/activate")
def activate(
    payload: LivePilotActivationRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = activate_live_pilot(
            db,
            actor=_actor(request),
            secondary_actor=payload.secondary_approval_actor,
            reason=payload.reason,
            symbols=payload.symbols,
            max_notional=payload.max_notional,
            max_order_notional=payload.max_order_notional,
            starts_at=payload.starts_at,
            expires_at=payload.expires_at,
            observation_window_sessions=payload.observation_window_sessions,
            rollback_target=payload.rollback_target,
            model_run_id=payload.model_run_id,
            paper_expectations=payload.paper_expectations,
            checklist=payload.checklist,
        )
        db.commit()
        return result
    except LivePilotError as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/promote")
def promote(
    payload: LivePilotPromotionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = promote_live_pilot(
            db,
            actor=_actor(request),
            secondary_actor=payload.secondary_approval_actor,
            reason=payload.reason,
        )
        db.commit()
        return result
    except LivePilotError as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/limits")
def update_limits(
    payload: LivePilotLimitChangeRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = update_live_pilot_limits(
            db,
            actor=_actor(request),
            secondary_actor=payload.secondary_approval_actor,
            reason=payload.reason,
            max_notional=payload.max_notional,
            max_order_notional=payload.max_order_notional,
            evidence_reference=payload.evidence_reference,
        )
        db.commit()
        return result
    except LivePilotError as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/stop")
def stop(
    payload: LivePilotActionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = stop_live_pilot(db, actor=_actor(request), reason=payload.reason)
        db.commit()
        return result
    except LivePilotError as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/rollback")
def rollback(
    payload: LivePilotActionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = rollback_live_pilot(
            db,
            actor=_actor(request),
            reason=payload.reason,
            secondary_actor=payload.secondary_approval_actor,
        )
        db.commit()
        return result
    except LivePilotError as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/review")
def review(
    payload: LivePilotActionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = review_live_pilot(db, actor=_actor(request), reason=payload.reason)
        db.commit()
        return result
    except LivePilotError as exc:
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc