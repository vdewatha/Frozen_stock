"""Durable coordinator for the governed stock learning cycle.

This service is intentionally orchestration-only.  Dataset creation, training,
holdout consumption, forward evaluation, lifecycle transitions, monitoring, and
recovery remain owned by their existing services.  A cycle records the links
and decisions between those systems without becoming a second model registry.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Iterable

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.models import (
    StockPaperAccount,
    StockDatasetSnapshot,
    StockLearningCycle,
    StockLearningCycleEvent,
    StockLearningScheduleControl,
    StockPaperPromotionDecision,
    StockModelRegistry,
    StockModelLifecycleState,
    StockPaperBindingState,
    StockPaperModelBinding,
    StockPaperPromotionReadinessReport,
    StockPaperRecoveryEvent,
    StockPaperRecoveryState,
    StockPaperTrial,
    StockPaperRunApproval,
    StockMonitoringSnapshot,
    StockTrainingJob,
    Notification,
    RiskRule,
)
from app.core.config import settings
from app.services.audit import write_audit_log
from app.services.intraday_data import NY, feed_status, session_bounds
from app.services.readiness import _scheduler_health
from app.services.stock_forward_trial import trial_feed_preflight
from app.services.stock_paper_ledger import (
    TRADIER_PAPER_EVIDENCE,
    active_paper_account,
)
from app.services.paper_venue_qualification import paper_venue_qualification_status
from app.services.broker import stock_paper_broker_status
from app.services.operational_hardening import _audit_chain_check
from app.services.portfolio_risk import portfolio_risk_snapshot
from app.services.risk import DEFAULT_RISK_RULES
from app.services.stock_recovery import HEARTBEAT_TIMEOUT
from app.services.stock_promotion_readiness import evaluate_promotion_readiness
from app.services.live_safety import evaluate_live_safety
from app.services.stock_training_jobs import (
    StockTrainingError,
    _current_lifecycle,
    _lock_lifecycle_admission,
    _transition_model,
    create_stock_paper_binding,
    create_stock_training_job,
    transition_stock_model_lifecycle,
)
from app.services.stock_forward_trial import (
    create_trial,
    start_trial,
    trial_position_handling_projection,
    validate_trial_artifact,
)

TERMINAL_STATUSES = {"complete", "promoted", "demoted", "rolled_back", "failed"}
VALID_TRIGGERS = {"manual", "scheduled"}
ONE_SESSION_SYMBOLS = ["AAPL", "MSFT", "QQQ", "SPY"]
MIN_COMPARISON_SAMPLES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    """Normalize legacy naive database timestamps before comparisons."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


SCHEDULE_CONTROL_ID = 1
SCHEDULE_PAUSE_ACTOR = "scheduled_learning_control"
SCHEDULE_PAUSED_REASON = "Scheduled paper learning is paused by operator"


def scheduled_learning_control_projection(db: Session) -> dict:
    control = db.get(StockLearningScheduleControl, SCHEDULE_CONTROL_ID)
    live_safety = evaluate_live_safety(db)
    return {
        "paused": bool(control.paused) if control else False,
        "pause_reason": control.pause_reason if control and control.paused else None,
        "updated_by": control.updated_by if control else "system",
        "updated_at": control.updated_at if control else None,
        "paper_only": True,
        "live_authorized": live_safety["live_authorized"],
        "live_safety": {
            "mode": live_safety["mode"],
            "status": live_safety["status"],
            "live_orders_allowed": live_safety["live_orders_allowed"],
        },
        "recovery_independent": True,
    }


def set_scheduled_learning_control(
    db: Session,
    *,
    paused: bool,
    actor: str,
    reason: str,
) -> StockLearningScheduleControl:
    reason = reason.strip()
    if len(reason) < 3:
        raise StockTrainingError("Scheduled learning control requires a reason")
    control = db.scalar(
        select(StockLearningScheduleControl)
        .where(StockLearningScheduleControl.id == SCHEDULE_CONTROL_ID)
        .with_for_update()
    )
    if control is None:
        control = StockLearningScheduleControl(id=SCHEDULE_CONTROL_ID)
        db.add(control)
        db.flush()
    control.paused = paused
    control.pause_reason = reason if paused else None
    control.updated_by = actor
    control.updated_at = _now()
    return control


def _scheduled_learning_pause_reason(db: Session) -> str | None:
    control = db.get(StockLearningScheduleControl, SCHEDULE_CONTROL_ID)
    if control and control.paused:
        return control.pause_reason or SCHEDULE_PAUSED_REASON
    return None


def _defer_scheduled_cycle_for_pause(
    db: Session,
    cycle: StockLearningCycle,
    *,
    reason: str,
    stage: str,
) -> None:
    if cycle.status in TERMINAL_STATUSES:
        return
    changed = cycle.status != "deferred" or cycle.stage != stage or cycle.last_reason != reason
    cycle.status = "deferred"
    cycle.stage = stage
    cycle.last_reason = reason
    if changed:
        _event(
            db,
            cycle=cycle,
            stage=stage,
            decision="deferred",
            actor=SCHEDULE_PAUSE_ACTOR,
            reason=reason,
            evidence={"scheduled_learning_paused": True},
        )


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def paper_run_runtime_bounds(
    cycle: StockLearningCycle,
    trial: StockPaperTrial | None = None,
) -> dict:
    """Return the non-secret bounds an approval must authorize exactly."""
    policy = trial.policy if trial else {}
    configured = (cycle.evidence or {}).get("paper_run_bounds")
    if isinstance(configured, dict):
        return _safe(configured)
    session_date = policy.get("paper_session_date") or cycle.cutoff_date.isoformat()
    try:
        session_day = date.fromisoformat(str(session_date))
    except ValueError:
        session_day = cycle.cutoff_date
        session_date = session_day.isoformat()
    bounds = session_bounds(session_day)
    start_at = bounds[0].isoformat() if bounds else None
    end_at = bounds[1].isoformat() if bounds else None
    regular_sessions = int(policy.get("regular_sessions", 20))
    one_session = regular_sessions == 1
    exposure_limits = {
        "max_order_notional": str(policy.get("max_order_notional", "2500")),
        "max_symbol_notional": str(policy.get("max_symbol_notional", "2500")),
        "max_aggregate_notional": str(
            policy.get("max_aggregate_notional", policy.get("max_allocated_notional", "10000"))
        ),
        "currency": "USD",
    }
    loss_limits = {
        "max_loss": str(policy.get("max_loss", policy.get("auto_pause_drawdown", "0.02"))),
        "unit": str(policy.get("loss_unit", "fraction_of_baseline_equity")),
        "currency": "USD",
    }
    return {
        "environment": "paper",
        "execution_provider": settings.active_paper_broker,
        "provider_switch": None,
        "symbols": sorted({str(symbol).strip().upper() for symbol in cycle.symbols}),
        "exposure_limits": exposure_limits if one_session else {
            "max_allocated_notional": str(policy.get("max_allocated_notional", "10000")),
            "max_risk_per_trade": str(policy.get("max_risk_per_trade", "0.0025")),
        },
        "loss_limits": loss_limits if one_session else {
            "max_loss": str(policy.get("auto_pause_drawdown", "0.02")),
            "unit": "fraction_of_baseline_equity",
            "currency": "USD",
        },
        "duration_sessions": regular_sessions,
        "schedule": (
            {
                "trigger": cycle.trigger,
                "session_date": str(session_date),
                "start_at": start_at,
                "end_at": end_at,
                "timezone": "America/New_York",
            }
            if one_session else {"trigger": cycle.trigger}
        ),
        "stop_conditions": sorted([
            "paper_only",
            "live_authorized:false",
            f"auto_pause_drawdown:{policy.get('auto_pause_drawdown', '0.02')}",
        ]),
        "stop_authority": str(policy.get("stop_authority", "operator_and_system")),
        "pending_order_treatment": str(policy.get("pending_order_treatment", "cancel")),
        "remaining_position_policy": str(policy.get("remaining_position_policy", "hold")),
    }


def _approval_projection(
    approval: StockPaperRunApproval | None,
    *,
    expected: dict,
    reason: str | None = None,
) -> dict:
    if approval is None:
        return {
            "status": "missing",
            "reason": reason or "Paper run approval is missing",
            "record": None,
            "expected": expected,
            "paper_only": True,
            "live_authorized": False,
        }
    return {
        "status": "pass" if reason is None else "mismatch",
        "reason": reason,
        "record": {
            "id": approval.id,
            "environment": approval.environment,
            "execution_provider": approval.execution_provider,
            "provider_switch": approval.provider_switch,
            "symbols": approval.symbols,
            "exposure_limits": approval.exposure_limits,
            "loss_limits": approval.loss_limits,
            "duration_sessions": approval.duration_sessions,
            "schedule": approval.schedule,
            "stop_conditions": approval.stop_conditions,
            "stop_authority": approval.stop_authority,
            "pending_order_treatment": approval.pending_order_treatment,
            "remaining_position_policy": approval.remaining_position_policy,
            "approving_actors": approval.approving_actors,
            "approval_sha256": approval.approval_sha256,
            "created_at": approval.created_at,
        },
        "expected": expected,
        "paper_only": True,
        "live_authorized": False,
    }


def _approval_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _approval_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_approval_value(item) for item in value]
    return str(value) if value is not None else None


def _approval_mismatch(
    approval: StockPaperRunApproval | None,
    expected: dict,
) -> str | None:
    if approval is None:
        return "Paper run approval is missing"
    if approval.environment != expected["environment"]:
        return "Paper run approval environment does not match paper execution"
    if approval.paper_only is not True or approval.live_authorized is not False:
        return "Paper run approval is not paper-only"
    actual = {
        "execution_provider": approval.execution_provider,
        "provider_switch": approval.provider_switch,
        "symbols": sorted({str(symbol).strip().upper() for symbol in approval.symbols}),
        "exposure_limits": _approval_value(approval.exposure_limits),
        "loss_limits": _approval_value(approval.loss_limits),
        "duration_sessions": approval.duration_sessions,
        "schedule": _approval_value(approval.schedule),
        "stop_conditions": sorted(str(item) for item in approval.stop_conditions),
        "stop_authority": approval.stop_authority,
        "pending_order_treatment": approval.pending_order_treatment,
        "remaining_position_policy": approval.remaining_position_policy,
    }
    comparable = {
        "execution_provider": expected["execution_provider"],
        "provider_switch": expected["provider_switch"],
        "symbols": expected["symbols"],
        "exposure_limits": expected["exposure_limits"],
        "loss_limits": expected["loss_limits"],
        "duration_sessions": expected["duration_sessions"],
        "schedule": expected["schedule"],
        "stop_conditions": sorted(expected["stop_conditions"]),
        "stop_authority": expected["stop_authority"],
        "pending_order_treatment": expected["pending_order_treatment"],
        "remaining_position_policy": expected["remaining_position_policy"],
    }
    for key, value in comparable.items():
        if actual.get(key) != value:
            return f"Paper run approval does not match requested {key.replace('_', ' ')}"
    if not approval.approving_actors:
        return "Paper run approval has no approving actor"
    return None


