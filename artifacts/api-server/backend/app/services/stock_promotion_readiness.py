"""Deterministic, read-only promotion-readiness evidence for frozen paper trials."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    StockDatasetSnapshot,
    StockModelRegistry,
    StockPaperModelBinding,
    StockPaperPromotionReadinessReport,
    StockPaperTrial,
    StockPaperTrialMetric,
)
from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder, StockPaperTrialLot
from app.services.stock_forward_trial import _hash, _trial_allocated_notional
from app.services.stock_paper_ledger import active_paper_account
from app.services.live_safety import evaluate_live_safety

DEFAULT_HISTORY_LIMIT = 10
MAX_HISTORY_LIMIT = 50
DEFAULT_SESSION_EVIDENCE_LIMIT = 20
MAX_SESSION_EVIDENCE_LIMIT = 100


def _gate(status: str, value: Any = None, required: Any = None, reason: str | None = None) -> dict:
    result = {"status": status}
    if value is not None:
        result["value"] = value
    if required is not None:
        result["required"] = required
    if reason:
        result["reason"] = reason
    return result


def _metric_value(metric: StockPaperTrialMetric | None, key: str) -> Any:
    return metric.payload.get(key) if metric else None


def _number(value: Any) -> Decimal | None:
    try:
        return None if value is None else Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _frozen_session_evidence_gate(
    trial: StockPaperTrial,
    metric: StockPaperTrialMetric | None,
    session_evidence: dict | None,
) -> dict:
    """Validate the immutable shape of post-handoff session evidence.

    Metrics are durable inputs, but their aggregate fields must not be trusted
    independently of the session evidence that produced them.  This gate is
    deliberately structural and read-only; the worker remains the only writer
    of observations.
    """
    if not isinstance(session_evidence, dict):
        return _gate("unknown", reason="frozen post-handoff session evidence is unavailable")
    window = session_evidence.get("window")
    aggregates = session_evidence.get("aggregates")
    sessions = session_evidence.get("sessions")
    if not isinstance(window, dict) or not isinstance(aggregates, dict) or not isinstance(sessions, list):
        return _gate("unknown", reason="frozen session evidence is incomplete")
    required = int(trial.policy["regular_sessions"])
    elapsed = window.get("elapsed_regular_sessions")
    complete = window.get("complete") is True
    try:
        elapsed_count = int(elapsed)
    except (TypeError, ValueError):
        elapsed_count = -1
    if not complete or elapsed_count < required or len(sessions) != elapsed_count:
        return _gate(
            "fail" if trial.status in {"completed", "stopped"} else "unknown",
            value=elapsed,
            required=required,
            reason="the frozen regular-session window is not complete",
        )
    expected = _number(aggregates.get("expected_decisions"))
    observed = _number(aggregates.get("observed_decisions"))
    verified = _number(aggregates.get("verified_decisions"))
    unknown_health = _number(aggregates.get("unknown_historical_feed_health"))
    metric_verified_coverage = _number(
        _metric_value(metric, "verified_decision_coverage")
    )
    evidence_verified_coverage = _number(
        aggregates.get("verified_decision_coverage")
    )
    universe_size = len(trial.lineage.get("universe") or [])
    expected_shape = elapsed_count * universe_size
    if (
        expected is None
        or observed is None
        or verified is None
        or unknown_health is None
        or expected != expected_shape
        or observed < 0
        or verified < 0
        or verified > observed
        or unknown_health != 0
        or metric_verified_coverage is None
        or evidence_verified_coverage is None
        or metric_verified_coverage != evidence_verified_coverage
    ):
        return _gate(
            "fail" if trial.status in {"completed", "stopped"} else "unknown",
            reason="post-handoff verified session evidence is inconsistent or unavailable",
        )
    return _gate(
        "pass",
        evidence={
            "elapsed_regular_sessions": elapsed_count,
            "expected_decisions": str(expected),
            "observed_decisions": str(observed),
            "verified_decisions": str(verified),
            "verified_decision_coverage": str(metric_verified_coverage),
        },
    )


def _lineage_gate(db: Session, trial: StockPaperTrial) -> tuple[dict, dict]:
    binding = db.get(StockPaperModelBinding, trial.binding_id)
    model = db.get(StockModelRegistry, binding.model_run_id) if binding else None
    snapshot = db.get(StockDatasetSnapshot, binding.snapshot_id) if binding else None
    expected_keys = (
        "model_run_id", "model_hash", "snapshot_id", "dataset_sha256",
        "cutoff_date", "universe", "cost_assumptions", "binding_hash", "policy_sha256",
    )
    complete = binding and model and snapshot and all(key in trial.lineage for key in expected_keys)
    if not complete:
        return _gate("unknown", reason="immutable lineage evidence is incomplete"), {}
    expected_digest = _hash({key: trial.lineage[key] for key in expected_keys})
    matches = (
        binding.binding_sha256 == trial.lineage["binding_hash"]
        and binding.model_run_id == trial.lineage["model_run_id"]
        and binding.snapshot_id == trial.lineage["snapshot_id"]
        and model.manifest_sha256 == trial.lineage["model_hash"]
        and model.snapshot_id == snapshot.snapshot_id
        and snapshot.dataset_sha256 == trial.lineage["dataset_sha256"]
        and trial.lineage.get("policy_sha256") == _hash(trial.policy)
        and trial.lineage.get("lineage_sha256") == expected_digest
    )
    snapshot_lineage = {
        "binding_id": binding.id,
        "binding_hash": binding.binding_sha256,
        "model_run_id": model.run_id,
        "model_hash": model.manifest_sha256,
        "snapshot_id": snapshot.snapshot_id,
        "dataset_sha256": snapshot.dataset_sha256,
        "lineage_sha256": trial.lineage.get("lineage_sha256"),
    }
    return (
        _gate("pass" if matches else "fail", reason=None if matches else "trial lineage no longer matches its binding"),
        snapshot_lineage,
    )


def _risk_gate(db: Session, trial: StockPaperTrial, account: StockPaperAccount | None) -> dict:
    lots = db.scalars(select(StockPaperTrialLot).where(StockPaperTrialLot.trial_id == trial.id)).all()
    if not lots or not account or not trial.baseline_equity:
        return _gate("unknown", reason="no complete broker-attributed trade-risk evidence")
    observed: list[Decimal] = []
    for lot in lots:
        fills = db.scalars(select(StockPaperFill).where(
            StockPaperFill.order_id == lot.entry_order_id, StockPaperFill.side == "buy"
        )).all()
        if not fills:
            return _gate("unknown", reason="an entry fill is not yet available")
        quantity = sum((fill.quantity for fill in fills), Decimal("0"))
        value = sum((fill.quantity * fill.price for fill in fills), Decimal("0"))
        if quantity <= 0:
            return _gate("unknown", reason="entry quantity is not positive")
        observed.append(value / quantity * lot.stop_fraction / trial.baseline_equity)
    maximum = max(observed)
    required = Decimal(str(trial.policy["max_risk_per_trade"]))
    return _gate("pass" if maximum <= required else "fail", value=str(maximum), required=str(required),
                 reason=None if maximum <= required else "a trial lot exceeds the frozen risk ceiling")


def _evidence_summary(report: StockPaperPromotionReadinessReport) -> dict:
    evidence = report.evidence or {}
    aggregate_metrics = evidence.get("aggregate_metrics") or {}
    session_evidence = evidence.get("session_evidence") or {}
    aggregates = session_evidence.get("aggregates") or {}
    window = session_evidence.get("window") or {}
    sessions = session_evidence.get("sessions") or []
    unknown_historical = aggregates.get("unknown_historical_feed_health", 0)
    classification = aggregate_metrics.get("classification")
    if not classification:
        classification = {
            "pass": "passing",
            "fail": "failing",
        }.get(report.decision, "insufficient")
    return {
        "elapsed_sessions": window.get("elapsed_regular_sessions", len(sessions)),
        "evidenced_sessions": aggregates.get(
            "evidenced_sessions",
            sum(1 for session in sessions if session.get("status") == "evidenced"),
        ),
        "expected_decisions": aggregates.get("expected_decisions", 0),
        "observed_decisions": aggregates.get("observed_decisions", 0),
        "verified_decisions": aggregates.get("verified_decisions", 0),
        "duplicate_exclusions": aggregates.get("duplicate_exclusions", 0),
        "rejected_decisions": aggregates.get("rejected_decisions", 0),
        "missing_decisions": aggregates.get("missing_decisions", 0),
        "decision_coverage": aggregates.get("decision_coverage"),
        "classification": classification,
        "costs_known": aggregate_metrics.get("costs_known"),
        "historical_evidence_status": "unknown" if unknown_historical or not sessions else "verified",
        "historical_evidence_reason": (
            "Historical feed or health evidence is unavailable; it was not reconstructed"
            if unknown_historical or not sessions
            else None
        ),
        "session_count": len(sessions),
    }


def _report_out(
    report: StockPaperPromotionReadinessReport,
    *,
    include_evidence: bool = True,
) -> dict:
    result = {
        "id": report.id,
        "version": report.report_version,
        "trial_id": report.trial_id,
        "source_metric_id": report.source_metric_id,
        "as_of": report.as_of,
        "report_hash": report.report_hash,
        "decision": report.decision,
        "gates": report.gates,
        "lineage": report.lineage,
        "policy": report.policy,
        "evidence": report.evidence if include_evidence else None,
        "paper_only": report.paper_only,
        "live_authorized": report.live_authorized,
        "created_at": report.created_at,
        "promotion_authorized": False,
    }
    if not include_evidence:
        result["summary"] = _evidence_summary(report)
        result["session_evidence_available"] = bool(
            (report.evidence or {}).get("session_evidence")
        )
    return result


def promotion_readiness_report_history(
    db: Session,
    trial_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict]:
    """Read immutable report rows only; this must never recalculate a trial."""
    if not db.get(StockPaperTrial, trial_id):
        raise ValueError("Trial not found")
    if limit is not None and not 1 <= limit <= MAX_HISTORY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_HISTORY_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    query = select(StockPaperPromotionReadinessReport).where(
        StockPaperPromotionReadinessReport.trial_id == trial_id,
    ).order_by(
        StockPaperPromotionReadinessReport.report_version.asc().nullsfirst(),
        StockPaperPromotionReadinessReport.id.asc(),
    )
    if limit is not None:
        query = query.offset(offset).limit(limit)
    reports = db.scalars(query).all()
    return [_report_out(report) for report in reports]


def promotion_readiness_report_history_page(
    db: Session,
    trial_id: str,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
    offset: int = 0,
) -> dict:
    """Return lightweight immutable report metadata for bounded history views."""
    if not db.get(StockPaperTrial, trial_id):
        raise ValueError("Trial not found")
    if not 1 <= limit <= MAX_HISTORY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_HISTORY_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    base = select(StockPaperPromotionReadinessReport).where(
        StockPaperPromotionReadinessReport.trial_id == trial_id,
    )
    reports = db.scalars(base.order_by(
        StockPaperPromotionReadinessReport.report_version.asc().nullsfirst(),
        StockPaperPromotionReadinessReport.id.asc(),
    ).offset(offset).limit(limit)).all()
    total = db.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0
    return {
        "items": [_report_out(report, include_evidence=False) for report in reports],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(reports) < total,
    }


def promotion_readiness_session_evidence(
    db: Session,
    trial_id: str,
    report_id: int,
    *,
    limit: int = DEFAULT_SESSION_EVIDENCE_LIMIT,
    offset: int = 0,
) -> dict:
    """Read a bounded slice from one immutable report's session evidence."""
    report = db.get(StockPaperPromotionReadinessReport, report_id)
    if not report or report.trial_id != trial_id:
        raise ValueError("Promotion readiness report not found")
    if not 1 <= limit <= MAX_SESSION_EVIDENCE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_SESSION_EVIDENCE_LIMIT}")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    evidence = report.evidence or {}
    session_evidence = evidence.get("session_evidence") or {}
    sessions = session_evidence.get("sessions") or []
    items = sessions[offset:offset + limit]
    bounded_session_evidence = {
        key: value for key, value in session_evidence.items() if key != "sessions"
    }
    bounded_session_evidence["sessions"] = items
    return {
        "trial_id": trial_id,
        "report_id": report.id,
        "as_of": report.as_of,
        "summary": _evidence_summary(report),
        "session_evidence": bounded_session_evidence,
        "items": items,
        "total": len(sessions),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < len(sessions),
    }


