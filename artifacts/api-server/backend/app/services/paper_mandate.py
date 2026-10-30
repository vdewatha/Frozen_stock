"""Standing paper mandate: an admin-approved envelope for automatic approvals.

An admin records one immutable mandate (symbol universe, exposure and loss
caps, schedule, stop/cancel/remaining-position policies, expiry and a session
budget).  While it is valid, the scheduled handoff mints the *exact* per-cycle
``StockPaperRunApproval`` itself, but only when the cycle's runtime bounds fit
inside the mandate.  The minted record goes through the unchanged approval
projection and mismatch checks, so dispatch-time rechecks do not change.

Safety contract:
* Paper only.  A mandate never carries live authority and never widens a
  cycle's bounds; it only authorizes bounds the cycle already derived.
* Mandate rows, revocations and minting links are append-only.
* Expired, revoked, exhausted or non-fitting mandates mint nothing; the
  handoff then defers exactly as it does without a mandate, with the reason.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    StockLearningCycle,
    StockPaperMandate,
    StockPaperMandateApproval,
    StockPaperMandateRevocation,
    StockPaperRunApproval,
    StockPaperTrial,
)
from app.services.audit import write_audit_log
from app.services.notifications import create_notification

MANDATE_ACTOR = "paper_mandate_automation"
REMAINING_POSITION_POLICIES = {"hold", "reduce", "flatten"}
ALLOWED_TRIGGERS = {"scheduled", "manual"}
REQUIRED_STOP_CONDITIONS = ("live_authorized:false", "paper_only")
NUMERIC_EXPOSURE_KEYS = (
    "max_order_notional", "max_symbol_notional", "max_aggregate_notional",
    "max_allocated_notional", "max_risk_per_trade",
)
MAX_MANDATE_DAYS = 180


class PaperMandateError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PaperMandateError(f"{label} must be numeric") from None
    if not number.is_finite() or number <= 0:
        raise PaperMandateError(f"{label} must be greater than zero")
    return number


def _digest(value: Any) -> str:
    from app.services.stock_learning_cycle import _digest as cycle_digest

    return cycle_digest(value)


def create_paper_mandate(
    db: Session,
    *,
    actor: str,
    symbols: Iterable[str],
    exposure_limits: dict,
    loss_limits: dict,
    expires_at: datetime,
    max_sessions: int,
    max_sessions_per_cycle: int = 20,
    schedule: dict | None = None,
    stop_conditions: Iterable[str] = REQUIRED_STOP_CONDITIONS,
    stop_authority: str = "operator_and_system",
    pending_order_treatment: str = "cancel",
    remaining_position_policies: Iterable[str] = ("hold",),
    approving_actors: Iterable[str] = (),
    reason: str,
    execution_provider: str | None = None,
    valid_from: datetime | None = None,
    now: datetime | None = None,
) -> StockPaperMandate:
    now = now or _now()
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise PaperMandateError("A mandate requires a reason")
    provider = execution_provider or settings.active_paper_broker
    if provider != settings.active_paper_broker:
        raise PaperMandateError("Mandate execution provider must be the active paper broker")
    universe = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    if not universe:
        raise PaperMandateError("A mandate requires a non-empty symbol universe")
    exposure: dict[str, str] = {}
    for key, value in (exposure_limits or {}).items():
        if key in NUMERIC_EXPOSURE_KEYS:
            exposure[key] = str(_decimal(value, f"Exposure cap {key}"))
        elif key == "currency":
            exposure[key] = str(value)
        else:
            raise PaperMandateError(f"Unsupported exposure cap {key}")
    if not any(key in exposure for key in NUMERIC_EXPOSURE_KEYS):
        raise PaperMandateError("A mandate requires at least one numeric exposure cap")
    if "max_loss" not in (loss_limits or {}) or not (loss_limits or {}).get("unit"):
        raise PaperMandateError("A mandate requires an explicit loss cap and unit")
    loss = {
        "max_loss": str(_decimal(loss_limits["max_loss"], "Loss cap")),
        "unit": str(loss_limits["unit"]),
        **({"currency": str(loss_limits["currency"])} if loss_limits.get("currency") else {}),
    }
    schedule = dict(schedule or {})
    triggers = sorted({str(item) for item in schedule.get("triggers", ["scheduled"])})
    if not triggers or not set(triggers) <= ALLOWED_TRIGGERS:
        raise PaperMandateError("Mandate schedule triggers must be scheduled and/or manual")
    if schedule.get("timezone", "America/New_York") != "America/New_York":
        raise PaperMandateError("Mandate schedule must use America/New_York regular sessions")
    schedule = {
        "triggers": triggers,
        "timezone": "America/New_York",
        "regular_sessions_only": True,
    }
    stops = sorted({str(item).strip() for item in stop_conditions if str(item).strip()} | set(REQUIRED_STOP_CONDITIONS))
    if stop_authority != "operator_and_system":
        raise PaperMandateError("Mandate stop authority must be operator_and_system")
    if pending_order_treatment != "cancel":
        raise PaperMandateError("Mandate pending-order treatment must be cancel")
    policies = sorted({str(item).strip() for item in remaining_position_policies if str(item).strip()})
    if not policies or not set(policies) <= REMAINING_POSITION_POLICIES:
        raise PaperMandateError("Mandate remaining-position policies must be hold, reduce and/or flatten")
    if not 1 <= int(max_sessions_per_cycle) <= 252 or not 1 <= int(max_sessions) <= 2520:
        raise PaperMandateError("Mandate session limits are out of range")
    if int(max_sessions_per_cycle) > int(max_sessions):
        raise PaperMandateError("Per-cycle sessions cannot exceed the mandate session budget")
    start = _utc(valid_from) if valid_from else now
    end = _utc(expires_at)
    if end <= now or end <= start:
        raise PaperMandateError("Mandate expiry must be in the future")
    if end - start > timedelta(days=MAX_MANDATE_DAYS):
        raise PaperMandateError(f"Mandate cannot last longer than {MAX_MANDATE_DAYS} days")
    actors = sorted({str(value).strip() for value in [*approving_actors, actor] if str(value).strip()})
    payload = {
        "environment": "paper",
        "execution_provider": provider,
        "symbols": universe,
        "exposure_limits": exposure,
        "loss_limits": loss,
        "schedule": schedule,
        "stop_conditions": stops,
        "stop_authority": stop_authority,
        "pending_order_treatment": pending_order_treatment,
        "remaining_position_policies": policies,
        "max_sessions_per_cycle": int(max_sessions_per_cycle),
        "max_sessions": int(max_sessions),
        "valid_from": start.isoformat(),
        "expires_at": end.isoformat(),
        "approving_actors": actors,
        "created_by": actor,
        "reason": reason,
        "paper_only": True,
        "live_authorized": False,
    }
    mandate = StockPaperMandate(
        environment="paper",
        execution_provider=provider,
        symbols=universe,
        exposure_limits=exposure,
        loss_limits=loss,
        schedule=schedule,
        stop_conditions=stops,
        stop_authority=stop_authority,
        pending_order_treatment=pending_order_treatment,
        remaining_position_policies=policies,
        max_sessions_per_cycle=int(max_sessions_per_cycle),
        max_sessions=int(max_sessions),
        valid_from=start,
        expires_at=end,
        approving_actors=actors,
        created_by=actor,
        reason=reason,
        mandate_sha256=_digest({**payload, "recorded_at": now.isoformat()}),
        paper_only=True,
        live_authorized=False,
    )
    db.add(mandate)
    db.flush()
    write_audit_log(
        db, event_type="stock_paper_mandate", action="create", status="approved",
        message="Standing paper mandate recorded", entity_type="stock_paper_mandate",
        entity_id=mandate.id, payload={**payload, "mandate_id": mandate.id, "mandate_sha256": mandate.mandate_sha256},
    )
    return mandate


def revoke_paper_mandate(db: Session, mandate_id: int, *, actor: str, reason: str) -> StockPaperMandateRevocation:
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise PaperMandateError("Revocation requires a reason")
    mandate = db.get(StockPaperMandate, mandate_id)
    if mandate is None:
        raise PaperMandateError("Paper mandate not found")
    existing = db.scalar(select(StockPaperMandateRevocation).where(
        StockPaperMandateRevocation.mandate_id == mandate_id))
    if existing:
        return existing
    row = StockPaperMandateRevocation(mandate_id=mandate_id, revoked_by=actor, reason=reason)
    db.add(row)
    db.flush()
    write_audit_log(
        db, event_type="stock_paper_mandate", action="revoke", status="complete", message=reason,
        entity_type="stock_paper_mandate", entity_id=mandate_id,
        payload={"mandate_id": mandate_id, "revoked_by": actor, "paper_only": True, "live_authorized": False},
    )
    return row


def _revocation(db: Session, mandate_id: int) -> StockPaperMandateRevocation | None:
    return db.scalar(select(StockPaperMandateRevocation).where(
        StockPaperMandateRevocation.mandate_id == mandate_id))


def sessions_used(db: Session, mandate_id: int) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(StockPaperMandateApproval.duration_sessions), 0)).where(
        StockPaperMandateApproval.mandate_id == mandate_id)) or 0)


def mandate_state(db: Session, mandate: StockPaperMandate, *, now: datetime | None = None) -> tuple[str, str | None]:
    now = now or _now()
    revocation = _revocation(db, mandate.id)
    if revocation:
        return "revoked", f"Standing paper mandate {mandate.id} was revoked: {revocation.reason}"
    if now < _utc(mandate.valid_from):
        return "pending", f"Standing paper mandate {mandate.id} is not yet valid"
    if now >= _utc(mandate.expires_at):
        return "expired", f"Standing paper mandate {mandate.id} expired"
    if sessions_used(db, mandate.id) >= mandate.max_sessions:
        return "exhausted", f"Standing paper mandate {mandate.id} session budget is exhausted"
    return "active", None


def current_mandate(db: Session) -> StockPaperMandate | None:
    """The newest recorded mandate (a newer mandate supersedes older ones)."""
    return db.scalar(select(StockPaperMandate).order_by(StockPaperMandate.id.desc()))


def mandate_fit(
    db: Session,
    mandate: StockPaperMandate,
    expected: dict,
    *,
    trigger: str,
    now: datetime | None = None,
) -> list[str]:
    """Return every reason the expected cycle bounds fall outside the mandate."""
    now = now or _now()
    reasons: list[str] = []
    state, state_reason = mandate_state(db, mandate, now=now)
    if state != "active":
        reasons.append(state_reason or f"Standing paper mandate is {state}")
    if expected.get("environment") != "paper" or expected.get("provider_switch") is not None:
        reasons.append("Cycle is not a paper run without a provider switch")
    if expected.get("execution_provider") != mandate.execution_provider:
        reasons.append("Cycle execution provider is outside the mandate")
    outside = sorted(set(expected.get("symbols") or []) - set(mandate.symbols))
    if outside or not expected.get("symbols"):
        reasons.append(f"Cycle symbols are outside the mandate universe: {', '.join(outside) or 'none'}")
    caps = mandate.exposure_limits or {}
    for key, value in (expected.get("exposure_limits") or {}).items():
        if key in NUMERIC_EXPOSURE_KEYS:
            if key not in caps:
                reasons.append(f"Mandate has no {key.replace('_', ' ')} cap")
                continue
            try:
                if Decimal(str(value)) > Decimal(str(caps[key])):
                    reasons.append(f"Cycle {key.replace('_', ' ')} {value} exceeds mandate cap {caps[key]}")
            except (InvalidOperation, TypeError, ValueError):
                reasons.append(f"Cycle {key.replace('_', ' ')} is not numeric")
        elif key in caps and str(caps[key]) != str(value):
            reasons.append(f"Cycle {key} does not match the mandate")
    loss = expected.get("loss_limits") or {}
    mandate_loss = mandate.loss_limits or {}
    if loss.get("unit") != mandate_loss.get("unit"):
        reasons.append("Cycle loss unit does not match the mandate")
    else:
        try:
            if Decimal(str(loss.get("max_loss"))) > Decimal(str(mandate_loss.get("max_loss"))):
                reasons.append(
                    f"Cycle loss limit {loss.get('max_loss')} exceeds mandate cap {mandate_loss.get('max_loss')}"
                )
        except (InvalidOperation, TypeError, ValueError):
            reasons.append("Cycle loss limit is not numeric")
    if mandate_loss.get("currency") and loss.get("currency") not in {None, mandate_loss.get("currency")}:
        reasons.append("Cycle loss currency does not match the mandate")
    duration = int(expected.get("duration_sessions") or 0)
    if duration < 1 or duration > mandate.max_sessions_per_cycle:
        reasons.append(f"Cycle duration {duration} sessions exceeds the mandate per-cycle limit")
    used = sessions_used(db, mandate.id)
    if used + duration > mandate.max_sessions:
        reasons.append(
            f"Cycle needs {duration} sessions but only {max(mandate.max_sessions - used, 0)} remain in the mandate"
        )
    schedule = expected.get("schedule") or {}
    if trigger not in (mandate.schedule or {}).get("triggers", []):
        reasons.append(f"Mandate does not cover {trigger} cycles")
    if schedule.get("timezone") not in {None, "America/New_York"}:
        reasons.append("Cycle schedule is not an America/New_York session")
    if schedule.get("end_at"):
        try:
            end_at = _utc(datetime.fromisoformat(str(schedule["end_at"])))
            start_at = _utc(datetime.fromisoformat(str(schedule["start_at"])))
            if start_at < _utc(mandate.valid_from) or end_at > _utc(mandate.expires_at):
                reasons.append("Cycle session falls outside the mandate validity window")
        except (KeyError, TypeError, ValueError):
            reasons.append("Cycle session boundaries are invalid")
    missing_stops = sorted(set(mandate.stop_conditions or []) - set(expected.get("stop_conditions") or []))
    if missing_stops:
        reasons.append(f"Cycle omits mandate stop conditions: {', '.join(missing_stops)}")
    if expected.get("stop_authority") != mandate.stop_authority:
        reasons.append("Cycle stop authority does not match the mandate")
    if expected.get("pending_order_treatment") != mandate.pending_order_treatment:
        reasons.append("Cycle pending-order treatment does not match the mandate")
    if expected.get("remaining_position_policy") not in (mandate.remaining_position_policies or []):
        reasons.append("Cycle remaining-position policy is outside the mandate")
    return reasons


def mint_paper_run_approval(
    db: Session,
    cycle: StockLearningCycle,
    trial: StockPaperTrial,
    *,
    now: datetime | None = None,
) -> tuple[StockPaperRunApproval | None, str | None]:
    """Mint the exact per-cycle approval from the current mandate when it fits.

    Returns ``(approval, None)`` on success, or ``(None, reason)`` when no
    mandate exists or the cycle does not fit inside it.  The approval carries
    exactly the cycle's derived runtime bounds, never the mandate's caps.
    """
    from app.services.stock_learning_cycle import _event, paper_run_runtime_bounds

    now = now or _now()
    mandate = current_mandate(db)
    if mandate is None:
        return None, "No standing paper mandate is recorded"
    if trial.status != "approved" or trial.source_cycle_id != cycle.cycle_id:
        return None, "Standing mandate requires the cycle's approved, not-yet-started forward trial"
    expected = paper_run_runtime_bounds(cycle, trial)
    reasons = mandate_fit(db, mandate, expected, trigger=cycle.trigger, now=now)
    if reasons:
        return None, "Standing paper mandate does not cover this cycle: " + "; ".join(reasons)
    actors = sorted({*mandate.approving_actors, f"paper_mandate:{mandate.id}"})
    approval_payload = {
        "cycle_id": cycle.cycle_id,
        **expected,
        "approving_actors": actors,
        "paper_only": True,
        "live_authorized": False,
    }
    from app.services.stock_learning_cycle import _digest as cycle_digest

    # Hash exactly like an operator approval so the order-time approval fence
    # (stock_paper_ledger._approved_trial_context) recomputes the same digest.
    # The mandate is identified by the ``paper_mandate:<id>`` actor and the
    # mandate-approval link row, not by extra hashed fields.
    approval_hash = cycle_digest(approval_payload)
    approval = db.scalar(select(StockPaperRunApproval).where(
        StockPaperRunApproval.approval_sha256 == approval_hash))
    if approval is None:
        approval = StockPaperRunApproval(
            cycle_id=cycle.cycle_id,
            environment="paper",
            execution_provider=expected["execution_provider"],
            provider_switch=None,
            symbols=expected["symbols"],
            exposure_limits=expected["exposure_limits"],
            loss_limits=expected["loss_limits"],
            duration_sessions=expected["duration_sessions"],
            schedule=expected["schedule"],
            stop_conditions=expected["stop_conditions"],
            stop_authority=expected["stop_authority"],
            pending_order_treatment=expected["pending_order_treatment"],
            remaining_position_policy=expected["remaining_position_policy"],
            approving_actors=actors,
            approval_sha256=approval_hash,
            paper_only=True,
            live_authorized=False,
        )
        db.add(approval)
        db.flush()
        db.add(StockPaperMandateApproval(
            mandate_id=mandate.id, approval_id=approval.id, cycle_id=cycle.cycle_id,
            duration_sessions=int(expected["duration_sessions"]),
            fit={"checked_at": now.isoformat(), "expected": expected},
        ))
        db.flush()
    # Pin the derived bounds exactly as an operator approval would.
    cycle.evidence = {**(cycle.evidence or {}), "paper_run_bounds": expected}
    trial.lineage = {
        **(trial.lineage or {}),
        "paper_run_bounds": expected,
        "paper_run_approval_sha256": approval_hash,
        "paper_mandate_id": mandate.id,
    }
    _event(
        db, cycle=cycle, stage="preflight", decision="pass", actor=MANDATE_ACTOR,
        reason=f"Paper run approval minted from standing mandate {mandate.id}",
        evidence={"approval_id": approval.id, "mandate_id": mandate.id, "approval_sha256": approval_hash},
    )
    write_audit_log(
        db, event_type="stock_learning_cycle", action="paper_run_approval", status="minted_from_mandate",
        message="Paper run approval minted from the standing paper mandate for the cycle's exact bounds",
        entity_type="stock_learning_cycle", payload={
            "cycle_id": cycle.cycle_id, "approval_id": approval.id, "approval_sha256": approval_hash,
            "mandate_id": mandate.id, "mandate_sha256": mandate.mandate_sha256,
            "approving_actors": actors, "actor": MANDATE_ACTOR,
            "paper_only": True, "live_authorized": False,
        },
    )
    return approval, None


def mandate_withdrawal_reason(
    db: Session, approval_id: int | None, *, now: datetime | None = None,
) -> str | None:
    """Reason a mandate-minted approval may no longer start a trial, else None.

    Only expired, revoked or pending mandates withdraw authority; exhaustion
    counts the approval's own sessions and does not withdraw it.  Approvals
    recorded directly by an operator are never affected.
    """
    mandate = minted_mandate_for_approval(db, approval_id)
    if mandate is None:
        return None
    state, reason = mandate_state(db, mandate, now=now)
    if state in {"expired", "revoked", "pending"}:
        return reason or f"Standing paper mandate {mandate.id} is {state}"
    return None


def minted_mandate_for_approval(db: Session, approval_id: int | None) -> StockPaperMandate | None:
    if not approval_id:
        return None
    link = db.scalar(select(StockPaperMandateApproval).where(StockPaperMandateApproval.approval_id == approval_id))
    return db.get(StockPaperMandate, link.mandate_id) if link else None


def _mandate_record(db: Session, mandate: StockPaperMandate, now: datetime) -> dict:
    state, reason = mandate_state(db, mandate, now=now)
    revocation = _revocation(db, mandate.id)
    used = sessions_used(db, mandate.id)
    return {
        "id": mandate.id,
        "status": state,
        "reason": reason,
        "execution_provider": mandate.execution_provider,
        "symbols": mandate.symbols,
        "exposure_limits": mandate.exposure_limits,
        "loss_limits": mandate.loss_limits,
        "schedule": mandate.schedule,
        "stop_conditions": mandate.stop_conditions,
        "stop_authority": mandate.stop_authority,
        "pending_order_treatment": mandate.pending_order_treatment,
        "remaining_position_policies": mandate.remaining_position_policies,
        "max_sessions_per_cycle": mandate.max_sessions_per_cycle,
        "max_sessions": mandate.max_sessions,
        "sessions_used": used,
        "sessions_remaining": max(mandate.max_sessions - used, 0),
        "valid_from": _utc(mandate.valid_from).isoformat(),
        "expires_at": _utc(mandate.expires_at).isoformat(),
        "approving_actors": mandate.approving_actors,
        "created_by": mandate.created_by,
        "reason_recorded": mandate.reason,
        "mandate_sha256": mandate.mandate_sha256,
        "created_at": mandate.created_at.isoformat() if mandate.created_at else None,
        "revoked_at": revocation.created_at.isoformat() if revocation and revocation.created_at else None,
        "revoked_by": revocation.revoked_by if revocation else None,
        "revocation_reason": revocation.reason if revocation else None,
    }


def _next_mint_preview(db: Session, mandate: StockPaperMandate | None, now: datetime) -> dict | None:
    """Read-only preview of the next approval the scheduler would mint."""
    from app.services.stock_learning_cycle import (
        TERMINAL_STATUSES, paper_run_approval_projection, paper_run_runtime_bounds,
    )

    cycle = db.scalar(select(StockLearningCycle).where(
        StockLearningCycle.trial_id.is_not(None),
        StockLearningCycle.status.not_in(tuple(TERMINAL_STATUSES)),
    ).order_by(StockLearningCycle.created_at.desc()))
    if cycle is None:
        return None
    trial = db.get(StockPaperTrial, cycle.trial_id)
    if trial is None or trial.status != "approved":
        return None
    approval = paper_run_approval_projection(db, cycle)
    expected = paper_run_runtime_bounds(cycle, trial)
    reasons = mandate_fit(db, mandate, expected, trigger=cycle.trigger, now=now) if mandate else [
        "No standing paper mandate is recorded",
    ]
    return {
        "cycle_id": cycle.cycle_id,
        "cycle_status": cycle.status,
        "approval_status": approval["status"],
        "would_mint": approval["status"] != "pass" and not reasons,
        "fits": not reasons,
        "reasons": reasons,
        "bounds": expected,
    }


def paper_mandate_projection(db: Session, *, now: datetime | None = None, history_limit: int = 10) -> dict:
    """Read-only mandate status for the control room; never mutates."""
    now = now or _now()
    mandate = current_mandate(db)
    history = db.scalars(select(StockPaperMandate).order_by(StockPaperMandate.id.desc()).limit(history_limit)).all()
    minted = db.scalars(select(StockPaperMandateApproval).order_by(
        StockPaperMandateApproval.id.desc()).limit(history_limit)).all()
    return {
        "current": _mandate_record(db, mandate, now) if mandate else None,
        "next_auto_minted_approval": _next_mint_preview(db, mandate, now),
        "minted_approvals": [
            {
                "mandate_id": row.mandate_id, "approval_id": row.approval_id, "cycle_id": row.cycle_id,
                "duration_sessions": row.duration_sessions,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in minted
        ],
        "history": [_mandate_record(db, row, now) for row in history],
        "checked_at": now.isoformat(),
        "paper_only": True,
        "live_authorized": False,
    }


def warn_expiring_mandate(db: Session, *, now: datetime | None = None) -> dict:
    """Raise one warning per mandate approaching expiry or session exhaustion."""
    from app.models import Notification

    now = now or _now()
    mandate = current_mandate(db)
    if mandate is None:
        return {"status": "no_mandate"}
    state, reason = mandate_state(db, mandate, now=now)
    remaining = mandate.max_sessions - sessions_used(db, mandate.id)
    warning = None
    if state == "active" and _utc(mandate.expires_at) - now <= timedelta(hours=settings.paper_mandate_expiry_warning_hours):
        warning = ("expiring", f"Standing paper mandate {mandate.id} expires at {_utc(mandate.expires_at).isoformat()}")
    elif state == "active" and remaining < mandate.max_sessions_per_cycle:
        warning = ("low_sessions", f"Standing paper mandate {mandate.id} has {remaining} session(s) left")
    elif state in {"expired", "exhausted"}:
        warning = (state, reason)
    if warning is None:
        return {"status": state, "warned": False}
    title = f"Paper mandate {warning[0].replace('_', ' ')}"
    exists = db.scalar(select(Notification.id).where(
        Notification.source == "paper_mandate", Notification.title == title,
        Notification.entity_id == mandate.id,
    ))
    if exists:
        return {"status": state, "warned": False}
    create_notification(
        db, category="paper_mandate", severity="warning" if state == "active" else "critical",
        source="paper_mandate", title=title,
        message=f"{warning[1]}. Scheduled paper handoffs defer once it lapses until an admin records a new mandate.",
        entity_type="stock_paper_mandate", entity_id=mandate.id,
        payload={"mandate_id": mandate.id, "state": state, "dedupe_key": f"paper_mandate:{mandate.id}:{warning[0]}"},
    )
    return {"status": state, "warned": True, "warning": warning[0]}
