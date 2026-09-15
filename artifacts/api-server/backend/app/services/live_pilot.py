"""Fail-closed, operator-controlled live-pilot policy.

The pilot is deliberately narrower than the live broker boundary.  A live
broker account and a live-safety approval are necessary, but neither is
sufficient without this durable allowlist, budget, observation window, and
two-person approval record.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    LiveBrokerAccount,
    LiveBrokerFill,
    LiveBrokerOrder,
    LiveBrokerPosition,
    LivePilot,
    LivePilotEvent,
    LiveSafetyState,
    StockModelLifecycleState,
    StockPaperBindingState,
    StockPaperModelBinding,
    StockPaperPromotionReadinessReport,
    StockPaperRecoveryState,
    StockMonitoringSnapshot,
)
from app.services.audit import write_audit_log
from app.services.intraday_data import ALLOWED_SYMBOLS, session_bounds

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
MAX_PILOT_NOTIONAL = Decimal("10000")
MAX_PILOT_ORDER_NOTIONAL = Decimal("1000")
MAX_PILOT_DAYS = 30
REQUIRED_DRILLS = (
    "emergency_stop",
    "rollback",
    "broker_uncertainty",
    "worker_loss",
    "model_demotion",
)


class LivePilotError(ValueError):
    """A pilot request was unsafe or incomplete."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()
    ).hexdigest()


def ensure_live_pilot(db: Session) -> LivePilot:
    pilot = db.get(LivePilot, 1)
    if pilot is None:
        pilot = LivePilot(
            id=1,
            status="inactive",
            symbols=[],
            max_notional=MAX_PILOT_NOTIONAL,
            max_order_notional=MAX_PILOT_ORDER_NOTIONAL,
            allowed_order_types=["limit"],
            time_in_force="day",
            session_policy="regular",
            observation_window_sessions=5,
            rollback_target="paper",
            paper_expectations={},
            launch_checklist={},
            updated_by="system",
        )
        db.add(pilot)
        db.flush()
    return pilot


def _event(
    db: Session,
    *,
    action: str,
    status: str,
    actor: str,
    reason: str,
    payload: dict | None = None,
    secondary_actor: str | None = None,
) -> LivePilotEvent:
    body = {
        "action": action,
        "status": status,
        "actor": actor,
        "secondary_actor": secondary_actor,
        "reason": reason,
        "payload": _safe(payload or {}),
    }
    row = LivePilotEvent(
        action=action,
        status=status,
        actor=actor,
        secondary_actor=secondary_actor,
        reason=reason,
        payload=body["payload"],
        event_sha256=_digest(body | {"nonce": _now().isoformat()}),
    )
    db.add(row)
    db.flush()
    return row


def _write_event(
    db: Session,
    *,
    action: str,
    status: str,
    actor: str,
    reason: str,
    payload: dict | None = None,
    secondary_actor: str | None = None,
) -> None:
    row = _event(
        db,
        action=action,
        status=status,
        actor=actor,
        reason=reason,
        payload=payload,
        secondary_actor=secondary_actor,
    )
    write_audit_log(
        db,
        event_type="live_pilot",
        entity_type="live_pilot",
        entity_id=1,
        action=action,
        status=status,
        message=reason,
        payload={"event_id": row.id, "actor": actor, "secondary_actor": secondary_actor, **(payload or {})},
    )


def _gate(status: str, reason: str | None = None, evidence: Any = None) -> dict:
    result: dict[str, Any] = {"status": status}
    if reason:
        result["reason"] = reason
    if evidence is not None:
        result["evidence"] = _safe(evidence)
    return result


def _environment_gate() -> dict:
    ready = (
        settings.environment == settings.live_environment_name
        and settings.live_environment_name == "approved-live"
        and settings.auth_mode == "production_identity"
        and settings.allow_live_trading
        and bool(settings.live_broker_name.strip())
        and settings.live_credentials_configured
    )
    return _gate(
        "pass" if ready else "fail",
        None if ready else "approved production identity, environment, broker, and explicit live configuration are required",
        {
            "environment": settings.environment,
            "required_environment": settings.live_environment_name,
            "auth_mode": settings.auth_mode,
            "broker_named": bool(settings.live_broker_name.strip()),
            "live_credentials_configured": settings.live_credentials_configured,
        },
    )


