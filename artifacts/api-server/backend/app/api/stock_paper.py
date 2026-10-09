from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.trading import StockPaperAccountTransitionRequest, StockPaperInitializeRequest
from app.schemas.trading import PaperTradingSignalRequest, StockPaperAccountingReviewRequest, StockPaperCloseRequest, StockPaperHaltRequest, StockPaperOrderRequest, StockPaperReduceRequest, StockPaperRecoveryRequest, StockPaperRollbackRequest, StockPaperRevalidationRequest, StockPaperResearchVenueActivationRequest, StockPaperVenueActivationRequest, StockPaperVenueQualificationRequest
from app.services.paper_venue_qualification import authorize_paper_venue_activation, paper_venue_qualification_status, record_paper_venue_qualification
from app.services.paper_research_venue import authorize as authorize_research_venue, record_qualification as create_research_venue_qualification, status as research_venue_status
from app.services.paper_transport_recovery import review as review_paper_transport_recovery
from app.services.stock_paper_ledger import (
    StockPaperError,
    active_paper_broker_name,
    create_stock_paper_signal,
    dispatch_reserved_order,
    halt_stock_paper_account,
    initialize_stock_paper_account,
    reconcile_stock_paper_account,
    reserve_full_close,
    reserve_position_reduction,
    reserve_stock_paper_order,
    resume_stock_paper_account,
    stock_paper_status,
    active_paper_account,
    transition_stock_paper_account,
)
from app.services.stock_recovery import acknowledge_stock_paper_accounting_review, cancel_open_stock_orders, recovery_evidence, recovery_status, rollback_to_last_known_good
from app.services.stock_training_jobs import StockTrainingError
from app.tasks.jobs import paper_trading_signal_job

router = APIRouter(prefix="/stock-paper", tags=["stock-paper"])

def _attribute(db: Session, request: Request) -> None:
    db.info["stock_paper_actor"] = getattr(request.state, "actor", "unknown")
    db.info["stock_paper_authorization"] = {
        "actor": getattr(request.state, "actor", "unknown"),
        "role": getattr(request.state, "auth_role", "unknown"),
        "request_id": getattr(request.state, "request_id", None),
        "auth_method": (getattr(request.state, "auth_evidence", {}) or {}).get("method", "unknown"),
    }


@router.get("/status")
def status(db: Session = Depends(get_db)) -> dict:
    return stock_paper_status(db)


@router.get("/venue-qualification")
def venue_qualification_status(db: Session = Depends(get_db)) -> dict:
    return paper_venue_qualification_status(db, active_paper_broker_name())