def create_paper_run_approval(
    db: Session,
    cycle_id: str,
    *,
    actor: str,
    environment: str,
    execution_provider: str,
    provider_switch: dict | None,
    symbols: Iterable[str],
    exposure_limits: dict,
    loss_limits: dict | None = None,
    duration_sessions: int,
    schedule: dict,
    stop_conditions: Iterable[str],
    stop_authority: str = "operator_and_system",
    pending_order_treatment: str = "cancel",
    remaining_position_policy: str = "hold",
    approving_actors: Iterable[str],
) -> StockPaperRunApproval:
    cycle = db.get(StockLearningCycle, cycle_id)
    if not cycle:
        raise StockTrainingError("Learning cycle not found")
    eligible, reason = launch_preflight_eligibility(
        cycle, prerequisites_ready=True,
    )
    if not eligible:
        raise StockTrainingError(f"Paper approval rejected at {cycle.stage}: {reason}")
    # A fresh result may repair a historical failed gate, but must not erase
    # a new scheduler failure recorded while that result was being collected.
    decision_fields = (
        "stage", "status", "symbols", "provider", "trigger", "gates",
        "last_reason", "model_run_id", "snapshot_id", "binding_id", "trial_id",
    )
    checked_decision = _digest({name: getattr(cycle, name) for name in decision_fields})
    checked_at = _now()
    current_gates = {
        **evaluate_cycle_prerequisites(
            db, symbols=cycle.symbols, provider=cycle.provider, now=checked_at,
        ),
        **evaluate_launch_admission_prerequisites(db, now=checked_at),
    }
    # Collect read-only prerequisites before taking a row lock: provider checks
    # must not hold up scheduler writes. Refresh even an identity-mapped cycle
    # after any concurrent UPDATE commits, then retain the lock through commit.
    cycle = db.scalar(
        select(StockLearningCycle)
        .where(StockLearningCycle.cycle_id == cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not cycle:
        raise StockTrainingError("Learning cycle not found")
    eligible, reason = launch_preflight_eligibility(
        cycle, prerequisites_ready=True,
    )
    if not eligible:
        raise StockTrainingError(f"Paper approval rejected at {cycle.stage}: {reason}")
    if checked_decision != _digest({name: getattr(cycle, name) for name in decision_fields}):
        raise StockTrainingError(
            "Paper approval rejected at preflight: cycle changed during prerequisite checks; retry approval"
        )
    if not current_gates:
        raise StockTrainingError("Paper approval rejected at preflight: prerequisite evidence is missing")
    # Fresh read-only evidence can supersede a repaired historical failure.
    gates = {**(cycle.gates or {}), **current_gates}
    for name, gate in gates.items():
        if name != "paper_run_approval" and gate.get("status") != "pass":
            raise StockTrainingError(
                f"Paper approval rejected at preflight prerequisite {name}: "
                f"{gate.get('reason') or 'Prerequisite has not passed'}"
            )
    trial = db.get(StockPaperTrial, cycle.trial_id, populate_existing=True)
    binding = db.get(StockPaperModelBinding, cycle.binding_id)
    binding_state = db.get(StockPaperBindingState, 1, populate_existing=True)
    if (
        not binding or not binding_state
        or binding_state.active_binding_id != cycle.binding_id
        or binding.model_run_id != cycle.model_run_id
        or binding.snapshot_id != cycle.snapshot_id
        or not binding.paper_only or binding.live_authorized
    ):
        raise StockTrainingError(
            "Paper approval rejected at preflight prerequisite paper_binding: "
            "The cycle binding must be the active paper-only canary with matching model and snapshot"
        )
    if (
        not trial or trial.binding_id != cycle.binding_id
        or trial.source_cycle_id != cycle.cycle_id or trial.status != "approved"
        or trial.lineage.get("model_run_id") != cycle.model_run_id
        or trial.lineage.get("snapshot_id") != cycle.snapshot_id
    ):
        raise StockTrainingError(
            "Paper approval rejected at preflight prerequisite forward_trial: "
            "An approved forward trial with matching cycle, binding, model and snapshot is required"
        )
    if cycle.status in {"blocked", "deferred"}:
        # Some handoff failures only set last_reason, leaving old passing gates.
        # Only a known approval blocker or an exactly identified repaired gate
        # can explain away a durable blocked state.
        explained = any(
            gate.get("status") != "pass"
            and bool(cycle.last_reason)
            and gate.get("reason") == cycle.last_reason
            and (
                name == "paper_run_approval"
                or current_gates.get(name, {}).get("status") == "pass"
            )
            for name, gate in (cycle.gates or {}).items()
        )
        if not explained:
            raise StockTrainingError(
                f"Paper approval rejected at preflight: unresolved {cycle.status} prerequisite: "
                f"{cycle.last_reason or 'Blocker provenance is unavailable'}"
            )
    try:
        validate_trial_artifact(db, trial)
    except (StockTrainingError, KeyError, TypeError, ValueError) as exc:
        raise StockTrainingError(
            f"Paper approval rejected at preflight prerequisite trial_artifact: {exc}"
        ) from None
    expected = paper_run_runtime_bounds(cycle, trial)
    requested_loss_limits = _approval_value(loss_limits or expected["loss_limits"])
    requested = {
        "environment": environment,
        "execution_provider": execution_provider,
        "provider_switch": provider_switch or None,
        "symbols": sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}),
        "exposure_limits": _approval_value(exposure_limits),
        "loss_limits": requested_loss_limits,
        "duration_sessions": duration_sessions,
        "schedule": _approval_value(schedule),
        "stop_conditions": sorted(str(item).strip() for item in stop_conditions if str(item).strip()),
        "stop_authority": str(stop_authority).strip(),
        "pending_order_treatment": str(pending_order_treatment).strip(),
        "remaining_position_policy": str(remaining_position_policy).strip(),
    }
    one_session_required = expected["duration_sessions"] == 1
    if requested["environment"] != "paper" or (
        one_session_required and requested["duration_sessions"] != 1
    ):
        raise StockTrainingError("Paper approval must authorize exactly one regular session")
    if requested["symbols"] != expected["symbols"]:
        raise StockTrainingError("Paper run approval symbols do not match the cycle universe")
    if one_session_required and requested["symbols"] != ONE_SESSION_SYMBOLS:
        raise StockTrainingError("One-session paper approval must cover AAPL, MSFT, QQQ, and SPY")
    if requested["execution_provider"] != expected["execution_provider"] or requested["provider_switch"] is not None:
        raise StockTrainingError("Paper run approval broker or provider switch does not match the paper environment")
    if one_session_required and (
        not requested["schedule"].get("session_date")
        or requested["schedule"].get("timezone") != "America/New_York"
    ):
        raise StockTrainingError("Paper approval requires a dated America/New_York session")
    if one_session_required:
        try:
            session_day = date.fromisoformat(str(requested["schedule"]["session_date"]))
            start_at = datetime.fromisoformat(str(requested["schedule"]["start_at"]))
            end_at = datetime.fromisoformat(str(requested["schedule"]["end_at"]))
        except (TypeError, ValueError, KeyError):
            raise StockTrainingError("Paper approval session boundaries must be valid timestamps") from None
        session_bounds_value = session_bounds(session_day)
        if (
            not session_bounds_value
            or _utc(start_at) != _utc(session_bounds_value[0])
            or _utc(end_at) != _utc(session_bounds_value[1])
            or _utc(start_at) >= _utc(end_at)
        ):
            raise StockTrainingError("Paper approval must cover one exact regular NYSE session")
    for name, values in (("exposure", requested["exposure_limits"]), ("loss", requested["loss_limits"])):
        if not isinstance(values, dict):
            raise StockTrainingError(f"Paper approval {name} limits must be explicit")
    if one_session_required:
        required_exposure = ("max_order_notional", "max_symbol_notional", "max_aggregate_notional")
        if any(key not in requested["exposure_limits"] for key in required_exposure):
            raise StockTrainingError("Paper approval requires per-order, per-symbol, and aggregate exposure caps")
        if "max_loss" not in requested["loss_limits"] or not requested["loss_limits"].get("unit"):
            raise StockTrainingError("Paper approval requires an explicit loss limit and unit")
        try:
            numbers = [Decimal(str(requested["exposure_limits"][key])) for key in required_exposure]
            numbers.append(Decimal(str(requested["loss_limits"]["max_loss"])))
        except (InvalidOperation, TypeError, ValueError):
            raise StockTrainingError("Paper approval limits must be numeric") from None
        if any(value <= 0 for value in numbers):
            raise StockTrainingError("Paper approval limits must be greater than zero")
        if requested["stop_authority"] not in {"operator_and_system"}:
            raise StockTrainingError("Paper approval stop authority is not supported")
        if requested["pending_order_treatment"] not in {"cancel"}:
            raise StockTrainingError("Paper approval pending-order treatment must be cancel")
        if requested["remaining_position_policy"] not in {"hold", "reduce", "flatten"}:
            raise StockTrainingError("Paper approval remaining-position policy is invalid")
    # The configuration is a mutable cycle pointer; each approval remains an
    # immutable snapshot and a changed pointer makes the prior snapshot fail
    # the projection check.
    cycle.evidence = {**(cycle.evidence or {}), "paper_run_bounds": requested}
    if trial:
        trial.lineage = {
            **(trial.lineage or {}),
            "paper_run_bounds": requested,
        }
    actors = sorted({
        str(value).strip() for value in [*approving_actors, actor] if str(value).strip()
    })
    if not actors:
        raise StockTrainingError("Paper run approval requires an approving actor")
    approval_payload = {
        "cycle_id": cycle_id,
        **requested,
        "approving_actors": actors,
        "paper_only": True,
        "live_authorized": False,
    }
    approval_hash = _digest(approval_payload)
    existing = db.scalar(select(StockPaperRunApproval).where(
        StockPaperRunApproval.cycle_id == cycle_id,
        StockPaperRunApproval.approval_sha256 == approval_hash,
    ))
    if existing:
        return existing
    approval = StockPaperRunApproval(
        cycle_id=cycle_id,
        environment="paper",
        execution_provider=requested["execution_provider"],
        provider_switch=requested["provider_switch"],
        symbols=requested["symbols"],
        exposure_limits=requested["exposure_limits"],
        loss_limits=requested["loss_limits"],
        duration_sessions=requested["duration_sessions"],
        schedule=requested["schedule"],
        stop_conditions=requested["stop_conditions"],
        stop_authority=requested["stop_authority"],
        pending_order_treatment=requested["pending_order_treatment"],
        remaining_position_policy=requested["remaining_position_policy"],
        approving_actors=actors,
        approval_sha256=approval_hash,
        paper_only=True,
        live_authorized=False,
    )
    db.add(approval)
    db.flush()
    if trial:
        trial.lineage = {
            **(trial.lineage or {}),
            "paper_run_approval_sha256": approval_hash,
        }
    write_audit_log(
        db,
        event_type="stock_learning_cycle",
        action="paper_run_approval",
        status="approved",
        message="Paper run approval recorded for the requested runtime bounds",
        entity_type="stock_learning_cycle",
        payload={
            "cycle_id": cycle_id,
            "approval_id": approval.id,
            "approval_sha256": approval_hash,
            "approving_actors": actors,
            "paper_only": True,
            "live_authorized": False,
        },
    )
    return approval


def paper_run_approval_projection(db: Session, cycle: StockLearningCycle) -> dict:
    approval = db.scalar(select(StockPaperRunApproval).where(
        StockPaperRunApproval.cycle_id == cycle.cycle_id
    ).order_by(
        StockPaperRunApproval.created_at.desc(),
        StockPaperRunApproval.id.desc(),
    ))
    trial = db.get(StockPaperTrial, cycle.trial_id) if cycle.trial_id else None
    expected = paper_run_runtime_bounds(cycle, trial)
    return _approval_projection(
        approval,
        expected=expected,
        reason=_approval_mismatch(approval, expected),
    )


def _gate(status: str, *, reason: str | None = None, evidence: Any = None) -> dict:
    result: dict[str, Any] = {"status": status}
    if reason:
        result["reason"] = reason
    if evidence is not None:
        result["evidence"] = _safe(evidence)
    return result


def _readiness_history_gate(db: Session) -> dict:
    """Require the immutable promotion-readiness table and its write fence."""
    try:
        inspector = inspect(db.get_bind())
        if "stock_paper_promotion_readiness_reports" not in inspector.get_table_names():
            return _gate("unknown", reason="promotion-readiness history table is unavailable")
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            found = db.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'stock_paper_promotion_readiness_reports_immutable_%'"
            )).scalar()
        elif dialect == "postgresql":
            found = db.execute(text(
                "SELECT 1 FROM pg_trigger WHERE tgrelid = "
                "to_regclass('stock_paper_promotion_readiness_reports') "
                "AND tgname = 'stock_paper_promotion_readiness_reports_immutable'"
            )).scalar()
        else:
            found = None
        return (
            _gate("pass", evidence={"table": True, "immutable_fence": bool(found)})
            if found
            else _gate("unknown", reason="promotion-readiness history immutability is not verified")
        )
    except Exception as exc:
        return _gate("unknown", reason=f"promotion-readiness history protection unavailable: {exc.__class__.__name__}")


def evaluate_cycle_prerequisites(
    db: Session,
    *,
    symbols: Iterable[str],
    provider: str,
    now: datetime | None = None,
) -> dict[str, dict]:
    """Evaluate all gates required before a dataset or training job exists."""
    observed_at = now or _now()
    normalized = [str(symbol).strip().upper() for symbol in symbols]
    bounds = session_bounds(observed_at.astimezone(NY).date())
    in_session = bool(bounds and bounds[0] <= observed_at < bounds[1])
    feed_results: list[dict] = []
    for symbol in normalized:
        try:
            result = feed_status(db, symbol, now=observed_at)
            feed_results.append(_safe({
                "symbol": symbol,
                "status": result.get("status"),
                "entitlement_state": result.get("entitlement_state"),
                "exchange_timestamp": result.get("exchange_timestamp"),
                "ingestion_timestamp": result.get("ingestion_timestamp"),
                "missing_intervals": result.get("missing_intervals", []),
                "unavailable_reason": result.get("unavailable_reason"),
            }))
        except Exception as exc:
            feed_results.append({
                "symbol": symbol,
                "status": "unknown",
                "unavailable_reason": f"feed status unavailable: {exc.__class__.__name__}",
            })
    if not normalized:
        feed_gate = _gate("fail", reason="at least one stock symbol is required")
    elif not in_session:
        feed_gate = _gate("unknown", reason="regular-session verified feed preflight is required",
                          evidence={"regular_session": False, "symbols": feed_results})
    elif all(row.get("status") == "ready" and row.get("entitlement_state") == "verified" for row in feed_results):
        feed_gate = _gate("pass", evidence={"regular_session": True, "symbols": feed_results})
    else:
        feed_gate = _gate("fail", reason="feed entitlement, freshness, or session completeness is unavailable",
                          evidence={"regular_session": True, "symbols": feed_results})

    try:
        scheduler = _scheduler_health()
        scheduler_gate = _gate(
            "pass" if scheduler.get("healthy") and not scheduler.get("recent_unresolved_failures") else "fail",
            reason=None if scheduler.get("healthy") and not scheduler.get("recent_unresolved_failures")
            else "worker and exactly one scheduler heartbeat are required",
            evidence=scheduler,
        )
    except Exception as exc:
        scheduler_gate = _gate("unknown", reason=f"scheduler health unavailable: {exc.__class__.__name__}")

    from app.models.stock_paper import StockPaperAccount
    account = active_paper_account(db)
    ledger_ready = bool(
        account and account.status == "reconciled"
        and not account.reconciliation_required and account.accounting_verified
    )
    ledger_gate = _gate(
        "pass" if ledger_ready else "fail",
        reason=None if ledger_ready else "paper ledger reconciliation is unavailable",
        evidence={
            "account_status": account.status if account else "uninitialized",
            "reconciliation_required": account.reconciliation_required if account else None,
            "accounting_verified": account.accounting_verified if account else False,
        },
    )
    provider_gate = _gate(
        "pass" if provider in {"yfinance", "yahoo_chart"} else "fail",
        reason=None if provider in {"yfinance", "yahoo_chart"} else "provider provenance is not verified",
        evidence={"provider": provider},
    )
    protection_gate = _readiness_history_gate(db)
    return {
        "verified_feed": feed_gate,
        "scheduler_health": scheduler_gate,
        "paper_ledger": ledger_gate,
        "dataset_provenance": provider_gate,
        "readiness_history": protection_gate,
    }