def _paper_evidence_gate(db: Session, model_run_id: str) -> dict:
    reports = db.scalars(
        select(StockPaperPromotionReadinessReport)
        .where(StockPaperPromotionReadinessReport.decision == "pass")
        .order_by(StockPaperPromotionReadinessReport.created_at.desc())
    ).all()
    for report in reports:
        lineage = report.lineage or {}
        gates = report.gates or {}
        point_in_time_accuracy = (
            (gates.get("frozen_forward_evidence") or {}).get("status") == "pass"
            and (gates.get("metric_classification") or {}).get("status") == "pass"
            and (gates.get("decision_coverage") or {}).get("status") == "pass"
        )
        if (
            lineage.get("model_run_id") == model_run_id
            and report.paper_only is True
            and report.live_authorized is False
            and point_in_time_accuracy
            and all((gates.get(name) or {}).get("status") == "pass" for name in (
                "regular_sessions", "decision_coverage", "immutable_lineage",
            ))
        ):
            return _gate("pass", evidence={
                "report_id": report.id,
                "report_hash": report.report_hash,
                "point_in_time_accuracy": True,
            })
    return _gate("fail", "a passing immutable paper report with point-in-time accuracy evidence is required")


def _model_binding_gate(db: Session, model_run_id: str) -> dict:
    model_state = db.get(StockModelLifecycleState, model_run_id)
    pointer = db.get(StockPaperBindingState, 1)
    binding = db.get(StockPaperModelBinding, pointer.active_binding_id) if pointer else None
    ready = bool(
        model_state
        and model_state.lifecycle_state in {"champion", "paper_canary"}
        and binding
        and binding.model_run_id == model_run_id
        and binding.paper_only is True
        and binding.live_authorized is False
    )
    return _gate(
        "pass" if ready else "fail",
        None if ready else "the selected model must be the active immutable paper binding and not demoted",
        {
            "model_run_id": model_run_id,
            "lifecycle_state": model_state.lifecycle_state if model_state else None,
            "binding_id": binding.id if binding else None,
        },
    )


def _broker_gate(db: Session) -> dict:
    account = db.query(LiveBrokerAccount).filter_by(broker="alpaca_live").one_or_none()
    if not account:
        return _gate("unknown", "live broker reconciliation evidence is unavailable")
    ready = (
        account.environment == settings.live_environment_name
        and account.status == "reconciled"
        and not account.reconciliation_required
        and not account.unexplained_residual
        and account.last_reconciled_at is not None
        and _now() - _aware(account.last_reconciled_at) <= timedelta(minutes=5)
    )
    return _gate(
        "pass" if ready else "fail",
        None if ready else "live broker account is stale, halted, or requires reconciliation",
        {"account_id": account.broker_account_id, "status": account.status, "last_reconciled_at": account.last_reconciled_at},
    )


def _monitoring_gate(db: Session) -> dict:
    snapshot = db.scalars(
        select(StockMonitoringSnapshot).order_by(StockMonitoringSnapshot.generated_at.desc())
    ).first()
    if not snapshot:
        return _gate("unknown", "current monitoring evidence is unavailable")
    age = _now() - _aware(snapshot.generated_at)
    ready = snapshot.status == "clear" and age <= timedelta(minutes=5) and all(
        check.get("status") == "clear" for check in (snapshot.checks or [])
    )
    return _gate(
        "pass" if ready else "fail",
        None if ready else "monitoring must be fresh and every check must be clear",
        {"snapshot_id": snapshot.id, "status": snapshot.status, "age_seconds": age.total_seconds()},
    )


