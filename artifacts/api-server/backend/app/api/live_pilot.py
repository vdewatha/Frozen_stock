from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.core.config import settings
from app.core.security import secondary_approval_identity
from app.schemas.trading import (
    LivePilotActionRequest,
    LivePilotActivationRequest,
    LivePilotLimitChangeRequest,
    LivePilotPromotionRequest,
    LiveMandateActionRequest,
    LiveMandateComplianceBlockRequest,
    LiveMandateRequest,
)
from app.services.live_mandate import (
    LiveMandateError,
    create_live_mandate,
    live_mandate_projection,
    record_compliance_block,
    reinstate_live_mandate,
    revoke_live_mandate,
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


def _secondary(request: Request, claimed_actor: str | None, minimum_role: str) -> str:
    # Every environment that can hold live or production authority (production
    # and the approved-live runtime) must verify the second identity; a named
    # actor in the request body is only accepted for local compatibility.
    if not settings.production_identity_required:
        return (claimed_actor or "").strip()
    try:
        evidence = secondary_approval_identity(request, minimum_role=minimum_role)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    actor = str(evidence["actor"])
    if claimed_actor and claimed_actor.strip() != actor:
        raise HTTPException(status_code=403, detail="Secondary approval identity does not match the request")
    return actor


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
            secondary_actor=_secondary(request, payload.secondary_approval_actor, "admin"),
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
            secondary_actor=_secondary(request, payload.secondary_approval_actor, "admin"),
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
            secondary_actor=_secondary(request, payload.secondary_approval_actor, "admin"),
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
            secondary_actor=_secondary(request, payload.secondary_approval_actor, "operator"),
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

@router.get("/mandate")
def mandate_status(db: Session = Depends(get_db)) -> dict:
    return live_mandate_projection(db)


def _mandate_write(request: Request, payload: LiveMandateRequest, db: Session, *, amend: bool) -> dict:
    try:
        create_live_mandate(
            db,
            actor=_actor(request),
            secondary_actor=_secondary(request, payload.secondary_approval_actor, "admin"),
            reason=payload.reason,
            symbols=payload.symbols,
            max_notional=payload.max_notional,
            max_order_notional=payload.max_order_notional,
            max_pilot_days=payload.max_pilot_days,
            renewal_lead_hours=payload.renewal_lead_hours,
            observation_window_sessions=payload.observation_window_sessions,
            max_cumulative_loss=payload.max_cumulative_loss,
            max_drawdown=payload.max_drawdown,
            rollback_target=payload.rollback_target,
            model_run_id=payload.model_run_id,
            checklist=payload.checklist,
            paper_expectations=payload.paper_expectations,
            valid_from=payload.valid_from,
            expires_at=payload.expires_at,
            amend=amend,
        )
        db.commit()
        return live_mandate_projection(db)
    except LiveMandateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/mandate")
def create_mandate(payload: LiveMandateRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    return _mandate_write(request, payload, db, amend=False)


@router.post("/mandate/amend")
def amend_mandate(payload: LiveMandateRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    return _mandate_write(request, payload, db, amend=True)


@router.post("/mandate/revoke")
def revoke_mandate(payload: LiveMandateActionRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        revoke_live_mandate(
            db, actor=_actor(request),
            secondary_actor=_secondary(request, payload.secondary_approval_actor, "admin"),
            reason=payload.reason,
        )
        db.commit()
        return live_mandate_projection(db)
    except LiveMandateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/mandate/reinstate")
def reinstate_mandate(payload: LiveMandateActionRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        reinstate_live_mandate(
            db, actor=_actor(request),
            secondary_actor=_secondary(request, payload.secondary_approval_actor, "admin"),
            reason=payload.reason,
        )
        db.commit()
        return live_mandate_projection(db)
    except LiveMandateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/mandate/compliance-block")
def compliance_block(
    payload: LiveMandateComplianceBlockRequest, request: Request, db: Session = Depends(get_db),
) -> dict:
    try:
        record_compliance_block(db, actor=_actor(request), reason=payload.reason)
        db.commit()
        return live_mandate_projection(db)
    except LiveMandateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