def evaluate_launch_admission_prerequisites(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, dict]:
    """Read current paper-launch evidence without changing execution state.

    Dataset preflight is necessary but not sufficient for a paper handoff.
    These gates use persisted evidence and configuration only: this projection
    never creates singleton state, invokes a broker gateway, or changes risk,
    recovery, notification, or audit records.
    """
    gates: dict[str, dict] = {}

    try:
        broker = stock_paper_broker_status(db)
        accounting = broker.get("accounting") or {}
        venue = str(broker.get("paper_broker") or "").strip().lower()
        provider_complete = accounting.get("provider_evidence_complete")
        accounting_ready = accounting.get("ready") is True
        venue_qualification = accounting.get("venue_qualification")
        # Keep the historical broker projection compatible for callers that
        # supply the pre-qualification shape, while the live status path below
        # always includes the durable package state.
        legacy_projection = venue_qualification is None
        venue_qualification = venue_qualification or paper_venue_qualification_status(db, venue)
        qualified = (
            venue == "alpaca_paper" and accounting_ready
            if legacy_projection else
            accounting_ready
            and provider_complete is not False
            and venue_qualification.get("ready_for_paper_admission") is True
        )
        gates["broker_qualification"] = _gate(
            "pass" if qualified else "fail",
            reason=None if qualified else (
                "Affirmative broker accounting and provider qualification evidence is required"
            ),
            evidence={
                "paper_broker": venue or None,
                "accounting_ready": accounting_ready,
                "provider_evidence_complete": provider_complete,
                "provider_evidence": TRADIER_PAPER_EVIDENCE
                if provider_complete is False else None,
                "venue_qualification": venue_qualification,
                "live_trading_blocked": broker.get("live_trading_blocked"),
            },
        )
        package_ready = (
            not legacy_projection
            and venue_qualification.get("ready_for_paper_admission") is True
        )
        gates["paper_venue_qualification"] = _gate(
            "pass" if package_ready else "fail",
            reason=None if package_ready else (
                "A passing account-specific paper venue package and separate activation authorization are required"
            ),
            evidence=venue_qualification,
        )
    except Exception as exc:
        gates["broker_qualification"] = _gate(
            "unknown",
            reason=f"Paper broker qualification evidence unavailable: {exc.__class__.__name__}",
        )

    try:
        risk_rule = db.query(RiskRule).filter(
            RiskRule.is_active.is_(True)
        ).order_by(RiskRule.id).first()
        rules = DEFAULT_RISK_RULES | ((risk_rule.value if risk_rule else {}) or {})
        portfolio = portfolio_risk_snapshot(db)
        breach_alerts = [
            alert for alert in portfolio.get("alerts", [])
            if alert.get("severity") == "breach"
        ]
        risk_ready = bool(
            risk_rule
            and rules.get("paper_only", True)
            and not rules.get("kill_switch_enabled", False)
            and not breach_alerts
        )
        gates["risk_state"] = _gate(
            "pass" if risk_ready else "fail",
            reason=None if risk_ready else (
                "Paper risk controls are missing, disabled, or the kill switch is active"
            ),
            evidence={
                "risk_rule_present": bool(risk_rule),
                "paper_only": bool(rules.get("paper_only", True)),
                "kill_switch_enabled": bool(rules.get("kill_switch_enabled", False)),
                "breach_alerts": breach_alerts,
            },
        )
    except Exception as exc:
        gates["risk_state"] = _gate(
            "unknown",
            reason=f"Paper risk state unavailable: {exc.__class__.__name__}",
        )

    try:
        recovery = db.get(StockPaperRecoveryState, 1)
        observed_at = _utc(now or _now())
        monitor_at = _utc(recovery.last_monitor_heartbeat_at) if recovery else None
        watchdog_at = _utc(recovery.last_watchdog_heartbeat_at) if recovery else None
        monitor_fresh = bool(
            monitor_at
            and timedelta(0) <= observed_at - monitor_at <= HEARTBEAT_TIMEOUT
        )
        watchdog_fresh = bool(
            watchdog_at
            and timedelta(0) <= observed_at - watchdog_at <= HEARTBEAT_TIMEOUT
        )
        recovery_ready = bool(
            recovery
            and recovery.status == "armed"
            and not recovery.accounting_review_required
            and monitor_fresh
            and watchdog_fresh
        )
        gates["recovery_state"] = _gate(
            "pass" if recovery_ready else "fail" if recovery else "unknown",
            reason=(
                None if recovery_ready else
                "Paper recovery is not armed with current monitor and watchdog evidence"
                if recovery else
                "Paper recovery readiness has not been initialized"
            ),
            evidence={
                "status": recovery.status if recovery else None,
                "accounting_review_required": (
                    recovery.accounting_review_required if recovery else None
                ),
                "monitor_heartbeat": (
                    recovery.last_monitor_heartbeat_at if recovery else None
                ),
                "watchdog_heartbeat": (
                    recovery.last_watchdog_heartbeat_at if recovery else None
                ),
                "monitor_fresh": monitor_fresh,
                "watchdog_fresh": watchdog_fresh,
                "heartbeat_timeout_seconds": HEARTBEAT_TIMEOUT.total_seconds(),
            },
        )
    except Exception as exc:
        gates["recovery_state"] = _gate(
            "unknown",
            reason=f"Paper recovery readiness unavailable: {exc.__class__.__name__}",
        )

    try:
        unresolved_critical = db.query(Notification).filter(
            Notification.status != "resolved",
            Notification.severity == "critical",
            Notification.category != "deployment_monitor",
        ).count()
        gates["notifications"] = _gate(
            "pass" if not unresolved_critical else "fail",
            reason=None if not unresolved_critical else (
                "Unresolved critical notifications require operator review"
            ),
            evidence={"unresolved_critical": unresolved_critical},
        )
    except Exception as exc:
        gates["notifications"] = _gate(
            "unknown",
            reason=f"Notification safety state unavailable: {exc.__class__.__name__}",
        )

    try:
        audit = _audit_chain_check(db)
        audit_status = audit.get("status")
        gates["audit_chain"] = _gate(
            "pass" if audit_status == "clear" else "fail" if audit_status == "breach" else "unknown",
            reason=None if audit_status == "clear" else (
                audit.get("message") or "Audit-chain integrity is not verified"
            ),
            evidence=audit,
        )
    except Exception as exc:
        gates["audit_chain"] = _gate(
            "unknown",
            reason=f"Audit-chain integrity unavailable: {exc.__class__.__name__}",
        )
    return gates


def _all_pass(gates: dict[str, dict]) -> bool:
    return bool(gates) and all(item.get("status") == "pass" for item in gates.values())


def classify_launch_prerequisites(gates: dict[str, dict]) -> tuple[str, str]:
    """Classify a non-mutating launch preflight result for the API contract.

    ``unknown_outside_session`` is intentionally narrower than generic
    ``unknown``: an unavailable regular-session feed is expected outside the
    decision session and must not be represented as a hard failure.  Any
    explicit failed gate remains blocked, even when another gate is unknown.
    """
    failed = next(
        (gate for gate in gates.values() if gate.get("status") == "fail"),
        None,
    )
    if failed is not None:
        return "blocked", failed.get("reason") or "A launch prerequisite is blocked"

    unknown = [
        gate for gate in gates.values()
        if gate.get("status") not in {"pass", "fail"}
    ]
    if unknown:
        feed = gates.get("verified_feed") or {}
        evidence = feed.get("evidence") or {}
        outside_session = (
            feed.get("status") == "unknown"
            and evidence.get("regular_session") is False
        )
        if outside_session:
            return (
                "unknown_outside_session",
                feed.get("reason")
                or "Regular-session feed preflight is unavailable outside the session",
            )
        return "unknown", (
            unknown[0].get("reason")
            or "A launch prerequisite is currently unknown"
        )

    if not gates:
        return "unknown", "Launch prerequisite evidence is unavailable"
    return "ready", "All current launch prerequisites passed"


def launch_preflight_eligibility(
    cycle: StockLearningCycle | None,
    *,
    prerequisites_ready: bool,
    cycle_gates: dict[str, dict] | None = None,
) -> tuple[bool, str]:
    """Return approval eligibility without requiring or inspecting approval.

    The approval belongs after paper-canary admission.  The durable handoff
    service represents that point as ``preflight``/``awaiting_preflight``.
    The handoff records a missing approval as ``blocked`` (or ``deferred`` on
    retry), so those statuses remain eligible when the admission lineage is
    intact; the approval gate itself is deliberately excluded.  Earlier
    training/admission states and later trial states must not claim approval
    eligibility merely because their prerequisite snapshot is clear.
    """
    if not prerequisites_ready:
        return False, "Current launch prerequisites have not all passed"
    if cycle is None:
        return False, "A learning cycle is required before paper approval eligibility can be granted"
    if cycle.stage != "preflight" or cycle.status not in {
        "awaiting_preflight", "blocked", "deferred",
    }:
        return (
            False,
            "Cycle must be at the preflight stage before paper approval eligibility can be granted",
        )
    if not cycle.model_run_id or not cycle.binding_id or not cycle.trial_id:
        return (
            False,
            "The preflight cycle must have an accepted model, paper binding, and forward trial",
        )
    non_approval_failures = [
        (name, gate)
        for name, gate in (cycle_gates or {}).items()
        if name != "paper_run_approval" and gate.get("status") == "fail"
    ]
    if non_approval_failures:
        name, gate = non_approval_failures[0]
        return (
            False,
            gate.get("reason")
            or f"Cycle admission gate {name} has not passed",
        )
    return True, "All current launch prerequisites passed; cycle is eligible for paper approval"


def _number(value: Any) -> float | None:
    """Parse finite metric values without allowing malformed evidence to pass."""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _metric_gate(
    *,
    name: str,
    challenger: dict,
    comparison: dict | None,
    direction: str,
    minimum_samples: int,
    challenger_sample_count: Any = None,
    comparison_sample_count: Any = None,
) -> dict:
    """Compare one immutable metric; every metric remains independently visible."""
    challenger_value = _number(challenger.get(name))
    comparison_value = _number(comparison.get(name)) if comparison else None
    challenger_samples = _number(
        challenger.get("sample_count") if challenger_sample_count is None else challenger_sample_count
    )
    comparison_samples = _number(
        comparison.get("sample_count") if comparison and comparison_sample_count is None
        else comparison_sample_count
    ) if comparison else None
    evidence = {
        "challenger": challenger_value,
        "comparison": comparison_value,
        "challenger_sample_count": challenger_samples,
        "comparison_sample_count": comparison_samples,
        "direction": direction,
        "minimum_sample_count": minimum_samples,
    }
    if challenger_value is None or challenger_samples is None or challenger_samples < minimum_samples:
        return _gate(
            "fail",
            reason=f"challenger {name} evidence is missing or below the minimum sample count",
            evidence=evidence,
        )
    if comparison is None:
        return _gate("unknown", reason=f"comparison {name} evidence is unavailable", evidence=evidence)
    if comparison_value is None or comparison_samples is None or comparison_samples < minimum_samples:
        return _gate(
            "unknown",
            reason=f"comparison {name} evidence is missing or below the minimum sample count",
            evidence=evidence,
        )
    passed = (
        challenger_value <= comparison_value
        if direction == "lower"
        else challenger_value >= comparison_value
    )
    return _gate(
        "pass" if passed else "fail",
        reason=None if passed else f"challenger {name} is weaker than its comparison",
        evidence=evidence,
    )