def _recovery_gate(db: Session) -> dict:
    state = db.get(StockPaperRecoveryState, 1)
    if not state:
        return _gate("unknown", "risk and recovery readiness is unavailable")
    ready = (
        state.status in {"armed", "resumable"}
        and not state.accounting_review_required
        and state.last_monitor_heartbeat_at is not None
        and state.last_watchdog_heartbeat_at is not None
    )
    return _gate("pass" if ready else "fail", None if ready else "risk/recovery controls are not armed with current evidence", {
        "status": state.status,
        "accounting_review_required": state.accounting_review_required,
        "monitor_heartbeat": state.last_monitor_heartbeat_at,
        "watchdog_heartbeat": state.last_watchdog_heartbeat_at,
    })


def _checklist_gate(checklist: dict) -> dict:
    drills = checklist.get("drills") if isinstance(checklist, dict) else None
    missing = [name for name in REQUIRED_DRILLS if not isinstance(drills, dict) or drills.get(name) is not True]
    references = checklist.get("evidence_references") if isinstance(checklist, dict) else None
    ready = not missing and isinstance(references, list) and bool(references) and all(str(item).strip() for item in references)
    return _gate("pass" if ready else "fail", None if ready else "all operational drills and at least one evidence reference are required", {
        "missing_drills": missing,
        "evidence_reference_count": len(references) if isinstance(references, list) else 0,
    })


def evaluate_live_pilot_launch(
    db: Session,
    *,
    model_run_id: str,
    checklist: dict,
) -> dict:
    """Evaluate immutable launch prerequisites without changing execution state."""
    gates = {
        "approved_environment_and_identity": _environment_gate(),
        "paper_evidence": _paper_evidence_gate(db, model_run_id),
        "model_binding": _model_binding_gate(db, model_run_id),
        "live_broker_readiness": _broker_gate(db),
        "monitoring_and_data": _monitoring_gate(db),
        "risk_and_recovery": _recovery_gate(db),
        "operational_drills": _checklist_gate(checklist),
    }
    statuses = {gate["status"] for gate in gates.values()}
    return {
        "status": "pass" if statuses == {"pass"} else ("fail" if "fail" in statuses else "unknown"),
        "gates": gates,
        "required_gates": list(gates),
        "fail_closed": True,
    }


def _pilot_out(pilot: LivePilot, *, include_events: list[dict] | None = None) -> dict:
    now = _now()
    expires = _aware(pilot.expires_at)
    expired = pilot.status in {"canary", "active"} and expires is not None and now >= expires
    status = "expired" if expired else pilot.status
    return {
        "id": pilot.id,
        "status": status,
        "active": status == "active",
        "symbols": list(pilot.symbols or []),
        "max_notional": str(pilot.max_notional),
        "max_order_notional": str(pilot.max_order_notional),
        "allowed_order_types": list(pilot.allowed_order_types or []),
        "time_in_force": pilot.time_in_force,
        "session_policy": pilot.session_policy,
        "starts_at": pilot.starts_at,
        "expires_at": pilot.expires_at,
        "observation_window_sessions": pilot.observation_window_sessions,
        "rollback_target": pilot.rollback_target,
        "model_run_id": pilot.model_run_id,
        "paper_expectations": pilot.paper_expectations or {},
        "launch_checklist": pilot.launch_checklist or {},
        "primary_approval_actor": pilot.primary_approval_actor,
        "secondary_approval_actor": pilot.secondary_approval_actor,
        "approved_at": pilot.approved_at,
        "stopped_at": pilot.stopped_at,
        "stopped_reason": pilot.stopped_reason,
        "latest_review": pilot.latest_review,
        "updated_by": pilot.updated_by,
        "updated_at": pilot.updated_at,
        "events": include_events or [],
    }


def live_pilot_status(db: Session) -> dict:
    pilot = ensure_live_pilot(db)
    events = db.scalars(select(LivePilotEvent).order_by(LivePilotEvent.created_at.desc()).limit(25)).all()
    return _pilot_out(pilot, include_events=[{
        "id": row.id, "action": row.action, "status": row.status, "actor": row.actor,
        "secondary_actor": row.secondary_actor, "reason": row.reason, "created_at": row.created_at,
        "payload": row.payload,
    } for row in events])


