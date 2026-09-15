#!/usr/bin/env python3
"""Write a read-only, durable evidence report for a bounded paper soak.

The report intentionally distinguishes runtime safety from forward-evidence
completion.  It never evaluates a trial, changes a binding, calls the broker,
or treats missing observations as successful evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from typing import Any, Iterable

from sqlalchemy import or_, select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import (
    AuditLog,
    StockDatasetSnapshot,
    StockLearningCycle,
    StockLearningCycleEvent,
    StockModelRegistry,
    StockPaperBindingState,
    StockPaperModelBinding,
    StockPaperPromotionDecision,
    StockPaperPromotionReadinessReport,
    StockPaperRecoveryEvent,
    StockPaperRecoveryState,
    StockPaperTrial,
    StockTrainingJob,
    StockMonitoringSnapshot,
    StockLearningScheduleControl,
)
from app.models.stock_paper import StockPaperOrder, StockPaperTrialLot
from app.services.broker import broker_status
from app.services.deployment_monitor import deployment_monitor_snapshot
from app.services.stock_recovery import recovery_status


INTERRUPTION_MATRIX_NAMES = ("feed", "ledger", "worker", "beat_lease", "redis")
INTERRUPTION_STATUSES = {
    "feed": {"deferred", "blocked"},
    "ledger": {"deferred", "blocked"},
    "worker": {"deferred", "blocked", "recovered", "resumed"},
    "beat_lease": {"blocked", "deferred", "recovered", "resumed"},
    "redis": {"blocked", "deferred", "recovered", "resumed"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()
    return str(value)


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _ids(rows: Iterable[Any], attribute: str) -> list[Any]:
    return [getattr(row, attribute) for row in rows]


def _created_since(statement, model, since: datetime | None):
    return statement.where(model.created_at >= since) if since else statement


def _latest_reports(db, trial_ids: list[str]) -> list[StockPaperPromotionReadinessReport]:
    if not trial_ids:
        return []
    rows = db.scalars(
        select(StockPaperPromotionReadinessReport)
        .where(StockPaperPromotionReadinessReport.trial_id.in_(trial_ids))
        .order_by(
            StockPaperPromotionReadinessReport.trial_id,
            StockPaperPromotionReadinessReport.report_version.desc(),
            StockPaperPromotionReadinessReport.id.desc(),
        )
    ).all()
    latest: dict[str, StockPaperPromotionReadinessReport] = {}
    for row in rows:
        latest.setdefault(row.trial_id, row)
    return list(latest.values())


def _forward_evidence_status(reports: list[StockPaperPromotionReadinessReport]) -> dict:
    if not reports:
        return {
            "complete": False,
            "reports": 0,
            "reason": "No cycle-owned promotion-readiness report exists",
        }
    incomplete: list[dict] = []
    for row in reports:
        if row.decision != "pass":
            incomplete.append({
                "report_id": row.id,
                "trial_id": row.trial_id,
                "reason": "readiness report is not passing",
            })
            continue
        gates = row.gates or {}
        if not gates or any(
            not isinstance(gate, dict) or gate.get("status") != "pass"
            for gate in gates.values()
        ):
            incomplete.append({
                "report_id": row.id,
                "trial_id": row.trial_id,
                "reason": "readiness report contains a non-passing gate",
            })
            continue
        evidence = row.evidence or {}
        session_evidence = evidence.get("session_evidence")
        window = session_evidence.get("window") if isinstance(session_evidence, dict) else None
        aggregates = session_evidence.get("aggregates") if isinstance(session_evidence, dict) else None
        policy = row.policy or {}
        required_sessions = policy.get("regular_sessions")
        required_coverage = policy.get("minimum_decision_coverage")
        try:
            verified_coverage = Decimal(str(
                (aggregates or {}).get("verified_decision_coverage")
            ))
            coverage_threshold = Decimal(str(required_coverage))
        except (InvalidOperation, TypeError, ValueError):
            verified_coverage = None
            coverage_threshold = None
        if (
            not isinstance(window, dict)
            or window.get("complete") is not True
            or window.get("elapsed_regular_sessions") != required_sessions
            or not isinstance(aggregates, dict)
            or aggregates.get("unknown_historical_feed_health") != 0
            or verified_coverage is None
            or coverage_threshold is None
            or verified_coverage < coverage_threshold
        ):
            incomplete.append({
                "report_id": row.id,
                "trial_id": row.trial_id,
                "reason": "frozen post-handoff session evidence is incomplete",
            })
    if incomplete:
        return {
            "complete": False,
            "reports": len(reports),
            "passing_reports": len(reports) - len(incomplete),
            "reason": "At least one cycle-owned readiness report lacks complete frozen evidence",
            "incomplete_reports": incomplete,
        }
    return {
        "complete": True,
        "reports": len(reports),
        "passing_reports": len(reports),
        "reason": "Every cycle-owned readiness report and frozen evidence gate is passing",
    }


def _cycle_projection(row: StockLearningCycle, events: list[StockLearningCycleEvent]) -> dict:
    return {
        "cycle_id": row.cycle_id,
        "trigger": row.trigger,
        "status": row.status,
        "stage": row.stage,
        "symbols": row.symbols,
        "training_job_id": row.training_job_id,
        "snapshot_id": row.snapshot_id,
        "model_run_id": row.model_run_id,
        "binding_id": row.binding_id,
        "trial_id": row.trial_id,
        "report_id": row.evidence.get("report_id") if isinstance(row.evidence, dict) else None,
        "monitor_snapshot_id": row.monitor_snapshot_id,
        "recovery_event_id": row.recovery_event_id,
        "last_reason": row.last_reason,
        "event_ids": [event.id for event in events],
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def build_report(
    db,
    *,
    since: datetime | None = None,
    interruption_records: list[dict] | None = None,
) -> dict:
    # The soak schema is isolated, so cycle rows are the authoritative scope
    # even when a caller deliberately reuses a schema for a retry.
    cycles_statement = select(StockLearningCycle).order_by(StockLearningCycle.created_at)
    cycles = db.scalars(cycles_statement).all()
    cycle_ids = _ids(cycles, "cycle_id")

    events = db.scalars(
        select(StockLearningCycleEvent)
        .where(StockLearningCycleEvent.cycle_id.in_(cycle_ids))
        .order_by(StockLearningCycleEvent.id)
    ).all() if cycle_ids else []
    snapshot_ids = [row.snapshot_id for row in cycles if row.snapshot_id]
    model_ids = [row.model_run_id for row in cycles if row.model_run_id]
    binding_ids = [row.binding_id for row in cycles if row.binding_id]
    trial_ids = [row.trial_id for row in cycles if row.trial_id]
    training_job_ids = [row.training_job_id for row in cycles if row.training_job_id]

    snapshots = db.scalars(
        select(StockDatasetSnapshot).where(StockDatasetSnapshot.snapshot_id.in_(snapshot_ids))
    ).all() if snapshot_ids else []
    models = db.scalars(
        select(StockModelRegistry).where(StockModelRegistry.run_id.in_(model_ids))
    ).all() if model_ids else []
    jobs = db.scalars(
        select(StockTrainingJob).where(StockTrainingJob.id.in_(training_job_ids))
    ).all() if training_job_ids else []
    bindings = db.scalars(
        select(StockPaperModelBinding).where(StockPaperModelBinding.id.in_(binding_ids))
    ).all() if binding_ids else []
    trials = db.scalars(
        select(StockPaperTrial).where(StockPaperTrial.id.in_(trial_ids))
    ).all() if trial_ids else []
    reports = _latest_reports(db, trial_ids)
    decisions = db.scalars(
        select(StockPaperPromotionDecision)
        .where(StockPaperPromotionDecision.cycle_id.in_(cycle_ids))
        .order_by(StockPaperPromotionDecision.id)
    ).all() if cycle_ids else []

    monitor_ids = {row.monitor_snapshot_id for row in cycles if row.monitor_snapshot_id}
    monitor_statement = select(StockMonitoringSnapshot).order_by(StockMonitoringSnapshot.id)
    if since:
        monitor_statement = monitor_statement.where(
            or_(
                StockMonitoringSnapshot.created_at >= since,
                StockMonitoringSnapshot.id.in_(monitor_ids),
            )
        )
    elif monitor_ids:
        monitor_statement = monitor_statement.where(StockMonitoringSnapshot.id.in_(monitor_ids))
    monitors = db.scalars(monitor_statement).all()
    recovery_ids = {row.recovery_event_id for row in cycles if row.recovery_event_id}
    recovery_statement = select(StockPaperRecoveryEvent).order_by(StockPaperRecoveryEvent.id)
    if since:
        recovery_statement = recovery_statement.where(
            or_(
                StockPaperRecoveryEvent.created_at >= since,
                StockPaperRecoveryEvent.id.in_(recovery_ids),
            )
        )
    elif recovery_ids:
        recovery_statement = recovery_statement.where(StockPaperRecoveryEvent.id.in_(recovery_ids))
    recovery_events = db.scalars(recovery_statement).all()
    recovery_state = db.get(StockPaperRecoveryState, 1)
    binding_state = db.get(StockPaperBindingState, 1)
    schedule_control = db.get(StockLearningScheduleControl, 1)

    audit_rows = db.scalars(
        select(AuditLog).where(
            AuditLog.event_type.in_(
                {
                    "stock_learning_cycle",
                    "stock_monitoring",
                    "stock_recovery",
                    "stock_paper_promotion",
                }
            )
        ).order_by(AuditLog.id)
    ).all()

    try:
        runtime = deployment_monitor_snapshot(db)
        runtime_error = None
    except Exception as exc:
        runtime = {
            "status": "blocked",
            "deployable": False,
            "blockers": ["deployment monitor unavailable"],
            "readiness_status": "unknown",
        }
        runtime_error = exc.__class__.__name__
    try:
        recovery = recovery_status(db)
        recovery_error = None
    except Exception as exc:
        recovery = {"status": "unknown", "events": []}
        recovery_error = exc.__class__.__name__
    try:
        broker = broker_status()
    except Exception as exc:
        broker = {"paper_trading_enabled": False, "live_trading_blocked": False}
        broker_error = exc.__class__.__name__
    else:
        broker_error = None

    source_binding_counts = Counter(row.source_cycle_id for row in bindings if row.source_cycle_id)
    source_trial_counts = Counter(row.source_cycle_id for row in trials if row.source_cycle_id)
    cycle_order_ids: list[str] = []
    if trial_ids:
        lots = db.scalars(
            select(StockPaperTrialLot).where(StockPaperTrialLot.trial_id.in_(trial_ids))
        ).all()
        lot_ids = [lot.id for lot in lots]
        if lot_ids:
            cycle_order_ids = _ids(
                db.scalars(
                    select(StockPaperOrder).where(StockPaperOrder.trial_lot_id.in_(lot_ids))
                ).all(),
                "client_order_id",
            )
    interruption_matrix = _interruption_matrix_status(interruption_records or [])
    interruption_evidence = {
        "scope": "bounded_soak",
        "recovery_event_ids": [row.id for row in recovery_events],
        "audit_log_ids": [row.id for row in audit_rows],
    }
    duplicate_checks = {
        "cycle_request_ids_unique": len(cycle_ids) == len(set(cycle_ids)),
        "scheduled_binding_per_cycle": max(source_binding_counts.values(), default=0) <= 1,
        "scheduled_trial_per_cycle": max(source_trial_counts.values(), default=0) <= 1,
        "cycle_owned_order_ids_unique": len(cycle_order_ids) == len(set(cycle_order_ids)),
        "recovery_event_ids_unique": len(interruption_evidence["recovery_event_ids"]) == len(set(interruption_evidence["recovery_event_ids"])),
        "audit_log_ids_unique": len(interruption_evidence["audit_log_ids"]) == len(set(interruption_evidence["audit_log_ids"])),
    }

    forward_evidence = _forward_evidence_status(reports)
    live_safe = (
        settings.allow_live_trading is False
        and broker.get("live_trading_blocked") is True
        and all(row.live_authorized is False and row.paper_only is True for row in bindings + decisions + reports)
        and all(
            (row.policy or {}).get("paper_only") is True
            and (row.policy or {}).get("live_authorized") is False
            for row in trials
        )
    )
    recovery_safe = recovery.get("status") in {"armed", "resumable"}
    runtime_ready = runtime.get("status") == "ready" and not runtime.get("blockers")
    readiness_blockers = []
    if not runtime_ready:
        readiness_blockers.extend(runtime.get("blockers") or ["runtime health is not ready"])
    if not live_safe:
        readiness_blockers.append("live-trading-disabled invariant is not proven")
    if not recovery_safe:
        readiness_blockers.append(f"recovery state is {recovery.get('status', 'unknown')}")
    if not all(duplicate_checks.values()):
        readiness_blockers.append("duplicate lineage or order identifiers detected")
    if not interruption_matrix["complete"]:
        readiness_blockers.append("controlled interruption matrix is incomplete")
    if not interruption_evidence["recovery_event_ids"] or not interruption_evidence["audit_log_ids"]:
        readiness_blockers.append("controlled interruptions have no durable recovery and audit identifiers")

    events_by_cycle: dict[str, list[StockLearningCycleEvent]] = {}
    for event in events:
        events_by_cycle.setdefault(event.cycle_id, []).append(event)
    report = {
        "schema_version": 1,
        "report_type": "bounded_autonomous_paper_soak",
        "generated_at": _iso(_now()),
        "soak_started_at": _iso(since),
        "environment": settings.environment,
        "runtime": {
            "deployment_monitor": runtime,
            "deployment_monitor_error": runtime_error,
            "broker": broker,
            "broker_error": broker_error,
            "recovery_status": recovery,
            "recovery_status_error": recovery_error,
            "schedule_control": {
                "paused": bool(schedule_control.paused) if schedule_control else False,
                "pause_reason": schedule_control.pause_reason if schedule_control else None,
                "updated_by": schedule_control.updated_by if schedule_control else "system",
                "updated_at": _iso(schedule_control.updated_at) if schedule_control else None,
            },
        },
        "lineage": {
            "cycles": [_cycle_projection(row, events_by_cycle.get(row.cycle_id, [])) for row in cycles],
            "datasets": [
                {
                    "snapshot_id": row.snapshot_id,
                    "dataset_sha256": row.dataset_sha256,
                    "provider": row.provider,
                    "cutoff_date": _iso(row.cutoff_date),
                }
                for row in snapshots
            ],
            "training_jobs": [
                {"id": row.id, "status": row.status, "result_run_id": row.result_run_id}
                for row in jobs
            ],
            "models": [
                {
                    "run_id": row.run_id,
                    "snapshot_id": row.snapshot_id,
                    "manifest_sha256": row.manifest_sha256,
                    "lifecycle_state": row.lifecycle_state,
                }
                for row in models
            ],
            "bindings": [
                {
                    "id": row.id,
                    "source_cycle_id": row.source_cycle_id,
                    "model_run_id": row.model_run_id,
                    "snapshot_id": row.snapshot_id,
                    "paper_only": row.paper_only,
                    "live_authorized": row.live_authorized,
                }
                for row in bindings
            ],
            "active_binding_id": binding_state.active_binding_id if binding_state else None,
            "trials": [
                {
                    "id": row.id,
                    "source_cycle_id": row.source_cycle_id,
                    "binding_id": row.binding_id,
                    "status": row.status,
                    "policy": row.policy,
                    "paper_only": True,
                    "live_authorized": False,
                }
                for row in trials
            ],
            "readiness_reports": [
                {
                    "id": row.id,
                    "trial_id": row.trial_id,
                    "report_hash": row.report_hash,
                    "decision": row.decision,
                    "report_version": row.report_version,
                    "paper_only": row.paper_only,
                    "live_authorized": row.live_authorized,
                }
                for row in reports
            ],
            "promotion_decisions": [
                {
                    "id": row.id,
                    "cycle_id": row.cycle_id,
                    "trial_id": row.trial_id,
                    "report_id": row.report_id,
                    "decision": row.decision,
                    "decision_sha256": row.decision_sha256,
                    "before_binding_id": row.before_binding_id,
                    "after_binding_id": row.after_binding_id,
                    "paper_only": row.paper_only,
                    "live_authorized": row.live_authorized,
                }
                for row in decisions
            ],
            "monitoring_snapshots": [
                {"id": row.id, "status": row.status, "source": row.source, "generated_at": _iso(row.generated_at)}
                for row in monitors
            ],
            "recovery_events": [
                {"id": row.id, "action": row.action, "status": row.status, "actor": row.actor}
                for row in recovery_events
            ],
            "audit_log_ids": [row.id for row in audit_rows],
            "cycle_event_ids": [row.id for row in events],
        },
        "interruptions": [
            record | {"evidence": interruption_evidence}
            for record in (interruption_records or [])
        ],
        "checks": {
            "duplicate_lineage": duplicate_checks,
            "interruption_matrix": interruption_matrix,
            "all_cycle_stages_observed": {
                stage: any(event.stage == stage for event in events)
                for stage in ("preflight", "dataset", "training", "validation", "qualification", "admission", "promotion", "recovery")
            },
            "forward_trial_links_observed": bool(trials),
            "recovery_prior_binding": {
                "status": recovery_state.status if recovery_state else "unknown",
                "last_known_good_binding_id": recovery_state.last_known_good_binding_id if recovery_state else None,
                "active_binding_id": binding_state.active_binding_id if binding_state else None,
            },
        },
        "forward_evidence": forward_evidence,
        "readiness": {
            "ready_for_continued_paper_operation": not readiness_blockers,
            "forward_evidence_complete": forward_evidence["complete"],
            "live_trading_ready": False,
            "profitability_claim": False,
            "blockers": readiness_blockers,
        },
    }
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="UTC ISO timestamp marking the soak start")
    parser.add_argument("--output", type=Path, required=True, help="Durable JSON report path")
    parser.add_argument(
        "--interruption",
        action="append",
        default=[],
        metavar="NAME=STATUS:REASON",
        help="Record an operator-observed interruption without changing database state",
    )
    return parser.parse_args(argv)


def _parse_interruption(value: str) -> dict:
    try:
        name, rest = value.split("=", 1)
        status, reason = rest.split(":", 1)
    except ValueError as exc:
        raise ValueError("interruption must use NAME=STATUS:REASON") from exc
    if not name.strip() or not status.strip() or not reason.strip():
        raise ValueError("interruption name, status, and reason are required")
    name = name.strip()
    status = status.strip()
    if name not in INTERRUPTION_MATRIX_NAMES:
        raise ValueError(f"unknown interruption {name!r}; expected one of {', '.join(INTERRUPTION_MATRIX_NAMES)}")
    if status not in INTERRUPTION_STATUSES[name]:
        expected = ", ".join(sorted(INTERRUPTION_STATUSES[name]))
        raise ValueError(f"invalid status {status!r} for {name}; expected one of {expected}")
    return {"name": name, "status": status, "reason": reason.strip()}


def _interruption_matrix_status(records: list[dict]) -> dict:
    by_name: dict[str, list[dict]] = {}
    for record in records:
        by_name.setdefault(record["name"], []).append(record)
    missing = [name for name in INTERRUPTION_MATRIX_NAMES if name not in by_name]
    duplicates = {name: len(rows) for name, rows in by_name.items() if len(rows) > 1}
    invalid_statuses = [
        {
            "name": record["name"],
            "status": record["status"],
            "expected": sorted(INTERRUPTION_STATUSES[record["name"]]),
        }
        for record in records
        if record["status"] not in INTERRUPTION_STATUSES[record["name"]]
    ]
    return {
        "required": list(INTERRUPTION_MATRIX_NAMES),
        "observed": [name for name in INTERRUPTION_MATRIX_NAMES if name in by_name],
        "missing": missing,
        "duplicates": duplicates,
        "invalid_statuses": invalid_statuses,
        "complete": not missing and not duplicates and not invalid_statuses,
    }


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        interruptions = [_parse_interruption(value) for value in args.interruption]
        with SessionLocal() as db:
            report = build_report(db, since=_parse_since(args.since), interruption_records=interruptions)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"paper soak report failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"paper_soak_report={args.output}")
    print(f"cycles={len(report['lineage']['cycles'])}")
    print(f"forward_evidence_complete={str(report['readiness']['forward_evidence_complete']).lower()}")
    print(f"ready_for_continued_paper_operation={str(report['readiness']['ready_for_continued_paper_operation']).lower()}")
    print(f"live_trading_ready={str(report['readiness']['live_trading_ready']).lower()}")
    return 0 if report["readiness"]["ready_for_continued_paper_operation"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))