def _model_comparison_gate(
    db: Session,
    cycle: StockLearningCycle,
    target_model: StockModelRegistry | None,
    *,
    before_model_run_id: str | None,
    readiness_report: dict | None,
) -> dict:
    """Require independent accuracy, calibration, cost, and risk comparisons.

    A single aggregate score is deliberately not used.  A challenger must
    clear each dimension against the simple training baseline and, when one is
    available, the incumbent model.  The comparison uses only immutable model
    manifests; forward-trial readiness and operational gates remain separate.
    """
    if target_model is None:
        return _gate("unknown", reason="challenger model evidence is unavailable")

    manifest = target_model.training_metadata or {}
    challenger = manifest.get("final_holdout_metrics") or {}
    baseline = manifest.get("final_holdout_baseline") or {}
    calibration = manifest.get("calibration_metrics") or {}
    selected_calibration = calibration.get("selected_calibrated") or {}
    baseline_calibration = calibration.get("training_prevalence_baseline") or {}

    incumbent_id = (
        (cycle.evidence or {}).get("handoff", {}).get("incumbent_model_run_id")
        or (cycle.evidence or {}).get("admission", {}).get("incumbent_model_run_id")
        or before_model_run_id
    )
    if incumbent_id == target_model.run_id:
        incumbent_id = None
    if incumbent_id is None:
        incumbent_state = db.scalar(
            select(StockModelLifecycleState)
            .where(
                StockModelLifecycleState.lifecycle_state == "champion",
                StockModelLifecycleState.model_run_id != target_model.run_id,
            )
            .order_by(StockModelLifecycleState.updated_at.desc())
        )
        incumbent_id = incumbent_state.model_run_id if incumbent_state else None
    incumbent_model = db.get(StockModelRegistry, incumbent_id) if incumbent_id else None
    incumbent_manifest = incumbent_model.training_metadata if incumbent_model else {}
    incumbent_metrics = incumbent_manifest.get("final_holdout_metrics") or {}
    incumbent_calibration = (incumbent_manifest.get("calibration_metrics") or {}).get(
        "selected_calibrated"
    ) or {}

    sample_gate = _gate(
        "pass" if _number(challenger.get("sample_count")) is not None
        and _number(challenger.get("sample_count")) >= MIN_COMPARISON_SAMPLES else "fail",
        reason=None if _number(challenger.get("sample_count")) is not None
        and _number(challenger.get("sample_count")) >= MIN_COMPARISON_SAMPLES
        else "challenger final holdout has fewer than the minimum comparison samples",
        evidence={
            "challenger_sample_count": challenger.get("sample_count"),
            "minimum_sample_count": MIN_COMPARISON_SAMPLES,
        },
    )
    baseline_accuracy = {
        "brier_score": baseline.get("brier_score"),
        "log_loss": baseline.get("log_loss"),
        "sample_count": baseline.get("sample_count"),
    }
    selected_accuracy = {
        "brier_score": challenger.get("brier_score"),
        "log_loss": challenger.get("log_loss"),
        "sample_count": challenger.get("sample_count"),
    }
    baseline_calibration_values = {
        "brier_score": baseline_calibration.get("brier_score"),
        "log_loss": baseline_calibration.get("log_loss"),
        "sample_count": baseline_calibration.get("sample_count"),
    }
    selected_calibration_values = {
        "brier_score": selected_calibration.get("brier_score"),
        "log_loss": selected_calibration.get("log_loss"),
        "sample_count": selected_calibration.get("sample_count"),
    }
    challenger_return = (challenger.get("cost_aware_nonoverlapping_returns") or {})
    baseline_return = (baseline.get("cost_aware_nonoverlapping_returns") or {})
    incumbent_return = (incumbent_metrics.get("cost_aware_nonoverlapping_returns") or {})

    subgates = {
        "minimum_samples": sample_gate,
        "baseline_brier_score": _metric_gate(
            name="brier_score", challenger=selected_accuracy,
            comparison=baseline_accuracy, direction="lower",
            minimum_samples=MIN_COMPARISON_SAMPLES,
        ),
        "baseline_log_loss": _metric_gate(
            name="log_loss", challenger=selected_accuracy,
            comparison=baseline_accuracy, direction="lower",
            minimum_samples=MIN_COMPARISON_SAMPLES,
        ),
        "baseline_calibration_brier_score": _metric_gate(
            name="brier_score", challenger=selected_calibration_values,
            comparison=baseline_calibration_values, direction="lower",
            minimum_samples=MIN_COMPARISON_SAMPLES,
        ),
        "baseline_calibration_log_loss": _metric_gate(
            name="log_loss", challenger=selected_calibration_values,
            comparison=baseline_calibration_values, direction="lower",
            minimum_samples=MIN_COMPARISON_SAMPLES,
        ),
        "baseline_total_return": _metric_gate(
            name="total_return", challenger=challenger_return,
            comparison=baseline_return, direction="higher",
            minimum_samples=MIN_COMPARISON_SAMPLES,
            challenger_sample_count=challenger.get("sample_count"),
            comparison_sample_count=baseline.get("sample_count"),
        ),
        "baseline_max_drawdown": _metric_gate(
            name="max_drawdown", challenger=challenger_return,
            comparison=baseline_return, direction="lower",
            minimum_samples=MIN_COMPARISON_SAMPLES,
            challenger_sample_count=challenger.get("sample_count"),
            comparison_sample_count=baseline.get("sample_count"),
        ),
    }
    if incumbent_model is None:
        incumbent_gate = _gate(
            "pass",
            reason="no prior champion exists; baseline comparison is the independent comparator",
            evidence={"incumbent_model_run_id": None},
        )
        incumbent_subgates = {}
    else:
        incumbent_subgates = {
            "brier_score": _metric_gate(
                name="brier_score", challenger=selected_accuracy,
                comparison=incumbent_metrics, direction="lower",
                minimum_samples=MIN_COMPARISON_SAMPLES,
            ),
            "log_loss": _metric_gate(
                name="log_loss", challenger=selected_accuracy,
                comparison=incumbent_metrics, direction="lower",
                minimum_samples=MIN_COMPARISON_SAMPLES,
            ),
            "calibration_brier_score": _metric_gate(
                name="brier_score", challenger=selected_calibration_values,
                comparison=incumbent_calibration, direction="lower",
                minimum_samples=MIN_COMPARISON_SAMPLES,
            ),
            "calibration_log_loss": _metric_gate(
                name="log_loss", challenger=selected_calibration_values,
                comparison=incumbent_calibration, direction="lower",
                minimum_samples=MIN_COMPARISON_SAMPLES,
            ),
            "total_return": _metric_gate(
                name="total_return", challenger=challenger_return,
                comparison=incumbent_return, direction="higher",
                minimum_samples=MIN_COMPARISON_SAMPLES,
                challenger_sample_count=challenger.get("sample_count"),
                comparison_sample_count=incumbent_metrics.get("sample_count"),
            ),
            "max_drawdown": _metric_gate(
                name="max_drawdown", challenger=challenger_return,
                comparison=incumbent_return, direction="lower",
                minimum_samples=MIN_COMPARISON_SAMPLES,
                challenger_sample_count=challenger.get("sample_count"),
                comparison_sample_count=incumbent_metrics.get("sample_count"),
            ),
        }
        incumbent_gate = _gate(
            "fail" if any(item["status"] == "fail" for item in incumbent_subgates.values())
            else "unknown" if any(item["status"] == "unknown" for item in incumbent_subgates.values())
            else "pass",
            reason=next(
                (item.get("reason") for item in incumbent_subgates.values() if item["status"] != "pass"),
                None,
            ),
            evidence={"incumbent_model_run_id": incumbent_model.run_id, "gates": incumbent_subgates},
        )

    baseline_gate = _gate(
        "fail" if any(item["status"] == "fail" for item in subgates.values())
        else "unknown" if any(item["status"] == "unknown" for item in subgates.values())
        else "pass",
        reason=next(
            (item.get("reason") for item in subgates.values() if item["status"] != "pass"),
            None,
        ),
        evidence={"gates": subgates},
    )
    readiness_risk = ((readiness_report or {}).get("gates") or {}).get("risk_per_trade")
    risk_gate = _gate(
        "pass" if readiness_risk and readiness_risk.get("status") == "pass" else
        "unknown" if readiness_report is None else "fail",
        reason=None if readiness_risk and readiness_risk.get("status") == "pass"
        else "forward risk evidence is not explicitly passing",
        evidence=readiness_risk or {},
    )
    all_subgates = {"baseline": baseline_gate, "incumbent": incumbent_gate, "forward_risk": risk_gate}
    status = (
        "fail" if any(item["status"] == "fail" for item in all_subgates.values())
        else "unknown" if any(item["status"] == "unknown" for item in all_subgates.values())
        else "pass"
    )
    return _gate(
        status,
        reason=next(
            (item.get("reason") for item in all_subgates.values() if item["status"] != "pass"),
            None,
        ),
        evidence={
            "challenger_model_run_id": target_model.run_id,
            "incumbent_model_run_id": incumbent_model.run_id if incumbent_model else None,
            "minimum_sample_count": MIN_COMPARISON_SAMPLES,
            "gates": all_subgates,
            "challenger": {
                "final_holdout": challenger,
                "calibration": selected_calibration,
                "cost_aware": challenger_return,
            },
            "baseline": {
                "final_holdout": baseline,
                "calibration": baseline_calibration,
                "cost_aware": baseline_return,
            },
        },
    )


def _event(
    db: Session,
    *,
    cycle: StockLearningCycle,
    stage: str,
    decision: str,
    actor: str,
    reason: str,
    evidence: dict | None = None,
) -> StockLearningCycleEvent:
    payload = _safe(evidence or {})
    identity = {
        "cycle_id": cycle.cycle_id,
        "stage": stage,
        "decision": decision,
        "actor": actor,
        "reason": reason.strip(),
        "evidence": payload,
    }
    digest = _digest(identity)
    existing = db.scalar(select(StockLearningCycleEvent).where(
        StockLearningCycleEvent.decision_sha256 == digest
    ))
    if existing:
        return existing
    row = StockLearningCycleEvent(
        cycle_id=cycle.cycle_id, stage=stage, decision=decision,
        actor=actor, reason=reason.strip(), decision_sha256=digest, evidence=payload,
    )
    db.add(row)
    db.flush()
    return row


def _projection_event(event: StockLearningCycleEvent) -> dict:
    return {
        "id": event.id, "stage": event.stage, "decision": event.decision,
        "actor": event.actor, "reason": event.reason,
        "decision_sha256": event.decision_sha256, "evidence": event.evidence,
        "created_at": event.created_at,
    }


def create_learning_cycle(
    db: Session,
    *,
    symbols: Iterable[str],
    cutoff_at: date,
    horizon_days: int,
    provider: str,
    actor: str,
    trigger: str = "manual",
    seed: int = 42,
    paper_session_date: date | None = None,
) -> tuple[StockLearningCycle, bool]:
    normalized = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    if trigger not in VALID_TRIGGERS:
        raise StockTrainingError("Only manual or scheduled learning cycles are allowed")
    if not normalized:
        raise StockTrainingError("At least one stock symbol is required")
    if paper_session_date is not None and session_bounds(paper_session_date) is None:
        raise StockTrainingError("Paper session date must be a regular NYSE session")
    if paper_session_date is not None and normalized != ONE_SESSION_SYMBOLS:
        raise StockTrainingError("One-session paper cycles must use AAPL, MSFT, QQQ, and SPY")
    request = {
        "symbols": normalized, "cutoff_date": cutoff_at.isoformat(),
        "horizon_days": horizon_days, "provider": provider,
        "trigger": trigger, "seed": seed,
        **({"paper_session_date": paper_session_date.isoformat()} if paper_session_date else {}),
    }
    request_sha256 = _digest(request)
    existing = db.scalar(select(StockLearningCycle).where(
        StockLearningCycle.request_sha256 == request_sha256
    ))
    if existing:
        return existing, True
    cycle = StockLearningCycle(
        cycle_id=request_sha256, request_sha256=request_sha256, trigger=trigger,
        status="blocked", stage="preflight", requested_by=actor, symbols=normalized,
        cutoff_date=cutoff_at, horizon_days=horizon_days, provider=provider, seed=seed,
        gates={}, evidence={"request": request}, last_reason="Preflight has not passed",
    )
    db.add(cycle)
    db.flush()
    gates = evaluate_cycle_prerequisites(db, symbols=normalized, provider=provider)
    cycle.gates = gates
    cycle.evidence = {"request": request, "preflight_at": _now().isoformat()}
    if not _all_pass(gates):
        deferred = trigger == "scheduled" and any(
            gate.get("status") == "unknown" for gate in gates.values()
        )
        cycle.status = "deferred" if deferred else "blocked"
        cycle.stage = "preflight"
        cycle.last_reason = "Scheduled cycle deferred pending prerequisite evidence" if deferred else next(
            (gate.get("reason") for gate in gates.values() if gate.get("status") != "pass"),
            "Required prerequisite gate is unavailable",
        )
        _event(
            db, cycle=cycle, stage="preflight",
            decision="deferred" if deferred else "blocked", actor=actor,
            reason=cycle.last_reason, evidence={"gates": gates},
        )
        return cycle, False
    _event(db, cycle=cycle, stage="preflight", decision="pass", actor=actor,
           reason="Verified feed, scheduler, paper ledger, provenance, and readiness-history gates passed",
           evidence={"gates": gates})
    try:
        job, duplicate = create_stock_training_job(
            db, symbols=normalized, horizon_bars=horizon_days, actor=actor,
            cutoff_at=cutoff_at, provider=provider, trigger=trigger, seed=seed,
        )
    except StockTrainingError as exc:
        cycle.status = "deferred" if trigger == "scheduled" else "blocked"
        cycle.last_reason = str(exc)
        _event(db, cycle=cycle, stage="dataset", decision="deferred" if trigger == "scheduled" else "blocked",
               actor=actor, reason=str(exc), evidence={"gates": gates})
        return cycle, False
    cycle.snapshot_id = job.snapshot_id
    cycle.training_job_id = job.id
    cycle.stage = "training"
    cycle.status = "deferred" if job.status == "deferred" else "queued"
    cycle.last_reason = job.queue_error or ("Existing deduplicated training intent" if duplicate else "Bounded challenger training queued")
    _event(db, cycle=cycle, stage="dataset", decision="pass", actor=actor,
           reason="Immutable verified dataset snapshot admitted", evidence={"snapshot_id": job.snapshot_id})
    _event(db, cycle=cycle, stage="training",
           decision="deferred" if job.status == "deferred" else "pass", actor=actor,
           reason=cycle.last_reason, evidence={"training_job_id": job.id, "deduplicated": duplicate})
    return cycle, duplicate


