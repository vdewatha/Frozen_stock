"""Leakage-resistant online accuracy and drift evidence for stock paper trials.

This module intentionally keeps forward outcomes separate from training,
calibration, and the reserved final holdout.  It evaluates only decisions
whose immutable feature vintage can still be reproduced and records every
other decision as unknown or blocked instead of treating it as a negative.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from statistics import mean
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    MarketPrice,
    StockPaperAccuracyReport,
    StockPaperTrial,
    StockPaperTrialDecision,
    StockPaperTrialOutcome,
)


MIN_ACCURACY_SAMPLES = 30
MIN_DRIFT_SAMPLES = 20
MAX_EVALUATION_SAMPLES = 200
ACCURACY_CONTRACT_VERSION = "stock-point-in-time-accuracy-v1"
VERIFIED_PROVIDERS = {"yfinance", "yahoo_chart"}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _row_vintage(row: MarketPrice) -> dict[str, Any]:
    imported = _utc(row.imported_at).isoformat() if row.imported_at else None
    return {
        "symbol": str(row.symbol).upper(),
        "date": row.price_date.isoformat(),
        "source": row.source,
        "imported_at": imported,
        "open": str(row.open),
        "close": str(row.close),
        "adjusted_close": str(row.adjusted_close),
        "volume": int(row.volume or 0),
    }


def market_price_vintage(rows: list[MarketPrice]) -> str:
    """Hash the exact provider rows used to create a causal feature frame."""
    return _hash([_row_vintage(row) for row in sorted(rows, key=lambda item: item.price_date)])


def _lineage_timestamp(lineage: dict[str, Any], key: str) -> datetime | None:
    value = lineage.get(key)
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _feature_contract(
    db: Session,
    decision: StockPaperTrialDecision,
) -> tuple[str, str | None]:
    lineage = decision.lineage or {}
    feature_at = _lineage_timestamp(lineage, "feature_timestamp")
    cutoff = _lineage_timestamp(lineage, "decision_cutoff_timestamp") or _utc(decision.decision_timestamp)
    required = ("feature_timestamp", "feature_data_version", "feature_config_id", "bar_exchange_timestamp")
    if any(not lineage.get(key) for key in required):
        return "unknown", "point_in_time_feature_lineage_incomplete"
    if feature_at is None or feature_at >= _utc(decision.decision_timestamp):
        return "blocked", "feature_timestamp_not_before_decision"
    if cutoff > _utc(decision.decision_timestamp):
        return "blocked", "decision_cutoff_after_decision"
    provider = lineage.get("feature_provider")
    source_filter = (MarketPrice.source == provider) if provider else MarketPrice.source.in_(tuple(VERIFIED_PROVIDERS))
    rows = list(db.scalars(select(MarketPrice).where(
        MarketPrice.symbol == decision.symbol,
        MarketPrice.price_date <= feature_at.date(),
        source_filter,
    ).order_by(MarketPrice.price_date.asc())).all())
    if not rows:
        return "unknown", "feature_data_unavailable"
    if any(row.imported_at is None or _utc(row.imported_at) > cutoff for row in rows):
        return "unknown", "feature_data_unavailable_at_decision"
    current = market_price_vintage(rows)
    if current != lineage.get("feature_data_version"):
        return "restated", "feature_data_restated"
    return "verified", None


def _future_label_rows(
    db: Session,
    decision: StockPaperTrialDecision,
    *,
    horizon: int,
    as_of: datetime,
) -> tuple[list[MarketPrice], str | None]:
    feature_at = _lineage_timestamp(decision.lineage or {}, "feature_timestamp")
    if feature_at is None:
        return [], "feature_timestamp_unavailable"
    rows = list(db.scalars(select(MarketPrice).where(
        MarketPrice.symbol == decision.symbol,
        MarketPrice.price_date > feature_at.date(),
        MarketPrice.source.in_(tuple(VERIFIED_PROVIDERS)),
    ).order_by(MarketPrice.price_date.asc())).all())
    rows = [row for row in rows if row.imported_at and _utc(row.imported_at) <= as_of]
    if len(rows) < horizon:
        return [], "label_data_incomplete"
    return rows[:horizon], None


def _cost_rate(trial: StockPaperTrial) -> Decimal | None:
    assumptions = trial.lineage.get("cost_assumptions") or {}
    values = []
    for key in ("fee_rate_per_side", "commission_rate", "slippage_rate_per_side", "slippage_rate"):
        if key in assumptions:
            try:
                values.append(Decimal(str(assumptions[key])))
            except (ValueError, ArithmeticError):
                return None
    if not values:
        return None
    return sum(values, Decimal("0"))


def resolve_trial_outcomes(
    db: Session,
    trial_id: str,
    *,
    as_of: datetime | None = None,
) -> list[StockPaperTrialOutcome]:
    """Resolve each forward decision at most once, fencing duplicate labels."""
    trial = db.get(StockPaperTrial, trial_id)
    if not trial:
        raise ValueError("Trial not found")
    checked_at = _utc(as_of or datetime.now(timezone.utc))
    horizon = int(trial.policy.get("label_horizon_sessions", trial.policy.get("horizon_days", 5)))
    horizon = max(1, min(horizon, 252))
    decisions = db.scalars(select(StockPaperTrialDecision).where(
        StockPaperTrialDecision.trial_id == trial_id,
    ).order_by(StockPaperTrialDecision.bar_timestamp.asc(), StockPaperTrialDecision.id.asc())).all()
    existing = {
        row.decision_id: row
        for row in db.scalars(select(StockPaperTrialOutcome).where(
            StockPaperTrialOutcome.trial_id == trial_id,
        )).all()
    }
    created: list[StockPaperTrialOutcome] = []
    for decision in decisions:
        if decision.id in existing or decision.action != "buy":
            continue
        lineage = dict(decision.lineage or {})
        contract_status, contract_reason = _feature_contract(db, decision)
        label_lineage = {
            "contract_version": ACCURACY_CONTRACT_VERSION,
            "model_run_id": lineage.get("model_run_id"),
            "snapshot_id": lineage.get("snapshot_id"),
            "dataset_sha256": lineage.get("dataset_sha256"),
            "feature_config_id": lineage.get("feature_config_id"),
            "feature_data_version": lineage.get("feature_data_version"),
            "decision_cutoff_timestamp": lineage.get("decision_cutoff_timestamp"),
            "evaluated_at": checked_at.isoformat(),
            "evaluation_population": "forward_paper_outcome",
        }
        if lineage.get("evaluation_split") in {"training", "calibration", "holdout"}:
            contract_status, contract_reason = "blocked", "non_live_label_population"
        label_status = "resolved"
        reason = None
        label_rows: list[MarketPrice] = []
        if contract_status != "verified":
            label_status = "restated" if contract_status == "restated" else contract_status
            reason = contract_reason
        else:
            label_rows, reason = _future_label_rows(db, decision, horizon=horizon, as_of=checked_at)
            if reason:
                label_status = "unknown"
        realized_return = cost_adjusted = None
        realized_up = costs_known = None
        label_end = None
        if label_rows:
            feature_at = _lineage_timestamp(lineage, "feature_timestamp")
            feature_provider = lineage.get("feature_provider")
            base_source = (
                MarketPrice.source == feature_provider
                if feature_provider
                else MarketPrice.source.in_(tuple(VERIFIED_PROVIDERS))
            )
            base = db.scalar(select(MarketPrice.close).where(
                MarketPrice.symbol == decision.symbol,
                MarketPrice.price_date == feature_at.date(),
                base_source,
            ))
            last = label_rows[-1]
            finish = last.adjusted_close or last.close
            if base is None or finish is None or Decimal(str(base)) <= 0:
                label_status, reason = "unknown", "label_price_unavailable"
            else:
                realized_return = Decimal(str(finish)) / Decimal(str(base)) - Decimal("1")
                realized_up = realized_return > 0
                rate = _cost_rate(trial)
                costs_known = rate is not None
                cost_adjusted = realized_return - (rate * Decimal("2")) if rate is not None else None
                label_end = datetime.combine(
                    last.price_date, datetime.max.time(), tzinfo=timezone.utc,
                )
                label_lineage.update({
                    "label_start_date": label_rows[0].price_date.isoformat(),
                    "label_end_date": last.price_date.isoformat(),
                    "label_provider": last.source,
                    "label_data_version": market_price_vintage(label_rows),
                })
        outcome = StockPaperTrialOutcome(
            trial_id=trial_id,
            decision_id=decision.id,
            symbol=decision.symbol,
            label_status=label_status,
            reason=reason,
            label_end=label_end,
            realized_return=realized_return,
            cost_adjusted_return=cost_adjusted,
            realized_up=realized_up,
            costs_known=costs_known,
            label_lineage=label_lineage,
        )
        db.add(outcome)
        try:
            with db.begin_nested():
                db.flush()
            existing[decision.id] = outcome
            created.append(outcome)
        except IntegrityError:
            # Another worker won the unique decision fence.  The savepoint
            # rollback keeps the caller's transaction usable; do not roll back
            # unrelated outcome evidence already staged by this evaluation.
            continue
    return list(existing.values())


def _wilson(successes: int, total: int) -> dict[str, float] | None:
    if total < MIN_ACCURACY_SAMPLES:
        return None
    z = 1.96
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return {"lower": max(0.0, center - margin), "upper": min(1.0, center + margin), "level": 0.95}


def _drift(decisions: list[StockPaperTrialDecision], probabilities: list[float]) -> dict[str, Any]:
    vectors = [
        [float((decision.lineage or {}).get("features", {}).get(name))
         for name in sorted((decision.lineage or {}).get("features", {}))
         if isinstance((decision.lineage or {}).get("features", {}).get(name), (int, float))]
        for decision in decisions
    ]
    if len(probabilities) < MIN_DRIFT_SAMPLES or len(vectors) < MIN_DRIFT_SAMPLES:
        return {"status": "unknown", "score": None, "reason": "insufficient_drift_samples"}
    midpoint = len(probabilities) // 2
    old, recent = probabilities[:midpoint], probabilities[midpoint:]
    probability_shift = abs(mean(recent) - mean(old))
    values = []
    for index in range(min(len(vectors[0]), max(len(row) for row in vectors))):
        left = [row[index] for row in vectors[:midpoint] if len(row) > index]
        right = [row[index] for row in vectors[midpoint:] if len(row) > index]
        if left and right:
            scale = max(float(np.std(left + right)), 1e-9)
            values.append(abs(mean(right) - mean(left)) / scale)
    score = float(mean(values)) if values else probability_shift
    threshold = 2.0
    return {
        "status": "degraded" if score > threshold or probability_shift > 0.2 else "stable",
        "score": score,
        "probability_shift": probability_shift,
        "threshold": threshold,
        "method": "recent_half_vs_earlier_half_standardized_mean_shift",
        "sample_count": len(probabilities),
    }


def build_accuracy_evidence(
    db: Session,
    trial: StockPaperTrial,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    checked_at = _utc(as_of or datetime.now(timezone.utc))
    outcomes = resolve_trial_outcomes(db, trial.id, as_of=checked_at)
    decisions = db.scalars(select(StockPaperTrialDecision).where(
        StockPaperTrialDecision.trial_id == trial.id,
        StockPaperTrialDecision.action == "buy",
    ).order_by(StockPaperTrialDecision.bar_timestamp.asc(), StockPaperTrialDecision.id.asc())).all()
    outcomes_by_decision = {row.decision_id: row for row in outcomes}
    contract_by_decision = {
        decision.id: _feature_contract(db, decision)
        for decision in decisions
    }
    resolved = [
        (decision, outcomes_by_decision[decision.id])
        for decision in decisions
        if decision.id in outcomes_by_decision
        and outcomes_by_decision[decision.id].label_status == "resolved"
        and outcomes_by_decision[decision.id].realized_up is not None
        and contract_by_decision[decision.id][0] == "verified"
    ][-MAX_EVALUATION_SAMPLES:]
    scored = [
        (decision, outcome)
        for decision, outcome in resolved
        if isinstance((decision.lineage or {}).get("probability"), (int, float))
        and math.isfinite(float((decision.lineage or {}).get("probability")))
    ]
    probabilities = [float((decision.lineage or {}).get("probability")) for decision, _ in scored]
    targets = [int(outcome.realized_up) for _, outcome in scored]
    returns = [
        float(outcome.cost_adjusted_return)
        for _, outcome in scored
        if outcome.cost_adjusted_return is not None
    ]
    unknown = [row for row in outcomes if row.label_status != "resolved"]
    contract_issues = [
        (status, reason)
        for status, reason in contract_by_decision.values()
        if status != "verified"
    ]
    prediction_count = len(decisions)
    sample_count = len(targets)
    accuracy = (sum(
        int((probability >= 0.5) == bool(target))
        for probability, target in zip(probabilities, targets)
    ) / sample_count
                if sample_count else None)
    brier = (sum((probability - target) ** 2 for probability, target in zip(probabilities, targets)) / sample_count
             if sample_count else None)
    clipped = [min(max(value, 1e-6), 1 - 1e-6) for value in probabilities]
    logloss = (-sum(target * math.log(probability) + (1 - target) * math.log(1 - probability)
                    for probability, target in zip(clipped, targets)) / sample_count
               if sample_count else None)
    prevalence = (sum(targets) / sample_count) if sample_count else None
    baseline_brier = (sum((prevalence - target) ** 2 for target in targets) / sample_count
                      if prevalence is not None else None)
    baseline_logloss = (
        -sum(target * math.log(min(max(prevalence, 1e-6), 1 - 1e-6))
             + (1 - target) * math.log(1 - min(max(prevalence, 1e-6), 1 - 1e-6))
             for target in targets) / sample_count
        if prevalence is not None else None
    )
    bins = []
    for lower, upper in ((0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.00001)):
        members = [(probability, target) for probability, target in zip(probabilities, targets)
                   if lower <= probability < upper]
        bins.append({
            "lower": lower, "upper": min(upper, 1.0), "count": len(members),
            "mean_probability": mean([item[0] for item in members]) if members else None,
            "observed_frequency": mean([item[1] for item in members]) if members else None,
        })
    ece = (sum(item["count"] * abs(item["mean_probability"] - item["observed_frequency"])
               for item in bins if item["count"]) / sample_count) if sample_count else None
    max_drawdown = None
    if returns:
        equity = peak = 1.0
        max_drawdown = 0.0
        for result in returns:
            equity *= 1 + result
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity / peak - 1)
    contract = {
        "version": ACCURACY_CONTRACT_VERSION,
        "population": "forward_paper_outcome",
        "feature_cutoff": "feature_timestamp < decision_timestamp",
        "data_vintage": "feature_data_version must match persisted provider rows",
        "labels": "live_or_paper_outcomes_only; training_calibration_holdout_excluded",
        "window_limit": MAX_EVALUATION_SAMPLES,
        "holdout_reuse": "forbidden",
    }
    data_health = {
        "status": "blocked"
        if any(row.label_status in {"blocked", "restated"} for row in unknown)
        or any(status in {"blocked", "restated"} for status, _ in contract_issues)
        else ("unknown" if unknown or contract_issues else "verified"),
        "unknown_labels": sum(row.label_status == "unknown" for row in unknown)
        + sum(status == "unknown" for status, _ in contract_issues),
        "restated_labels": sum(row.label_status == "restated" for row in unknown)
        + sum(status == "restated" for status, _ in contract_issues),
        "blocked_labels": sum(row.label_status == "blocked" for row in unknown)
        + sum(status == "blocked" for status, _ in contract_issues),
        "reasons": sorted(
            {row.reason for row in unknown if row.reason}
            | {reason for _, reason in contract_issues if reason}
        ),
    }
    drift = _drift([decision for decision, _ in scored], probabilities)
    if data_health["status"] == "blocked":
        classification, reason = "blocked", "feature_or_label_vintage_not_reproducible"
    elif sample_count < MIN_ACCURACY_SAMPLES:
        classification = "insufficient"
        reason = "minimum_accuracy_sample_not_met"
    elif drift["status"] == "degraded":
        classification, reason = "degraded", "model_or_feature_drift_exceeds_threshold"
    elif baseline_brier is not None and brier is not None and brier > baseline_brier:
        classification, reason = "degraded", "model_underperforms_prevalence_baseline"
    else:
        classification, reason = "sufficient", None
    return {
        "version": 1,
        "as_of": checked_at.isoformat(),
        "classification": classification,
        "reason": reason,
        "contract": contract,
        "lineage": {
            "model_run_id": trial.lineage.get("model_run_id"),
            "snapshot_id": trial.lineage.get("snapshot_id"),
            "dataset_sha256": trial.lineage.get("dataset_sha256"),
            "feature_config_id": next(
                (
                    (decision.lineage or {}).get("feature_config_id")
                    for decision in decisions
                    if (decision.lineage or {}).get("feature_config_id")
                ),
                trial.lineage.get("feature_config_id"),
            ),
            "universe": trial.lineage.get("universe"),
        },
        "window": {
            "sample_count": sample_count,
            "prediction_count": prediction_count,
            "unknown_count": len(unknown),
            "bounded": True,
        },
        "accuracy": {
            "value": accuracy,
            "confidence_interval": _wilson(sum(
                int((probability >= 0.5) == bool(target))
                for probability, target in zip(probabilities, targets)
            ), sample_count),
        },
        "calibration": {
            "brier_score": brier,
            "log_loss": logloss,
            "expected_calibration_error": ece,
            "bins": bins,
        },
        "baseline": {
            "kind": "empirical_live_outcome_prevalence",
            "prevalence": prevalence,
            "brier_score": baseline_brier,
            "log_loss": baseline_logloss,
        },
        "coverage": {
            "value": sample_count / prediction_count if prediction_count else 0.0,
            "resolved": sample_count,
            "predictions": prediction_count,
            "confidence_interval": _wilson(sample_count, prediction_count),
        },
        "cost_aware": {
            "sample_count": len(returns),
            "total_return": sum(returns) if returns else None,
            "mean_return": mean(returns) if returns else None,
            "max_drawdown": max_drawdown,
            "costs_known": len(returns) == sample_count,
        },
        "data_health": data_health,
        "drift": drift,
        "limitations": [
            "Forward paper outcomes are not training or calibration labels.",
            "Unknown, stale, incomplete, and restated data receive no accuracy credit.",
            "Historical extraction-time snapshots are not point-in-time historical evidence.",
            "Cross-version comparisons are comparable only when data, feature, universe, and evaluation contracts match.",
        ],
    }


def persist_accuracy_evidence(
    db: Session,
    trial: StockPaperTrial,
    *,
    as_of: datetime | None = None,
) -> tuple[StockPaperAccuracyReport, dict[str, Any]]:
    evidence = build_accuracy_evidence(db, trial, as_of=as_of)
    report_hash = _hash(evidence)
    existing = db.scalar(select(StockPaperAccuracyReport).where(
        StockPaperAccuracyReport.report_hash == report_hash,
    ))
    if existing:
        return existing, evidence
    report = StockPaperAccuracyReport(
        trial_id=trial.id,
        as_of=_utc(as_of or datetime.now(timezone.utc)),
        classification=evidence["classification"],
        report_hash=report_hash,
        lineage=evidence["lineage"],
        evidence=evidence,
    )
    db.add(report)
    db.flush()
    return report, evidence


def accuracy_report_out(report: StockPaperAccuracyReport) -> dict[str, Any]:
    return {
        "id": report.id,
        "trial_id": report.trial_id,
        "as_of": report.as_of,
        "classification": report.classification,
        "report_hash": report.report_hash,
        "lineage": report.lineage,
        "evidence": report.evidence,
        "paper_only": True,
        "live_authorized": False,
    }


def compare_accuracy_reports(reports: list[StockPaperAccuracyReport]) -> dict[str, Any]:
    if not reports:
        return {"status": "unknown", "reason": "no_accuracy_reports", "items": []}
    signatures = {
        _hash({
            "feature_config_id": report.lineage.get("feature_config_id"),
            "universe": report.lineage.get("universe"),
            "snapshot_id": report.lineage.get("snapshot_id"),
        })
        for report in reports
    }
    comparable = len(signatures) == 1
    return {
        "status": "comparable" if comparable else "not_comparable",
        "reason": None if comparable else "model_versions_use_different_data_or_feature_contracts",
        "items": [accuracy_report_out(report) for report in reports],
    }