@router.post("/venue-qualification")
def record_venue_qualification(payload: StockPaperVenueQualificationRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    account = active_paper_account(db)
    if account is None:
        raise HTTPException(status_code=409, detail="An initialized paper account is required before qualification")
    try:
        row = record_paper_venue_qualification(
            db, provider=payload.provider, account_id=payload.account_id,
            evidence=payload.evidence, reviewer=str(request.state.actor),
        )
        db.commit()
        return {"qualification_id": row.id, "provider": row.provider, "status": row.status,
                "report_sha256": row.report_sha256, "paper_only": True, "live_authorized": False}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/venue-activation")
def authorize_venue_activation(payload: StockPaperVenueActivationRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        row = authorize_paper_venue_activation(
            db, qualification_id=payload.qualification_id, provider=payload.provider,
            authorizer=str(request.state.actor), reason=payload.reason,
        )
        db.commit()
        return {"authorization_id": row.id, "qualification_id": row.qualification_id,
                "provider": row.provider, "authorization_sha256": row.authorization_sha256,
                "paper_only": True, "live_authorized": False}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/research-venue")
def research_venue(db: Session = Depends(get_db)) -> dict:
    return research_venue_status(db, active_paper_account(db))


@router.post("/research-venue/qualification")
def record_research_venue_qualification(request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    account = active_paper_account(db)
    if account is None:
        raise HTTPException(status_code=409, detail="An initialized paper account is required before research qualification")
    try:
        row = create_research_venue_qualification(db, account, reviewer=str(request.state.actor))
        db.commit()
        return {"qualification_id": row.id, "report_sha256": row.report_sha256,
                "paper_only": True, "live_authorized": False, "execution_authorized": False}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/research-venue/activation")
def authorize_research_venue_activation(payload: StockPaperResearchVenueActivationRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        row = authorize_research_venue(
            db, qualification_id=payload.qualification_id,
            authorizer=str(request.state.actor), reason=payload.reason,
        )
        db.commit()
        return {"authorization_id": row.id, "qualification_id": row.qualification_id,
                "paper_only": True, "live_authorized": False, "execution_authorized": False}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/recovery")
def recovery(db: Session = Depends(get_db)) -> dict:
    return recovery_status(db)


@router.get("/recovery/evidence")
def recovery_evidence_export(db: Session = Depends(get_db)) -> dict:
    return recovery_evidence(db)


@router.post("/recovery/transport-review")
def transport_recovery_review(request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        result = review_paper_transport_recovery(
            db, actor=str(getattr(request.state, "actor", "operator")), apply=True,
        )
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/recovery/cancel")
def cancel_recovery(payload: StockPaperRecoveryRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        result = cancel_open_stock_orders(
            db,
            actor=str(getattr(request.state, "actor", "operator")),
            flatten_policy=payload.flatten_policy,
        )
        return recovery_status(db) | {"action_result": result}
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/recovery/rollback")
def rollback_recovery(payload: StockPaperRollbackRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        return rollback_to_last_known_good(
            db,
            actor=str(getattr(request.state, "actor", "operator")),
            reason=payload.reason,
        )
    except (StockPaperError, StockTrainingError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/recovery/accounting-review")
def accounting_review(payload: StockPaperAccountingReviewRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        return acknowledge_stock_paper_accounting_review(
            db,
            actor=str(getattr(request.state, "actor", "operator")),
            reason=payload.reason,
            confirm_residual_review=payload.confirm_residual_review,
        )
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/initialize")
def initialize(request: Request, payload: StockPaperInitializeRequest | None = None, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        if payload is None:
            contract = "alpaca-activities-v2" if active_paper_broker_name() == "alpaca_paper" else None
            return initialize_stock_paper_account(db, **({"activity_contract": contract} if contract else {}))
        if payload.activity_contract is None:
            contract = "alpaca-activities-v2" if active_paper_broker_name() == "alpaca_paper" else None
            return initialize_stock_paper_account(db, **({"activity_contract": contract} if contract else {}))
        return initialize_stock_paper_account(db, activity_contract=payload.activity_contract)
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/account-transition")
def account_transition(payload: StockPaperAccountTransitionRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        return transition_stock_paper_account(db, reason=payload.reason)
    except StockPaperError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/reconcile")
def reconcile(request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        return reconcile_stock_paper_account(db)
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/halt")
def halt(payload: StockPaperHaltRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        return halt_stock_paper_account(db, payload.reason)
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/resume")
def resume(payload: StockPaperRevalidationRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        return resume_stock_paper_account(db, reason=payload.reason)
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/signal-cycle")
def dispatch_signal_cycle(request: Request, db: Session = Depends(get_db)) -> dict:
    """Queue one normal paper signal cycle; never submits an order directly."""
    _attribute(db, request)
    from app.services.readiness import readiness_snapshot

    readiness = readiness_snapshot(db)
    if not readiness["paper_trading_allowed"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Paper signal cycle is blocked by readiness gates",
                "paper_only": True,
                "live_trading": False,
                "readiness": readiness,
            },
        )
    try:
        task = paper_trading_signal_job.apply_async(queue="paper_trading")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Paper signal queue is unavailable") from exc
    return {
        "task_id": task.id,
        "status": "queued",
        "queue": "paper_trading",
        "paper_only": True,
        "live_trading": False,
        "message": "Queued one governed paper signal cycle; no direct order was submitted.",
    }


def _order_response(order) -> dict:
    return {
        "id": order.id, "client_order_id": order.client_order_id,
        "broker_order_id": order.broker_order_id, "symbol": order.symbol,
        "side": order.side, "quantity": str(order.quantity),
        "order_type": order.order_type, "limit_price": str(order.limit_price) if order.limit_price is not None else None,
        "signal_id": order.signal_id, "strategy_id": order.strategy_id, "evidence_id": order.evidence_id,
        "status": order.status, "reserved_cash": str(order.reserved_cash),
        "uncertain_submission": order.uncertain_submission,
    }


@router.post("/orders")
def reserve_order(payload: StockPaperOrderRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        order = reserve_stock_paper_order(db, symbol=payload.symbol, side=payload.side,
            quantity=payload.quantity, reference_price=payload.reference_price,
            idempotency_key=payload.idempotency_key, source=payload.source, signal_id=payload.signal_id)
        return {"mode": "paper", "action": "reserved", "order": _order_response(order)}
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/signal")
def generate_signal(payload: PaperTradingSignalRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        return create_stock_paper_signal(db, payload.symbol, payload.strategy)
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/orders/{order_id}/dispatch")
def dispatch_order(order_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        order = dispatch_reserved_order(db, order_id)
        return {"mode": "paper", "action": "submitted" if order.status != "unknown" else "halted_uncertain",
                "order": _order_response(order)}
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/positions/{symbol}/close")
def close_position(symbol: str, payload: StockPaperCloseRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        order = reserve_full_close(db, symbol, payload.idempotency_key)
        return {"mode": "paper", "action": "reserved_close", "order": _order_response(order)}
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/positions/{symbol}/reduce")
def reduce_position(symbol: str, payload: StockPaperReduceRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        order = reserve_position_reduction(db, symbol, payload.reduce_pct, payload.idempotency_key)
        return {"mode": "paper", "action": "reserved_reduce", "order": _order_response(order)}
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