def sync_cycle_from_training_job(db: Session, job_id: str, *, actor: str = "training_worker") -> StockLearningCycle | None:
    cycle = db.scalar(select(StockLearningCycle).where(StockLearningCycle.training_job_id == job_id))
    if not cycle:
        return None
    job = db.get(StockTrainingJob, job_id)
    if not job:
        return cycle
    if job.status in {"queued", "running", "cancel_requested"}:
        cycle.status = "running" if job.status == "running" else "queued"
        cycle.stage = "training"
        return cycle
    if job.status != "succeeded" or not job.result_run_id:
        cycle.status = "failed" if job.status == "failed" else "blocked"
        cycle.stage = "training"
        cycle.last_reason = job.failure_detail or f"Training ended with status {job.status}"
        _event(db, cycle=cycle, stage="training", decision="fail", actor=actor,
               reason=cycle.last_reason, evidence={"job_id": job.id, "status": job.status})
        return cycle
    model = db.get(StockModelRegistry, job.result_run_id)
    if not model:
        cycle.status, cycle.stage, cycle.last_reason = "blocked", "validation", "Training completed without a model registry record"
        _event(db, cycle=cycle, stage="validation", decision="unknown", actor=actor,
               reason=cycle.last_reason, evidence={"job_id": job.id})
        return cycle
    manifest = model.training_metadata or {}
    validation_ok = bool(
        manifest.get("purged_expanding_walkforward")
        and int(manifest.get("final_holdout_evaluation_count", 0)) == 1
        and manifest.get("final_holdout_consumption")
        and int(manifest.get("embargo_days", -1)) >= 0
    )
    admission_deferral_reason = (
        cycle.last_reason
        if cycle.status == "deferred" and cycle.stage == "admission"
        else None
    )
    cycle.model_run_id = model.run_id
    cycle.stage = "admission"
    cycle.status = "awaiting_admission" if cycle.trigger == "scheduled" else "awaiting_forward_evidence"
    cycle.last_reason = admission_deferral_reason or (
        "Training completed; awaiting automatic paper-canary admission"
        if cycle.trigger == "scheduled"
        else "Awaiting a completed, aligned forward paper trial"
    )
    _event(
        db, cycle=cycle, stage="validation",
        decision="pass" if validation_ok else "fail", actor=actor,
        reason="Purged walk-forward, embargo, and one-use holdout evidence verified"
        if validation_ok else "Registered model is missing leakage-safe validation or holdout proof",
        evidence={
            "model_run_id": model.run_id, "manifest_sha256": model.manifest_sha256,
            "purged_walkforward": bool(manifest.get("purged_expanding_walkforward")),
            "embargo_days": manifest.get("embargo_days"),
            "holdout_evaluation_count": manifest.get("final_holdout_evaluation_count"),
            "holdout_consumption": manifest.get("final_holdout_consumption"),
        },
    )
    if not validation_ok:
        cycle.status = "blocked"
        cycle.last_reason = "Leakage-safe validation and exactly-once holdout evidence are incomplete"
    return cycle


HANDOFF_ACTOR = "paper_learning_automation"
RESOLVED_PAPER_TRIAL_STATUSES = {"stopped", "completed"}


def _active_scheduled_binding_is_resolved(
    db: Session,
    binding: StockPaperModelBinding,
) -> bool:
    """Return whether a scheduled binding no longer owns an active trial."""
    if not binding.source_cycle_id:
        return False
    trial_status = db.scalar(
        select(StockPaperTrial.status).where(
            StockPaperTrial.source_cycle_id == binding.source_cycle_id,
        )
    )
    return trial_status in RESOLVED_PAPER_TRIAL_STATUSES


def _handoff_failure(
    db: Session,
    cycle: StockLearningCycle,
    *,
    stage: str,
    status: str,
    reason: str,
    evidence: dict | None = None,
) -> StockLearningCycle:
    attempt_evidence = {
        **(evidence or {}),
        "attempted_at": _now().isoformat(),
    }
    cycle.stage = stage
    cycle.status = status
    cycle.last_reason = reason.strip()
    _event(
        db,
        cycle=cycle,
        stage=stage,
        decision="deferred" if status == "deferred" else "blocked",
        actor=HANDOFF_ACTOR,
        reason=cycle.last_reason,
        evidence=attempt_evidence,
    )
    write_audit_log(
        db,
        event_type="stock_learning_cycle",
        action="automatic_paper_trial_handoff",
        status=status,
        message=cycle.last_reason,
        entity_type="stock_learning_cycle",
        payload={
            "cycle_id": cycle.cycle_id,
            "binding_id": cycle.binding_id,
            "trial_id": cycle.trial_id,
            "paper_only": True,
            "live_authorized": False,
            **attempt_evidence,
        },
    )
    return cycle


def admit_scheduled_learning_cycle(
    db: Session,
    cycle_id: str,
    *,
    actor: str = HANDOFF_ACTOR,
) -> StockLearningCycle:
    """Admit one completed scheduled challenger into exactly one paper trial.

    The cycle-owned source keys on the binding and trial are the durable
    idempotency fence. Lifecycle transitions remain delegated to the existing
    binding service; this coordinator never mutates the immutable registry row.
    """
    cycle = db.get(StockLearningCycle, cycle_id)
    if not cycle:
        raise StockTrainingError("Learning cycle not found")
    if cycle.trigger != "scheduled":
        raise StockTrainingError("Automatic paper admission is limited to scheduled cycles")
    pause_reason = _scheduled_learning_pause_reason(db)
    if pause_reason:
        _defer_scheduled_cycle_for_pause(
            db, cycle, reason=pause_reason, stage="admission",
        )
        return cycle
    if cycle.binding_id and cycle.trial_id:
        return cycle
    if cycle.status in {"complete", "promoted", "demoted", "rolled_back", "failed"}:
        return cycle

    job = db.get(StockTrainingJob, cycle.training_job_id) if cycle.training_job_id else None
    if not job or job.status != "succeeded" or not job.result_run_id:
        return _handoff_failure(
            db, cycle, stage="admission", status="deferred",
            reason="Scheduled challenger is not yet a successfully completed training job",
            evidence={"training_job_id": cycle.training_job_id, "job_status": job.status if job else None},
        )
    if cycle.model_run_id != job.result_run_id:
        cycle.model_run_id = job.result_run_id
    validation = _automatic_validation_gate(db, cycle)
    cycle.gates = {**(cycle.gates or {}), "admission_validation": validation}
    if validation["status"] != "pass":
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked",
            reason=validation.get("reason", "Leakage-safe training validation is incomplete"),
            evidence={"validation": validation},
        )

    model = db.get(StockModelRegistry, cycle.model_run_id)
    snapshot = db.get(StockDatasetSnapshot, cycle.snapshot_id)
    if not model or not snapshot or model.snapshot_id != cycle.snapshot_id:
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked",
            reason="Completed challenger is missing its exact immutable model or dataset snapshot",
            evidence={"model_run_id": cycle.model_run_id, "snapshot_id": cycle.snapshot_id},
        )
    if snapshot.metadata_json.get("binding_eligible") is not True:
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked",
            reason=snapshot.metadata_json.get(
                "binding_eligibility_reason", "Dataset snapshot is not eligible for paper binding"
            ),
            evidence={"snapshot_id": snapshot.snapshot_id, "binding_eligible": False},
        )

    _lock_lifecycle_admission(db)
    active_state = db.scalar(
        select(StockPaperBindingState)
        .where(StockPaperBindingState.id == 1)
        .with_for_update()
    )
    active_binding = (
        db.get(StockPaperModelBinding, active_state.active_binding_id)
        if active_state and active_state.active_binding_id
        else None
    )
    incumbent_binding_id = active_binding.id if active_binding else None
    incumbent_model_run_id = active_binding.model_run_id if active_binding else None
    if (
        active_binding
        and active_binding.source_cycle_id
        and active_binding.source_cycle_id != cycle.cycle_id
        and not _active_scheduled_binding_is_resolved(db, active_binding)
    ):
        return _handoff_failure(
            db,
            cycle,
            stage="admission",
            status="deferred",
            reason=(
                "Another scheduled cycle owns the active paper canary; "
                "retry admission after its trial resolves"
            ),
            evidence={
                "active_cycle_id": active_binding.source_cycle_id,
                "active_binding_id": active_binding.id,
            },
        )
    binding = db.get(StockPaperModelBinding, cycle.binding_id) if cycle.binding_id else db.scalar(
        select(StockPaperModelBinding).where(
            StockPaperModelBinding.source_cycle_id == cycle.cycle_id,
        )
    )
    try:
        if binding is None:
            binding = create_stock_paper_binding(
                db,
                model_run_id=model.run_id,
                snapshot_id=snapshot.snapshot_id,
                actor=actor,
                purpose="scheduled paper trial",
                reason=f"Automatic admission for scheduled learning cycle {cycle.cycle_id}",
                source_cycle_id=cycle.cycle_id,
            )
        if (
            binding.model_run_id != model.run_id
            or binding.snapshot_id != snapshot.snapshot_id
            or binding.paper_only is not True
            or binding.live_authorized is not False
        ):
            raise StockTrainingError("Paper binding lineage or authorization policy is invalid")
        cycle.binding_id = binding.id
        cycle.active_binding_id = binding.id
        state = db.get(StockPaperBindingState, 1)
        if not state or state.active_binding_id != binding.id:
            raise StockTrainingError("Scheduled paper binding is not the active paper canary")
        lifecycle = db.get(StockModelLifecycleState, model.run_id)
        if not lifecycle or lifecycle.lifecycle_state != "paper_canary":
            raise StockTrainingError("Scheduled challenger did not reach the paper-canary lifecycle state")
        trial = db.get(StockPaperTrial, cycle.trial_id) if cycle.trial_id else db.scalar(
            select(StockPaperTrial).where(StockPaperTrial.source_cycle_id == cycle.cycle_id)
        )
        if trial is None:
            trial = create_trial(
                db, binding_id=binding.id, actor=actor, source_cycle_id=cycle.cycle_id,
            )
        if trial.binding_id != binding.id:
            raise StockTrainingError("Forward trial is linked to a different paper binding")
        cycle.trial_id = trial.id
    except StockTrainingError as exc:
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked", reason=str(exc),
            evidence={"model_run_id": model.run_id, "snapshot_id": snapshot.snapshot_id},
        )

    if trial.status == "blocked" and trial.blocked_reason:
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked",
            reason=trial.blocked_reason,
            evidence={"binding_id": binding.id, "trial_id": trial.id},
        )
    cycle.stage = "preflight"
    cycle.status = "awaiting_preflight"
    cycle.last_reason = "Paper canary and forward trial admitted; awaiting authenticated preflight"
    cycle.gates = {
        **(cycle.gates or {}),
        "paper_binding": _gate(
            "pass",
            evidence={
                "binding_id": binding.id,
                "model_run_id": binding.model_run_id,
                "snapshot_id": binding.snapshot_id,
                "paper_only": True,
                "live_authorized": False,
            },
        ),
        "forward_trial": _gate(
            "pass",
            evidence={"trial_id": trial.id, "binding_id": trial.binding_id, "policy": trial.policy},
        ),
    }
    cycle.evidence = {
        **(cycle.evidence or {}),
        "handoff": {
            "binding_id": binding.id,
            "trial_id": trial.id,
            "incumbent_binding_id": incumbent_binding_id,
            "incumbent_model_run_id": incumbent_model_run_id,
            "admitted_at": _now().isoformat(),
            "paper_only": True,
            "live_authorized": False,
        },
    }
    _event(
        db,
        cycle=cycle,
        stage="admission",
        decision="pass",
        actor=actor,
        reason=cycle.last_reason,
        evidence={"binding_id": binding.id, "trial_id": trial.id},
    )
    db.flush()
    return cycle


def start_scheduled_learning_trial(
    db: Session,
    cycle_id: str,
    *,
    actor: str = HANDOFF_ACTOR,
) -> StockLearningCycle:
    """Retry only the admitted trial's safe, authenticated start gates."""
    cycle = admit_scheduled_learning_cycle(db, cycle_id, actor=actor)
    if _scheduled_learning_pause_reason(db):
        return cycle
    if not cycle.binding_id or not cycle.trial_id:
        return cycle
    if cycle.status in {"complete", "promoted", "demoted", "rolled_back", "failed"}:
        return cycle
    trial = db.get(StockPaperTrial, cycle.trial_id)
    binding_state = db.get(StockPaperBindingState, 1)
    if not trial:
        return _handoff_failure(
            db, cycle, stage="admission", status="blocked",
            reason="Scheduled cycle points to a missing forward trial",
        )
    if not binding_state or binding_state.active_binding_id != cycle.binding_id:
        return _handoff_failure(
            db, cycle, stage="preflight", status="blocked",
            reason="The cycle paper binding is no longer the active paper canary",
            evidence={"binding_id": cycle.binding_id},
        )
    if trial.status == "running":
        cycle.stage, cycle.status = "forward_trial", "running_forward_trial"
        cycle.last_reason = "Forward paper trial is running"
        return cycle
    if trial.status == "completed":
        cycle.stage, cycle.status = "promotion", "awaiting_forward_evidence"
        cycle.last_reason = "Forward paper trial completed; awaiting immutable readiness evidence"
        return cycle

    approval = paper_run_approval_projection(db, cycle)
    cycle.gates = {
        **(cycle.gates or {}),
        "paper_run_approval": _gate(
            "pass" if approval["status"] == "pass" else "fail",
            reason=approval["reason"],
            evidence={
                "approval_id": (approval.get("record") or {}).get("id"),
                "expected": approval["expected"],
                "paper_only": True,
                "live_authorized": False,
            },
        ),
    }
    if approval["status"] != "pass":
        return _handoff_failure(
            db,
            cycle,
            stage="preflight",
            status="blocked",
            reason=approval["reason"] or "Paper run approval is missing or mismatched",
            evidence={"approval": approval},
        )

    recovery = db.get(StockPaperRecoveryState, 1)
    if recovery and recovery.status != "armed":
        return _handoff_failure(
            db, cycle, stage="preflight", status="deferred",
            reason="Paper recovery is paused or awaiting operator revalidation",
            evidence={"recovery_status": recovery.status},
        )
    try:
        validate_trial_artifact(db, trial)
    except StockTrainingError as exc:
        return _handoff_failure(
            db, cycle, stage="preflight", status="blocked", reason=str(exc),
            evidence={"trial_id": trial.id},
        )
    preflight = trial_feed_preflight(db, trial, authenticated_probe=True)
    cycle.gates = {**(cycle.gates or {}), "preflight": _gate(
        "pass" if preflight.get("ready") else "unknown",
        reason=None if preflight.get("ready") else preflight.get("reason") or "Authenticated paper preflight is incomplete",
        evidence=preflight,
    )}
    cycle.evidence = {**(cycle.evidence or {}), "preflight": preflight}
    if not preflight.get("ready") or not preflight.get("regular_session"):
        reason = preflight.get("reason") or "Regular-session authenticated preflight is required"
        return _handoff_failure(
            db, cycle, stage="preflight", status="deferred",
            reason=reason, evidence={"trial_id": trial.id, "preflight": preflight},
        )
    started = start_trial(db, trial.id, actor=actor)
    if started.status == "running":
        cycle.stage, cycle.status = "forward_trial", "running_forward_trial"
        cycle.last_reason = "Authenticated paper preflight passed; forward trial started"
        _event(
            db, cycle=cycle, stage="preflight", decision="pass", actor=actor,
            reason=cycle.last_reason,
            evidence={"trial_id": trial.id, "preflight": preflight},
        )
    else:
        reason = started.blocked_reason or started.pause_reason or "Forward trial did not start"
        return _handoff_failure(
            db, cycle, stage="preflight", status="deferred",
            reason=reason, evidence={"trial_id": trial.id, "preflight": preflight},
        )
    db.flush()
    return cycle


