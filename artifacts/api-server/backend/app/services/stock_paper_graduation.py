"""Immutable, redacted paper graduation packages and reviewer dispositions."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Notification,
    StockLearningCycle,
    StockMonitoringSnapshot,
    StockPaperAccuracyReport,
    StockPaperGraduationPackage,
    StockPaperPromotionDecision,
    StockPaperPromotionReadinessReport,
    StockPaperRecoveryEvent,
    StockPaperRecoveryState,
    StockPaperTrial,
)
from app.models.stock_paper import StockPaperAccount, StockPaperOrder
from app.services.audit import write_audit_log
from app.services.stock_accuracy import accuracy_report_out
from app.services.stock_paper_ledger import active_paper_account


REQUIRED_SESSIONS = 20
INTERRUPTION_NAMES = {"feed", "ledger", "worker", "beat_lease", "redis"}
REPORTS_ROOT = Path(__file__).resolve().parents[2] / "reports"
REPORT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}\.json$")


class PaperGraduationError(RuntimeError):
    pass


class PaperGraduationBlocked(PaperGraduationError):
    pass


def _safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_safe(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _latest_soak_name() -> str | None:
    names = sorted(path.name for path in REPORTS_ROOT.glob("autonomous-paper-soak-*.json"))
    return names[-1] if names else None


def _load_soak_report(name: str | None) -> tuple[dict | None, str | None, str | None]:
    selected = name or _latest_soak_name()
    if not selected:
        return None, None, None
    if not REPORT_NAME.fullmatch(selected):
        raise PaperGraduationError("Soak report name is invalid")
    path = REPORTS_ROOT / selected
    if not path.is_file():
        raise PaperGraduationError("Soak report was not found")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PaperGraduationError("Soak report is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PaperGraduationError("Soak report must be a JSON object")
    return payload, selected, hashlib.sha256(raw).hexdigest()


def _soak_summary(report: dict | None) -> dict | None:
    if report is None:
        return None
    lineage = report.get("lineage") if isinstance(report.get("lineage"), dict) else {}
    checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
    safe_lineage_keys = (
        "active_binding_id", "cycles", "datasets", "models", "bindings", "trials",
        "readiness_reports", "promotion_decisions", "monitoring_snapshots",
        "recovery_events", "audit_log_ids", "cycle_event_ids",
    )
    return _safe({
        "schema_version": report.get("schema_version"),
        "report_type": report.get("report_type"),
        "generated_at": report.get("generated_at"),
        "soak_started_at": report.get("soak_started_at"),
        "environment": report.get("environment"),
        "interruptions": [
            {
                "name": row.get("name"),
                "status": row.get("status"),
                "reason": row.get("reason"),
                "evidence": {
                    "scope": (row.get("evidence") or {}).get("scope"),
                    "recovery_event_ids": (row.get("evidence") or {}).get("recovery_event_ids", []),
                    "audit_log_ids": (row.get("evidence") or {}).get("audit_log_ids", []),
                },
            }
            for row in report.get("interruptions", [])
            if isinstance(row, dict)
        ],
        "checks": {
            "duplicate_lineage": checks.get("duplicate_lineage"),
            "interruption_matrix": checks.get("interruption_matrix"),
            "all_cycle_stages_observed": checks.get("all_cycle_stages_observed"),
            "forward_trial_links_observed": checks.get("forward_trial_links_observed"),
            "recovery_prior_binding": checks.get("recovery_prior_binding"),
        },
        "forward_evidence": report.get("forward_evidence"),
        "readiness": report.get("readiness"),
        "lineage": {key: lineage.get(key) for key in safe_lineage_keys},
    })


def _block(blockers: list[dict], category: str, key: str, reason: str, source: Any = None) -> None:
    blockers.append(_safe({
        "category": category,
        "key": key,
        "status": "blocked",
        "reason": reason,
        "source": source,
    }))


def _validate_soak(
    blockers: list[dict],
    soak: dict | None,
    trial: StockPaperTrial,
    readiness: StockPaperPromotionReadinessReport,
) -> None:
    if not soak:
        _block(blockers, "interruption", "soak_report", "A server-generated soak report is unavailable")
        return
    if soak.get("report_type") != "bounded_autonomous_paper_soak" or soak.get("schema_version") != 1:
        _block(blockers, "interruption", "soak_contract", "The soak report contract is unsupported")
    checks = soak.get("checks") if isinstance(soak.get("checks"), dict) else {}
    matrix = checks.get("interruption_matrix") if isinstance(checks.get("interruption_matrix"), dict) else {}
    interruptions = [row for row in soak.get("interruptions", []) if isinstance(row, dict)]
    if matrix.get("complete") is not True or {row.get("name") for row in interruptions} != INTERRUPTION_NAMES:
        _block(blockers, "interruption", "controlled_matrix", "All five controlled interruptions are not complete")
    for row in interruptions:
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        if not evidence.get("recovery_event_ids") or not evidence.get("audit_log_ids"):
            _block(
                blockers, "interruption", f"{row.get('name')}_evidence",
                "Controlled interruption lacks durable recovery or audit identifiers",
            )
    duplicate = checks.get("duplicate_lineage") if isinstance(checks.get("duplicate_lineage"), dict) else {}
    if not duplicate or not all(value is True for value in duplicate.values()):
        _block(blockers, "campaign", "duplicate_lineage", "Soak duplicate-lineage checks are incomplete or failing")
    stages = checks.get("all_cycle_stages_observed") if isinstance(checks.get("all_cycle_stages_observed"), dict) else {}
    if not stages or not all(value is True for value in stages.values()):
        _block(blockers, "campaign", "cycle_stages", "The full scheduled learning and recovery path was not observed")
    forward = soak.get("forward_evidence") if isinstance(soak.get("forward_evidence"), dict) else {}
    if forward.get("complete") is not True:
        _block(blockers, "campaign", "soak_forward_evidence", forward.get("reason") or "Soak forward evidence is incomplete")
    readiness_summary = soak.get("readiness") if isinstance(soak.get("readiness"), dict) else {}
    if readiness_summary.get("ready_for_continued_paper_operation") is not True:
        _block(
            blockers, "safety", "continued_paper_readiness",
            "; ".join(str(item) for item in readiness_summary.get("blockers", []))
            or "Soak runtime readiness is blocked",
        )
    if readiness_summary.get("live_trading_ready") is not False or readiness_summary.get("profitability_claim") is not False:
        _block(blockers, "safety", "soak_safety_contract", "Soak safety declarations are invalid")
    lineage = soak.get("lineage") if isinstance(soak.get("lineage"), dict) else {}
    report_refs = lineage.get("readiness_reports") if isinstance(lineage.get("readiness_reports"), list) else []
    if not any(
        row.get("id") == readiness.id
        and row.get("trial_id") == trial.id
        and row.get("report_hash") == readiness.report_hash
        and row.get("paper_only") is True
        and row.get("live_authorized") is False
        for row in report_refs if isinstance(row, dict)
    ):
        _block(blockers, "campaign", "soak_report_link", "Soak report does not link the selected immutable readiness report")


def build_graduation_evidence(
    db: Session,
    *,
    trial: StockPaperTrial,
    readiness: StockPaperPromotionReadinessReport,
    soak_report: dict | None,
    soak_report_name: str | None,
    soak_report_hash: str | None,
) -> tuple[dict, list[dict], StockPaperAccuracyReport | None]:
    blockers: list[dict] = []
    if readiness.trial_id != trial.id:
        raise PaperGraduationError("Readiness report does not belong to this trial")
    if trial.policy.get("regular_sessions") != REQUIRED_SESSIONS:
        _block(blockers, "campaign", "frozen_policy", "Paper graduation requires the frozen twenty-session policy")
    if readiness.decision != "pass":
        _block(blockers, "campaign", "readiness_decision", "Promotion-readiness report is not passing", readiness.id)
    for key, gate in sorted((readiness.gates or {}).items()):
        if not isinstance(gate, dict) or gate.get("status") != "pass":
            _block(
                blockers, "campaign", f"readiness.{key}",
                gate.get("reason") if isinstance(gate, dict) else "Readiness gate is unavailable",
                readiness.id,
            )
    evidence = readiness.evidence if isinstance(readiness.evidence, dict) else {}
    session_evidence = evidence.get("session_evidence") if isinstance(evidence.get("session_evidence"), dict) else {}
    window = session_evidence.get("window") if isinstance(session_evidence.get("window"), dict) else {}
    sessions = session_evidence.get("sessions") if isinstance(session_evidence.get("sessions"), list) else []
    if (
        window.get("complete") is not True
        or window.get("elapsed_regular_sessions") != REQUIRED_SESSIONS
        or len(sessions) != REQUIRED_SESSIONS
    ):
        _block(blockers, "campaign", "frozen_window", "The exact twenty-session evidence window is incomplete")

    accuracy = db.scalar(select(StockPaperAccuracyReport).where(
        StockPaperAccuracyReport.trial_id == trial.id,
    ).order_by(StockPaperAccuracyReport.as_of.desc(), StockPaperAccuracyReport.id.desc()))
    if accuracy is None:
        _block(blockers, "model", "point_in_time_accuracy", "Point-in-time accuracy evidence is unavailable")
        accuracy_out = None
    else:
        accuracy_out = accuracy_report_out(accuracy)
        accuracy_evidence = accuracy.evidence or {}
        if accuracy.classification != "sufficient":
            _block(blockers, "model", "accuracy_classification", accuracy_evidence.get("reason") or "Accuracy evidence is not sufficient", accuracy.id)
        if (accuracy_evidence.get("data_health") or {}).get("status") != "verified":
            _block(blockers, "model", "data_health", "Point-in-time data health is not verified", accuracy.id)
        if (accuracy_evidence.get("drift") or {}).get("status") != "stable":
            _block(blockers, "model", "drift", "Drift evidence is not stable", accuracy.id)
        if (accuracy_evidence.get("cost_aware") or {}).get("costs_known") is not True:
            _block(blockers, "accounting", "accuracy_costs", "Cost-aware outcome evidence is incomplete", accuracy.id)

    account = active_paper_account(db)
    account_evidence = None
    if account is None:
        _block(blockers, "accounting", "paper_account", "Active paper broker account evidence is unavailable")
    else:
        account_evidence = _safe({
            "status": account.status,
            "accounting_verified": account.accounting_verified,
            "reconciliation_required": account.reconciliation_required,
            "unexplained_residual": account.unexplained_residual,
            "costs_known": account.costs_known,
            "last_reconciled_at": account.last_reconciled_at,
            "source_timestamp": account.source_timestamp,
        })
        if (
            account.status != "reconciled"
            or not account.accounting_verified
            or account.reconciliation_required
            or account.unexplained_residual
        ):
            _block(blockers, "accounting", "ledger_reconciliation", "Paper broker accounting is not fully reconciled")
        if not account.costs_known:
            _block(blockers, "accounting", "broker_costs", "Broker cost evidence remains unknown")
    uncertain_order_ids = list(db.scalars(select(StockPaperOrder.id).where(
        StockPaperOrder.uncertain_submission.is_(True),
    )).all())
    if uncertain_order_ids:
        _block(blockers, "accounting", "uncertain_orders", "Ambiguous paper order outcomes remain unresolved", uncertain_order_ids)

    recovery = db.get(StockPaperRecoveryState, 1)
    recovery_evidence = None
    if recovery is None:
        _block(blockers, "safety", "recovery_state", "Paper recovery state is unavailable")
    else:
        recovery_evidence = _safe({
            "status": recovery.status,
            "pause_reason": recovery.pause_reason,
            "last_known_good_model_run_id": recovery.last_known_good_model_run_id,
            "last_known_good_binding_id": recovery.last_known_good_binding_id,
            "last_monitor_heartbeat_at": recovery.last_monitor_heartbeat_at,
            "last_watchdog_heartbeat_at": recovery.last_watchdog_heartbeat_at,
            "last_revalidation_at": recovery.last_revalidation_at,
            "accounting_review_required": recovery.accounting_review_required,
            "accounting_reviewed_at": recovery.accounting_reviewed_at,
        })
        if recovery.status not in {"armed", "resumable"} or recovery.accounting_review_required:
            _block(blockers, "safety", "recovery_state", "Paper recovery is not clear for graduation")

    cycle = db.get(StockLearningCycle, trial.source_cycle_id) if trial.source_cycle_id else None
    monitor = db.get(StockMonitoringSnapshot, cycle.monitor_snapshot_id) if cycle and cycle.monitor_snapshot_id else db.scalar(
        select(StockMonitoringSnapshot).order_by(
            StockMonitoringSnapshot.generated_at.desc(), StockMonitoringSnapshot.id.desc()
        )
    )
    if monitor is None or monitor.status not in {"clear", "healthy"}:
        _block(blockers, "safety", "monitoring", "A clear monitoring snapshot is unavailable", monitor.id if monitor else None)
    unresolved_critical = db.scalar(select(func.count()).select_from(Notification).where(
        Notification.severity == "critical",
        Notification.status.not_in(("resolved", "closed")),
    )) or 0
    if unresolved_critical:
        _block(blockers, "safety", "critical_notifications", "Critical notifications remain unresolved", unresolved_critical)

    _validate_soak(blockers, soak_report, trial, readiness)
    cycle_events = []
    promotion_decisions = []
    if cycle:
        from app.models import StockLearningCycleEvent
        cycle_events = [
            _safe({
                "id": row.id, "stage": row.stage, "decision": row.decision,
                "reason": row.reason, "decision_sha256": row.decision_sha256,
            })
            for row in db.scalars(select(StockLearningCycleEvent).where(
                StockLearningCycleEvent.cycle_id == cycle.cycle_id,
            ).order_by(StockLearningCycleEvent.id)).all()
        ]
        promotion_decisions = [
            _safe({
                "id": row.id, "decision": row.decision, "reason": row.reason,
                "decision_sha256": row.decision_sha256,
                "before_binding_id": row.before_binding_id,
                "after_binding_id": row.after_binding_id,
                "paper_only": row.paper_only,
                "live_authorized": row.live_authorized,
            })
            for row in db.scalars(select(StockPaperPromotionDecision).where(
                StockPaperPromotionDecision.cycle_id == cycle.cycle_id,
            ).order_by(StockPaperPromotionDecision.id)).all()
        ]
    recovery_event_ids = list(db.scalars(select(StockPaperRecoveryEvent.id).order_by(
        StockPaperRecoveryEvent.id.desc()
    ).limit(100)).all())
    package_evidence = _safe({
        "schema_version": 1,
        "report_type": "paper_graduation_package",
        "trial": {
            "id": trial.id,
            "status": trial.status,
            "source_cycle_id": trial.source_cycle_id,
            "binding_id": trial.binding_id,
            "policy": trial.policy,
            "lineage": trial.lineage,
            "started_at": trial.started_at,
            "stopped_at": trial.stopped_at,
        },
        "readiness": {
            "id": readiness.id,
            "report_hash": readiness.report_hash,
            "decision": readiness.decision,
            "version": readiness.report_version,
            "as_of": readiness.as_of,
            "gates": readiness.gates,
            "lineage": readiness.lineage,
            "policy": readiness.policy,
            "evidence": readiness.evidence,
        },
        "accuracy": accuracy_out,
        "accounting": account_evidence,
        "uncertain_order_ids": uncertain_order_ids,
        "recovery": recovery_evidence,
        "recent_recovery_event_ids": recovery_event_ids,
        "monitoring": _safe({
            "id": monitor.id,
            "status": monitor.status,
            "generated_at": monitor.generated_at,
            "source": monitor.source,
        }) if monitor else None,
        "unresolved_critical_notifications": unresolved_critical,
        "learning_cycle": _safe({
            "cycle_id": cycle.cycle_id,
            "status": cycle.status,
            "stage": cycle.stage,
            "snapshot_id": cycle.snapshot_id,
            "model_run_id": cycle.model_run_id,
            "binding_id": cycle.binding_id,
            "trial_id": cycle.trial_id,
            "monitor_snapshot_id": cycle.monitor_snapshot_id,
            "recovery_event_id": cycle.recovery_event_id,
            "gates": cycle.gates,
            "events": cycle_events,
            "promotion_decisions": promotion_decisions,
        }) if cycle else None,
        "soak_report": {
            "name": soak_report_name,
            "sha256": soak_report_hash,
            "summary": _soak_summary(soak_report),
        },
        "blockers": blockers,
        "paper_only": True,
        "live_authorized": False,
        "live_orders_allowed": False,
        "profitability_claim": False,
    })
    return package_evidence, blockers, accuracy


def graduation_package_out(row: StockPaperGraduationPackage, *, include_evidence: bool = True) -> dict:
    return _safe({
        "id": row.id,
        "trial_id": row.trial_id,
        "cycle_id": row.cycle_id,
        "readiness_report_id": row.readiness_report_id,
        "accuracy_report_id": row.accuracy_report_id,
        "package_hash": row.package_hash,
        "readiness_report_hash": row.readiness_report_hash,
        "soak_report_hash": row.soak_report_hash,
        "decision": row.decision,
        "blockers": row.blockers,
        "evidence": row.evidence if include_evidence else None,
        "reviewer_actor": row.reviewer_actor,
        "reviewer_reason": row.reviewer_reason,
        "authorization": row.authorization,
        "paper_only": True,
        "live_authorized": False,
        "live_orders_allowed": False,
        "created_at": row.created_at,
    })


def create_graduation_package(
    db: Session,
    *,
    trial_id: str,
    readiness_report_id: int,
    decision: str,
    reason: str,
    actor: str,
    authorization: dict,
    soak_report_name: str | None = None,
    soak_report: dict | None = None,
    soak_report_hash: str | None = None,
) -> dict:
    normalized_reason = reason.strip()
    if len(normalized_reason) < 3:
        raise PaperGraduationError("Graduation review requires a reason")
    if decision not in {"approved", "rejected"}:
        raise PaperGraduationError("Graduation decision must be approved or rejected")
    trial = db.get(StockPaperTrial, trial_id)
    if not trial:
        raise PaperGraduationError("Trial not found")
    readiness = db.get(StockPaperPromotionReadinessReport, readiness_report_id)
    if not readiness or readiness.trial_id != trial.id:
        raise PaperGraduationError("Promotion-readiness report not found")
    if soak_report is None:
        soak_report, soak_report_name, soak_report_hash = _load_soak_report(soak_report_name)
    evidence, blockers, accuracy = build_graduation_evidence(
        db,
        trial=trial,
        readiness=readiness,
        soak_report=soak_report,
        soak_report_name=soak_report_name,
        soak_report_hash=soak_report_hash,
    )
    if decision == "approved" and blockers:
        write_audit_log(
            db,
            event_type="stock_paper_graduation",
            entity_type="stock_paper_trial",
            action="graduation_approval_denied",
            status="blocked",
            message="Paper graduation approval was denied by unresolved evidence",
            payload={
                "trial_id": trial.id,
                "readiness_report_id": readiness.id,
                "actor": actor,
                "blockers": blockers,
                "paper_only": True,
                "live_authorized": False,
            },
        )
        raise PaperGraduationBlocked("Paper graduation approval is blocked by unresolved evidence")
    canonical = {
        "evidence": evidence,
        "decision": decision,
        "reviewer_actor": actor,
        "reviewer_reason": normalized_reason,
        "authorization": _safe(authorization),
        "paper_only": True,
        "live_authorized": False,
    }
    package_hash = _hash(canonical)
    existing = db.scalar(select(StockPaperGraduationPackage).where(
        StockPaperGraduationPackage.package_hash == package_hash,
    ))
    if existing:
        return graduation_package_out(existing)
    row = StockPaperGraduationPackage(
        trial_id=trial.id,
        cycle_id=trial.source_cycle_id,
        readiness_report_id=readiness.id,
        accuracy_report_id=accuracy.id if accuracy else None,
        package_hash=package_hash,
        readiness_report_hash=readiness.report_hash,
        soak_report_hash=soak_report_hash,
        decision=decision,
        blockers=blockers,
        evidence=evidence,
        reviewer_actor=actor,
        reviewer_reason=normalized_reason,
        authorization=_safe(authorization),
        paper_only=True,
        live_authorized=False,
    )
    db.add(row)
    db.flush()
    write_audit_log(
        db,
        event_type="stock_paper_graduation",
        entity_type="stock_paper_graduation_package",
        entity_id=row.id,
        action=f"paper_graduation_{decision}",
        status=decision,
        message=f"Paper graduation was {decision}; live trading remains disabled",
        payload={
            "trial_id": trial.id,
            "readiness_report_id": readiness.id,
            "package_hash": package_hash,
            "blocker_count": len(blockers),
            "actor": actor,
            "paper_only": True,
            "live_authorized": False,
        },
    )
    return graduation_package_out(row)


def graduation_package_history(db: Session, trial_id: str) -> list[dict]:
    if not db.get(StockPaperTrial, trial_id):
        raise PaperGraduationError("Trial not found")
    rows = db.scalars(select(StockPaperGraduationPackage).where(
        StockPaperGraduationPackage.trial_id == trial_id,
    ).order_by(
        StockPaperGraduationPackage.created_at.desc(),
        StockPaperGraduationPackage.id.desc(),
    )).all()
    return [graduation_package_out(row, include_evidence=False) for row in rows]


def graduation_package(db: Session, trial_id: str, package_id: int) -> dict:
    row = db.get(StockPaperGraduationPackage, package_id)
    if not row or row.trial_id != trial_id:
        raise PaperGraduationError("Paper graduation package not found")
    return graduation_package_out(row)