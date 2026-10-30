"""Standing two-person live mandate and the scheduler that operates inside it.

Two distinct admin identities approve one immutable mandate: a symbol
allowlist, total and per-order budgets, limit-only orders, a maximum pilot
window per renewal, a hard outer expiry, a cumulative live-loss cap, a
drawdown cap, a renewal lead time, the observation window and the rollback
target.  While the newest mandate is valid, ``run_live_mandate_cycle`` may:

* mint a bounded canary pilot from ``shadow`` once every launch gate is green;
* promote canary -> approved-live after the observation sessions complete
  cleanly, through the unchanged manual promotion path;
* renew the pilot window before it expires, re-running the full launch
  evaluation every time.

Authority is always attributed to the two mandate approvers; the automation
identity is recorded only as the executor.  Any non-green renewal lets live
authority lapse to the rollback target and sends an external alert.  Revocation
triggers (cumulative loss, drawdown, emergency stop, broker ambiguity,
reconciliation failure, compliance blocked) suspend the mandate and end the
pilot; a suspended mandate needs two distinct humans to reinstate, and an
emergency stop still needs the existing two-person reset first.

The mandate never changes after approval.  Amendment creates a superseding
row; every other state change is an append-only ``LiveMandateEvent``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    LiveBrokerAccount,
    LiveBrokerAccountSnapshot,
    LiveBrokerOrder,
    LiveBrokerPosition,
    LiveMandate,
    LiveMandateEvent,
)
from app.services.audit import write_audit_log
from app.services.compliance_readiness import evaluate_compliance_readiness
from app.services.exchange_sessions import session_bounds
from app.services.intraday_data import ALLOWED_SYMBOLS
from app.services.notifications import create_notification

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
MANDATE_ACTOR = "live_mandate_automation"
MAX_MANDATE_DAYS = 180
MAX_RENEWAL_LEAD_HOURS = 72
MAX_DRAWDOWN_CAP = Decimal("0.5")
MIN_RENEWAL_WINDOW = timedelta(hours=1)
LIVE_PILOT_STATUSES = {"canary", "active"}
# Market-risk and broker-truth triggers latch the live kill switch; the
# existing two-person emergency-stop reset is then required.  A compliance
# block has no open market anomaly, so it lapses to the rollback target.
EMERGENCY_TRIGGERS = {"cumulative_loss", "drawdown", "broker_ambiguity", "reconciliation_failure"}
EVALUATION_ACTIONS = (
    "activate", "activation_blocked", "renew", "renewal_blocked", "lapse",
    "auto_promote", "promotion_held",
)
REVOCATION_ACTIONS = ("revoke", "suspend", "reinstate", "compliance_blocked", "lapse")


class LiveMandateError(ValueError):
    """A live-mandate request was unsafe or incomplete."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _safe(value: Any) -> Any:
    from app.services.live_pilot import _safe as pilot_safe

    return pilot_safe(value)


def _digest(value: Any) -> str:
    from app.services.live_pilot import _digest as pilot_digest

    return pilot_digest(value)