def sync_cycle_from_trial(
    db: Session,
    trial_id: str,
    *,
    report_id: int | None = None,
    actor: str = "forward_trial_worker",
) -> StockLearningCycle | None:
    """Attach worker progress to the exact originating scheduled cycle."""
    cycle = db.scalar(select(StockLearningCycle).where(StockLearningCycle.trial_id == trial_id))
    trial = db.get(StockPaperTrial, trial_id)
    if not cycle or not trial:
        return cycle
    if trial.status == "running":
        cycle.stage, cycle.status = "forward_trial", "running_forward_trial"
        cycle.last_reason = "Forward paper trial is running"
    elif trial.status == "completed":
        cycle.stage, cycle.status = "promotion", "awaiting_forward_evidence"
        cycle.last_reason = "Forward paper trial completed; awaiting immutable readiness evidence"
    elif trial.status in {"blocked", "paused"}:
        cycle.stage, cycle.status = "preflight", "awaiting_preflight"
        cycle.last_reason = trial.blocked_reason or trial.pause_reason or "Forward trial is waiting for a safe retry"
    if report_id:
        cycle.evidence = {
            **(cycle.evidence or {}),
            "forward_report": {"trial_id": trial_id, "report_id": report_id},
        }
    _event(
        db, cycle=cycle, stage=cycle.stage, decision="pass" if trial.status in {"running", "completed"} else "deferred",
        actor=actor, reason=cycle.last_reason, evidence={"trial_id": trial_id, "report_id": report_id},
    )
    return cycle


def review_learning_cycle(
    db: Session,
    cycle_id: str,
    *,
    actor: str,
    reason: str,
    trial_id: str | None = None,
) -> StockLearningCycle:
    cycle = db.get(StockLearningCycle, cycle_id)
    if not cycle:
        raise StockTrainingError("Learning cycle not found")
    if not reason.strip():
        raise StockTrainingError("A cycle review reason is required")
    if trial_id:
        trial = db.get(StockPaperTrial, trial_id)
        if not trial or trial.lineage.get("model_run_id") != cycle.model_run_id:
            raise StockTrainingError("Forward trial is not immutably linked to this cycle model")
        cycle.trial_id = trial_id
    model = db.get(StockModelRegistry, cycle.model_run_id) if cycle.model_run_id else None
    job = db.get(StockTrainingJob, cycle.training_job_id) if cycle.training_job_id else None
    validation = bool(
        model and job and job.status == "succeeded"
        and model.training_metadata.get("purged_expanding_walkforward")
        and model.training_metadata.get("final_holdout_consumption")
        and model.training_metadata.get("final_holdout_evaluation_count") == 1
    )
    forward: dict
    if cycle.trial_id:
        forward = evaluate_promotion_readiness(db, cycle.trial_id)
        forward_status = forward.get("decision", "unknown")
    else:
        forward = {"decision": "unknown", "reason": "No completed forward paper trial is linked to this cycle"}
        forward_status = "unknown"
    gates = {
        "validation": _gate("pass" if validation else "fail", reason=None if validation else "registered validation evidence is incomplete"),
        "forward_trial": _gate("pass" if forward_status == "pass" else "fail" if forward_status == "fail" else "unknown", evidence=forward),
        "paper_only": _gate("pass", evidence={"live_authorized": False}),
    }
    cycle.gates = gates
    cycle.evidence = {
        **(cycle.evidence or {}),
        "operator_review": {"actor": actor, "reason": reason.strip(), "reviewed_at": _now().isoformat()},
        "forward_evidence": forward,
    }
    cycle.stage = "operator_review"
    cycle.status = "operator_review" if all(gate["status"] == "pass" for gate in gates.values()) else "blocked"
    cycle.last_reason = (
        "All qualification evidence passed; explicit lifecycle action is still required"
        if cycle.status == "operator_review"
        else next(gate.get("reason", "Required evidence is incomplete") for gate in gates.values() if gate["status"] != "pass")
    )
    _event(db, cycle=cycle, stage="qualification",
           decision="pass" if cycle.status == "operator_review" else "blocked",
           actor=actor, reason=cycle.last_reason, evidence={"gates": gates})
    return cycle


def cycle_action(
    db: Session,
    cycle_id: str,
    *,
    action: str,
    actor: str,
    reason: str,
) -> StockLearningCycle:
    cycle = db.get(StockLearningCycle, cycle_id)
    if not cycle:
        raise StockTrainingError("Learning cycle not found")
    if action not in {"mark_eligible", "start_canary", "promote", "demote", "retire"}:
        raise StockTrainingError("Unsupported learning-cycle action")
    if not reason.strip():
        raise StockTrainingError("A lifecycle action reason is required")
    if not cycle.model_run_id:
        raise StockTrainingError("Cycle has no registered model")
    model = db.get(StockModelRegistry, cycle.model_run_id)
    if model is None:
        raise StockTrainingError("Cycle model is unavailable")
    current = _current_lifecycle(db, model)
    desired_state = {
        "mark_eligible": "eligible",
        "start_canary": "paper_canary",
        "promote": "champion",
        "demote": "demoted",
        "retire": "retired",
    }[action]
    if (
        action in {"mark_eligible", "start_canary", "promote"}
        and cycle.status != "operator_review"
        and current.lifecycle_state != desired_state
    ):
        raise StockTrainingError("Cycle evidence must pass operator review before this action")
    # Lifecycle actions are retriable requests.  Once the requested mutable
    # pointer already has the desired state, do not append another history
    # event or attempt a second invalid transition.
    if current.lifecycle_state == desired_state:
        cycle.active_binding_id = db.scalar(select(StockPaperBindingState.active_binding_id).where(
            StockPaperBindingState.id == 1
        ))
        cycle.status = "complete" if desired_state == "champion" else "demoted" if desired_state == "demoted" else cycle.status
        cycle.stage = "promotion" if action in {"mark_eligible", "start_canary", "promote"} else "recovery"
        cycle.last_reason = reason.strip()
        return cycle
    transition_stock_model_lifecycle(
        db, model_run_id=cycle.model_run_id, action=action, actor=actor, reason=reason,
    )
    state = _current_lifecycle(db, model, for_update=False).lifecycle_state
    cycle.active_binding_id = db.scalar(select(StockPaperBindingState.active_binding_id).where(
        StockPaperBindingState.id == 1
    ))
    cycle.status = "complete" if state == "champion" else "demoted" if state == "demoted" else cycle.status
    cycle.stage = "promotion" if action in {"mark_eligible", "start_canary", "promote"} else "recovery"
    cycle.last_reason = reason.strip()
    _event(db, cycle=cycle, stage=cycle.stage, decision="complete", actor=actor,
           reason=reason, evidence={"action": action, "lifecycle_state": state, "active_binding_id": cycle.active_binding_id})
    return cycle


AUTOMATIC_PROMOTION_ACTOR = "paper_learning_automation"
AUTOMATIC_PROMOTION_JOB = "scheduled_stock_paper_promotion_job"


def _automatic_validation_gate(db: Session, cycle: StockLearningCycle) -> dict:
    job = db.get(StockTrainingJob, cycle.training_job_id) if cycle.training_job_id else None
    model = db.get(StockModelRegistry, cycle.model_run_id) if cycle.model_run_id else None
    manifest = model.training_metadata if model else {}
    passed = bool(
        job and job.status == "succeeded" and job.result_run_id == cycle.model_run_id
        and model and model.snapshot_id == cycle.snapshot_id
        and bool(manifest.get("purged_expanding_walkforward"))
        and manifest.get("final_holdout_consumption")
        and manifest.get("final_holdout_evaluation_count") == 1
        and int(manifest.get("embargo_days", -1)) >= 0
    )
    return _gate(
        "pass" if passed else "fail",
        reason=None if passed else "completed model is missing exact leakage-safe validation or one-use holdout proof",
        evidence={
            "job_id": job.id if job else None,
            "job_status": job.status if job else None,
            "model_run_id": model.run_id if model else None,
            "snapshot_id": model.snapshot_id if model else None,
            "purged_expanding_walkforward": manifest.get("purged_expanding_walkforward"),
            "embargo_days": manifest.get("embargo_days"),
            "final_holdout_evaluation_count": manifest.get("final_holdout_evaluation_count"),
            "final_holdout_consumption": manifest.get("final_holdout_consumption"),
        },
    )


def _automatic_monitor_gate(
    db: Session,
    trial: StockPaperTrial | None = None,
) -> tuple[dict, int | None]:
    snapshot = db.query(StockMonitoringSnapshot).filter_by(
        monitor_key="stock_continuous_monitor",
    ).order_by(
        StockMonitoringSnapshot.generated_at.desc(),
        StockMonitoringSnapshot.id.desc(),
    ).first()
    if not snapshot:
        return _gate("unknown", reason="current monitoring evidence is unavailable"), None
    if trial and trial.started_at:
        generated_at = _utc(snapshot.generated_at)
        started_at = _utc(trial.started_at)
        if generated_at < started_at:
            return _gate(
                "unknown",
                reason="a fresh monitoring snapshot after the paper-canary start is required",
                evidence={
                    "snapshot_id": snapshot.id,
                    "generated_at": generated_at.isoformat(),
                    "canary_started_at": started_at.isoformat(),
                },
            ), snapshot.id
    checks = snapshot.checks or []
    passed = snapshot.status == "clear" and bool(checks) and all(
        check.get("status") == "clear" for check in checks
    )
    return _gate(
        "pass" if passed else "fail",
        reason=None if passed else "current monitoring health is not clear",
        evidence={"snapshot_id": snapshot.id, "status": snapshot.status, "checks": checks},
    ), snapshot.id


def _automatic_trial_and_report(
    db: Session, cycle: StockLearningCycle, active_binding: StockPaperModelBinding | None,
) -> tuple[StockPaperTrial | None, dict | None]:
    # Never infer a scheduled trial from the current active binding. The cycle
    # owns one immutable trial link, and a later binding must not redirect old
    # evidence or promotion decisions.
    trial = db.get(StockPaperTrial, cycle.trial_id) if cycle.trial_id else None
    if trial is None:
        return None, None
    try:
        report = evaluate_promotion_readiness(db, trial.id)
    except Exception:
        # A malformed or unavailable report is evidence of uncertainty, never
        # permission to promote.
        report = None
    return trial, report


def _decision_projection(row: StockPaperPromotionDecision) -> dict:
    return {
        "id": row.id,
        "cycle_id": row.cycle_id,
        "trial_id": row.trial_id,
        "report_id": row.report_id,
        "model_run_id": row.model_run_id,
        "snapshot_id": row.snapshot_id,
        "decision": row.decision,
        "gates": row.gates,
        "lineage": row.lineage,
        "evidence": row.evidence,
        "actor": row.actor,
        "source_job": row.source_job,
        "correlation_id": row.correlation_id,
        "reason": row.reason,
        "before_binding_id": row.before_binding_id,
        "before_model_run_id": row.before_model_run_id,
        "after_binding_id": row.after_binding_id,
        "after_model_run_id": row.after_model_run_id,
        "decision_sha256": row.decision_sha256,
        "paper_only": row.paper_only,
        "live_authorized": row.live_authorized,
        "created_at": row.created_at,
    }


