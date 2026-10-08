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


def stock_paper_broker_status(db: Session) -> dict:
    """Read-only, redacted projection; venue safety is not accounting readiness."""
    from app.services.stock_paper_ledger import (
        TRADIER_PAPER_EVIDENCE, active_paper_account, active_paper_broker_name,
    )
    from app.services.paper_venue_qualification import paper_venue_qualification_status
    from app.services.alpaca_paper_cost_contract import execution_policy_status
    from app.models import StockPaperLedgerEvent

    venue = active_paper_broker_name()
    account = active_paper_account(db)
    qualification = paper_venue_qualification_status(db, venue)
    # Some read-only callers provide a lightweight account projection rather
    # than an ORM row.  In that case there is no durable event lookup to do;
    # fail closed instead of making status reporting depend on a test/mock
    # implementation detail.
    reconciliation = None
    account_id = getattr(account, "id", None)
    if account_id is not None:
        reconciliation = db.query(StockPaperLedgerEvent).filter_by(
            account_id=account_id,
            event_type="activity_reconciliation",
        ).order_by(StockPaperLedgerEvent.id.desc()).first()
    activation_authorized = bool(qualification.get("activation_authorized"))
    paper_execution = execution_policy_status(
        provider=venue,
        activity_contract=getattr(account, "activity_contract", "") if account else "",
        account_status=account.status if account else None,
        reconciliation_required=bool(not account or account.reconciliation_required),
        unexplained_residual=bool(account and account.unexplained_residual),
        reconciliation_payload=reconciliation.payload if reconciliation else None,
        venue_activation_authorized=activation_authorized,
    )
    provider_complete = TRADIER_PAPER_EVIDENCE["complete"] if venue == "tradier_sandbox" else None
    accounting_ready = bool(
        account and account.status == "reconciled"
        and account.accounting_verified and account.costs_known
        and not account.reconciliation_required and not account.unexplained_residual
        and provider_complete is not False
    )
    return {
        "paper_broker": venue,
        "scope": "stock_paper",
        "paper_trading_enabled": True,
        "live_trading_enabled": False,
        "live_trading_blocked": True,
        "message": (
            f"Stock-paper execution uses {venue}; this paper route cannot place live orders. "
            "Complete accounting remains a separate required gate for research and graduation."
        ),
        "accounting": {
            "paper_broker": venue,
            "ready": accounting_ready,
            "status": account.status if account else "uninitialized",
            "accounting_verified": bool(account and account.accounting_verified),
            "costs_known": bool(account and account.costs_known),
            "reconciliation_required": bool(not account or account.reconciliation_required),
            "unexplained_residual": bool(account and account.unexplained_residual),
            "provider_evidence_complete": provider_complete,
            "venue_qualification": qualification,
            "reason": (
                "Complete broker accounting is verified."
                if accounting_ready else
                "Tradier sandbox history cannot establish complete broker accounting."
                if provider_complete is False else
                "Complete broker accounting requires a reconciled account, verified accounting, "
                "known costs, and no unexplained residual."
            ),
        },
        "paper_execution": paper_execution,
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


def block_live_order(db: Session, payload: dict, *, safety: dict | None = None) -> dict:
    safety = safety or {"status": "blocked", "live_orders_allowed": False}
    result = {
        "broker": "live_broker",
        "status": "blocked",
        "paper_only": False,
        "live_trading_enabled": False,
        "reason": safety.get("reason", "Live order execution is blocked by the live safety contract."),
        "safety_contract": safety,
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
