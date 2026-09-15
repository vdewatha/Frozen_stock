"""Shared fail-closed contract for a future live-trading executor.

This module is deliberately not an order executor.  It owns the durable mode
machine and the one readiness decision that every future live route or worker
must call.  The current application remains permanently paper-only because
the broker-account gate has no verified live evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    LiveSafetyEvent,
    LiveSafetyState,
    StockDatasetSnapshot,
    StockModelRegistry,
    StockPaperBindingState,
    StockPaperModelBinding,
    StockPaperRecoveryState,
    StockMonitoringSnapshot,
)
from app.services.audit import write_audit_log

MODES = {"research", "paper", "shadow", "canary-live", "approved-live", "emergency-stop"}
LIVE_MODES = {"canary-live", "approved-live"}
LIVE_RECOVERY_COOLDOWN = timedelta(minutes=5)
ALLOWED_TRANSITIONS = {
    "research": {"paper", "shadow"},
    "paper": {"research", "shadow"},
    "shadow": {"paper", "canary-live"},
    "canary-live": {"shadow", "paper", "approved-live", "emergency-stop"},
    "approved-live": {"canary-live", "paper", "emergency-stop"},
    "emergency-stop": {"research", "paper", "shadow"},
}


class LiveSafetyError(ValueError):
    """A requested live-safety transition was denied."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()
    ).hexdigest()


def _gate(status: str, reason: str | None = None, evidence: Any = None) -> dict:
    result = {"status": status}
    if reason:
        result["reason"] = reason
    if evidence is not None:
        result["evidence"] = _safe(evidence)
    return result


def ensure_live_safety_state(db: Session) -> LiveSafetyState:
    state = db.get(LiveSafetyState, 1)
    if state is None:
        state = LiveSafetyState(
            id=1,
            mode="research",
            gates={},
            lineage={},
            last_reason="Live execution is not approved",
            updated_by="system",
        )
        db.add(state)
        db.flush()
    return state


def _environment_gate() -> dict:
    explicit = (
        bool(settings.allow_live_trading)
        and settings.environment == settings.live_environment_name
        and bool(settings.live_broker_name.strip())
        and settings.live_credentials_configured
    )
    return _gate(
        "pass" if explicit else "fail",
        None if explicit else (
            "live execution requires the explicitly separated approved-live environment, "
            "allow_live_trading=true, and a named live broker"
        ),
        {
            "environment": settings.environment,
            "required_environment": settings.live_environment_name,
            "broker_named": bool(settings.live_broker_name.strip()),
            "live_credentials_configured": settings.live_credentials_configured,
            "configuration_is_not_authorization": True,
        },
    )


def _approval_gate(state: LiveSafetyState) -> dict:
    if not state.approval_actor or not state.approval_at:
        return _gate("unknown", "explicit operator approval is not recorded")
    return _gate(
        "pass",
        evidence={
            "approval_actor": state.approval_actor,
            "approval_at": state.approval_at,
            "secondary_approval_actor": state.secondary_approval_actor,
            "secondary_approval_at": state.secondary_approval_at,
        },
    )


def _broker_account_gate(db: Session) -> dict:
    # This evidence is populated only by the isolated live-broker reconciler.
    # Paper-account rows and configuration flags are deliberately not accepted
    # as proof of a live account.
    from app.models import LiveBrokerAccount

    account = db.query(LiveBrokerAccount).filter_by(broker="alpaca_live").one_or_none()
    if not account:
        return _gate("unknown", "verified live broker and account state is unavailable")
    ready = (
        account.environment == settings.live_environment_name
        and account.status == "reconciled"
        and not account.reconciliation_required
        and not account.unexplained_residual
    )
    return _gate(
        "pass" if ready else "fail",
        None if ready else "live broker account requires reconciliation or is halted",
        {
            "broker": account.broker,
            "account_id": account.broker_account_id,
            "environment": account.environment,
            "status": account.status,
            "reconciliation_required": account.reconciliation_required,
            "unexplained_residual": account.unexplained_residual,
            "last_reconciled_at": account.last_reconciled_at,
        },
    )


