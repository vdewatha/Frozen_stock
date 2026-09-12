from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.trading import PaperTradingSignalRequest, StockPaperCloseRequest, StockPaperHaltRequest, StockPaperOrderRequest, StockPaperReduceRequest, StockPaperRecoveryRequest, StockPaperRollbackRequest
from app.services.stock_paper_ledger import (
    StockPaperError,
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
)
from app.services.stock_recovery import cancel_open_stock_orders, recovery_status, rollback_to_last_known_good
from app.services.stock_training_jobs import StockTrainingError

router = APIRouter(prefix="/stock-paper", tags=["stock-paper"])

def _attribute(db: Session, request: Request) -> None:
    db.info["stock_paper_actor"] = getattr(request.state, "actor", "unknown")


@router.get("/status")
def status(db: Session = Depends(get_db)) -> dict:
    return stock_paper_status(db)


@router.get("/recovery")
def recovery(db: Session = Depends(get_db)) -> dict:
    return recovery_status(db)


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


@router.post("/initialize")
def initialize(request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        return initialize_stock_paper_account(db)
    except StockPaperError as exc:
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
def resume(request: Request, db: Session = Depends(get_db)) -> dict:
    _attribute(db, request)
    try:
        return resume_stock_paper_account(db)
    except StockPaperError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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