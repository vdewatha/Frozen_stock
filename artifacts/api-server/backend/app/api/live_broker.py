from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.trading import LiveBrokerCancelRequest, LiveBrokerOrderRequest
from app.services.live_broker import (
    LiveBrokerError,
    cancel_live_order,
    dispatch_live_order,
    live_broker_status,
    reconcile_live_broker_account,
    reserve_live_order,
)

router = APIRouter(prefix="/live-broker", tags=["live-broker"])


def _attribute(db: Session, request: Request) -> None:
    db.info["live_broker_actor"] = getattr(request.state, "actor", "unknown")
    db.info["live_broker_authorization"] = {
        "actor": getattr(request.state, "actor", "unknown"),
        "role": getattr(request.state, "auth_role", "unknown"),
        "request_id": getattr(request.state, "request_id", None),
        "auth_method": (getattr(request.state, "auth_evidence", {}) or {}).get("method", "unknown"),
    }


def _order_response(order) -> dict:
    return {
        "id": order.id,
        "client_order_id": order.client_order_id,
        "broker_order_id": order.broker_order_id,
        "symbol": order.symbol,
        "side": order.side,
        "quantity": str(order.quantity),
        "order_type": order.order_type,
        "time_in_force": order.time_in_force,
        "reference_price": str(order.reference_price),
        "limit_price": str(order.limit_price) if order.limit_price is not None else None,
        "model_run_id": order.model_run_id,
        "signal_id": order.signal_id,
        "risk_decision_id": order.risk_decision_id,
        "account_snapshot_id": order.account_snapshot_id,
        "actor": order.actor,
        "idempotency_key": order.idempotency_key,
        "status": order.status,
        "uncertain_submission": order.uncertain_submission,
    }


@router.get("/status")
def status(db: Session = Depends(get_db)) -> dict:
    return live_broker_status(db)


@router.post("/reconcile")
def reconcile(request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        return reconcile_live_broker_account(db)
    except LiveBrokerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/orders")
def reserve_order(payload: LiveBrokerOrderRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        order = reserve_live_order(
            db,
            symbol=payload.symbol,
            side=payload.side,
            quantity=payload.quantity,
            reference_price=payload.reference_price,
            order_type=payload.order_type,
            time_in_force=payload.time_in_force,
            limit_price=payload.limit_price,
            idempotency_key=payload.idempotency_key,
            model_run_id=payload.model_run_id,
            signal_id=payload.signal_id,
            risk_decision_id=payload.risk_decision_id,
            actor=str(getattr(request.state, "actor", "unknown")),
            request_id=getattr(request.state, "request_id", None),
        )
        return {"mode": "live", "action": "reserved", "order": _order_response(order)}
    except LiveBrokerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/orders/{order_id}/dispatch")
def dispatch_order(order_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        order = dispatch_live_order(db, order_id)
        return {"mode": "live", "action": "submitted" if not order.uncertain_submission else "halted_uncertain", "order": _order_response(order)}
    except LiveBrokerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, payload: LiveBrokerCancelRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        order = cancel_live_order(
            db, order_id, actor=str(getattr(request.state, "actor", "unknown")),
            reason=payload.reason,
        )
        return {"mode": "live", "action": "cancel_requested", "order": _order_response(order)}
    except LiveBrokerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc