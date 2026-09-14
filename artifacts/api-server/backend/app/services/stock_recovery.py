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
from app.models.stock_paper import (
    StockPaperAccount,
    StockPaperBrokerActivity,
    StockPaperEquitySnapshot,
    StockPaperFill,
    StockPaperOrder,
    StockPaperPosition,
)
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
AUTOMATIC_RECOVERY_ACTOR = "stock_recovery_automation"


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _accounting_review_digest(account: StockPaperAccount, db: Session | None = None) -> str:
    evidence: dict[str, Any] = {"account": account.raw_payload or {}}
    if db is not None:
        evidence["positions"] = [
            row.raw_payload or {}
            for row in db.query(StockPaperPosition).filter_by(account_id=account.id).order_by(StockPaperPosition.symbol).all()
        ]
        evidence["fills"] = [
            row.raw_payload or {}
            for row in db.query(StockPaperFill).filter_by(account_id=account.id).order_by(StockPaperFill.broker_activity_id).all()
        ]
        evidence["activities"] = [
            row.raw_payload or {}
            for row in db.query(StockPaperBrokerActivity).filter_by(account_id=account.id).order_by(StockPaperBrokerActivity.broker_activity_id).all()
        ]
        evidence["orders"] = [
            row.raw_payload or {}
            for row in db.query(StockPaperOrder).filter_by(account_id=account.id).order_by(StockPaperOrder.client_order_id).all()
        ]
    return hashlib.sha256(_canonical(evidence)).hexdigest()


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
    account = db.query(StockPaperAccount).filter_by(broker=BROKER).one_or_none()
    pause_event = db.query(StockPaperRecoveryEvent).filter(
        StockPaperRecoveryEvent.action == "pause",
    ).order_by(
        StockPaperRecoveryEvent.created_at.desc(),
        StockPaperRecoveryEvent.id.desc(),
    ).first()
    boundaries = [
        _utc(pause_event.created_at) if pause_event and pause_event.created_at else None,
        _utc(account.halted_at) if account and account.halted_at else None,
    ]
    boundaries = [boundary for boundary in boundaries if boundary is not None]
    if boundaries:
        boundary = max(boundaries)
        if generated <= boundary:
            return False, (
                "Monitoring evidence was generated before the current recovery pause "
                f"or accounting halt ({boundary.isoformat()})"
            )
    if now - generated > HEARTBEAT_TIMEOUT:
        return False, "Monitoring evidence is stale"
    if snapshot.status != "clear":
        return False, f"Monitoring status is {snapshot.status}, not clear"
    if any(check.get("status") in {"warning", "breach", "unknown"} for check in snapshot.checks):
        return False, "Every monitoring check must be clear before resuming"
    return True, "Monitoring evidence is fresh and clear"


def _record_monitoring_preflight_result(
    db: Session,
    *,
    actor: str,
    status: str,
    reason: str,
    now: datetime,
    payload: dict | None = None,
) -> None:
    _event(
        db,
        action="monitoring_preflight",
        status=status,
        actor=actor,
        reason=reason,
        payload={"checked_at": now.isoformat(), **(payload or {})},
    )
    write_audit_log(
        db,
        event_type="stock_recovery",
        action="monitoring_preflight",
        status=status,
        message=reason,
        entity_type="stock_paper",
        payload={"actor": actor, "checked_at": now.isoformat(), **(payload or {})},
    )


def _record_monitoring_preflight_rejection(
    db: Session,
    *,
    actor: str,
    reason: str,
    now: datetime,
) -> None:
    _record_monitoring_preflight_result(
        db,
        actor=actor,
        status="blocked",
        reason=reason,
        now=now,
    )