def automate_paper_promotion(
    db: Session,
    cycle_id: str,
    *,
    source_job: str = AUTOMATIC_PROMOTION_JOB,
    actor: str = AUTOMATIC_PROMOTION_ACTOR,
) -> dict:
    """Evaluate and, only when every persisted gate passes, promote a paper canary.

    The coordinator deliberately requires an already-created paper canary and a
    completed forward trial. It never creates a trial, changes its policy, or
    treats a missing observation as evidence.
    """
    cycle = db.get(StockLearningCycle, cycle_id)
    if cycle is None:
        raise StockTrainingError("Learning cycle not found")
    if cycle.trigger != "scheduled":
        raise StockTrainingError("Automatic promotion is limited to scheduled paper cycles")
    pause_reason = _scheduled_learning_pause_reason(db)
    if pause_reason:
        _defer_scheduled_cycle_for_pause(
            db, cycle, reason=pause_reason, stage="promotion",
        )
        db.flush()
        return {
            "id": None,
            "cycle_id": cycle.cycle_id,
            "decision": "deferred",
            "reason": pause_reason,
            "paper_only": True,
            "live_authorized": False,
        }
    prior_decision = db.query(StockPaperPromotionDecision).filter_by(
        cycle_id=cycle.cycle_id,
    ).order_by(StockPaperPromotionDecision.id.desc()).first()
    if cycle.status in {"complete", "promoted"} and prior_decision and prior_decision.decision == "promoted":
        return _decision_projection(prior_decision)
    if cycle.status == "demoted" and prior_decision and prior_decision.decision == "rejected":
        return _decision_projection(prior_decision)

    _lock_lifecycle_admission(db)
    binding_state = db.scalar(select(StockPaperBindingState).where(
        StockPaperBindingState.id == 1,
    ).with_for_update())
    active_binding = db.get(
        StockPaperModelBinding,
        binding_state.active_binding_id,
    ) if binding_state else None
    before_binding_id = active_binding.id if active_binding else None
    before_model_run_id = active_binding.model_run_id if active_binding else None
    trial, report = _automatic_trial_and_report(db, cycle, active_binding)
    validation = _automatic_validation_gate(db, cycle)
    monitor, monitor_snapshot_id = _automatic_monitor_gate(db, trial)
    account = active_paper_account(db)
    recovery = db.get(StockPaperRecoveryState, 1)
    target_model = db.get(StockModelRegistry, cycle.model_run_id) if cycle.model_run_id else None
    target_state = _current_lifecycle(db, target_model, for_update=False) if target_model else None
    trial_gate = _gate(
        "pass" if trial and trial.status == "completed" else "unknown",
        reason=None if trial and trial.status == "completed"
        else "one completed forward paper trial must be linked to the scheduled cycle",
        evidence={"trial_id": trial.id if trial else None, "status": trial.status if trial else None},
    )
    validation_evidence = validation.get("evidence") or {}
    holdout_count = validation_evidence.get("final_holdout_evaluation_count")
    holdout_consumption = validation_evidence.get("final_holdout_consumption")
    holdout_gate = _gate(
        "pass" if holdout_count == 1 and isinstance(holdout_consumption, dict)
        and holdout_consumption.get("count") == 1 else "fail",
        reason=None if holdout_count == 1 and isinstance(holdout_consumption, dict)
        and holdout_consumption.get("count") == 1
        else "final holdout evidence is absent or was consumed more than once",
        evidence={"evaluation_count": holdout_count, "consumption": holdout_consumption},
    )
    report_gate = _gate(
        "pass" if report and report.get("decision") == "pass"
        and all(gate.get("status") == "pass" for gate in (report.get("gates") or {}).values())
        else "unknown" if report is None else "fail",
        reason=None if report and report.get("decision") == "pass"
        and all(gate.get("status") == "pass" for gate in (report.get("gates") or {}).values())
        else "promotion-readiness report is missing, incomplete, or not passing",
        evidence={
            "report_id": report.get("id") if report else None,
            "report_hash": report.get("report_hash") if report else None,
            "decision": report.get("decision") if report else None,
            "gates": report.get("gates") if report else None,
        },
    )
    binding_gate = _gate(
        "pass" if active_binding and cycle.model_run_id
        and active_binding.model_run_id == cycle.model_run_id
        and active_binding.paper_only is True
        and active_binding.live_authorized is False
        and target_state and target_state.lifecycle_state == "paper_canary"
        else "fail" if active_binding and active_binding.model_run_id != cycle.model_run_id
        else "unknown",
        reason=None if active_binding and cycle.model_run_id
        and active_binding.model_run_id == cycle.model_run_id
        and active_binding.paper_only is True
        and active_binding.live_authorized is False
        and target_state and target_state.lifecycle_state == "paper_canary"
        else "the scheduled model must already be the active paper canary",
        evidence={
            "active_binding_id": before_binding_id,
            "active_model_run_id": before_model_run_id,
            "target_model_run_id": cycle.model_run_id,
            "target_lifecycle_state": target_state.lifecycle_state if target_state else None,
        },
    )
    ledger_gate = _gate(
        "pass" if account and account.status == "reconciled"
        and not account.reconciliation_required and account.accounting_verified
        else "fail",
        reason=None if account and account.status == "reconciled"
        and not account.reconciliation_required and account.accounting_verified
        else "paper ledger reconciliation is not verified",
        evidence={
            "account_status": account.status if account else "uninitialized",
            "reconciliation_required": account.reconciliation_required if account else None,
            "accounting_verified": account.accounting_verified if account else False,
        },
    )
    recovery_gate = _gate(
        "pass" if recovery is None or recovery.status == "armed" else "fail",
        reason=None if recovery is None or recovery.status == "armed"
        else "paper recovery is paused or awaiting revalidation",
        evidence={"status": recovery.status if recovery else "uninitialized"},
    )
    paper_gate = _gate(
        "pass" if (not report or report.get("paper_only") is True)
        and (not report or report.get("live_authorized") is False)
        and (not active_binding or active_binding.paper_only is True)
        and (not active_binding or active_binding.live_authorized is False)
        else "fail",
        reason=None if (not report or report.get("paper_only") is True)
        and (not report or report.get("live_authorized") is False)
        and (not active_binding or active_binding.paper_only is True)
        and (not active_binding or active_binding.live_authorized is False)
        else "live authorization is not permitted",
        evidence={"paper_only": True, "live_authorized": False},
    )
    lineage = (report or {}).get("lineage") or {}
    lineage_gate = _gate(
        "pass" if report and lineage.get("model_run_id") == cycle.model_run_id
        and lineage.get("snapshot_id") == cycle.snapshot_id
        and trial and cycle.binding_id == trial.binding_id
        and trial.binding_id == before_binding_id
        else "unknown",
        reason=None if report and lineage.get("model_run_id") == cycle.model_run_id
        and lineage.get("snapshot_id") == cycle.snapshot_id
        and trial and cycle.binding_id == trial.binding_id
        and trial.binding_id == before_binding_id
        else "model, dataset, trial, and active binding lineage is incomplete or conflicting",
        evidence={
            "lineage": lineage,
            "cycle_snapshot_id": cycle.snapshot_id,
            "cycle_binding_id": cycle.binding_id,
            "active_binding_id": before_binding_id,
        },
    )
    report_gates = (report or {}).get("gates") or {}
    data_gate_names = ("regular_sessions", "historical_feed_health", "decision_coverage")
    data_gate_values = [report_gates.get(name) for name in data_gate_names]
    data_ready = bool(data_gate_values) and all(
        gate and gate.get("status") == "pass" for gate in data_gate_values
    )
    data_readiness_gate = _gate(
        "pass" if data_ready else "unknown" if report is None else "fail",
        reason=None if data_ready else "aligned forward data-quality evidence is not explicitly passing",
        evidence={name: report_gates.get(name) for name in data_gate_names},
    )
    comparison = _model_comparison_gate(
        db,
        cycle,
        target_model,
        before_model_run_id=before_model_run_id,
        readiness_report=report,
    )
    comparison_evidence = comparison.get("evidence") or {}
    comparison_subgates = comparison_evidence.get("gates") or {}
    gates = {
        "leakage_safe_validation": validation,
        "single_use_holdout": holdout_gate,
        "challenger_comparison": comparison,
        "challenger_sample_size": comparison_subgates.get("baseline", {}).get("evidence", {}).get("gates", {}).get("minimum_samples", _gate("unknown", reason="comparison evidence is unavailable")),
        "incumbent_comparison": comparison_subgates.get("incumbent", _gate("unknown", reason="incumbent comparison evidence is unavailable")),
        "forward_trial": trial_gate,
        "promotion_readiness_report": report_gate,
        "immutable_lineage": lineage_gate,
        "paper_canary": binding_gate,
        "paper_ledger": ledger_gate,
        "data_readiness": data_readiness_gate,
        "monitor_health": monitor,
        "recovery_state": recovery_gate,
        "paper_only": paper_gate,
    }
    evidence = {
        "report_hash": report.get("report_hash") if report else None,
        "report_id": report.get("id") if report else None,
        "monitor_snapshot_id": monitor_snapshot_id,
        "trial_id": trial.id if trial else None,
        "gates": gates,
    }
    decision_basis = {
        "cycle_id": cycle.cycle_id,
        "model_run_id": cycle.model_run_id,
        "snapshot_id": cycle.snapshot_id,
        "trial_id": trial.id if trial else None,
        "report_hash": report.get("report_hash") if report else None,
        "gates": gates,
        "before_binding_id": before_binding_id,
        "before_model_run_id": before_model_run_id,
    }
    digest = _digest(decision_basis)
    existing = db.scalar(select(StockPaperPromotionDecision).where(
        StockPaperPromotionDecision.decision_sha256 == digest,
    ))
    if existing:
        return _decision_projection(existing)

    passed = _all_pass(gates)
    reason = (
        "Every immutable paper promotion gate passed"
        if passed
        else next(
            (gate.get("reason") for gate in gates.values() if gate.get("status") != "pass"),
            "Promotion evidence is incomplete",
        )
    )
    after_binding_id = before_binding_id if passed else None
    after_model_run_id = cycle.model_run_id if passed else None
    rejected = (
        not passed
        and comparison.get("status") == "fail"
        and target_model is not None
        and target_state is not None
        and target_state.lifecycle_state == "paper_canary"
    )
    if passed:
        if target_model is None or target_state is None:
            raise StockTrainingError("Promotion target model is unavailable")
        champions = db.scalars(select(StockModelLifecycleState).where(
            StockModelLifecycleState.lifecycle_state == "champion",
            StockModelLifecycleState.model_run_id != target_model.run_id,
        ).with_for_update()).all()
        recovery_state = recovery or StockPaperRecoveryState(
            id=1, status="armed", flatten_policy="none", updated_by=actor,
        )
        if recovery is None:
            db.add(recovery_state)
            db.flush()
        for champion in champions:
            previous_binding = db.query(StockPaperModelBinding).filter_by(
                model_run_id=champion.model_run_id,
            ).order_by(StockPaperModelBinding.id.desc()).first()
            _transition_model(
                db,
                model=db.get(StockModelRegistry, champion.model_run_id),
                action="demote",
                actor=actor,
                reason=f"Automatic paper promotion replaced champion: {cycle.cycle_id}",
                binding_id=previous_binding.id if previous_binding else None,
            )
        _transition_model(
            db, model=target_model, action="promote", actor=actor,
            reason=f"Automatic paper promotion: {cycle.cycle_id}",
            binding_id=active_binding.id if active_binding else None,
        )
        recovery_state.last_known_good_model_run_id = (
            champions[0].model_run_id if champions else None
        )
        recovery_state.last_known_good_binding_id = (
            db.query(StockPaperModelBinding).filter_by(
                model_run_id=champions[0].model_run_id,
            ).order_by(StockPaperModelBinding.id.desc()).first().id
            if champions and db.query(StockPaperModelBinding).filter_by(
                model_run_id=champions[0].model_run_id,
            ).order_by(StockPaperModelBinding.id.desc()).first()
            else None
        )
        cycle.status, cycle.stage = "promoted", "promotion"
        cycle.monitor_snapshot_id = monitor_snapshot_id
        cycle.active_binding_id = after_binding_id
        cycle.last_reason = reason
        decision_name = "promoted"
    else:
        if rejected:
            # A measured underperformer is a terminal rejection, not a
            # retryable evidence gap.  Keep its immutable binding and model
            # rows for audit, but remove the mutable active-canary pointer so
            # the known-good champion is never silently replaced.
            _transition_model(
                db,
                model=target_model,
                action="demote",
                actor=actor,
                reason=f"Automatic paper challenger rejected: {cycle.cycle_id}",
                binding_id=before_binding_id,
            )
            if binding_state is not None:
                db.delete(binding_state)
                db.flush()
            cycle.status, cycle.stage = "demoted", "promotion"
            decision_name = "rejected"
        else:
            cycle.status, cycle.stage = "blocked", "promotion"
            decision_name = "blocked"
        cycle.monitor_snapshot_id = monitor_snapshot_id
        cycle.active_binding_id = None if rejected else before_binding_id
        cycle.last_reason = reason
    cycle.trial_id = trial.id if trial else cycle.trial_id
    cycle.gates = gates
    cycle.evidence = {**(cycle.evidence or {}), "automatic_promotion": evidence}
    decision_identity = {
        **decision_basis,
        "decision": decision_name,
        "reason": reason,
        "after_binding_id": after_binding_id,
        "after_model_run_id": after_model_run_id,
    }
    row = StockPaperPromotionDecision(
        cycle_id=cycle.cycle_id,
        trial_id=trial.id if trial else None,
        report_id=report.get("id") if report else None,
        model_run_id=cycle.model_run_id,
        snapshot_id=cycle.snapshot_id,
        decision=decision_name,
        gates=gates,
        lineage=lineage,
        evidence=evidence,
        actor=actor,
        source_job=source_job,
        correlation_id=cycle.cycle_id,
        reason=reason,
        before_binding_id=before_binding_id,
        before_model_run_id=before_model_run_id,
        after_binding_id=after_binding_id,
        after_model_run_id=after_model_run_id,
        decision_sha256=digest,
        paper_only=True,
        live_authorized=False,
    )
    db.add(row)
    _event(
        db, cycle=cycle, stage="promotion",
        decision="complete" if passed else "blocked", actor=actor,
        reason=reason,
        evidence={"automatic_decision": decision_name, **evidence, "before_binding_id": before_binding_id,
                  "after_binding_id": after_binding_id},
    )
    write_audit_log(
        db, event_type="stock_learning_cycle", action="automatic_paper_promotion",
        status="complete" if passed else "blocked", message=reason,
        entity_type="stock_learning_cycle", payload={
            "cycle_id": cycle.cycle_id, "actor": actor, "source_job": source_job,
            "correlation_id": cycle.cycle_id, "decision": decision_name,
            "model_run_id": cycle.model_run_id, "snapshot_id": cycle.snapshot_id,
            "trial_id": trial.id if trial else None, "report_id": report.get("id") if report else None,
            "gates": gates, "before_binding_id": before_binding_id, "after_binding_id": after_binding_id,
            "decision_sha256": row.decision_sha256, "paper_only": True, "live_authorized": False,
        },
    )
    db.flush()
    return _decision_projection(row)