def _current_data_gate(db: Session, now: datetime) -> dict:
    snapshot = db.scalars(
        select(StockMonitoringSnapshot).order_by(StockMonitoringSnapshot.generated_at.desc())
    ).first()
    if not snapshot:
        return _gate("unknown", "current live decision-data evidence is unavailable")
    generated = snapshot.generated_at
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    age = (now - generated).total_seconds()
    feed_checks = [
        check for check in (snapshot.checks or [])
        if check.get("metric") in {"intraday_feed_health", "market_data_health", "data_freshness"}
        or any(token in str(check.get("key", "")).lower() for token in ("feed", "freshness", "provenance"))
        or str(check.get("category", "")).lower() in {"market_data", "data"}
    ]
    healthy = snapshot.status == "clear" and age <= 300 and bool(feed_checks) and all(
        check.get("status") == "clear" for check in feed_checks
    )
    return _gate(
        "pass" if healthy else "fail",
        None if healthy else "live data must be current and have a clear feed-health observation",
        {"snapshot_id": snapshot.id, "status": snapshot.status, "age_seconds": round(age, 3)},
    )


def _monitoring_gate(db: Session, now: datetime) -> dict:
    snapshot = db.scalars(
        select(StockMonitoringSnapshot).order_by(StockMonitoringSnapshot.generated_at.desc())
    ).first()
    if not snapshot:
        return _gate("unknown", "monitoring health has not been evidenced")
    generated = snapshot.generated_at
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    age = (now - generated).total_seconds()
    healthy = snapshot.status == "clear" and age <= 300 and bool(snapshot.checks)
    return _gate(
        "pass" if healthy else "fail",
        None if healthy else "monitoring health is stale or not clear",
        {"snapshot_id": snapshot.id, "status": snapshot.status, "age_seconds": round(age, 3)},
    )


def _recovery_gate(db: Session) -> dict:
    state = db.get(StockPaperRecoveryState, 1)
    if not state:
        return _gate("unknown", "recovery readiness has not been initialized")
    ready = (
        state.status in {"armed", "resumable"}
        and not state.accounting_review_required
        and state.last_watchdog_heartbeat_at is not None
        and state.last_monitor_heartbeat_at is not None
    )
    return _gate(
        "pass" if ready else "fail",
        None if ready else "recovery is not armed with current monitor and watchdog evidence",
        {
            "status": state.status,
            "accounting_review_required": state.accounting_review_required,
            "monitor_heartbeat": state.last_monitor_heartbeat_at,
            "watchdog_heartbeat": state.last_watchdog_heartbeat_at,
        },
    )


def _lineage_gate(db: Session) -> dict:
    pointer = db.get(StockPaperBindingState, 1)
    binding = db.get(StockPaperModelBinding, pointer.active_binding_id) if pointer else None
    model = db.get(StockModelRegistry, binding.model_run_id) if binding else None
    snapshot = db.get(StockDatasetSnapshot, binding.snapshot_id) if binding else None
    if not binding or not model or not snapshot:
        return _gate("unknown", "immutable model and dataset lineage is unavailable")
    metadata = model.training_metadata if isinstance(model.training_metadata, dict) else {}
    lifecycle_eligible = model.lifecycle_state in {"paper_canary", "champion"} or (
        not metadata and model.lifecycle_state == "challenger"
    )
    explicitly_eligible = metadata.get("live_eligible") is not False and metadata.get("eligible_for_trading") is not False
    complete = bool(
        binding.binding_sha256
        and model.manifest_sha256
        and snapshot.dataset_sha256
        and binding.model_run_id == model.run_id
        and binding.snapshot_id == snapshot.snapshot_id
        and lifecycle_eligible
        and explicitly_eligible
    )
    return _gate(
        "pass" if complete else "fail",
        None if complete else "immutable model lineage or model eligibility does not permit live execution",
        {
            "binding_id": binding.id,
            "binding_sha256": binding.binding_sha256,
            "model_run_id": model.run_id,
            "model_manifest_sha256": model.manifest_sha256,
            "snapshot_id": snapshot.snapshot_id,
            "dataset_sha256": snapshot.dataset_sha256,
            "lifecycle_state": model.lifecycle_state,
            "live_eligible": metadata.get("live_eligible"),
            "eligible_for_trading": metadata.get("eligible_for_trading"),
        },
    )