def resume_stock_paper_after_revalidation(
    db: Session,
    *,
    actor: str,
    reason: str,
) -> dict:
    now = _now()
    actor = actor.strip()
    reason = reason.strip()
    if not actor:
        raise StockPaperError("Recovery revalidation requires an explicit actor")
    if not reason:
        raise StockPaperError("Recovery revalidation requires a non-empty reason")
    state = _state(db)
    if actor == AUTOMATIC_RECOVERY_ACTOR:
        raise StockPaperError("Authorized operator revalidation is required before recovery can resume")
    if state.status not in {"cooldown", "revalidation_required", "resumable"}:
        raise StockPaperError(f"Recovery state {state.status} is not waiting for revalidation")
    if state.cooldown_until and _utc(state.cooldown_until) > now:
        raise StockPaperError(f"Recovery cooldown remains active until {_utc(state.cooldown_until).isoformat()}")
    account = db.query(StockPaperAccount).filter_by(broker=BROKER).with_for_update().one_or_none()
    if not account or account.status != "reconciled" or account.reconciliation_required:
        raise StockPaperError("A successful broker reconciliation after recovery is required")
    if state.accounting_review_required:
        if not state.accounting_reviewed_at or not state.accounting_reviewed_by:
            raise StockPaperError("Explicit operator accounting review is required before recovery can resume")
        if state.accounting_review_digest != _accounting_review_digest(account, db):
            raise StockPaperError("Accounting review evidence is stale; review the latest broker reconciliation")
        if account.unexplained_residual:
            raise StockPaperError("The unexplained accounting residual remains unresolved")
    if db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id,
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).count():
        raise StockPaperError("In-flight orders must be terminal before recovery can resume")
    okay, monitoring_reason = _fresh_monitoring_is_clear(db, now)
    if not okay:
        _record_monitoring_preflight_rejection(db, actor=actor, reason=monitoring_reason, now=now)
        db.commit()
        raise StockPaperError(monitoring_reason)
    _record_monitoring_preflight_result(
        db,
        actor=actor,
        status="clear",
        reason=monitoring_reason,
        now=now,
    )
    state.status = "resumable"
    state.last_revalidation_at = now
    state.updated_by = actor
    state.pause_reason = None
    _set_kill_switch(db, False)
    account.status, account.halt_reason, account.reconciliation_required = "reconciled", None, False
    _event(
        db,
        action="resume",
        status="revalidated",
        actor=actor,
        reason=reason,
        payload={"monitoring_at": now.isoformat()},
    )
    db.commit()
    return recovery_status(db)


def _automatic_recovery_notification(db: Session, *, reason: str, payload: dict | None = None) -> None:
    existing = db.query(Notification).filter(
        Notification.category == "stock_recovery",
        Notification.source == AUTOMATIC_RECOVERY_ACTOR,
        Notification.status == "open",
        Notification.entity_type == "stock_paper",
    ).first()
    if existing:
        return
    create_notification(
        db,
        category="stock_recovery",
        severity="critical",
        source=AUTOMATIC_RECOVERY_ACTOR,
        title="Automatic paper-account recovery is blocked",
        message=reason,
        entity_type="stock_paper",
        payload=payload or {},
    )


