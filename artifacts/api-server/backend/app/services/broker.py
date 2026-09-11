from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4
from math import isfinite

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.audit import write_audit_log


PAPER_BROKER_NAME = "internal_paper_stub"


def broker_status() -> dict:
    return {
        "paper_broker": PAPER_BROKER_NAME,
        "paper_trading_enabled": True,
        "live_trading_enabled": False,
        "live_trading_blocked": True,
        "message": "Version 1 routes broker-shaped orders to paper only. Live order placement is blocked.",
    }


def submit_paper_order(
    db: Session,
    *,
    symbol: str,
    side: str,
    quantity: float,
    order_type: str = "market",
    time_in_force: str = "day",
    source: str = "manual",
    paper_trade_id: Optional[int] = None,
    signal_id: Optional[int] = None,
) -> dict:
    symbol = symbol.strip().upper()
    side = side.strip().lower()
    if side not in {"buy", "sell"}:
        raise ValueError("Paper broker only accepts buy or sell orders.")
    if not isfinite(quantity) or quantity <= 0:
        raise ValueError("Paper broker quantity must be positive.")

    order = {
        "broker": PAPER_BROKER_NAME,
        "broker_order_id": f"paper-{uuid4().hex[:16]}",
        "symbol": symbol,
        "side": side,
        "quantity": round(float(quantity), 6),
        "order_type": order_type,
        "time_in_force": time_in_force,
        "status": "accepted",
        "paper_only": True,
        "submitted_at": datetime.utcnow().isoformat(),
        "source": source,
        "paper_trade_id": paper_trade_id,
        "signal_id": signal_id,
    }
    write_audit_log(
        db,
        event_type="broker_order",
        entity_type="paper_trade" if paper_trade_id else "broker_order",
        entity_id=paper_trade_id,
        action="submit_paper_order",
        status="accepted",
        message=f"Submitted paper {side} order for {symbol} through {PAPER_BROKER_NAME}.",
        payload=order,
    )
    return order


def block_live_order(db: Session, payload: dict) -> dict:
    result = {
        "broker": "live_broker",
        "status": "blocked",
        "paper_only": False,
        "live_trading_enabled": False,
        "reason": "Live trading is disabled in Version 1.",
        "requested_order": payload,
        "blocked_at": datetime.utcnow().isoformat(),
    }
    write_audit_log(
        db,
        event_type="broker_order",
        entity_type="broker_order",
        entity_id=None,
        action="block_live_order",
        status="blocked",
        message=result["reason"],
        payload=result,
    )
    return result