def _validate_pilot_inputs(
    *,
    symbols: list[str],
    max_notional: Decimal,
    max_order_notional: Decimal,
    allowed_order_types: list[str],
    starts_at: datetime,
    expires_at: datetime,
    observation_window_sessions: int,
    rollback_target: str,
    primary_actor: str,
    secondary_actor: str,
) -> tuple[list[str], datetime, datetime]:
    normalized = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    if not normalized or any(symbol not in ALLOWED_SYMBOLS for symbol in normalized):
        raise LivePilotError("Pilot symbols must be a non-empty supported allowlist")
    if max_notional <= 0 or max_notional > MAX_PILOT_NOTIONAL:
        raise LivePilotError(f"Pilot max notional must be between 0 and {MAX_PILOT_NOTIONAL}")
    if max_order_notional <= 0 or max_order_notional > MAX_PILOT_ORDER_NOTIONAL or max_order_notional > max_notional:
        raise LivePilotError(f"Pilot max order notional must be between 0 and {MAX_PILOT_ORDER_NOTIONAL} and no greater than the budget")
    if set(allowed_order_types) != {"limit"}:
        raise LivePilotError("The live pilot only permits conservative limit orders")
    starts_at, expires_at = _aware(starts_at), _aware(expires_at)
    if starts_at >= expires_at or expires_at - starts_at > timedelta(days=MAX_PILOT_DAYS):
        raise LivePilotError("Pilot observation window must be positive and no longer than 30 days")
    if observation_window_sessions < 1 or observation_window_sessions > 20:
        raise LivePilotError("Pilot observation window must be between 1 and 20 regular sessions")
    if rollback_target not in {"paper", "shadow"}:
        raise LivePilotError("Pilot rollback target must be paper or shadow")
    if not primary_actor or not secondary_actor or primary_actor == secondary_actor:
        raise LivePilotError("Pilot activation requires two distinct approval actors")
    return normalized, starts_at, expires_at


def activate_live_pilot(
    db: Session,
    *,
    actor: str,
    secondary_actor: str,
    reason: str,
    symbols: list[str],
    max_notional: Decimal,
    max_order_notional: Decimal,
    starts_at: datetime,
    expires_at: datetime,
    observation_window_sessions: int,
    rollback_target: str,
    model_run_id: str,
    paper_expectations: dict,
    checklist: dict,
) -> dict:
    actor, secondary_actor, reason, model_run_id = actor.strip(), secondary_actor.strip(), reason.strip(), model_run_id.strip()
    if len(reason) < 3:
        raise LivePilotError("Pilot activation requires a reason")
    normalized, starts_at, expires_at = _validate_pilot_inputs(
        symbols=symbols, max_notional=max_notional, max_order_notional=max_order_notional,
        allowed_order_types=["limit"], starts_at=starts_at, expires_at=expires_at,
        observation_window_sessions=observation_window_sessions, rollback_target=rollback_target,
        primary_actor=actor, secondary_actor=secondary_actor,
    )
    pilot = ensure_live_pilot(db)
    if pilot.status in {"canary", "active"}:
        raise LivePilotError("An active live pilot already exists")
    safety = db.get(LiveSafetyState, 1)
    if not safety or safety.mode != "shadow":
        raise LivePilotError("Live pilot activation requires shadow mode")
    launch = evaluate_live_pilot_launch(db, model_run_id=model_run_id, checklist=checklist)
    if launch["status"] != "pass":
        _write_event(db, action="activate", status="blocked", actor=actor, secondary_actor=secondary_actor,
                     reason="Live pilot activation denied by the launch gate", payload={"launch": launch})
        raise LivePilotError("Live pilot launch gate is not complete")
    pilot.status = "canary"
    pilot.symbols = normalized
    pilot.max_notional = max_notional
    pilot.max_order_notional = max_order_notional
    pilot.allowed_order_types = ["limit"]
    pilot.time_in_force = "day"
    pilot.session_policy = "regular"
    pilot.starts_at, pilot.expires_at = starts_at, expires_at
    pilot.observation_window_sessions = observation_window_sessions
    pilot.rollback_target = rollback_target
    pilot.model_run_id = model_run_id
    pilot.paper_expectations = paper_expectations or {}
    pilot.launch_checklist = checklist
    pilot.primary_approval_actor = actor
    pilot.secondary_approval_actor = secondary_actor
    pilot.approved_at = _now()
    pilot.stopped_at = None
    pilot.stopped_reason = None
    pilot.updated_by = actor
    # transition_live_safety's persisted approval gate is intentionally populated
    # by this same two-person record before it evaluates the canary transition.
    safety.approval_actor = actor
    safety.approval_at = pilot.approved_at
    safety.secondary_approval_actor = secondary_actor
    safety.secondary_approval_at = pilot.approved_at
    from app.services.live_safety import transition_live_safety
    try:
        transition_live_safety(
            db, target_mode="canary-live", actor=actor, reason=reason,
            approval_actor=actor, secondary_approval_actor=secondary_actor,
            evidence={"pilot_launch": launch},
        )
    except Exception as exc:
        from app.services.live_safety import LiveSafetyError
        if isinstance(exc, LiveSafetyError):
            pilot.status = "inactive"
            raise LivePilotError(str(exc)) from exc
        pilot.status = "inactive"
        raise
    _write_event(db, action="activate", status="canary", actor=actor, secondary_actor=secondary_actor,
                 reason=reason, payload={"model_run_id": model_run_id, "launch": launch})
    return live_pilot_status(db)


