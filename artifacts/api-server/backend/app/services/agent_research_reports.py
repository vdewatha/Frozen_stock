"""Durable, read-only comparison snapshots for the shadow research lane."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import math
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AgentResearchReport, AgentResearchRun
from app.services.agent_research import refresh_agent_research_evaluation


SCHEMA_VERSION = "agent-comparison-report-v1"
POLICY_VERSION = "shadow-research-evaluation-v1"
HORIZON_DAYS = 5
MAX_REPORT_RUNS = 100


def _safe(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(_safe(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _valid_probability(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and 0 <= float(value) <= 1
    )


def _as_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    text = str(value)
    return text[:10] if text else None


def _observation(row: AgentResearchRun) -> dict:
    evaluation = row.evaluation if isinstance(row.evaluation, dict) else {}
    result = row.result if isinstance(row.result, dict) else {}
    evaluation_status = evaluation.get("status")
    if row.status in {"queued", "running"}:
        status = "pending"
        reason = "Research run has not produced a result"
    elif row.status != "completed" or not result:
        status = "unavailable"
        reason = row.error or "Research run did not complete"
    elif evaluation_status == "pending":
        status = "pending"
        reason = evaluation.get("pending_reason") or "Exact five-observation outcome is not available"
    elif evaluation_status == "unavailable":
        status = "unavailable"
        reason = evaluation.get("pending_reason") or evaluation.get("reason") or "Forward evaluation is unavailable"
    elif evaluation_status == "complete" and evaluation.get("outcome_date"):
        status = "complete"
        reason = None
    else:
        status = "pending"
        reason = "Forward evaluation has not produced an exact eligible outcome"

    source_snapshot = _safe(row.source_snapshot or [])
    agent = evaluation.get("agent") if isinstance(evaluation.get("agent"), dict) else {}
    baseline = evaluation.get("baseline") if isinstance(evaluation.get("baseline"), dict) else {}
    baseline = _safe(baseline)
    if not baseline.get("status"):
        baseline = {
            "status": "unavailable",
            "reason": "No exact existing-model comparison is available",
        }
    elif baseline.get("status") == "complete" and not _valid_probability(
        baseline.get("probability_up")
    ):
        baseline = {
            **baseline,
            "status": "unavailable",
            "reason": "Existing-model prediction has an invalid probability",
        }
    if status == "complete" and baseline.get("status") != "complete":
        status = "unavailable"
        reason = baseline.get("reason") or "No exact existing-model comparison is available"

    return {
        "run_id": row.run_id,
        "symbol": row.symbol,
        "status": status,
        "reason": reason,
        "source_cutoff": _as_date(evaluation.get("source_cutoff")),
        "outcome_date": _as_date(evaluation.get("outcome_date")),
        "outcome_up": evaluation.get("outcome_up") if isinstance(evaluation.get("outcome_up"), bool) else None,
        "outcome_return": evaluation.get("outcome_return"),
        "outcome_observations": evaluation.get("outcome_observations", []),
        "framework_version": row.framework_version,
        "prompt_version": row.prompt_version,
        "model_name": row.model_name,
        "provider_model": (row.usage or {}).get("model"),
        "source_snapshot": source_snapshot,
        "source_sha256": _digest(source_snapshot),
        "agent": {
            "recommendation": agent.get("recommendation", result.get("recommendation")),
            "directional_correct": agent.get("directional_correct"),
        },
        "baseline": baseline,
        "requested_by": row.requested_by,
        "decision_at": _safe(row.decision_at),
        "run_created_at": _safe(row.created_at),
    }


def _build_report(db: Session) -> dict:
    rows = db.scalars(
        select(AgentResearchRun)
        .order_by(AgentResearchRun.created_at.desc(), AgentResearchRun.id.desc())
        .limit(MAX_REPORT_RUNS)
    ).all()
    for row in rows:
        refresh_agent_research_evaluation(db, row)
    observations = [_observation(row) for row in rows]

    matched_observations = [
        item
        for item in observations
        if item["status"] == "complete"
        and item["baseline"].get("status") == "complete"
        and _valid_probability(item["baseline"].get("probability_up"))
    ]
    valid_directional = [
        item
        for item in matched_observations
        if item["agent"].get("recommendation") in {"BUY", "SELL"}
        and isinstance(item["agent"].get("directional_correct"), bool)
        and isinstance(item.get("outcome_up"), bool)
    ]
    hold_count = sum(
        item["agent"].get("recommendation") == "HOLD" for item in matched_observations
    )
    directional_count = len(valid_directional)
    agent_accuracy = (
        sum(bool(item["agent"]["directional_correct"]) for item in valid_directional) / directional_count
        if directional_count
        else None
    )
    baseline_accuracy = (
        sum(
            (
                float(item["baseline"]["probability_up"]) >= 0.5
            )
            == bool(item["outcome_up"])
            for item in valid_directional
            if isinstance(item.get("outcome_up"), bool)
        )
        / directional_count
        if directional_count
        else None
    )
    # Both lanes are scored against the same observed label, including HOLD
    # in baseline-only Brier. Never infer a label from an agent score.
    brier_values: list[float] = []
    for item in matched_observations:
        probability = float(item["baseline"]["probability_up"])
        outcome_up = item.get("outcome_up")
        if isinstance(outcome_up, bool):
            brier_values.append((probability - int(outcome_up)) ** 2)

    pending_count = sum(item["status"] == "pending" for item in observations)
    unavailable_count = sum(
        item["status"] == "unavailable"
        or (item["status"] == "complete" and item["baseline"].get("status") != "complete")
        for item in observations
    )
    if not observations or all(item["status"] == "pending" for item in observations):
        report_status = "pending"
    elif all(item["status"] == "unavailable" for item in observations):
        report_status = "unavailable"
    elif any(item["status"] in {"pending", "unavailable"} for item in observations):
        report_status = "partial"
    else:
        report_status = "complete"

    cutoffs = sorted(
        item["source_cutoff"] for item in observations if item.get("source_cutoff")
    )
    symbols = sorted({item["symbol"] for item in observations})
    limitations = [
        f"Snapshot includes at most the {MAX_REPORT_RUNS} most recently created research runs; older runs are outside this snapshot. Previously saved snapshots remain available.",
        "Observations are research evidence only and eligible_for_trading is always false.",
        "Baseline direction accuracy and agent accuracy use the same exact baseline pairs and exclude HOLD recommendations.",
        "An up label means positive close-to-close return; zero return is non-up. Baseline P(up) >= 0.5 predicts up.",
        "Five days means five stored daily observations after the source cutoff, not calendar days; target observations from today or later remain pending.",
        "Missing baseline model versions are not inferred from source names. Agent confidence is not a calibrated probability.",
    ]
    if brier_values and hold_count:
        limitations.append(
            "Baseline Brier score includes matched HOLD observations when their exact forward outcome is available; HOLD is not included in directional accuracy."
        )
    if unavailable_count:
        limitations.append(
            "An unavailable baseline means no exact symbol, source cutoff date, and five-day prediction was used."
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "status": report_status,
        "eligible_for_trading": False,
        "scope": {
            "symbols": symbols,
            "horizon_days": HORIZON_DAYS,
            "run_count": len(observations),
            "source_cutoff_start": cutoffs[0] if cutoffs else None,
            "source_cutoff_end": cutoffs[-1] if cutoffs else None,
            "description": (
                f"Bounded comparison of up to {MAX_REPORT_RUNS} most recent shadow research runs "
                "against exact existing-model predictions and five-observation outcomes."
            ),
        },
        "coverage": {
            "matched": len(matched_observations),
            "pending": pending_count,
            "unavailable": unavailable_count,
            "hold": hold_count,
        },
        "metrics": {
            "agent_directional_accuracy": round(agent_accuracy, 8) if agent_accuracy is not None else None,
            "baseline_directional_accuracy": round(baseline_accuracy, 8) if baseline_accuracy is not None else None,
            "baseline_brier_score": round(sum(brier_values) / len(brier_values), 8) if brier_values else None,
            "directional_pair_count": directional_count,
        },
        "observations": observations,
        "limitations": limitations,
    }
    return _safe(report)


def _project(row: AgentResearchReport) -> dict:
    return {
        "report_id": row.report_id,
        "created_at": _safe(row.created_at),
        "content_sha256": row.content_sha256,
        "report": _safe(row.report),
    }


def create_agent_research_report(db: Session) -> dict:
    """Create or return the immutable snapshot for the current report content."""
    report = _build_report(db)
    content_sha256 = _digest(report)
    existing = db.scalar(
        select(AgentResearchReport).where(AgentResearchReport.content_sha256 == content_sha256)
    )
    if existing is not None:
        return _project(existing)

    row = AgentResearchReport(
        report_id=str(uuid4()),
        content_sha256=content_sha256,
        eligible_for_trading=False,
        report=report,
    )
    # The unique content index is the concurrency boundary.  A nested
    # transaction lets a concurrent creator lose the race without poisoning the
    # caller's outer transaction.
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(AgentResearchReport).where(
                AgentResearchReport.content_sha256 == content_sha256
            )
        )
        if existing is None:
            raise
        return _project(existing)
    return _project(row)


def list_agent_research_reports(db: Session, limit: int, offset: int) -> dict:
    limit = max(1, min(int(limit), MAX_REPORT_RUNS))
    offset = max(0, int(offset))
    rows = db.scalars(
        select(AgentResearchReport)
        .order_by(AgentResearchReport.created_at.desc(), AgentResearchReport.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [_project(row) for row in rows],
        "total": db.scalar(select(func.count()).select_from(AgentResearchReport)) or 0,
        "limit": limit,
        "offset": offset,
    }


def get_agent_research_report(db: Session, report_id: str) -> dict | None:
    row = db.scalar(
        select(AgentResearchReport).where(AgentResearchReport.report_id == report_id)
    )
    return _project(row) if row is not None else None