def _recovery_control_gate(state: LiveSafetyState, now: datetime) -> dict:
    recovery = (state.gates or {}).get("_recovery", {})
    cooldown_until = recovery.get("cooldown_until")
    if cooldown_until:
        try:
            until = datetime.fromisoformat(str(cooldown_until).replace("Z", "+00:00"))
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if now < until:
                return _gate(
                    "fail",
                    "live recovery cooldown is still active",
                    {
                        "cooldown_until": until,
                        "kill_switch": bool(recovery.get("kill_switch", False)),
                        "last_known_good": recovery.get("last_known_good", {}),
                    },
                )
        except ValueError:
            return _gate("fail", "live recovery cooldown evidence is invalid")
    if state.mode == "emergency-stop" or recovery.get("kill_switch"):
        return _gate(
            "fail",
            "live kill switch is active; explicit revalidation is required",
            {"kill_switch": True, "last_known_good": recovery.get("last_known_good", {})},
        )
    return _gate(
        "pass",
        evidence={
            "cooldown_until": cooldown_until,
            "kill_switch": False,
            "last_known_good": recovery.get("last_known_good", {}),
        },
    )


def evaluate_live_safety(db: Session, *, now: datetime | None = None) -> dict:
    """Return the authoritative, read-only live activation decision."""
    observed_at = now or _now()
    state = ensure_live_safety_state(db)
    from app.services.live_pilot import evaluate_live_pilot_launch, ensure_live_pilot
    pilot = ensure_live_pilot(db)
    pilot_gate = _gate("unknown", "live pilot is not activated")
    if pilot.status in {"canary", "active"} and pilot.model_run_id:
        launch = evaluate_live_pilot_launch(
            db,
            model_run_id=pilot.model_run_id,
            checklist=pilot.launch_checklist or {},
        )
        pilot_gate = _gate(
            launch["status"],
            None if launch["status"] == "pass" else "live pilot launch evidence is incomplete or stale",
            {"pilot_status": pilot.status, "launch": launch},
        )
    gates = {
        "environment_separation": _environment_gate(),
        "operator_approval": _approval_gate(state),
        "broker_account": _broker_account_gate(db),
        "current_data": _current_data_gate(db, observed_at),
        "monitoring_health": _monitoring_gate(db, observed_at),
        "recovery_readiness": _recovery_gate(db),
        "immutable_model_lineage": _lineage_gate(db),
        "recovery_control": _recovery_control_gate(state, observed_at),
        "pilot_launch": pilot_gate,
    }
    all_pass = bool(gates) and all(gate["status"] == "pass" for gate in gates.values())
    eligible = state.mode in LIVE_MODES and all_pass
    return {
        "mode": state.mode,
        "gates": gates,
        "status": "ready" if eligible else "blocked",
        "live_orders_allowed": eligible and state.mode == "approved-live",
        "paper_only": not (eligible and state.mode == "approved-live"),
        "live_authorized": eligible and state.mode == "approved-live",
        "last_reason": state.last_reason,
        "updated_by": state.updated_by,
        "updated_at": state.updated_at,
        "approval_actor": state.approval_actor,
        "secondary_approval_actor": state.secondary_approval_actor,
        "contract": {
            "version": 1,
            "fail_closed": True,
            "implicit_live_activation": False,
            "required_gates": list(gates),
        },
    }


def live_order_decision(db: Session) -> dict:
    """The single policy check a future live order route/worker must call."""
    result = evaluate_live_safety(db)
    if not result["live_orders_allowed"]:
        result["reason"] = "live order execution is blocked by the live safety contract"
    return result


def _record_event(
    db: Session,
    *,
    state: LiveSafetyState,
    to_mode: str,
    action: str,
    actor: str,
    reason: str,
    gates: dict,
    evidence: dict,
    approval_actor: str | None = None,
    secondary_approval_actor: str | None = None,
) -> LiveSafetyEvent:
    payload = _safe(evidence)
    identity = {
        "from_mode": state.mode,
        "to_mode": to_mode,
        "action": action,
        "actor": actor,
        "approval_actor": approval_actor,
        "secondary_approval_actor": secondary_approval_actor,
        "reason": reason,
        "gates": _safe(gates),
        "evidence": payload,
    }
    row = LiveSafetyEvent(
        from_mode=state.mode,
        to_mode=to_mode,
        action=action,
        actor=actor,
        approval_actor=approval_actor,
        secondary_approval_actor=secondary_approval_actor,
        reason=reason,
        gates=_safe(gates),
        evidence=payload,
        event_sha256=_digest(identity),
    )
    db.add(row)
    db.flush()
    return row