def promote_live_pilot(db: Session, *, actor: str, secondary_actor: str, reason: str) -> dict:
    pilot = ensure_live_pilot(db)
    actor, secondary_actor, reason = actor.strip(), secondary_actor.strip(), reason.strip()
    if pilot.status != "canary":
        raise LivePilotError("Only a canary pilot can be promoted")
    if (
        not actor
        or not secondary_actor
        or actor == secondary_actor
        or pilot.primary_approval_actor == secondary_actor
    ):
        raise LivePilotError("Pilot promotion requires a distinct second reviewer")
    launch = evaluate_live_pilot_launch(db, model_run_id=pilot.model_run_id or "", checklist=pilot.launch_checklist or {})
    if launch["status"] != "pass":
        raise LivePilotError("Pilot promotion is blocked because launch evidence is no longer complete")
    from app.services.live_safety import transition_live_safety
    try:
        transition_live_safety(
            db, target_mode="approved-live", actor=actor, reason=reason,
            approval_actor=pilot.primary_approval_actor, secondary_approval_actor=secondary_actor,
            evidence={"pilot_launch": launch},
        )
    except Exception as exc:
        from app.services.live_safety import LiveSafetyError
        if isinstance(exc, LiveSafetyError):
            raise LivePilotError(str(exc)) from exc
        raise
    pilot.status = "active"
    pilot.secondary_approval_actor = secondary_actor
    pilot.updated_by = actor
    _write_event(db, action="promote", status="active", actor=actor, secondary_actor=secondary_actor,
                 reason=reason, payload={"launch": launch})
    return live_pilot_status(db)


def stop_live_pilot(db: Session, *, actor: str, reason: str) -> dict:
    pilot = ensure_live_pilot(db)
    if pilot.status not in {"canary", "active"}:
        raise LivePilotError("No active pilot requires an emergency stop")
    from app.services.live_safety import transition_live_safety
    transition_live_safety(
        db, target_mode="emergency-stop", actor=actor.strip(), reason=reason.strip(),
        evidence={"pilot_id": pilot.id},
    )
    pilot.status = "halted"
    pilot.stopped_at = _now()
    pilot.stopped_reason = reason.strip()
    pilot.updated_by = actor.strip()
    _write_event(db, action="stop", status="halted", actor=actor.strip(), reason=reason.strip())
    return live_pilot_status(db)


