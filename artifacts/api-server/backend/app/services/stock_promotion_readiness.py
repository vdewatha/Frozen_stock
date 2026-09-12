"""Deterministic, read-only promotion-readiness evidence for frozen paper trials."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
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


def _report_out(report: StockPaperPromotionReadinessReport) -> dict:
    return {
        "id": report.id,
        "trial_id": report.trial_id,
        "source_metric_id": report.source_metric_id,
        "report_hash": report.report_hash,
        "decision": report.decision,
        "gates": report.gates,
        "lineage": report.lineage,
        "policy": report.policy,
        "paper_only": report.paper_only,
        "live_authorized": report.live_authorized,
        "created_at": report.created_at,
        "promotion_authorized": False,
    }


def evaluate_promotion_readiness(db: Session, trial_id: str) -> dict:
    """Evaluate evidence without changing trial, model, binding, or execution state."""
    trial = db.get(StockPaperTrial, trial_id)
    if not trial:
        raise ValueError("Trial not found")
    metric = db.scalar(select(StockPaperTrialMetric).where(
        StockPaperTrialMetric.trial_id == trial_id
    ).order_by(StockPaperTrialMetric.as_of.desc()))
    account = db.scalar(select(StockPaperAccount).where(StockPaperAccount.broker == "alpaca_paper"))
    sessions = _number(_metric_value(metric, "observed_sessions"))
    coverage = _number(_metric_value(metric, "decision_coverage"))
    closed = _number(_metric_value(metric, "closed_trades"))
    required_sessions = Decimal(str(trial.policy["regular_sessions"]))
    required_coverage = Decimal(str(trial.policy["minimum_decision_coverage"]))
    required_closed = Decimal(str(trial.policy["minimum_closed_trades"]))
    complete = bool(sessions is not None and sessions >= required_sessions)
    gates = {
        "regular_sessions": _gate("pass" if sessions is not None and sessions >= required_sessions else
                                  ("fail" if trial.status in {"completed", "stopped"} else "unknown"),
                                  str(sessions) if sessions is not None else None, str(required_sessions),
                                  None if sessions is not None and sessions >= required_sessions else "required verified sessions are not complete"),
        "decision_coverage": _gate("pass" if complete and coverage is not None and coverage >= required_coverage else
                                   ("fail" if complete and coverage is not None else "unknown"),
                                   str(coverage) if coverage is not None else None, str(required_coverage),
                                   None if complete and coverage is not None and coverage >= required_coverage else "coverage evidence is incomplete or below the frozen threshold"),
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
    }
    lineage_gate, lineage = _lineage_gate(db, trial)
    gates["immutable_lineage"] = lineage_gate
    statuses = {gate["status"] for gate in gates.values()}
    decision = "fail" if "fail" in statuses else ("pass" if statuses == {"pass"} else "unknown")
    evidence = {
        "trial_id": trial.id,
        "source_metric_id": metric.id if metric else None,
        "source_metric_as_of": metric.as_of.isoformat() if metric else None,
        "decision": decision,
        "gates": gates,
        "lineage": lineage or trial.lineage,
        "policy": trial.policy,
        "paper_only": True,
        "live_authorized": False,
        "promotion_authorized": False,
    }
    report_hash = _hash(evidence)
    existing = db.scalar(select(StockPaperPromotionReadinessReport).where(
        StockPaperPromotionReadinessReport.report_hash == report_hash
    ))
    if existing:
        return _report_out(existing)
    report = StockPaperPromotionReadinessReport(
        trial_id=trial.id, source_metric_id=metric.id if metric else None,
        report_hash=report_hash, decision=decision, gates=gates,
        lineage=evidence["lineage"], policy=trial.policy,
        paper_only=True, live_authorized=False,
    )
    db.add(report)
    db.flush()
    return _report_out(report)