def attempt_automatic_stock_recovery(
    db: Session,
    *,
    candidate: bool,
    evidence: dict | None = None,
) -> dict:
    """Resolve only a broker-complete residual, then use normal recovery gates.

    The only automatic accounting proof accepted here is a later commission
    enrichment for an existing fill. The fill's immutable trade fields must
    already match, every persisted fill must have a reported fee, every broker
    activity must be a timestamped fill, and reconciliation must have found no
    other mismatch. A repeated snapshot is never treated as proof.

    The returned mapping is the public automatic-recovery response: ``status``
    is always present and ``reason`` is present whenever the status explains a
    blocked, waiting, or operator-action outcome. Reconciliation exposes this
    exact mapping under its ``automatic_recovery`` key.
    """
    evidence = evidence or {}
    state = _state(db)
    account = db.query(StockPaperAccount).filter_by(broker=BROKER).with_for_update().one_or_none()
    if account is None:
        return {"status": "uninitialized"}

    review_complete = (
        not state.accounting_review_required
        and not account.unexplained_residual
        and state.accounting_reviewed_by == AUTOMATIC_RECOVERY_ACTOR
        and account.accounting_verified
        and account.costs_known
    )
    if not state.accounting_review_required and not review_complete:
        if state.accounting_reviewed_by == AUTOMATIC_RECOVERY_ACTOR and (
            account.status == "halted" or account.reconciliation_required
        ):
            blocked_reason = (
                "Fresh reconciliation invalidated the prior automatic accounting proof; "
                "the account remains halted until the new broker evidence is reviewed."
            )
            _automatic_recovery_notification(db, reason=blocked_reason, payload={"evidence": evidence})
            _event(
                db,
                action="automatic_accounting_review",
                status="blocked",
                actor=AUTOMATIC_RECOVERY_ACTOR,
                reason=blocked_reason,
                payload={"evidence": evidence},
            )
            db.commit()
            return {"status": "blocked", "reason": blocked_reason}
        return {"status": "not_required"}
    if review_complete and state.status == "resumable":
        return {"status": "already_resumed"}

    if not review_complete:
        blocked_reason = None
        if not candidate:
            blocked_reason = (
                "Automatic review requires a later broker commission enrichment that exactly "
                "explains the residual; stable account values are insufficient."
            )
        elif not evidence.get("enriched_activity_ids"):
            blocked_reason = "No immutable fill was enriched with a broker-reported commission."
        else:
            activities = db.query(StockPaperBrokerActivity).filter_by(account_id=account.id).all()
            fills = db.query(StockPaperFill).filter_by(account_id=account.id).all()
            if any(fill.fee is None or not fill.cost_known for fill in fills):
                blocked_reason = "At least one persisted fill still has an unknown broker fee."
            elif any(
                str(activity.activity_type or "").upper() != "FILL"
                or not (activity.raw_payload or {}).get("transaction_time")
                and not (activity.raw_payload or {}).get("created_at")
                for activity in activities
            ):
                blocked_reason = "Broker activity history contains an unsupported flow or stale timestamp."
            elif not account.reconciliation_required or account.status != "halted":
                blocked_reason = "The latest residual state is not a clean halted reconciliation state."
            else:
                blocked_reason = None
        if blocked_reason:
            _automatic_recovery_notification(db, reason=blocked_reason, payload={"evidence": evidence})
            _event(
                db,
                action="automatic_accounting_review",
                status="blocked",
                actor=AUTOMATIC_RECOVERY_ACTOR,
                reason=blocked_reason,
                payload={"evidence": evidence},
            )
            db.commit()
            return {"status": "blocked", "reason": blocked_reason}

    # A qualifying residual starts the same cooldown as any other recovery.
    # This never jumps directly from a ledger halt to trading.
    if state.status == "armed":
        enter_stock_recovery(
            db,
            reason="Automatic recovery requires cooldown after broker-complete accounting review",
            actor=AUTOMATIC_RECOVERY_ACTOR,
        )
        state = _state(db)
        account = db.query(StockPaperAccount).filter_by(broker=BROKER).with_for_update().one()

    now = _now()
    digest = _accounting_review_digest(account, db)
    if state.accounting_review_required:
        state.accounting_review_required = False
        state.accounting_reviewed_at = now
        state.accounting_reviewed_by = AUTOMATIC_RECOVERY_ACTOR
        state.accounting_review_reason = (
            "Broker commission enrichment exactly reconciled the prior residual; "
            "all persisted broker fills have known fees and timestamped fill evidence."
        )
        state.accounting_review_digest = digest
        account.unexplained_residual = False
        account.reconciliation_required = False
        account.accounting_verified = True
        account.costs_known = True
        account.status = "reconciled"
        account.halt_reason = None
        ledger_event(
            db,
            account,
            "accounting_review",
            "automated",
            "Automatic broker-complete accounting review resolved the residual",
            {"review_digest": digest, "enriched_activity_ids": evidence.get("enriched_activity_ids", [])},
        )
        _event(
            db,
            action="automatic_accounting_review",
            status="complete",
            actor=AUTOMATIC_RECOVERY_ACTOR,
            reason=state.accounting_review_reason,
            payload={"review_digest": digest, "evidence": evidence},
        )
        write_audit_log(
            db,
            event_type="stock_recovery",
            action="automatic_accounting_review",
            status="complete",
            message=state.accounting_review_reason,
            entity_type="stock_paper",
            payload={"review_digest": digest, "actor": AUTOMATIC_RECOVERY_ACTOR, "evidence": evidence},
        )
        db.commit()

    account = db.query(StockPaperAccount).filter_by(broker=BROKER).with_for_update().one()
    if account.status == "halted":
        if account.reconciliation_required:
            reason = account.halt_reason or "A fresh broker reconciliation is required before automatic resume"
            _automatic_recovery_notification(db, reason=reason, payload={"evidence": evidence})
            db.commit()
            return {"status": "blocked", "reason": reason}
        account.status = "reconciled"
        account.halt_reason = None

    now = _now()
    if state.cooldown_until and _utc(state.cooldown_until) > now:
        return {"status": "waiting", "reason": f"Recovery cooldown remains active until {_utc(state.cooldown_until).isoformat()}"}
    if db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id,
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).count():
        reason = "In-flight orders must be terminal before recovery can resume"
        _halt(account, reason, reconciliation_required=False)
        _automatic_recovery_notification(db, reason=reason, payload={"evidence": evidence})
        _event(db, action="automatic_resume", status="blocked", actor=AUTOMATIC_RECOVERY_ACTOR, reason=reason, payload={"evidence": evidence})
        db.commit()
        return {"status": "blocked", "reason": reason}
    okay, reason = _fresh_monitoring_is_clear(db, now)
    if not okay:
        _halt(account, reason, reconciliation_required=False)
        _record_monitoring_preflight_rejection(
            db,
            actor=AUTOMATIC_RECOVERY_ACTOR,
            reason=reason,
            now=now,
        )
        _automatic_recovery_notification(db, reason=reason, payload={"evidence": evidence})
        _event(db, action="automatic_resume", status="blocked", actor=AUTOMATIC_RECOVERY_ACTOR, reason=reason, payload={"evidence": evidence})
        db.commit()
        return {"status": "blocked", "reason": reason}
    _record_monitoring_preflight_result(
        db,
        actor=AUTOMATIC_RECOVERY_ACTOR,
        status="clear",
        reason=reason,
        now=now,
    )
    operator_reason = (
        "Accounting proof, cooldown, monitoring, reconciliation, and order preflight passed; "
        "an authorized operator must perform the final resume revalidation."
    )
    _event(
        db,
        action="automatic_resume",
        status="awaiting_operator",
        actor=AUTOMATIC_RECOVERY_ACTOR,
        reason=operator_reason,
        payload={"evidence": evidence, "preflight_at": now.isoformat()},
    )
    db.commit()
    return {"status": "awaiting_operator_revalidation", "reason": operator_reason}