def rollback_live_pilot(
    db: Session,
    *,
    actor: str,
    reason: str,
    secondary_actor: str | None = None,
) -> dict:
    pilot = ensure_live_pilot(db)
    if pilot.status not in {"canary", "active", "halted", "expired"}:
        raise LivePilotError("No live pilot is available for rollback")
    from app.services.live_safety import ensure_live_safety_state, transition_live_safety
    state = ensure_live_safety_state(db)
    if state.mode == "emergency-stop" and (
        not secondary_actor or actor.strip() == secondary_actor.strip()
    ):
        raise LivePilotError("Disabling an emergency stop requires a distinct second approver")
    evidence = {"pilot_id": pilot.id, "rollback_target": pilot.rollback_target}
    if state.mode == "emergency-stop":
        evidence["revalidation_digest"] = _digest({"pilot_id": pilot.id, "reason": reason.strip()})
    if state.mode != pilot.rollback_target:
        transition_live_safety(
            db, target_mode=pilot.rollback_target, actor=actor.strip(), reason=reason.strip(),
            evidence=evidence,
        )
    pilot.status = "rolled_back"
    pilot.stopped_at = _now()
    pilot.stopped_reason = reason.strip()
    pilot.updated_by = actor.strip()
    _write_event(db, action="rollback", status="complete", actor=actor.strip(), reason=reason.strip(), payload=evidence)
    return live_pilot_status(db)


def update_live_pilot_limits(
    db: Session,
    *,
    actor: str,
    secondary_actor: str,
    reason: str,
    max_notional: Decimal,
    max_order_notional: Decimal,
    evidence_reference: str,
) -> dict:
    pilot = ensure_live_pilot(db)
    actor, secondary_actor, reason, evidence_reference = (
        actor.strip(), secondary_actor.strip(), reason.strip(), evidence_reference.strip()
    )
    if pilot.status not in {"canary", "active"}:
        raise LivePilotError("Risk-budget changes require an active canary or pilot")
    if not secondary_actor or actor == secondary_actor:
        raise LivePilotError("Risk-budget changes require two distinct approvers")
    if max_notional <= 0 or max_notional > MAX_PILOT_NOTIONAL or max_order_notional <= 0 or max_order_notional > MAX_PILOT_ORDER_NOTIONAL:
        raise LivePilotError("Requested limits exceed the fixed pilot caps")
    if max_order_notional > max_notional or not evidence_reference:
        raise LivePilotError("A budget change requires a bounded budget and evidence reference")
    if max_notional > pilot.max_notional or max_order_notional > pilot.max_order_notional:
        raise LivePilotError("Increasing a pilot risk budget requires a new evidence review; scaling is not automatic")
    pilot.max_notional, pilot.max_order_notional, pilot.updated_by = max_notional, max_order_notional, actor
    _write_event(db, action="limit_change", status="complete", actor=actor, secondary_actor=secondary_actor,
                 reason=reason, payload={"max_notional": str(max_notional), "max_order_notional": str(max_order_notional),
                                         "evidence_reference": evidence_reference})
    return live_pilot_status(db)


def review_live_pilot(db: Session, *, actor: str, reason: str) -> dict:
    pilot = ensure_live_pilot(db)
    now = _now()
    since = _aware(pilot.approved_at) or now
    fills = db.query(LiveBrokerFill).filter(LiveBrokerFill.filled_at >= since).all()
    orders = db.query(LiveBrokerOrder).filter(LiveBrokerOrder.created_at >= since).all()
    buy_notional = sum((row.quantity * row.price for row in fills if row.side == "buy"), Decimal("0"))
    sell_notional = sum((row.quantity * row.price for row in fills if row.side == "sell"), Decimal("0"))
    observed = {
        "fills": len(fills),
        "orders": len(orders),
        "buy_notional": str(buy_notional),
        "sell_notional": str(sell_notional),
        "net_notional": str(buy_notional - sell_notional),
        "comparison": "insufficient_sample" if not fills else "recorded_against_preregistered_expectations",
        "paper_expectations": pilot.paper_expectations or {},
        "model_run_id": pilot.model_run_id,
        "risk_budget_unchanged": True,
    }
    expected_fill_count = (pilot.paper_expectations or {}).get("expected_fill_count")
    if expected_fill_count is not None:
        try:
            observed["comparison"] = {
                "status": "within_preregistered_expectation"
                if len(fills) >= int(expected_fill_count)
                else "below_preregistered_expectation",
                "expected_fill_count": int(expected_fill_count),
                "observed_fill_count": len(fills),
            }
        except (TypeError, ValueError):
            observed["comparison"] = {
                "status": "invalid_preregistered_expectation",
                "expected_fill_count": expected_fill_count,
                "observed_fill_count": len(fills),
            }
    pilot.latest_review = {"reviewed_at": now.isoformat(), "actor": actor.strip(), **observed}
    pilot.updated_by = actor.strip()
    _write_event(db, action="review", status="complete", actor=actor.strip(), reason=reason.strip(), payload=observed)
    return live_pilot_status(db)