def transition_live_safety(
    db: Session,
    *,
    target_mode: str,
    actor: str,
    reason: str,
    approval_actor: str | None = None,
    secondary_approval_actor: str | None = None,
    evidence: dict | None = None,
    correlation_id: str | None = None,
    authorization: dict | None = None,
) -> dict:
    """Apply one explicit transition, recording denied attempts as evidence."""
    reason = reason.strip()
    if target_mode not in MODES:
        raise LiveSafetyError("Unknown live-safety mode")
    if len(reason) < 3:
        raise LiveSafetyError("Live-safety transitions require a reason")
    state = ensure_live_safety_state(db)
    prior_mode = state.mode
    gates = evaluate_live_safety(db)["gates"]
    allowed = target_mode in ALLOWED_TRANSITIONS.get(prior_mode, set())
    approval_ok = target_mode not in LIVE_MODES or bool(approval_actor)
    if target_mode == "approved-live":
        approval_ok = (
            approval_ok
            and bool(secondary_approval_actor)
            and approval_actor != secondary_approval_actor
            and prior_mode == "canary-live"
        )
    if target_mode in LIVE_MODES:
        allowed = allowed and approval_ok and all(item["status"] == "pass" for item in gates.values())
    if target_mode == "emergency-stop":
        allowed = allowed and prior_mode in LIVE_MODES
    if target_mode in {"research", "paper", "shadow"} and prior_mode == "emergency-stop":
        allowed = allowed and bool(evidence and evidence.get("revalidation_digest"))
    if not allowed:
        denial_reason = (
            f"Transition {prior_mode} -> {target_mode} denied by the live safety contract"
        )
        event = _record_event(
            db, state=state, to_mode=target_mode, action="transition_denied",
            actor=actor, reason=denial_reason, gates=gates, evidence=evidence or {},
            approval_actor=approval_actor, secondary_approval_actor=secondary_approval_actor,
        )
        write_audit_log(
            db, event_type="live_safety", entity_type="live_safety_state", entity_id=state.id,
            action="transition_denied", status="blocked", message=denial_reason,
            payload={
                "event_id": event.id, "from_mode": prior_mode, "to_mode": target_mode,
                "actor": actor, "correlation_id": correlation_id, "authorization": authorization or {},
            },
        )
        raise LiveSafetyError(denial_reason)

    state.mode = target_mode
    state.approval_actor = approval_actor if target_mode in LIVE_MODES else None
    state.approval_at = _now() if target_mode in LIVE_MODES else None
    state.secondary_approval_actor = secondary_approval_actor if target_mode == "approved-live" else None
    state.secondary_approval_at = _now() if target_mode == "approved-live" else None
    recovery = dict((state.gates or {}).get("_recovery", {}))
    if target_mode == "emergency-stop":
        recovery.update({
            "kill_switch": True,
            "cooldown_until": (_now() + LIVE_RECOVERY_COOLDOWN).isoformat(),
            "last_known_good": _safe(state.lineage),
            "stopped_by": actor,
            "stopped_reason": reason,
        })
    elif target_mode in LIVE_MODES:
        recovery.update({
            "kill_switch": False,
            "last_known_good": _safe(gates.get("immutable_model_lineage", {}).get("evidence", {})),
            "revalidated_by": actor,
            "revalidated_at": _now().isoformat(),
        })
    elif prior_mode == "emergency-stop" and evidence and evidence.get("revalidation_digest"):
        # Revalidation clears the kill latch, but deliberately preserves the
        # cooldown timestamp established by the stop.
        recovery.update({
            "kill_switch": False,
            "revalidated_by": actor,
            "revalidated_at": _now().isoformat(),
            "revalidation_digest": evidence["revalidation_digest"],
        })
    state.gates = {**_safe(gates), "_recovery": _safe(recovery)}
    state.lineage = _safe(gates.get("immutable_model_lineage", {}).get("evidence", {}))
    state.last_reason = reason
    state.updated_by = actor
    event = _record_event(
        db, state=LiveSafetyState(
            mode=prior_mode, id=state.id,
        ), to_mode=target_mode, action="transition", actor=actor, reason=reason,
        gates=gates, evidence=evidence or {}, approval_actor=approval_actor,
        secondary_approval_actor=secondary_approval_actor,
    )
    write_audit_log(
        db, event_type="live_safety", entity_type="live_safety_state", entity_id=state.id,
        action="transition", status="complete", message=reason,
        payload={
            "event_id": event.id, "from_mode": prior_mode, "to_mode": target_mode,
            "actor": actor, "correlation_id": correlation_id, "authorization": authorization or {},
        },
    )
    return evaluate_live_safety(db)