def _decimal(value: Any, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise LiveMandateError(f"{label} must be numeric") from None
    if not number.is_finite() or number <= 0:
        raise LiveMandateError(f"{label} must be greater than zero")
    return number


# ---------------------------------------------------------------- events


def _mandate_event(
    db: Session,
    mandate: LiveMandate,
    *,
    action: str,
    status: str,
    actor: str,
    reason: str,
    payload: dict | None = None,
    secondary_actor: str | None = None,
) -> LiveMandateEvent:
    body = {
        "mandate_id": mandate.id,
        "mandate_sha256": mandate.mandate_sha256,
        "action": action,
        "status": status,
        "actor": actor,
        "secondary_actor": secondary_actor,
        "reason": reason,
        "payload": _safe(payload or {}),
    }
    row = LiveMandateEvent(
        mandate_id=mandate.id,
        action=action,
        status=status,
        actor=actor,
        secondary_actor=secondary_actor,
        reason=reason,
        payload=body["payload"],
        event_sha256=_digest(body | {"nonce": f"{_now().isoformat()}:{uuid.uuid4().hex}"}),
    )
    db.add(row)
    db.flush()
    write_audit_log(
        db,
        event_type="live_mandate",
        entity_type="live_mandate",
        entity_id=mandate.id,
        action=action,
        status=status,
        message=reason,
        payload={
            "event_id": row.id,
            "mandate_sha256": mandate.mandate_sha256,
            "actor": actor,
            "secondary_actor": secondary_actor,
            "authority": _authority(mandate),
        },
    )
    return row


def _events(db: Session, mandate_id: int, actions: tuple[str, ...] | None = None) -> list[LiveMandateEvent]:
    query = select(LiveMandateEvent).where(LiveMandateEvent.mandate_id == mandate_id)
    if actions:
        query = query.where(LiveMandateEvent.action.in_(actions))
    return list(db.scalars(query.order_by(LiveMandateEvent.id)).all())


def _latest_event(db: Session, mandate_id: int, actions: tuple[str, ...] | None = None) -> LiveMandateEvent | None:
    query = select(LiveMandateEvent).where(LiveMandateEvent.mandate_id == mandate_id)
    if actions:
        query = query.where(LiveMandateEvent.action.in_(actions))
    return db.scalars(query.order_by(LiveMandateEvent.id.desc()).limit(1)).first()


def _alert(db: Session, mandate: LiveMandate, *, severity: str, title: str, message: str, payload: dict | None = None) -> None:
    # create_notification queues warning/critical rows for external delivery;
    # the delivery job sends them outside this transaction.
    create_notification(
        db,
        category="live_mandate",
        severity=severity,
        source="live_mandate",
        title=title,
        message=message,
        entity_type="live_mandate",
        entity_id=mandate.id,
        payload=_safe({"mandate_id": mandate.id, **(payload or {})}),
    )


def _authority(mandate: LiveMandate) -> dict:
    return {
        "mandate_id": mandate.id,
        "mandate_sha256": mandate.mandate_sha256,
        "primary_approval_actor": mandate.primary_approval_actor,
        "secondary_approval_actor": mandate.secondary_approval_actor,
        "executed_by": MANDATE_ACTOR,
    }


# ---------------------------------------------------------------- state


def current_live_mandate(db: Session) -> LiveMandate | None:
    return db.scalars(select(LiveMandate).order_by(LiveMandate.id.desc()).limit(1)).first()


def _root(db: Session, mandate: LiveMandate) -> LiveMandate:
    seen: set[int] = set()
    row = mandate
    while row.supersedes_mandate_id and row.id not in seen:
        seen.add(row.id)
        parent = db.get(LiveMandate, row.supersedes_mandate_id)
        if parent is None:
            break
        row = parent
    return row


def mandate_state(db: Session, mandate: LiveMandate, *, now: datetime | None = None) -> tuple[str, str | None]:
    now = _aware(now) or _now()
    actions = [row.action for row in _events(db, mandate.id, ("revoke", "suspend", "reinstate"))]
    if "revoke" in actions:
        return "revoked", "the mandate was revoked by two approvers"
    successor = db.scalars(select(LiveMandate.id).where(LiveMandate.supersedes_mandate_id == mandate.id)).first()
    if successor:
        return "superseded", f"amended by mandate {successor}"
    suspension = [action for action in actions if action in {"suspend", "reinstate"}]
    if suspension and suspension[-1] == "suspend":
        latest = _latest_event(db, mandate.id, ("suspend",))
        return "suspended", latest.reason if latest else "the mandate was suspended"
    if now < _aware(mandate.valid_from):
        return "pending", "the mandate is not valid yet"
    if now >= _aware(mandate.expires_at):
        return "expired", "the mandate outer expiry has passed"
    return "active", None


def mandate_allows_orders(db: Session, mandate_id: int | None, *, now: datetime | None = None) -> dict:
    """Order-time fence: a mandate-minted pilot may trade only while its mandate is active."""
    if mandate_id is None:
        return {"allowed": True}
    mandate = db.get(LiveMandate, mandate_id)
    if mandate is None:
        return {"allowed": False, "reason": "the live mandate behind this pilot is unavailable"}
    state, reason = mandate_state(db, mandate, now=now)
    if state != "active":
        return {"allowed": False, "reason": f"standing live mandate is {state}: {reason}"}
    return {"allowed": True, "mandate_id": mandate.id}


# ---------------------------------------------------------------- evidence


def _account(db: Session) -> LiveBrokerAccount | None:
    return db.query(LiveBrokerAccount).filter_by(broker="alpaca_live").one_or_none()


def cumulative_live_loss(db: Session, mandate: LiveMandate) -> dict:
    """Reconciled equity decline since the mandate chain began.

    The baseline is the first reconciled snapshot at or after the first
    mandate in the amendment chain, so an amendment cannot silently reset the
    loss.  A withdrawal also reads as a loss, which is the safe direction.
    """
    cap = Decimal(str(mandate.max_cumulative_loss))
    account = _account(db)
    if account is None:
        return {"status": "unknown", "reason": "no reconciled live account exists", "cap": str(cap)}
    root = _root(db, mandate)
    baseline = db.scalars(
        select(LiveBrokerAccountSnapshot)
        .where(
            LiveBrokerAccountSnapshot.account_id == account.id,
            LiveBrokerAccountSnapshot.observed_at >= _aware(root.created_at),
        )
        .order_by(LiveBrokerAccountSnapshot.observed_at, LiveBrokerAccountSnapshot.id)
        .limit(1)
    ).first()
    current = db.scalars(
        select(LiveBrokerAccountSnapshot)
        .where(LiveBrokerAccountSnapshot.account_id == account.id)
        .order_by(LiveBrokerAccountSnapshot.observed_at.desc(), LiveBrokerAccountSnapshot.id.desc())
        .limit(1)
    ).first()
    if baseline is None or current is None:
        return {"status": "unknown", "reason": "no reconciled equity baseline exists for this mandate", "cap": str(cap)}
    loss = max(Decimal("0"), Decimal(str(baseline.equity)) - Decimal(str(current.equity)))
    return {
        "status": "measured",
        "loss": str(loss),
        "cap": str(cap),
        "breached": loss >= cap,
        "baseline_snapshot_id": baseline.id,
        "current_snapshot_id": current.id,
        "baseline_equity": str(baseline.equity),
        "current_equity": str(current.equity),
    }


def _peak_equity(db: Session, account_id: int, since: datetime) -> Decimal | None:
    value = db.scalar(
        select(func.max(LiveBrokerAccountSnapshot.equity)).where(
            LiveBrokerAccountSnapshot.account_id == account_id,
            LiveBrokerAccountSnapshot.observed_at >= since,
        )
    )
    return Decimal(str(value)) if value is not None else None


def live_drawdown(db: Session, mandate: LiveMandate, *, now: datetime | None = None) -> dict:
    """Drawdown derived from durable reconciled equity snapshots.

    ``strategy_drawdown`` is the decline of the latest reconciled equity from
    its peak since the original mandate in the amendment chain; ``daily_drawdown``
    uses the peak since the start of the current New York trading day.  If an
    upstream risk process also records ``risk_metrics`` on the account, the more
    conservative (larger) value wins.  No reconciled snapshot means ``unknown``.
    """
    cap = Decimal(str(mandate.max_drawdown))
    now = _aware(now) or _now()
    account = _account(db)
    if account is None:
        return {"status": "unknown", "reason": "no reconciled live account exists", "cap": str(cap)}
    current = db.scalars(
        select(LiveBrokerAccountSnapshot)
        .where(LiveBrokerAccountSnapshot.account_id == account.id)
        .order_by(LiveBrokerAccountSnapshot.observed_at.desc(), LiveBrokerAccountSnapshot.id.desc())
        .limit(1)
    ).first()
    root = _root(db, mandate)
    strategy_peak = _peak_equity(db, account.id, _aware(root.created_at))
    if current is None or strategy_peak is None:
        return {"status": "unknown", "reason": "no reconciled equity snapshot exists for this mandate", "cap": str(cap)}
    current_equity = Decimal(str(current.equity))
    day_start = datetime.combine(now.astimezone(NY).date(), datetime.min.time(), tzinfo=NY).astimezone(UTC)
    daily_peak = _peak_equity(db, account.id, max(day_start, _aware(root.created_at))) or current_equity

    def _fraction(peak: Decimal) -> Decimal:
        if peak <= 0:
            return Decimal("1")
        return max(Decimal("0"), (peak - current_equity) / peak)

    values = {"daily_drawdown": _fraction(daily_peak), "strategy_drawdown": _fraction(strategy_peak)}
    metrics = (account.raw_payload or {}).get("risk_metrics") if isinstance(account.raw_payload, dict) else None
    if isinstance(metrics, dict):
        for name in values:
            if metrics.get(name) is None:
                continue
            try:
                parsed = Decimal(str(metrics.get(name)))
            except (InvalidOperation, TypeError, ValueError):
                return {"status": "unknown", "reason": f"live {name} evidence is invalid", "cap": str(cap)}
            if not parsed.is_finite() or parsed < 0:
                return {"status": "unknown", "reason": f"live {name} evidence is invalid", "cap": str(cap)}
            values[name] = max(values[name], parsed)
    worst = max(values.values())
    return {
        "status": "measured",
        "source": "reconciled_equity_snapshots",
        "current_snapshot_id": current.id,
        "current_equity": str(current_equity),
        "strategy_peak_equity": str(strategy_peak),
        "daily_peak_equity": str(daily_peak),
        "daily_drawdown": str(values["daily_drawdown"].quantize(Decimal("0.000001"))),
        "strategy_drawdown": str(values["strategy_drawdown"].quantize(Decimal("0.000001"))),
        "worst": str(worst.quantize(Decimal("0.000001"))),
        "cap": str(cap),
        "breached": worst >= cap,
    }


def evaluate_revocation_triggers(
    db: Session,
    mandate: LiveMandate,
    *,
    now: datetime | None = None,
    include_compliance_block_events: bool = True,
) -> list[dict]:
    """Read-only evaluation of every condition that must end mandate authority."""
    from app.services.live_pilot import ensure_live_pilot
    from app.services.live_safety import ensure_live_safety_state

    triggers: list[dict] = []
    safety = ensure_live_safety_state(db)
    pilot = ensure_live_pilot(db)
    if safety.mode == "emergency-stop" or (pilot.status == "halted" and pilot.mandate_id is not None):
        triggers.append({"trigger": "emergency_stop", "reason": "the live emergency stop is engaged"})
    loss = cumulative_live_loss(db, mandate)
    if loss.get("breached"):
        triggers.append({"trigger": "cumulative_loss", "reason": "cumulative live loss reached the mandate cap", "evidence": loss})
    drawdown = live_drawdown(db, mandate, now=now)
    if drawdown.get("breached"):
        triggers.append({"trigger": "drawdown", "reason": "live drawdown reached the mandate cap", "evidence": drawdown})
    account = _account(db)
    if account is not None:
        uncertain = [
            row.id for row in db.query(LiveBrokerOrder).filter(
                LiveBrokerOrder.account_id == account.id,
            ).filter(
                (LiveBrokerOrder.uncertain_submission.is_(True)) | (LiveBrokerOrder.status == "unknown")
            ).all()
        ]
        if uncertain:
            triggers.append({
                "trigger": "broker_ambiguity",
                "reason": "a live order outcome is uncertain at the broker",
                "evidence": {"order_ids": uncertain[:20], "count": len(uncertain)},
            })
        if account.status == "halted" or account.reconciliation_required or account.unexplained_residual:
            triggers.append({
                "trigger": "reconciliation_failure",
                "reason": "live broker reconciliation failed or left a residual",
                "evidence": {
                    "status": account.status,
                    "reconciliation_required": account.reconciliation_required,
                    "unexplained_residual": account.unexplained_residual,
                },
            })
    compliance = evaluate_compliance_readiness(
        (mandate.launch_checklist or {}).get("compliance_readiness")
    )
    if compliance["status"] != "pass":
        triggers.append({
            "trigger": "compliance_blocked",
            "reason": "the mandate compliance record no longer passes the readiness rules",
            "evidence": {"unresolved_count": len(compliance["unresolved_items"])},
        })
    elif include_compliance_block_events:
        block = _latest_event(db, mandate.id, ("compliance_blocked",))
        reinstate = _latest_event(db, mandate.id, ("reinstate",))
        if block and (reinstate is None or reinstate.id < block.id):
            triggers.append({
                "trigger": "compliance_blocked",
                "reason": "compliance status was recorded as blocked",
                "evidence": {"event_id": block.id},
            })
    return triggers


def evaluate_mandate_renewal(db: Session, mandate: LiveMandate, *, now: datetime | None = None) -> dict:
    """The full launch evaluation plus mandate-level bounds; read-only."""
    from app.services.live_pilot import _gate, evaluate_live_pilot_launch

    now = _aware(now) or _now()
    launch = evaluate_live_pilot_launch(
        db, model_run_id=mandate.model_run_id, checklist=mandate.launch_checklist or {},
    )
    remaining = _aware(mandate.expires_at) - now
    window_gate = _gate(
        "pass" if remaining >= MIN_RENEWAL_WINDOW else "fail",
        None if remaining >= MIN_RENEWAL_WINDOW else "the mandate outer expiry leaves no room for another pilot window",
        {"mandate_expires_at": mandate.expires_at, "remaining_seconds": int(remaining.total_seconds())},
    )
    loss = cumulative_live_loss(db, mandate)
    loss_gate = _gate(
        "unknown" if loss["status"] != "measured" else ("fail" if loss["breached"] else "pass"),
        loss.get("reason") or ("cumulative live loss reached the mandate cap" if loss.get("breached") else None),
        loss,
    )
    drawdown = live_drawdown(db, mandate, now=now)
    drawdown_gate = _gate(
        "unknown" if drawdown["status"] != "measured" else ("fail" if drawdown["breached"] else "pass"),
        drawdown.get("reason") or ("live drawdown reached the mandate cap" if drawdown.get("breached") else None),
        drawdown,
    )
    gates = {
        **launch["gates"],
        "mandate_window": window_gate,
        "mandate_cumulative_loss": loss_gate,
        "mandate_drawdown": drawdown_gate,
    }
    statuses = {gate["status"] for gate in gates.values()}
    return {
        "status": "pass" if statuses == {"pass"} else ("fail" if "fail" in statuses else "unknown"),
        "gates": gates,
        "required_gates": list(gates),
        "evaluated_at": now.isoformat(),
        "fail_closed": True,
    }


def _gate_digest(evaluation: dict) -> str:
    return _digest({name: gate.get("status") for name, gate in sorted((evaluation.get("gates") or {}).items())})


def _failing(evaluation: dict) -> list[str]:
    return sorted(name for name, gate in (evaluation.get("gates") or {}).items() if gate.get("status") != "pass")


def _record_once(
    db: Session,
    mandate: LiveMandate,
    *,
    action: str,
    reason: str,
    evaluation: dict | None = None,
    extra: dict | None = None,
) -> bool:
    """Write a repeated non-terminal evaluation only when its gate shape changes."""
    digest = _gate_digest(evaluation or {}) + ":" + _digest(extra or {})
    latest = _latest_event(db, mandate.id)
    if latest and latest.action == action and (latest.payload or {}).get("dedupe") == digest:
        return False
    _mandate_event(
        db, mandate, action=action, status="blocked", actor=MANDATE_ACTOR, reason=reason,
        payload={"dedupe": digest, "failing_gates": _failing(evaluation or {}), "evaluation": evaluation, **(extra or {})},
    )
    return True


def completed_sessions_since(start: datetime | None, now: datetime) -> int:
    if start is None:
        return 0
    start, now = _aware(start), _aware(now)
    day: date = start.astimezone(NY).date()
    end = now.astimezone(NY).date()
    count = 0
    while day <= end and (day - start.astimezone(NY).date()).days <= 400:
        bounds = session_bounds(day)
        if bounds and _aware(bounds[0]) >= start and _aware(bounds[1]) <= now:
            count += 1
        day += timedelta(days=1)
    return count


def _canary_metrics(db: Session, mandate: LiveMandate, since: datetime | None) -> dict:
    account = _account(db)
    if account is None:
        return {"status": "unknown", "reason": "no reconciled live account exists"}
    query = db.query(LiveBrokerOrder).filter(LiveBrokerOrder.account_id == account.id)
    if since is not None:
        query = query.filter(LiveBrokerOrder.created_at >= since)
    orders = query.all()
    rejected = [row.id for row in orders if row.status == "rejected"]
    uncertain = [row.id for row in orders if row.uncertain_submission or row.status == "unknown"]
    positions = db.query(LiveBrokerPosition).filter_by(account_id=account.id).all()
    exposure = sum((abs(row.market_value or Decimal("0")) for row in positions), Decimal("0"))
    outside = []
    if rejected:
        outside.append("rejected live orders were observed during the canary")
    if uncertain:
        outside.append("uncertain live orders were observed during the canary")
    if exposure > Decimal(str(mandate.max_notional)):
        outside.append("live exposure exceeded the mandate budget")
    return {
        "status": "outside_bounds" if outside else "within_bounds",
        "reasons": outside,
        "orders": len(orders),
        "rejected_order_ids": rejected[:20],
        "uncertain_order_ids": uncertain[:20],
        "gross_exposure": str(exposure),
        "max_notional": str(mandate.max_notional),
    }


# ---------------------------------------------------------------- human actions


def _validate(
    *,
    actor: str,
    secondary_actor: str,
    reason: str,
    symbols: list[str],
    max_notional: Any,
    max_order_notional: Any,
    max_pilot_days: int,
    renewal_lead_hours: int,
    observation_window_sessions: int,
    max_cumulative_loss: Any,
    max_drawdown: Any,
    rollback_target: str,
    model_run_id: str,
    checklist: dict,
    valid_from: datetime,
    expires_at: datetime,
    now: datetime,
) -> dict:
    from app.services.live_pilot import (
        MAX_PILOT_DAYS, MAX_PILOT_NOTIONAL, MAX_PILOT_ORDER_NOTIONAL, _checklist_gate, _compliance_gate,
    )

    if len(reason) < 3:
        raise LiveMandateError("A live mandate requires a reason")
    if not actor or not secondary_actor or actor == secondary_actor:
        raise LiveMandateError("A live mandate requires two distinct admin approvers")
    normalized = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    if not normalized or any(symbol not in ALLOWED_SYMBOLS for symbol in normalized):
        raise LiveMandateError("Mandate symbols must be a non-empty supported allowlist")
    budget = _decimal(max_notional, "Mandate budget")
    order_cap = _decimal(max_order_notional, "Mandate per-order cap")
    if budget > MAX_PILOT_NOTIONAL:
        raise LiveMandateError(f"Mandate budget cannot exceed the fixed pilot cap of {MAX_PILOT_NOTIONAL}")
    if order_cap > MAX_PILOT_ORDER_NOTIONAL or order_cap > budget:
        raise LiveMandateError(
            f"Mandate per-order cap must be at most {MAX_PILOT_ORDER_NOTIONAL} and no greater than the budget"
        )
    loss_cap = _decimal(max_cumulative_loss, "Cumulative live-loss cap")
    if loss_cap > budget:
        raise LiveMandateError("Cumulative live-loss cap cannot exceed the mandate budget")
    drawdown_cap = _decimal(max_drawdown, "Drawdown cap")
    if drawdown_cap > MAX_DRAWDOWN_CAP:
        raise LiveMandateError(f"Drawdown cap must be a fraction no greater than {MAX_DRAWDOWN_CAP}")
    if not 1 <= int(max_pilot_days) <= MAX_PILOT_DAYS:
        raise LiveMandateError(f"Each renewed pilot window must be between 1 and {MAX_PILOT_DAYS} days")
    if not 1 <= int(renewal_lead_hours) <= MAX_RENEWAL_LEAD_HOURS:
        raise LiveMandateError(f"Renewal lead time must be between 1 and {MAX_RENEWAL_LEAD_HOURS} hours")
    if not 1 <= int(observation_window_sessions) <= 20:
        raise LiveMandateError("Observation window must be between 1 and 20 regular sessions")
    if rollback_target not in {"paper", "shadow"}:
        raise LiveMandateError("Rollback target must be paper or shadow")
    if not model_run_id:
        raise LiveMandateError("A live mandate must bind one model run")
    valid_from, expires_at = _aware(valid_from), _aware(expires_at)
    if expires_at <= valid_from or expires_at <= now:
        raise LiveMandateError("Mandate outer expiry must be in the future and after its start")
    if expires_at - valid_from > timedelta(days=MAX_MANDATE_DAYS):
        raise LiveMandateError(f"Mandate outer expiry cannot exceed {MAX_MANDATE_DAYS} days")
    drills = _checklist_gate(checklist)
    if drills["status"] != "pass":
        raise LiveMandateError("A live mandate requires every operational drill and an evidence reference")
    compliance = _compliance_gate(checklist)
    if compliance["status"] != "pass":
        raise LiveMandateError("A live mandate requires a passing, human-determined compliance record")
    return {
        "symbols": normalized,
        "max_notional": budget,
        "max_order_notional": order_cap,
        "allowed_order_types": ["limit"],
        "max_pilot_days": int(max_pilot_days),
        "renewal_lead_hours": int(renewal_lead_hours),
        "observation_window_sessions": int(observation_window_sessions),
        "max_cumulative_loss": loss_cap,
        "max_drawdown": drawdown_cap,
        "rollback_target": rollback_target,
        "model_run_id": model_run_id,
        "valid_from": valid_from,
        "expires_at": expires_at,
    }


def create_live_mandate(
    db: Session,
    *,
    actor: str,
    secondary_actor: str,
    reason: str,
    symbols: list[str],
    max_notional: Any,
    max_order_notional: Any,
    max_pilot_days: int,
    renewal_lead_hours: int,
    observation_window_sessions: int,
    max_cumulative_loss: Any,
    max_drawdown: Any,
    rollback_target: str,
    model_run_id: str,
    checklist: dict,
    expires_at: datetime,
    paper_expectations: dict | None = None,
    valid_from: datetime | None = None,
    amend: bool = False,
    now: datetime | None = None,
) -> LiveMandate:
    from app.services.live_safety import ensure_live_safety_state

    now = _aware(now) or _now()
    actor, secondary_actor, reason = actor.strip(), (secondary_actor or "").strip(), (reason or "").strip()
    fields = _validate(
        actor=actor, secondary_actor=secondary_actor, reason=reason, symbols=symbols,
        max_notional=max_notional, max_order_notional=max_order_notional, max_pilot_days=max_pilot_days,
        renewal_lead_hours=renewal_lead_hours, observation_window_sessions=observation_window_sessions,
        max_cumulative_loss=max_cumulative_loss, max_drawdown=max_drawdown, rollback_target=rollback_target,
        model_run_id=(model_run_id or "").strip(), checklist=checklist or {},
        valid_from=valid_from or now, expires_at=expires_at, now=now,
    )
    if ensure_live_safety_state(db).mode == "emergency-stop":
        raise LiveMandateError("Clear the emergency stop with two approvers before recording a mandate")
    current = current_live_mandate(db)
    state = mandate_state(db, current, now=now)[0] if current else None
    if amend:
        if current is None or state not in {"active", "pending"}:
            raise LiveMandateError("Only an active or pending mandate can be amended")
    elif state in {"active", "pending", "suspended"}:
        raise LiveMandateError("A standing live mandate already exists; amend or revoke it instead")
    canonical = {
        **fields,
        "launch_checklist": checklist or {},
        "paper_expectations": paper_expectations or {},
        "primary_approval_actor": actor,
        "secondary_approval_actor": secondary_actor,
        "reason": reason,
        "supersedes_mandate_id": current.id if amend else None,
        "approved_at": now.isoformat(),
    }
    mandate = LiveMandate(
        **fields,
        launch_checklist=_safe(checklist or {}),
        paper_expectations=_safe(paper_expectations or {}),
        primary_approval_actor=actor,
        secondary_approval_actor=secondary_actor,
        reason=reason,
        supersedes_mandate_id=current.id if amend else None,
        mandate_sha256=_digest(_safe(canonical)),
    )
    db.add(mandate)
    db.flush()
    _mandate_event(
        db, mandate, action="amend" if amend else "create", status="approved", actor=actor,
        secondary_actor=secondary_actor, reason=reason,
        payload={"supersedes_mandate_id": mandate.supersedes_mandate_id, "bounds": _bounds(mandate)},
    )
    return mandate


def _require_two(actor: str, secondary_actor: str | None, reason: str, label: str) -> tuple[str, str, str]:
    actor, secondary_actor, reason = actor.strip(), (secondary_actor or "").strip(), (reason or "").strip()
    if len(reason) < 3:
        raise LiveMandateError(f"{label} requires a reason")
    if not actor or not secondary_actor or actor == secondary_actor:
        raise LiveMandateError(f"{label} requires two distinct admin approvers")
    return actor, secondary_actor, reason


def revoke_live_mandate(db: Session, *, actor: str, secondary_actor: str | None, reason: str, now: datetime | None = None) -> LiveMandate:
    actor, secondary_actor, reason = _require_two(actor, secondary_actor, reason, "Mandate revocation")
    mandate = current_live_mandate(db)
    if mandate is None or mandate_state(db, mandate, now=now)[0] not in {"active", "pending", "suspended"}:
        raise LiveMandateError("No standing live mandate is available to revoke")
    _mandate_event(db, mandate, action="revoke", status="revoked", actor=actor, secondary_actor=secondary_actor, reason=reason)
    _end_pilot(db, mandate, reason=f"Live mandate {mandate.id} was revoked: {reason}", emergency=False)
    return mandate


def reinstate_live_mandate(db: Session, *, actor: str, secondary_actor: str | None, reason: str, now: datetime | None = None) -> LiveMandate:
    from app.services.live_pilot import ensure_live_pilot
    from app.services.live_safety import ensure_live_safety_state

    actor, secondary_actor, reason = _require_two(actor, secondary_actor, reason, "Mandate reinstatement")
    mandate = current_live_mandate(db)
    if mandate is None or mandate_state(db, mandate, now=now)[0] != "suspended":
        raise LiveMandateError("Only a suspended live mandate can be reinstated")
    if ensure_live_safety_state(db).mode == "emergency-stop" or ensure_live_pilot(db).status == "halted":
        raise LiveMandateError("Reset the emergency stop with two approvers before reinstating the mandate")
    # The two approvers explicitly resolve a recorded compliance block; every
    # other trigger must already be clear in current evidence.
    triggers = evaluate_revocation_triggers(db, mandate, now=now, include_compliance_block_events=False)
    if triggers:
        raise LiveMandateError(
            "Mandate reinstatement is blocked: " + ", ".join(sorted({row["trigger"] for row in triggers}))
        )
    _mandate_event(db, mandate, action="reinstate", status="active", actor=actor, secondary_actor=secondary_actor, reason=reason)
    return mandate


def record_compliance_block(db: Session, *, actor: str, reason: str, now: datetime | None = None) -> LiveMandate:
    """Any operator may record that compliance is blocked; it only removes authority."""
    actor, reason = actor.strip(), (reason or "").strip()
    if not actor or len(reason) < 3:
        raise LiveMandateError("A compliance block requires an actor and reason")
    mandate = current_live_mandate(db)
    state = mandate_state(db, mandate, now=now)[0] if mandate else None
    if state not in {"active", "pending", "suspended"}:
        raise LiveMandateError("No standing live mandate is available")
    _mandate_event(db, mandate, action="compliance_blocked", status="blocked", actor=actor, reason=reason)
    if state != "suspended":
        _suspend(db, mandate, [{"trigger": "compliance_blocked", "reason": reason, "evidence": {"actor": actor}}])
        _end_pilot(db, mandate, reason=f"Compliance was recorded as blocked: {reason}", emergency=False)
    return mandate


def suspend_for_pilot_action(db: Session, mandate_id: int | None, *, trigger: str, reason: str) -> None:
    """Hook for manual emergency stop / rollback of a mandate-minted pilot."""
    if mandate_id is None:
        return
    mandate = db.get(LiveMandate, mandate_id)
    if mandate is None:
        return
    # A human stop must also bind every amendment of the pilot's mandate;
    # otherwise an active successor could re-mint authority on the next pass.
    chain = [mandate]
    seen = {mandate.id}
    while True:
        successor = db.scalars(
            select(LiveMandate).where(LiveMandate.supersedes_mandate_id == chain[-1].id).order_by(LiveMandate.id)
        ).first()
        if successor is None or successor.id in seen:
            break
        seen.add(successor.id)
        chain.append(successor)
    for row in chain:
        if mandate_state(db, row)[0] in {"active", "pending"} or row is mandate and mandate_state(db, row)[0] not in {"suspended", "revoked"}:
            _suspend(db, row, [{"trigger": trigger, "reason": reason, "pilot_mandate_id": mandate.id}])


# ---------------------------------------------------------------- automation


def _suspend(db: Session, mandate: LiveMandate, triggers: list[dict]) -> None:
    names = sorted({row["trigger"] for row in triggers})
    reason = "Automatic mandate suspension: " + ", ".join(names)
    _mandate_event(db, mandate, action="suspend", status="suspended", actor=MANDATE_ACTOR, reason=reason,
                   payload={"triggers": triggers})
    _alert(
        db, mandate, severity="critical", title="Live mandate suspended",
        message=f"{reason}. Live authority ended; two distinct approvers must reinstate the mandate.",
        payload={"triggers": names},
    )


def _lapse_transitions(db: Session, target: str, reason: str, evidence: dict) -> None:
    from app.services.live_safety import ALLOWED_TRANSITIONS, LIVE_MODES, ensure_live_safety_state, transition_live_safety

    mode = ensure_live_safety_state(db).mode
    if mode not in LIVE_MODES:
        return
    path = [target] if target in ALLOWED_TRANSITIONS.get(mode, set()) else ["paper", *([target] if target != "paper" else [])]
    for step in path:
        transition_live_safety(db, target_mode=step, actor=MANDATE_ACTOR, reason=reason, evidence=evidence)


def _end_pilot(db: Session, mandate: LiveMandate, *, reason: str, emergency: bool, evaluation: dict | None = None) -> str:
    from app.services.live_pilot import _write_event, ensure_live_pilot, stop_live_pilot
    from app.services.live_safety import LIVE_MODES, ensure_live_safety_state

    pilot = ensure_live_pilot(db)
    if pilot.status not in LIVE_PILOT_STATUSES:
        return "none"
    if emergency and ensure_live_safety_state(db).mode in LIVE_MODES:
        stop_live_pilot(db, actor=MANDATE_ACTOR, reason=reason)
        return "emergency_stop"
    evidence = {"mandate_id": mandate.id, "pilot_id": pilot.id, "rollback_target": pilot.rollback_target, "lapse": True}
    _lapse_transitions(db, pilot.rollback_target, reason, evidence)
    pilot.status = "rolled_back"
    pilot.stopped_at = _now()
    pilot.stopped_reason = reason
    pilot.updated_by = MANDATE_ACTOR
    _write_event(db, action="lapse", status="rolled_back", actor=MANDATE_ACTOR, reason=reason,
                 payload={**evidence, "failing_gates": _failing(evaluation or {})})
    return "lapsed"


def _lapse(db: Session, mandate: LiveMandate, *, reason: str, evaluation: dict | None = None) -> dict:
    outcome = _end_pilot(db, mandate, reason=reason, emergency=False, evaluation=evaluation)
    _mandate_event(db, mandate, action="lapse", status="lapsed", actor=MANDATE_ACTOR, reason=reason,
                   payload={"failing_gates": _failing(evaluation or {}), "evaluation": evaluation, "pilot": outcome})
    _alert(
        db, mandate, severity="critical", title="Live authority lapsed",
        message=f"{reason}. Live authority returned to the rollback target.",
        payload={"failing_gates": _failing(evaluation or {})},
    )
    return {"action": "lapse", "reason": reason, "pilot": outcome}


def _window_end(mandate: LiveMandate, now: datetime) -> datetime:
    return min(now + timedelta(days=mandate.max_pilot_days), _aware(mandate.expires_at))


def _activate(db: Session, mandate: LiveMandate, now: datetime) -> dict:
    from app.services.live_pilot import LivePilotError, activate_live_pilot

    evaluation = evaluate_mandate_renewal(db, mandate, now=now)
    if evaluation["status"] != "pass":
        if _record_once(db, mandate, action="activation_blocked",
                        reason="Mandate pilot activation waits for every launch gate to be green", evaluation=evaluation):
            _alert(db, mandate, severity="warning", title="Live mandate activation blocked",
                   message="A gate is not green; no live pilot window was minted.",
                   payload={"failing_gates": _failing(evaluation)})
        return {"action": "activation_blocked", "failing_gates": _failing(evaluation)}
    expires = _window_end(mandate, now)
    try:
        activate_live_pilot(
            db,
            actor=mandate.primary_approval_actor,
            secondary_actor=mandate.secondary_approval_actor,
            reason=f"Standing live mandate {mandate.id} minted a bounded canary window",
            symbols=list(mandate.symbols or []),
            max_notional=Decimal(str(mandate.max_notional)),
            max_order_notional=Decimal(str(mandate.max_order_notional)),
            starts_at=now,
            expires_at=expires,
            observation_window_sessions=mandate.observation_window_sessions,
            rollback_target=mandate.rollback_target,
            model_run_id=mandate.model_run_id,
            paper_expectations=mandate.paper_expectations or {},
            checklist=mandate.launch_checklist or {},
            executed_by=MANDATE_ACTOR,
            mandate_id=mandate.id,
        )
    except LivePilotError as exc:
        _record_once(db, mandate, action="activation_blocked", reason=f"Mandate pilot activation was denied: {exc}",
                     evaluation=evaluation, extra={"error": str(exc)})
        return {"action": "activation_blocked", "error": str(exc)}
    _mandate_event(db, mandate, action="activate", status="canary", actor=MANDATE_ACTOR,
                   reason="Bounded canary window minted inside the mandate",
                   payload={"evaluation": evaluation, "starts_at": now, "expires_at": expires})
    return {"action": "activate", "expires_at": expires.isoformat()}


def _renew(db: Session, mandate: LiveMandate, pilot, now: datetime) -> dict:
    from app.services.live_pilot import _write_event

    expires = _aware(pilot.expires_at)
    rebinding = pilot.mandate_id != mandate.id
    if pilot.model_run_id != mandate.model_run_id:
        return _lapse(db, mandate, reason="The mandate binds a different model; a fresh canary is required")
    evaluation = evaluate_mandate_renewal(db, mandate, now=now)
    if evaluation["status"] != "pass":
        if rebinding or expires is None or now >= expires:
            return _lapse(db, mandate, reason="Pilot renewal was denied because a launch gate is not green",
                          evaluation=evaluation)
        if _record_once(db, mandate, action="renewal_blocked",
                        reason="Pilot renewal is waiting for every launch gate to be green", evaluation=evaluation):
            _alert(db, mandate, severity="warning", title="Live pilot renewal blocked",
                   message="A gate is not green; live authority lapses at the window expiry unless it recovers.",
                   payload={"failing_gates": _failing(evaluation), "pilot_expires_at": expires})
        return {"action": "renewal_blocked", "failing_gates": _failing(evaluation)}
    new_expiry = _window_end(mandate, now)
    previous = {"mandate_id": pilot.mandate_id, "starts_at": pilot.starts_at, "expires_at": pilot.expires_at}
    pilot.symbols = list(mandate.symbols or [])
    pilot.max_notional = Decimal(str(mandate.max_notional))
    pilot.max_order_notional = Decimal(str(mandate.max_order_notional))
    pilot.allowed_order_types = ["limit"]
    pilot.rollback_target = mandate.rollback_target
    pilot.observation_window_sessions = mandate.observation_window_sessions
    pilot.starts_at, pilot.expires_at = now, new_expiry
    pilot.mandate_id = mandate.id
    # The renewed window's authority is exactly the two mandate approvers.
    pilot.primary_approval_actor = mandate.primary_approval_actor
    pilot.secondary_approval_actor = mandate.secondary_approval_actor
    pilot.launch_checklist = mandate.launch_checklist or {}
    pilot.updated_by = MANDATE_ACTOR
    payload = {"authority": _authority(mandate), "previous": previous, "starts_at": now, "expires_at": new_expiry}
    _write_event(db, action="renew", status=pilot.status, actor=MANDATE_ACTOR,
                 secondary_actor=mandate.secondary_approval_actor,
                 reason=f"Standing live mandate {mandate.id} renewed the pilot window",
                 payload=_safe({**payload, "launch_status": evaluation["status"]}))
    _mandate_event(db, mandate, action="renew", status=pilot.status, actor=MANDATE_ACTOR,
                   reason="Pilot window renewed inside the mandate", payload={**payload, "evaluation": evaluation})
    return {"action": "renew", "expires_at": new_expiry.isoformat()}


def _maybe_promote(db: Session, mandate: LiveMandate, pilot, now: datetime) -> dict | None:
    from app.services.live_pilot import LivePilotError, promote_live_pilot

    sessions = completed_sessions_since(pilot.approved_at, now)
    if sessions < pilot.observation_window_sessions:
        return {"action": "observing", "completed_sessions": sessions, "required_sessions": pilot.observation_window_sessions}
    metrics = _canary_metrics(db, mandate, _aware(pilot.approved_at))
    if metrics["status"] == "outside_bounds":
        return _lapse(db, mandate, reason="Canary metrics were outside the mandate bounds: " + "; ".join(metrics["reasons"]))
    evaluation = evaluate_mandate_renewal(db, mandate, now=now)
    if metrics["status"] != "within_bounds" or evaluation["status"] != "pass":
        _record_once(db, mandate, action="promotion_held",
                     reason="Automatic promotion waits for clean reconciliation and every gate to be green",
                     evaluation=evaluation, extra={"metrics_status": metrics["status"]})
        return {"action": "promotion_held", "failing_gates": _failing(evaluation), "metrics": metrics["status"]}
    try:
        promote_live_pilot(
            db, actor=MANDATE_ACTOR, secondary_actor=mandate.secondary_approval_actor,
            reason=f"Standing live mandate {mandate.id}: {sessions} observation sessions completed cleanly",
        )
    except LivePilotError as exc:
        _record_once(db, mandate, action="promotion_held", reason=f"Automatic promotion was denied: {exc}",
                     evaluation=evaluation, extra={"error": str(exc)})
        return {"action": "promotion_held", "error": str(exc)}
    _mandate_event(db, mandate, action="auto_promote", status="active", actor=MANDATE_ACTOR,
                   reason="Canary promoted to approved-live inside the mandate",
                   payload={"completed_sessions": sessions, "metrics": metrics, "evaluation": evaluation})
    return {"action": "auto_promote", "completed_sessions": sessions}


def run_live_mandate_cycle(db: Session, *, now: datetime | None = None) -> dict:
    """One scheduler pass: revoke, promote, renew or mint inside the mandate."""
    from app.services.live_pilot import ensure_live_pilot
    from app.services.live_safety import ensure_live_safety_state

    now = _aware(now) or _now()
    pilot = ensure_live_pilot(db)
    mandate_pilot = pilot.status in LIVE_PILOT_STATUSES and pilot.mandate_id is not None
    mandate = current_live_mandate(db)
    if mandate is None:
        return {"status": "no_mandate", "actions": []}
    state, state_reason = mandate_state(db, mandate, now=now)
    result: dict[str, Any] = {"mandate_id": mandate.id, "mandate_state": state, "actions": []}
    if state in {"revoked", "expired", "superseded"}:
        if mandate_pilot:
            result["actions"].append(_lapse(db, mandate, reason=f"Live mandate is {state}: {state_reason}"))
        result["status"] = state
        return result
    if state == "suspended":
        if mandate_pilot:
            result["actions"].append({"action": "end", "pilot": _end_pilot(db, mandate, reason="Live mandate is suspended", emergency=False)})
        result["status"] = "suspended"
        return result
    if state == "pending":
        result["status"] = "pending"
        return result

    triggers = evaluate_revocation_triggers(db, mandate, now=now)
    if triggers:
        names = {row["trigger"] for row in triggers}
        _suspend(db, mandate, triggers)
        outcome = _end_pilot(
            db, mandate, reason="Live mandate revocation trigger: " + ", ".join(sorted(names)),
            emergency=bool(names & EMERGENCY_TRIGGERS),
        ) if pilot.mandate_id is not None else "none"
        result["actions"].append({"action": "suspend", "triggers": sorted(names), "pilot": outcome})
        result["status"] = "suspended"
        return result

    if pilot.status in LIVE_PILOT_STATUSES and pilot.mandate_id is None:
        # A manually approved pilot keeps its own two-person record.
        result["status"] = "manual_pilot"
        return result
    if pilot.status in LIVE_PILOT_STATUSES:
        if pilot.status == "canary" and pilot.mandate_id == mandate.id:
            promotion = _maybe_promote(db, mandate, pilot, now)
            if promotion:
                result["actions"].append(promotion)
                if promotion["action"] == "lapse":
                    result["status"] = "lapsed"
                    return result
        expires = _aware(pilot.expires_at)
        due = (
            pilot.mandate_id != mandate.id
            or expires is None
            or now >= expires - timedelta(hours=mandate.renewal_lead_hours)
        )
        if due:
            result["actions"].append(_renew(db, mandate, pilot, now))
        result["status"] = "operating"
        return result
    if pilot.status == "halted":
        result["status"] = "halted"
        return result
    if ensure_live_safety_state(db).mode != "shadow":
        result["status"] = "waiting_for_shadow"
        return result
    result["actions"].append(_activate(db, mandate, now))
    result["status"] = "operating"
    return result


# ---------------------------------------------------------------- projection


def _bounds(mandate: LiveMandate) -> dict:
    return _safe({
        "symbols": list(mandate.symbols or []),
        "max_notional": mandate.max_notional,
        "max_order_notional": mandate.max_order_notional,
        "allowed_order_types": list(mandate.allowed_order_types or []),
        "max_pilot_days": mandate.max_pilot_days,
        "renewal_lead_hours": mandate.renewal_lead_hours,
        "observation_window_sessions": mandate.observation_window_sessions,
        "max_cumulative_loss": mandate.max_cumulative_loss,
        "max_drawdown": mandate.max_drawdown,
        "rollback_target": mandate.rollback_target,
        "model_run_id": mandate.model_run_id,
        "valid_from": mandate.valid_from,
        "expires_at": mandate.expires_at,
    })


def _event_out(row: LiveMandateEvent) -> dict:
    payload = dict(row.payload or {})
    payload.pop("dedupe", None)
    evaluation = payload.pop("evaluation", None)
    gates = None
    if isinstance(evaluation, dict):
        gates = {
            name: {"status": gate.get("status"), "reason": gate.get("reason")}
            for name, gate in (evaluation.get("gates") or {}).items()
        }
    return _safe({
        "id": row.id,
        "mandate_id": row.mandate_id,
        "action": row.action,
        "status": row.status,
        "actor": row.actor,
        "secondary_actor": row.secondary_actor,
        "reason": row.reason,
        "created_at": row.created_at,
        "evaluation_status": evaluation.get("status") if isinstance(evaluation, dict) else None,
        "gates": gates,
        "failing_gates": payload.get("failing_gates"),
        "triggers": [item.get("trigger") for item in payload.get("triggers", []) if isinstance(item, dict)] or None,
    })


def _mandate_out(db: Session, mandate: LiveMandate, now: datetime) -> dict:
    state, reason = mandate_state(db, mandate, now=now)
    return _safe({
        "id": mandate.id,
        "state": state,
        "state_reason": reason,
        "mandate_sha256": mandate.mandate_sha256,
        "supersedes_mandate_id": mandate.supersedes_mandate_id,
        "primary_approval_actor": mandate.primary_approval_actor,
        "secondary_approval_actor": mandate.secondary_approval_actor,
        "reason": mandate.reason,
        "created_at": mandate.created_at,
        **_bounds(mandate),
    })


def live_mandate_projection(db: Session, *, now: datetime | None = None) -> dict:
    """Read-only mandate status; never triggers renewal, suspension or alerts."""
    from app.services.live_pilot import ensure_live_pilot

    now = _aware(now) or _now()
    current = current_live_mandate(db)
    pilot = ensure_live_pilot(db)
    history = db.scalars(select(LiveMandate).order_by(LiveMandate.id.desc()).limit(10)).all()
    revocations = db.scalars(
        select(LiveMandateEvent).where(LiveMandateEvent.action.in_(REVOCATION_ACTIONS))
        .order_by(LiveMandateEvent.id.desc()).limit(25)
    ).all()
    record = _mandate_out(db, current, now) if current else None
    next_renewal_at = None
    last_evaluation = None
    triggers: list[dict] = []
    loss = drawdown = None
    if current:
        latest = _latest_event(db, current.id, EVALUATION_ACTIONS)
        last_evaluation = _event_out(latest) if latest else None
        if pilot.status in LIVE_PILOT_STATUSES and pilot.mandate_id == current.id and pilot.expires_at:
            next_renewal_at = _aware(pilot.expires_at) - timedelta(hours=current.renewal_lead_hours)
        if record["state"] == "active":
            triggers = [
                {"trigger": row["trigger"], "reason": row["reason"]}
                for row in evaluate_revocation_triggers(db, current, now=now)
            ]
        loss = cumulative_live_loss(db, current)
        drawdown = live_drawdown(db, current, now=now)
    return _safe({
        "current": record,
        "pilot": {
            "status": pilot.status,
            "mandate_id": pilot.mandate_id,
            "starts_at": pilot.starts_at,
            "expires_at": pilot.expires_at,
            "approved_at": pilot.approved_at,
            "completed_observation_sessions": (
                completed_sessions_since(pilot.approved_at, now)
                if pilot.status == "canary" and pilot.mandate_id is not None else None
            ),
        },
        "next_renewal_at": next_renewal_at,
        "evaluation_cadence_minutes": 5,
        "last_renewal_evaluation": last_evaluation,
        "active_triggers": triggers,
        "cumulative_loss": loss,
        "drawdown": drawdown,
        "revocation_history": [_event_out(row) for row in revocations],
        "history": [_mandate_out(db, row, now) for row in history],
        "contract": {
            "two_person_create_amend_revoke_reinstate": True,
            "emergency_stop_reset_two_person": True,
            "graduation_grants_live_authority": False,
            "live_authority_only_via_mandate_or_manual_two_person_pilot": True,
            "allow_live_trading_is_a_deployment_decision": True,
        },
    })