def acknowledge_stock_paper_accounting_review(
    db: Session,
    *,
    actor: str,
    reason: str,
    confirm_residual_review: bool,
) -> dict:
    """Record an attempted manual review without approving the residual.

    An operator acknowledgement is not broker evidence. Keeping this endpoint
    as a durable blocked event preserves the API contract for existing clients
    without allowing a human confirmation to clear an unexplained residual.
    """
    reason = reason.strip()
    if not reason:
        raise StockPaperError("Accounting review requires a non-empty reason")
    if not confirm_residual_review:
        raise StockPaperError("Explicit confirmation is required to acknowledge the accounting residual")
    state = _state(db)
    account = db.query(StockPaperAccount).filter_by(broker=BROKER).with_for_update().one_or_none()
    if account is None:
        raise StockPaperError("Stock paper account is not initialized")
    if not state.accounting_review_required or not account.unexplained_residual:
        raise StockPaperError("No unexplained accounting residual is awaiting operator review")
    blocked_reason = (
        "Manual accounting acknowledgement cannot resolve an unexplained residual; "
        "a later immutable broker fill commission enrichment is required."
    )
    now = _now()
    _automatic_recovery_notification(db, reason=blocked_reason, payload={"actor": actor})
    ledger_event(db, account, "accounting_review", "blocked", blocked_reason, {"actor": actor})
    _event(
        db,
        action="accounting_review",
        status="blocked",
        actor=actor,
        reason=blocked_reason,
        payload={"requested_reason": reason, "reviewed_at": now.isoformat()},
    )
    write_audit_log(
        db,
        event_type="stock_recovery",
        action="accounting_review",
        status="blocked",
        message=blocked_reason,
        entity_type="stock_paper",
        payload={"requested_reason": reason, "actor": actor},
    )
    db.commit()
    return recovery_status(db)