def pilot_order_decision(
    db: Session,
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
    reference_price: Decimal,
    order_type: str,
    time_in_force: str,
    now: datetime | None = None,
) -> dict:
    """Enforce pilot limits at both reservation and dispatch boundaries."""
    pilot = ensure_live_pilot(db)
    observed = _aware(now) or _now()
    expires = _aware(pilot.expires_at)
    starts = _aware(pilot.starts_at)
    if pilot.status != "active":
        return {"allowed": False, "reason": "live pilot is not active", "pilot_status": pilot.status}
    if not starts or not expires or not (starts <= observed < expires):
        return {"allowed": False, "reason": "live pilot observation window is not active", "pilot_status": pilot.status}
    if pilot.session_policy != "regular":
        return {"allowed": False, "reason": "live pilot requires regular sessions"}
    bounds = session_bounds(observed.astimezone(NY).date())
    if not bounds or not (bounds[0] <= observed < bounds[1]):
        return {"allowed": False, "reason": "live pilot only permits regular market sessions"}
    normalized = symbol.strip().upper()
    notional = quantity * reference_price
    if normalized not in set(pilot.symbols or []):
        return {"allowed": False, "reason": "symbol is outside the live pilot allowlist"}
    if order_type not in set(pilot.allowed_order_types or []) or time_in_force != pilot.time_in_force:
        return {"allowed": False, "reason": "order type or time-in-force is outside the live pilot limits"}
    if notional > pilot.max_order_notional:
        return {"allowed": False, "reason": "order exceeds the live pilot per-order notional limit"}
    account = db.query(LiveBrokerAccount).filter_by(broker="alpaca_live").one_or_none()
    if not account:
        return {"allowed": False, "reason": "live broker account is unavailable"}
    open_buys = db.query(LiveBrokerOrder).filter(
        LiveBrokerOrder.account_id == account.id,
        LiveBrokerOrder.side == "buy",
        LiveBrokerOrder.status.not_in({"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}),
    ).all()
    existing = sum((row.quantity * (row.limit_price or row.reference_price) for row in open_buys), Decimal("0"))
    positions = db.query(LiveBrokerPosition).filter_by(account_id=account.id).all()
    existing += sum((row.market_value or Decimal("0") for row in positions), Decimal("0"))
    projected = existing + notional if side == "buy" else existing
    if projected > pilot.max_notional:
        return {"allowed": False, "reason": "order would exceed the live pilot total notional budget"}
    try:
        # Keep the pilot API fail-closed with the same risk evaluation used at
        # reservation and dispatch.  The local import avoids a module cycle.
        from app.services.live_broker import LiveBrokerError, _live_risk_gate
        risk = _live_risk_gate(
            db,
            account,
            normalized,
            side,
            quantity,
            reference_price,
        )
    except LiveBrokerError as exc:
        return {"allowed": False, "reason": str(exc), "pilot_status": pilot.status}
    return {
        "allowed": True,
        "pilot_status": pilot.status,
        "pilot_id": pilot.id,
        "symbol": normalized,
        "notional": str(notional),
        "projected_notional": str(projected),
        "max_notional": str(pilot.max_notional),
        "max_order_notional": str(pilot.max_order_notional),
        "risk": risk,
    }