def run_scheduled_paper_trial_handoff_job(
    db: Session,
    *,
    source_job: str = "scheduled_stock_paper_trial_handoff_job",
) -> dict:
    """Advance completed scheduled challengers through admission and start.

    Every candidate is evaluated independently so one unavailable feed or
    account does not erase the durable reason for another cycle. Re-running
    this job is safe because the cycle, binding, and trial source keys are
    unique.
    """
    pause_reason = _scheduled_learning_pause_reason(db)
    rows = db.scalars(select(StockLearningCycle).where(
        StockLearningCycle.trigger == "scheduled",
        StockLearningCycle.training_job_id.is_not(None),
        StockLearningCycle.status.not_in((
            "blocked", "complete", "promoted", "demoted", "rolled_back", "failed",
        )),
    ).order_by(StockLearningCycle.created_at, StockLearningCycle.cycle_id)).all()
    if pause_reason:
        for cycle in rows:
            _defer_scheduled_cycle_for_pause(
                db, cycle, reason=pause_reason, stage="handoff",
            )
        db.commit()
        return {
            "status": "paused",
            "job": source_job,
            "deferred": [
                {"cycle_id": cycle.cycle_id, "reason": pause_reason}
                for cycle in rows
            ],
            "reason": pause_reason,
            "paper_only": True,
            "live_authorized": False,
        }
    results: list[dict] = []
    for cycle in rows:
        try:
            sync_cycle_from_training_job(db, cycle.training_job_id, actor=HANDOFF_ACTOR)
            cycle = start_scheduled_learning_trial(db, cycle.cycle_id, actor=HANDOFF_ACTOR)
            db.commit()
            results.append({
                "cycle_id": cycle.cycle_id,
                "binding_id": cycle.binding_id,
                "trial_id": cycle.trial_id,
                "status": cycle.status,
                "stage": cycle.stage,
                "reason": cycle.last_reason,
                "paper_only": True,
                "live_authorized": False,
            })
        except Exception as exc:
            db.rollback()
            cycle = db.get(StockLearningCycle, cycle.cycle_id)
            if cycle:
                _handoff_failure(
                    db,
                    cycle,
                    stage="admission",
                    status="deferred",
                    reason=f"Scheduled paper handoff retry unavailable: {exc.__class__.__name__}",
                    evidence={"error_type": exc.__class__.__name__},
                )
                db.commit()
                results.append({
                    "cycle_id": cycle.cycle_id,
                    "binding_id": cycle.binding_id,
                    "trial_id": cycle.trial_id,
                    "status": cycle.status,
                    "stage": cycle.stage,
                    "reason": cycle.last_reason,
                    "paper_only": True,
                    "live_authorized": False,
                })
    return {
        "status": "complete",
        "job": source_job,
        "evaluated": len(results),
        "results": results,
        "paper_only": True,
        "live_authorized": False,
    }


def run_automatic_paper_promotion_job(
    db: Session,
    *,
    source_job: str = AUTOMATIC_PROMOTION_JOB,
) -> dict:
    """Admit/start scheduled trials, then evaluate exact completed evidence."""
    handoff = run_scheduled_paper_trial_handoff_job(db, source_job=source_job)
    if handoff.get("status") == "paused":
        return {
            "status": "paused",
            "job": source_job,
            "handoff": handoff,
            "evaluated": 0,
            "results": [],
            "reason": handoff.get("reason"),
            "paper_only": True,
            "live_authorized": False,
        }
    rows = db.scalars(select(StockLearningCycle).where(
        StockLearningCycle.trigger == "scheduled",
        StockLearningCycle.model_run_id.is_not(None),
        StockLearningCycle.stage.in_(("forward_trial", "operator_review", "promotion")),
        StockLearningCycle.status.not_in(("complete", "promoted", "demoted", "rolled_back", "failed")),
    ).order_by(StockLearningCycle.created_at, StockLearningCycle.cycle_id)).all()
    results: list[dict] = []
    for cycle in rows:
        try:
            result = automate_paper_promotion(db, cycle.cycle_id, source_job=source_job)
            db.commit()
            results.append({
                "cycle_id": cycle.cycle_id,
                "decision": result["decision"],
                "reason": result["reason"],
                "decision_id": result["id"],
            })
        except Exception as exc:
            db.rollback()
            cycle = db.get(StockLearningCycle, cycle.cycle_id)
            if cycle is not None:
                reason = f"Automatic promotion evaluation unavailable: {exc.__class__.__name__}"
                cycle.status, cycle.stage, cycle.last_reason = "deferred", "promotion", reason
                _event(
                    db,
                    cycle=cycle,
                    stage="promotion",
                    decision="deferred",
                    actor=AUTOMATIC_PROMOTION_ACTOR,
                    reason=reason,
                    evidence={"source_job": source_job, "error_type": exc.__class__.__name__},
                )
                write_audit_log(
                    db,
                    event_type="stock_learning_cycle",
                    action="automatic_paper_promotion",
                    status="deferred",
                    message=reason,
                    entity_type="stock_learning_cycle",
                    payload={
                        "cycle_id": cycle.cycle_id,
                        "actor": AUTOMATIC_PROMOTION_ACTOR,
                        "source_job": source_job,
                        "paper_only": True,
                        "live_authorized": False,
                    },
                )
                db.commit()
            results.append({
                "cycle_id": cycle.cycle_id,
                "decision": "deferred",
                "reason": reason,
            })
    return {
        "status": "complete",
        "job": source_job,
        "handoff": handoff,
        "evaluated": len(results),
        "results": results,
        "paper_only": True,
        "live_authorized": False,
    }


def cycle_projection(db: Session, cycle: StockLearningCycle) -> dict:
    binding_state = db.get(StockPaperBindingState, 1)
    active_binding = db.get(StockPaperModelBinding, binding_state.active_binding_id) if binding_state else None
    binding = db.get(StockPaperModelBinding, cycle.binding_id) if cycle.binding_id else None
    trial = db.get(StockPaperTrial, cycle.trial_id) if cycle.trial_id else None
    latest_report = db.scalar(select(StockPaperPromotionReadinessReport).where(
        StockPaperPromotionReadinessReport.trial_id == cycle.trial_id,
    ).order_by(
        StockPaperPromotionReadinessReport.report_version.desc().nullslast(),
        StockPaperPromotionReadinessReport.id.desc(),
    )) if cycle.trial_id else None
    monitor = db.get(StockMonitoringSnapshot, cycle.monitor_snapshot_id) if cycle.monitor_snapshot_id else None
    if monitor is None:
        monitor = db.query(StockMonitoringSnapshot).order_by(StockMonitoringSnapshot.generated_at.desc()).first()
    recovery = db.get(StockPaperRecoveryState, 1)
    recovery_event = db.query(StockPaperRecoveryEvent).order_by(StockPaperRecoveryEvent.created_at.desc()).first()
    automatic_decision = db.query(StockPaperPromotionDecision).filter_by(
        cycle_id=cycle.cycle_id,
    ).order_by(StockPaperPromotionDecision.id.desc()).first()
    approval = paper_run_approval_projection(db, cycle)
    return {
        "cycle_id": cycle.cycle_id, "status": cycle.status, "stage": cycle.stage,
        "trigger": cycle.trigger, "requested_by": cycle.requested_by,
        "symbols": cycle.symbols, "cutoff_date": cycle.cutoff_date,
        "horizon_days": cycle.horizon_days, "provider": cycle.provider,
        "snapshot_id": cycle.snapshot_id, "training_job_id": cycle.training_job_id,
        "model_run_id": cycle.model_run_id, "binding_id": cycle.binding_id,
        "trial_id": cycle.trial_id,
        "active_binding_id": active_binding.id if active_binding else cycle.active_binding_id,
        "active_binding_model_run_id": active_binding.model_run_id if active_binding else None,
        "handoff": {
            "stage": cycle.stage,
            "status": cycle.status,
            "binding_id": binding.id if binding else None,
            "trial_id": trial.id if trial else None,
            "trial_status": trial.status if trial else None,
            "preflight": (cycle.gates or {}).get("preflight"),
            "approval": approval,
            "reason": cycle.last_reason,
            "report_id": latest_report.id if latest_report else None,
            "report_decision": latest_report.decision if latest_report else None,
        },
        "position_handling": (
            trial_position_handling_projection(db, trial)
            if trial else {
                "approved_policy": None,
                "stop_status": "not_started",
                "stop_reason": None,
                "stopped_at": None,
                "new_entries_stopped": True,
                "handling_status": "not_started",
                "remaining_positions": [],
                "managed_lots": [],
                "has_exit_intent": False,
                "reconciliation": {
                    "status": "unknown",
                    "reconciliation_required": True,
                    "last_reconciled_at": None,
                },
            }
        ),
        "gates": cycle.gates, "evidence": cycle.evidence,
        "last_reason": cycle.last_reason, "paper_only": True, "live_authorized": False,
        "scheduled_learning_paused": bool(_scheduled_learning_pause_reason(db)),
        "automatic_promotion": _decision_projection(automatic_decision) if automatic_decision else None,
        "monitoring": {
            "snapshot_id": monitor.id if monitor else None,
            "status": monitor.status if monitor else "unknown",
            "generated_at": monitor.generated_at if monitor else None,
            "actions": monitor.actions if monitor else [],
        },
        "recovery": {
            "status": recovery.status if recovery else "unknown",
            "last_known_good_model_run_id": recovery.last_known_good_model_run_id if recovery else None,
            "last_known_good_binding_id": recovery.last_known_good_binding_id if recovery else None,
            "latest_event_id": recovery_event.id if recovery_event else None,
            "latest_event_action": recovery_event.action if recovery_event else None,
        },
        "events": [_projection_event(event) for event in db.scalars(
            select(StockLearningCycleEvent).where(StockLearningCycleEvent.cycle_id == cycle.cycle_id)
            .order_by(StockLearningCycleEvent.id)
        ).all()],
        "created_at": cycle.created_at,
        "updated_at": cycle.updated_at,
    }


def sync_cycle_observability(
    db: Session,
    *,
    monitor_snapshot_id: int | None = None,
    recovery_event_id: int | None = None,
    actor: str = "monitor",
) -> list[str]:
    """Durably attach monitoring/recovery evidence to affected active cycles."""
    monitor = db.get(StockMonitoringSnapshot, monitor_snapshot_id) if monitor_snapshot_id else None
    recovery_event = db.get(StockPaperRecoveryEvent, recovery_event_id) if recovery_event_id else None
    binding_state = db.get(StockPaperBindingState, 1)
    active_binding = db.get(StockPaperModelBinding, binding_state.active_binding_id) if binding_state else None
    model_run_id = active_binding.model_run_id if active_binding else None
    if not model_run_id and monitor:
        for action in monitor.actions or []:
            if action.get("model_run_id"):
                model_run_id = action["model_run_id"]
                break
    if not model_run_id and not recovery_event:
        return []
    statement = select(StockLearningCycle)
    if model_run_id:
        statement = statement.where(StockLearningCycle.model_run_id == model_run_id)
    else:
        statement = statement.where(StockLearningCycle.status.not_in(TERMINAL_STATUSES))
    cycles = db.scalars(statement).all()
    affected: list[str] = []
    for cycle in cycles:
        if monitor:
            cycle.monitor_snapshot_id = monitor.id
        if recovery_event:
            cycle.recovery_event_id = recovery_event.id
        if recovery_event and recovery_event.action in {"pause", "demote_model", "pause_stock_path", "flatten_positions"}:
            cycle.status = "demoted"
            cycle.stage = "recovery"
            cycle.last_reason = recovery_event.reason
            _event(
                db, cycle=cycle, stage="recovery", decision="complete", actor=actor,
                reason=recovery_event.reason,
                evidence={"monitor_snapshot_id": monitor.id if monitor else None, "recovery_event_id": recovery_event.id},
            )
        affected.append(cycle.cycle_id)
    return affected


def list_learning_cycles(db: Session, *, limit: int = 25) -> list[dict]:
    rows = db.scalars(select(StockLearningCycle).order_by(
        StockLearningCycle.created_at.desc()
    ).limit(limit)).all()
    return [cycle_projection(db, row) for row in rows]