def rollback_to_last_known_good(db: Session, *, actor: str, reason: str) -> dict:
    """Restore the recorded last-known-good binding atomically with lifecycle evidence."""
    actor = actor.strip()
    reason = reason.strip()
    if not actor:
        raise StockPaperError("Rollback requires an explicit actor")
    if not reason:
        raise StockPaperError("Rollback requires a non-empty reason")
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
    monitoring_event = db.query(StockPaperRecoveryEvent).filter(
        StockPaperRecoveryEvent.action == "monitoring_preflight",
    ).order_by(
        StockPaperRecoveryEvent.created_at.desc(),
        StockPaperRecoveryEvent.id.desc(),
    ).first()
    monitoring_preflight = (
        {
            "status": monitoring_event.status,
            "actor": monitoring_event.actor,
            "reason": monitoring_event.reason,
            "created_at": monitoring_event.created_at.isoformat() if monitoring_event.created_at else None,
            "payload": monitoring_event.payload,
        }
        if monitoring_event
        else None
    )
    if monitoring_event and monitoring_event.status == "blocked":
        latest_snapshot = db.query(StockMonitoringSnapshot).filter_by(
            monitor_key="stock_continuous_monitor",
        ).order_by(
            StockMonitoringSnapshot.generated_at.desc(),
            StockMonitoringSnapshot.id.desc(),
        ).first()
        checked_at_value = (monitoring_event.payload or {}).get("checked_at")
        try:
            blocked_checked_at = (
                datetime.fromisoformat(str(checked_at_value).replace("Z", "+00:00"))
                if checked_at_value
                else monitoring_event.created_at
            )
            blocked_checked_at = _utc(blocked_checked_at) if blocked_checked_at else None
        except (TypeError, ValueError):
            blocked_checked_at = _utc(monitoring_event.created_at) if monitoring_event.created_at else None
        if latest_snapshot and blocked_checked_at and _utc(latest_snapshot.generated_at) > blocked_checked_at:
            monitoring_clear, monitoring_reason = _fresh_monitoring_is_clear(db, _now())
            if monitoring_clear:
                monitoring_preflight = {
                    "status": "clear",
                    "actor": latest_snapshot.source,
                    "reason": monitoring_reason,
                    "created_at": latest_snapshot.created_at.isoformat() if latest_snapshot.created_at else None,
                    "payload": {
                        "checked_at": _utc(latest_snapshot.generated_at).isoformat(),
                        "evidence_generated_at": _utc(latest_snapshot.generated_at).isoformat(),
                    },
                }
    automatic_review_status = (
        "blocked"
        if state.accounting_review_required
        else "complete"
        if state.accounting_reviewed_by == AUTOMATIC_RECOVERY_ACTOR
        else "not_required"
    )
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
        "accounting_review_required": state.accounting_review_required,
        "accounting_reviewed_at": state.accounting_reviewed_at.isoformat() if state.accounting_reviewed_at else None,
        "accounting_reviewed_by": state.accounting_reviewed_by,
        "accounting_review_reason": state.accounting_review_reason,
        "automatic_review_enabled": True,
        "automatic_review_status": automatic_review_status,
        "last_monitoring_preflight": monitoring_preflight,
        "account_status": account.status if account else "uninitialized",
        "accounting_residual": bool(account.unexplained_residual) if account else False,
        "account_reconciliation_required": bool(account.reconciliation_required) if account else True,
        "account_halt_reason": account.halt_reason if account else "Stock paper account is not initialized",
        "accounting_verified": bool(account.accounting_verified) if account else False,
        "costs_known": bool(account.costs_known) if account else False,
        "events": [{"id": event.id, "action": event.action, "status": event.status, "actor": event.actor, "reason": event.reason, "created_at": event.created_at.isoformat() if event.created_at else None, "payload": event.payload} for event in events],
    }


