"""Fail-closed rollback and recovery coordination for the stock paper path."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Asset,
    Notification,
    RiskRule,
    StockMonitoringSnapshot,
    StockPaperBindingState,
    StockPaperRecoveryEvent,
    StockPaperRecoveryState,
    StockPaperModelBinding,
)
from app.models.stock_paper import StockPaperAccount, StockPaperOrder, StockPaperPosition
from app.services.audit import write_audit_log
from app.services.intraday_data import feed_status
from app.services.notifications import create_notification
from app.services.stock_paper_ledger import (
    BROKER,
    NONTERMINAL_ORDER_STATUSES,
    AlpacaPaperGateway,
    StockPaperError,
    StockPaperUnavailable,
    _event as ledger_event,
    _halt,
    _utc,
    dispatch_reserved_order,
    reconcile_stock_paper_account,
)
from app.services.stock_training_jobs import (
    StockTrainingError,
    transition_stock_model_lifecycle,
)

UTC = timezone.utc
RECOVERY_COOLDOWN = timedelta(minutes=15)
HEARTBEAT_TIMEOUT = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _state(db: Session, *, for_update: bool = True) -> StockPaperRecoveryState:
    statement = select(StockPaperRecoveryState).where(StockPaperRecoveryState.id == 1)
    if for_update:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    if row is None:
        row = StockPaperRecoveryState(id=1, status="armed", flatten_policy="none", updated_by="system")
        db.add(row)
        db.flush()
    return row


def _event(
    db: Session,
    *,
    action: str,
    status: str,
    actor: str,
    reason: str,
    payload: dict | None = None,
) -> StockPaperRecoveryEvent:
    payload = payload or {}
    identity = {
        "action": action,
        "status": status,
        "actor": actor,
        "reason": reason,
        "payload": payload,
        "nonce": f"{_now().isoformat()}:{action}:{actor}",
    }
    row = StockPaperRecoveryEvent(
        action=action,
        status=status,
        actor=actor,
        reason=reason,
        event_sha256=hashlib.sha256(_canonical(identity)).hexdigest(),
        payload=payload,
    )
    db.add(row)
    return row


def _set_kill_switch(db: Session, enabled: bool) -> None:
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    if rule:
        rule.value = (rule.value or {}) | {"kill_switch_enabled": enabled, "paper_only": True}


def enter_stock_recovery(
    db: Session,
    *,
    reason: str,
    actor: str = "system",
    flatten_policy: str = "none",
    cooldown: timedelta = RECOVERY_COOLDOWN,
) -> StockPaperRecoveryState:
    if not reason.strip():
        raise StockPaperError("Recovery requires a non-empty reason")
    if flatten_policy not in {"none", "positions"}:
        raise StockPaperError("Recovery flatten policy must be none or positions")
    now = _now()
    state = _state(db)
    state.status = "cooldown"
    state.flatten_policy = flatten_policy
    state.cooldown_until = now + cooldown
    state.pause_reason = reason.strip()
    state.updated_by = actor
    state.updated_at = now
    _set_kill_switch(db, True)
    account = db.query(StockPaperAccount).filter_by(broker=BROKER).with_for_update().one_or_none()
    if account:
        _halt(account, reason)
        ledger_event(db, account, "recovery_pause", "halted", reason, {"flatten_policy": flatten_policy})
    _event(db, action="pause", status="cooldown", actor=actor, reason=reason, payload={"flatten_policy": flatten_policy, "cooldown_until": state.cooldown_until.isoformat()})
    write_audit_log(db, event_type="stock_recovery", action="pause", status="complete", message=reason, entity_type="stock_paper", payload={"flatten_policy": flatten_policy, "cooldown_until": state.cooldown_until.isoformat()})
    return state


def record_stock_monitor_heartbeat(db: Session, *, observed_at: datetime | None = None) -> None:
    state = _state(db)
    state.last_monitor_heartbeat_at = observed_at or _now()
    state.updated_by = "stock_monitor"


def record_stock_watchdog_heartbeat(db: Session, *, status: str, details: dict | None = None) -> None:
    state = _state(db)
    now = _now()
    state.last_watchdog_heartbeat_at = now
    state.updated_by = "stock_watchdog"
    _event(db, action="watchdog_heartbeat", status=status, actor="stock_watchdog", reason="Independent watchdog evaluation", payload=details or {})


def _recovery_order(
    db: Session,
    account: StockPaperAccount,
    position: StockPaperPosition,
) -> StockPaperOrder:
    key = f"recovery-flatten:{account.id}:{position.symbol}:{position.quantity}"
    client_order_id = "sp-" + hashlib.sha256(key.encode()).hexdigest()[:45]
    existing = db.query(StockPaperOrder).filter_by(client_order_id=client_order_id).one_or_none()
    if existing:
        return existing
    if not position.current_price or position.current_price <= 0:
        raise StockPaperError(f"Cannot flatten {position.symbol} without a broker-observed current price")
    order = StockPaperOrder(
        account_id=account.id,
        client_order_id=client_order_id,
        symbol=position.symbol,
        side="sell",
        quantity=position.quantity,
        order_type="limit",
        time_in_force="day",
        limit_price=position.current_price,
        reserved_cash=Decimal("0"),
        status="reserved",
        source="recovery_flatten",
    )
    db.add(order)
    db.flush()
    _event(db, account, "recovery_flatten_reservation", "reserved", "Recovery flatten reservation", {"order_id": order.id, "symbol": order.symbol, "quantity": str(order.quantity)})
    return order


def cancel_open_stock_orders(
    db: Session,
    *,
    actor: str,
    flatten_policy: str = "none",
    gateway: AlpacaPaperGateway | None = None,
) -> dict:
    """Pause first, durably request every cancellation, then optionally flatten.

    The database state is committed before broker calls. A cancellation batch is
    never reported complete until a later reconciliation confirms terminal order
    states. Any broker ambiguity keeps the account halted.
    """
    state = enter_stock_recovery(
        db,
        reason=f"Recovery cancellation requested by {actor}",
        actor=actor,
        flatten_policy=flatten_policy,
        cooldown=RECOVERY_COOLDOWN,
    )
    account = db.query(StockPaperAccount).filter_by(broker=BROKER).with_for_update().one_or_none()
    if not account:
        raise StockPaperError("Stock paper account is not initialized")
    orders = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id,
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).with_for_update().all()
    local_only = []
    broker_orders = []
    for order in orders:
        if order.source == "broker_import":
            raise StockPaperError("Externally submitted nonterminal order requires manual broker review")
        if order.broker_order_id:
            order.status = "pending_cancel"
            broker_orders.append(order)
        else:
            order.status = "cancelled"
            local_only.append(order)
    _event(db, action="cancel_batch_started", status="pending", actor=actor, reason="Durable cancellation batch created", payload={"order_ids": [order.id for order in orders]})
    db.commit()

    cancelled = []
    failures = []
    gateway = gateway or __import__("app.services.stock_paper_ledger", fromlist=["AlpacaPaperClient"]).AlpacaPaperClient()
    for order in broker_orders:
        try:
            gateway.cancel_order(order.broker_order_id)
            cancelled.append(order.id)
        except (StockPaperError, StockPaperUnavailable) as exc:
            failures.append({"order_id": order.id, "error": str(exc)})
    db.expire_all()
    account = db.query(StockPaperAccount).filter_by(id=account.id).with_for_update().one()
    if failures:
        _halt(account, "Atomic recovery cancellation did not complete for every broker order")
        state = _state(db)
        state.status = "paused"
        state.pause_reason = account.halt_reason
        _event(db, action="cancel_batch", status="failed", actor=actor, reason=account.halt_reason, payload={"cancelled_order_ids": cancelled, "failures": failures})
        db.commit()
        return {"status": "failed", "cancelled_order_ids": cancelled, "failures": failures, "flattened_order_ids": []}

    _event(db, action="cancel_batch", status="requested", actor=actor, reason="All broker cancellation requests returned successfully", payload={"cancelled_order_ids": cancelled, "local_order_ids": [order.id for order in local_only]})
    db.commit()
    try:
        reconcile_stock_paper_account(db, gateway=gateway)
    except StockPaperError as exc:
        return {"status": "reconciliation_required", "cancelled_order_ids": cancelled, "failures": [{"error": str(exc)}], "flattened_order_ids": []}

    db.expire_all()
    account = db.query(StockPaperAccount).filter_by(id=account.id).one()
    outstanding = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id,
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).count()
    if outstanding:
        _halt(account, "Cancellation requests require reconciliation before recovery can continue")
        _event(db, action="cancel_batch", status="pending_reconciliation", actor=actor, reason=account.halt_reason, payload={"outstanding_orders": outstanding})
        db.commit()
        return {"status": "reconciliation_required", "cancelled_order_ids": cancelled, "failures": [], "flattened_order_ids": []}

    flattened = []
    if flatten_policy == "positions":
        positions = db.query(StockPaperPosition).filter(
            StockPaperPosition.account_id == account.id,
            StockPaperPosition.quantity > 0,
        ).with_for_update().all()
        for position in positions:
            flattened.append(_recovery_order(db, account, position))
        db.commit()
        dispatched = []
        for order in flattened:
            try:
                dispatch_reserved_order(db, order.id, gateway=gateway)
                dispatched.append(order.id)
            except StockPaperError as exc:
                failures.append({"order_id": order.id, "error": str(exc)})
        flattened_ids = dispatched
    else:
        flattened_ids = []
    db.expire_all()
    state = _state(db)
    state.status = "revalidation_required" if not failures else "paused"
    state.pause_reason = "Recovery actions completed; fresh evidence and cooldown are required before resuming" if not failures else "Recovery flattening did not complete for every position"
    db.commit()
    return {"status": "revalidation_required" if not failures else "failed", "cancelled_order_ids": cancelled, "failures": failures, "flattened_order_ids": flattened_ids}


def reconcile_inflight_stock_orders_on_restart(db: Session) -> dict:
    """Reconcile every durable in-flight order before any resume decision."""
    account = db.query(StockPaperAccount).filter_by(broker=BROKER).one_or_none()
    if not account:
        return {"status": "uninitialized", "in_flight_order_ids": []}
    in_flight = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id,
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).all()
    result = reconcile_stock_paper_account(db)
    db.expire_all()
    state = _state(db)
    _event(
        db,
        action="restart_reconciliation",
        status="complete" if result.get("status") != "halted" else "halted",
        actor="restart_reconciler",
        reason="Restart reconciliation imported broker truth for durable in-flight orders",
        payload={"in_flight_order_ids": [order.id for order in in_flight], "result_status": result.get("status")},
    )
    if state.status == "paused" and result.get("status") == "reconciled":
        state.status = "revalidation_required"
        state.updated_by = "restart_reconciler"
    db.commit()
    return {"status": result.get("status"), "in_flight_order_ids": [order.id for order in in_flight]}


def _fresh_monitoring_is_clear(db: Session, now: datetime) -> tuple[bool, str]:
    snapshot = db.query(StockMonitoringSnapshot).filter_by(monitor_key="stock_continuous_monitor").order_by(StockMonitoringSnapshot.generated_at.desc()).first()
    if not snapshot:
        return False, "No monitoring evidence exists after the recovery pause"
    generated = _utc(snapshot.generated_at)
    if now - generated > HEARTBEAT_TIMEOUT:
        return False, "Monitoring evidence is stale"
    if snapshot.status != "clear":
        return False, f"Monitoring status is {snapshot.status}, not clear"
    if any(check.get("status") in {"warning", "breach", "unknown"} for check in snapshot.checks):
        return False, "Every monitoring check must be clear before resuming"
    return True, "Monitoring evidence is fresh and clear"


def resume_stock_paper_after_revalidation(db: Session, *, actor: str) -> dict:
    now = _now()
    state = _state(db)
    if state.status not in {"cooldown", "revalidation_required", "resumable"}:
        raise StockPaperError(f"Recovery state {state.status} is not waiting for revalidation")
    if state.cooldown_until and _utc(state.cooldown_until) > now:
        raise StockPaperError(f"Recovery cooldown remains active until {_utc(state.cooldown_until).isoformat()}")
    account = db.query(StockPaperAccount).filter_by(broker=BROKER).with_for_update().one_or_none()
    if not account or account.status != "reconciled" or account.reconciliation_required:
        raise StockPaperError("A successful broker reconciliation after recovery is required")
    if db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id,
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).count():
        raise StockPaperError("In-flight orders must be terminal before recovery can resume")
    okay, reason = _fresh_monitoring_is_clear(db, now)
    if not okay:
        raise StockPaperError(reason)
    state.status = "resumable"
    state.last_revalidation_at = now
    state.updated_by = actor
    state.pause_reason = None
    _set_kill_switch(db, False)
    account.status, account.halt_reason, account.reconciliation_required = "reconciled", None, False
    _event(db, action="resume", status="revalidated", actor=actor, reason="Cooldown elapsed and fresh monitoring/reconciliation evidence passed", payload={"monitoring_at": now.isoformat()})
    db.commit()
    return recovery_status(db)


def rollback_to_last_known_good(db: Session, *, actor: str, reason: str) -> dict:
    """Restore the recorded last-known-good binding atomically with lifecycle evidence."""
    state = _state(db)
    if not state.last_known_good_model_run_id or not state.last_known_good_binding_id:
        raise StockPaperError("No last-known-good model binding has been recorded")
    binding_state = db.get(StockPaperBindingState, 1)
    target = db.get(StockPaperModelBinding, state.last_known_good_binding_id)
    if not target or target.model_run_id != state.last_known_good_model_run_id:
        raise StockPaperError("Last-known-good binding evidence is incomplete")
    from app.models import StockModelLifecycleState, StockModelRegistry
    target_model = db.get(StockModelRegistry, target.model_run_id)
    if not target_model:
        raise StockPaperError("Last-known-good model no longer exists")
    if binding_state:
        binding_state.active_binding_id = target.id
        binding_state.changed_by = actor
        binding_state.reason = reason
        binding_state.changed_at = _now()
    else:
        binding_state = StockPaperBindingState(id=1, active_binding_id=target.id, changed_by=actor, reason=reason)
        db.add(binding_state)
    champions = db.scalars(select(StockModelLifecycleState).where(StockModelLifecycleState.lifecycle_state == "champion").with_for_update()).all()
    for champion in champions:
        if champion.model_run_id != target.model_run_id:
            transition_stock_model_lifecycle(db, model_run_id=champion.model_run_id, action="demote", actor=actor, reason=f"Rollback: {reason}")
    target_state = db.get(StockModelLifecycleState, target.model_run_id)
    if target_state and target_state.lifecycle_state != "champion":
        transition_stock_model_lifecycle(db, model_run_id=target.model_run_id, action="rollback", actor=actor, reason=reason)
    _event(db, action="rollback", status="complete", actor=actor, reason=reason, payload={"model_run_id": target.model_run_id, "binding_id": target.id})
    write_audit_log(db, event_type="stock_recovery", action="rollback", status="complete", message=reason, entity_type="stock_model", payload={"model_run_id": target.model_run_id, "binding_id": target.id})
    db.commit()
    return recovery_status(db)


def recovery_status(db: Session) -> dict:
    state = _state(db, for_update=False)
    account = db.query(StockPaperAccount).filter_by(broker=BROKER).one_or_none()
    events = db.query(StockPaperRecoveryEvent).order_by(StockPaperRecoveryEvent.created_at.desc()).limit(25).all()
    return {
        "status": state.status,
        "flatten_policy": state.flatten_policy,
        "cooldown_until": state.cooldown_until.isoformat() if state.cooldown_until else None,
        "pause_reason": state.pause_reason,
        "last_known_good_model_run_id": state.last_known_good_model_run_id,
        "last_known_good_binding_id": state.last_known_good_binding_id,
        "last_monitor_heartbeat_at": state.last_monitor_heartbeat_at.isoformat() if state.last_monitor_heartbeat_at else None,
        "last_watchdog_heartbeat_at": state.last_watchdog_heartbeat_at.isoformat() if state.last_watchdog_heartbeat_at else None,
        "last_revalidation_at": state.last_revalidation_at.isoformat() if state.last_revalidation_at else None,
        "account_status": account.status if account else "uninitialized",
        "events": [{"id": event.id, "action": event.action, "status": event.status, "actor": event.actor, "reason": event.reason, "created_at": event.created_at.isoformat() if event.created_at else None, "payload": event.payload} for event in events],
    }


def run_stock_watchdog(db: Session) -> dict:
    now = _now()
    state = _state(db)
    account = db.query(StockPaperAccount).filter_by(broker=BROKER).one_or_none()
    reasons = []
    if account and account.last_reconciled_at and now - _utc(account.last_reconciled_at) > HEARTBEAT_TIMEOUT:
        reasons.append("broker reconciliation heartbeat is stale")
    if account and not account.last_reconciled_at:
        reasons.append("broker reconciliation heartbeat is missing")
    if state.last_monitor_heartbeat_at and now - _utc(state.last_monitor_heartbeat_at) > HEARTBEAT_TIMEOUT:
        reasons.append("continuous monitor heartbeat is stale")
    if state.last_monitor_heartbeat_at is None:
        reasons.append("continuous monitor heartbeat is missing")
    if reasons and (account is None or account.status != "halted"):
        enter_stock_recovery(db, reason="Independent watchdog: " + "; ".join(reasons), actor="stock_watchdog")
        status = "paused"
    else:
        status = "clear"
    record_stock_watchdog_heartbeat(db, status=status, details={"reasons": reasons})
    db.commit()
    return {"status": status, "reasons": reasons, "generated_at": now}