def promotion_readiness_report(db: Session, trial_id: str, report_id: int) -> dict:
    """Retrieve one immutable report; deliberately no live evaluator calls."""
    report = db.get(StockPaperPromotionReadinessReport, report_id)
    if not report or report.trial_id != trial_id:
        raise ValueError("Promotion readiness report not found")
    return _report_out(report)


def evaluate_promotion_readiness(db: Session, trial_id: str) -> dict:
    """Evaluate evidence without changing trial, model, binding, or execution state."""
    trial = db.get(StockPaperTrial, trial_id)
    if not trial:
        raise ValueError("Trial not found")
    metric = db.scalar(select(StockPaperTrialMetric).where(
        StockPaperTrialMetric.trial_id == trial_id
    ).order_by(StockPaperTrialMetric.as_of.desc(), StockPaperTrialMetric.id.desc()))
    account = active_paper_account(db)
    sessions = _number(_metric_value(metric, "observed_sessions"))
    coverage = _number(_metric_value(metric, "verified_decision_coverage"))
    closed = _number(_metric_value(metric, "closed_trades"))
    required_sessions = Decimal(str(trial.policy["regular_sessions"]))
    required_coverage = Decimal(str(trial.policy["minimum_decision_coverage"]))
    required_closed = Decimal(str(trial.policy["minimum_closed_trades"]))
    complete = bool(sessions is not None and sessions >= required_sessions)
    session_evidence = metric.payload.get("session_evidence") if metric else None
    aggregates = session_evidence.get("aggregates", {}) if isinstance(session_evidence, dict) else {}
    historical_health_unknown = aggregates.get("unknown_historical_feed_health")
    frozen_evidence = _frozen_session_evidence_gate(trial, metric, session_evidence)
    frozen_window_complete = frozen_evidence["status"] == "pass"
    live_safety = evaluate_live_safety(db)
    gates = {
        "regular_sessions": _gate("pass" if frozen_window_complete and sessions is not None and sessions >= required_sessions else
                                  ("fail" if trial.status in {"completed", "stopped"} else "unknown"),
                                  str(sessions) if sessions is not None else None, str(required_sessions),
                                   None if frozen_window_complete and sessions is not None and sessions >= required_sessions else "required verified sessions are not complete"),
        "decision_coverage": _gate("pass" if frozen_window_complete and complete and coverage is not None and coverage >= required_coverage else
                                   ("fail" if complete and coverage is not None else "unknown"),
                                   str(coverage) if coverage is not None else None, str(required_coverage),
                                   None if frozen_window_complete and complete and coverage is not None and coverage >= required_coverage else "coverage evidence is incomplete or below the frozen threshold"),
        "historical_feed_health": _gate(
            "pass" if frozen_window_complete and session_evidence and historical_health_unknown == 0 else "unknown",
            str(historical_health_unknown) if historical_health_unknown is not None else None,
            "0",
            None if frozen_window_complete and session_evidence and historical_health_unknown == 0
            else "historical feed or health evidence is unavailable; it was not reconstructed",
        ),
        "closed_trades": _gate("pass" if closed is not None and closed >= required_closed else
                               ("fail" if trial.status in {"completed", "stopped"} and closed is not None else "unknown"),
                               str(closed) if closed is not None else None, str(required_closed),
                               None if closed is not None and closed >= required_closed else "required closed lots are not available"),
        "allocated_notional": _gate("pass" if account is not None and _trial_allocated_notional(db, trial) <= Decimal(str(trial.policy["max_allocated_notional"])) else
                                    ("unknown" if account is None else "fail"),
                                    str(_trial_allocated_notional(db, trial)) if account is not None else None,
                                    trial.policy["max_allocated_notional"],
                                    None if account is not None else "paper account evidence is unavailable"),
        "risk_per_trade": _risk_gate(db, trial, account),
        "broker_costs": _gate("pass" if metric and metric.payload.get("costs_known") is True else "unknown",
                              reason=None if metric and metric.payload.get("costs_known") is True else "broker cost evidence is unknown"),
        "metric_classification": _gate("pass" if metric and metric.classification == "passing" else
                                       ("fail" if metric and metric.classification == "failing" else "unknown"),
                                       metric.classification if metric else None,
                                       "passing",
                                       None if metric and metric.classification == "passing" else "trial performance evidence is not passing"),
        "paper_only": _gate("pass" if trial.policy.get("paper_only") is True and trial.policy.get("live_authorized") is False else "fail"),
        "live_trading_disabled": _gate("pass" if trial.policy.get("live_authorized") is False else "fail"),
        "frozen_forward_evidence": frozen_evidence,
        "live_safety_contract": _gate(
            "pass" if not live_safety["live_orders_allowed"] else "fail",
            reason=None if not live_safety["live_orders_allowed"] else "live order authority must not coexist with paper promotion",
            value={
                "mode": live_safety["mode"],
                "status": live_safety["status"],
                "live_orders_allowed": live_safety["live_orders_allowed"],
            },
        ),
    }
    lineage_gate, lineage = _lineage_gate(db, trial)
    gates["immutable_lineage"] = lineage_gate
    statuses = {gate["status"] for gate in gates.values()}
    decision = "fail" if "fail" in statuses else ("pass" if statuses == {"pass"} else "unknown")
    evidence = {
        "version": 1,
        "trial_id": trial.id,
        "source_metric_id": metric.id if metric else None,
        "source_metric_as_of": metric.as_of.isoformat() if metric else None,
        "as_of": (
            session_evidence.get("as_of") if isinstance(session_evidence, dict)
            else (metric.as_of.isoformat() if metric else None)
        ),
        "decision": decision,
        "gates": gates,
        "lineage": lineage or trial.lineage,
        "policy": trial.policy,
        "aggregate_metrics": metric.payload if metric else None,
        "session_evidence": session_evidence,
        "paper_only": True,
        "live_authorized": False,
        "promotion_authorized": False,
        "live_safety": {
            "mode": live_safety["mode"],
            "status": live_safety["status"],
            "live_orders_allowed": live_safety["live_orders_allowed"],
        },
    }
    report_hash = _hash(evidence)
    existing = db.scalar(select(StockPaperPromotionReadinessReport).where(
        StockPaperPromotionReadinessReport.report_hash == report_hash
    ))
    if existing:
        return _report_out(existing)
    latest_version = db.scalar(select(StockPaperPromotionReadinessReport.report_version).where(
        StockPaperPromotionReadinessReport.trial_id == trial.id,
        StockPaperPromotionReadinessReport.report_version.is_not(None),
    ).order_by(StockPaperPromotionReadinessReport.report_version.desc()))
    report = StockPaperPromotionReadinessReport(
        trial_id=trial.id, source_metric_id=metric.id if metric else None,
        report_hash=report_hash, decision=decision, gates=gates,
        lineage=evidence["lineage"], policy=trial.policy,
        paper_only=True, live_authorized=False,
        report_version=(latest_version or 0) + 1,
        as_of=metric.as_of if metric else None,
        evidence=evidence,
    )
    db.add(report)
    db.flush()
    return _report_out(report)