def recovery_evidence(db: Session) -> dict:
    """Return the non-sensitive broker evidence behind the accounting proof.

    The accounting digest is intentionally calculated from immutable broker
    payloads, but reviewers should not need access to those payloads (which may
    contain broker-specific fields). This projection exposes only identifiers
    and timestamps, never credentials, prices, balances, fees, or P/L.
    """
    state = _state(db, for_update=False)
    account = db.query(StockPaperAccount).filter_by(broker=BROKER).one_or_none()
    if account is None:
        return {
            "status": state.status,
            "accounting_review_digest": state.accounting_review_digest,
            "proof_digest": state.accounting_review_digest,
            "proof_digest_algorithm": "sha256",
            "proof_digest_status": "not_recorded" if not state.accounting_review_digest else "unverifiable",
            "broker_evidence": {
                "account": None,
                "positions": [],
                "fills": [],
                "activities": [],
                "orders": [],
            },
            "reconciliation": None,
            "evidence_identifiers": {
                "broker_account_id": None,
                "broker_activity_ids": [],
                "broker_order_ids": [],
                "client_order_ids": [],
            },
        }

    positions = db.query(StockPaperPosition).filter_by(account_id=account.id).order_by(
        StockPaperPosition.symbol
    ).all()
    fills = db.query(StockPaperFill).filter_by(account_id=account.id).order_by(
        StockPaperFill.broker_activity_id
    ).all()
    activities = db.query(StockPaperBrokerActivity).filter_by(account_id=account.id).order_by(
        StockPaperBrokerActivity.broker_activity_id
    ).all()
    orders = db.query(StockPaperOrder).filter_by(account_id=account.id).order_by(
        StockPaperOrder.client_order_id
    ).all()
    latest_snapshot = db.query(StockPaperEquitySnapshot).filter_by(
        account_id=account.id
    ).order_by(
        StockPaperEquitySnapshot.observed_at.desc(),
        StockPaperEquitySnapshot.id.desc(),
    ).first()

    proof_digest = state.accounting_review_digest
    current_digest = _accounting_review_digest(account, db)
    proof_digest_status = (
        "not_recorded"
        if not proof_digest
        else "current"
        if proof_digest == current_digest
        else "stale"
    )
    broker_order_ids = sorted({
        value for value in (
            [fill.broker_order_id for fill in fills]
            + [order.broker_order_id for order in orders]
        )
        if value
    })
    client_order_ids = sorted({
        order.client_order_id for order in orders if order.client_order_id
    })

    return {
        "status": state.status,
        "accounting_review_required": state.accounting_review_required,
        "accounting_reviewed_at": (
            state.accounting_reviewed_at.isoformat()
            if state.accounting_reviewed_at
            else None
        ),
        "accounting_reviewed_by": state.accounting_reviewed_by,
        "accounting_review_reason": state.accounting_review_reason,
        "accounting_review_digest": proof_digest,
        "proof_digest": proof_digest,
        "proof_digest_algorithm": "sha256",
        "proof_digest_status": proof_digest_status,
        "broker_evidence": {
            "account": {
                "database_id": account.id,
                "broker": account.broker,
                "broker_account_id": account.broker_account_id,
            },
            "positions": [
                {"database_id": row.id, "symbol": row.symbol}
                for row in positions
            ],
            "fills": [
                {
                    "database_id": row.id,
                    "broker_activity_id": row.broker_activity_id,
                    "broker_order_id": row.broker_order_id,
                    "order_database_id": row.order_id,
                    "filled_at": row.filled_at.isoformat() if row.filled_at else None,
                }
                for row in fills
            ],
            "activities": [
                {
                    "database_id": row.id,
                    "broker_activity_id": row.broker_activity_id,
                    "activity_type": row.activity_type,
                    "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
                }
                for row in activities
            ],
            "orders": [
                {
                    "database_id": row.id,
                    "client_order_id": row.client_order_id,
                    "broker_order_id": row.broker_order_id,
                    "status": row.status,
                }
                for row in orders
            ],
        },
        "reconciliation": {
            "snapshot_database_id": latest_snapshot.id if latest_snapshot else None,
            "observed_at": latest_snapshot.observed_at.isoformat() if latest_snapshot else None,
            "source": latest_snapshot.source if latest_snapshot else None,
            "last_reconciled_at": (
                account.last_reconciled_at.isoformat()
                if account.last_reconciled_at
                else None
            ),
        },
        "evidence_identifiers": {
            "broker_account_id": account.broker_account_id,
            "broker_activity_ids": [row.broker_activity_id for row in activities],
            "broker_order_ids": broker_order_ids,
            "client_order_ids": client_order_ids,
        },
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