"""Qualification for an observed paper-research start, not live or legacy admission."""
from datetime import datetime, timezone
import hashlib
import json

from app.models import StockPaperAccount, StockPaperResearchAuthorization, StockPaperResearchQualification
from app.services.paper_research_accounting import assess

VERSION = "paper-research-venue-v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _package(accounting, reviewer):
    body = {"version": VERSION, "scope": "closed_inventory_paper_research_start",
            "accounting": accounting, "reviewed_by": reviewer,
            "costs_verified": False, "legacy_admission_authorized": False, "live_authorized": False}
    return {**body, "report_sha256": _digest(body)}


def record_qualification(db, account, *, reviewer):
    if not reviewer.strip():
        raise ValueError("An identified research reviewer is required")
    accounting = assess(db, account)
    if accounting["accounting_observation_ready"] is not True:
        raise ValueError("Current paper accounting must pass before research qualification")
    report = _package(accounting, reviewer.strip())
    existing = db.query(StockPaperResearchQualification).filter_by(report_sha256=report["report_sha256"]).first()
    if existing:
        return existing
    row = StockPaperResearchQualification(account_id=account.id, report=report,
        report_sha256=report["report_sha256"], reviewed_by=reviewer.strip(), reviewed_at=datetime.now(timezone.utc))
    db.add(row)
    db.flush()
    return row


def _valid(db, row):
    account = db.get(StockPaperAccount, row.account_id)
    current = assess(db, account)
    try:
        snapshot = row.report["accounting"]
        stable = lambda report: {k: v for k, v in report.items() if k not in {
            "report_sha256", "observed_at", "reconciliation_event_id"}}
        snapshot_body = {k: v for k, v in snapshot.items() if k != "report_sha256"}
        return bool(row.reviewed_by.strip()
            and current["accounting_observation_ready"] is True
            and snapshot["report_sha256"] == _digest(snapshot_body)
            and stable(snapshot) == stable(current)
            and row.report == _package(snapshot, row.reviewed_by)
            and row.report_sha256 == row.report["report_sha256"])
    except (KeyError, TypeError, AttributeError):
        return False


def _authorization_body(qualification, authorizer, reason):
    return {"qualification_id": qualification.id, "report_sha256": qualification.report_sha256,
            "authorized_by": authorizer, "reason": reason, "paper_only": True, "live_authorized": False}


def authorize(db, *, qualification_id, authorizer, reason):
    row = db.get(StockPaperResearchQualification, qualification_id)
    if not row or not _valid(db, row):
        raise ValueError("An exact research qualification matching current accounting is required")
    if not authorizer.strip() or not reason.strip() or authorizer.strip() == row.reviewed_by.strip():
        raise ValueError("A distinct identified authorizer and activation reason are required")
    body = _authorization_body(row, authorizer.strip(), reason.strip())
    digest = _digest(body)
    existing = db.query(StockPaperResearchAuthorization).filter_by(authorization_sha256=digest).first()
    if existing:
        return existing
    approval = StockPaperResearchAuthorization(qualification_id=row.id, authorized_by=authorizer.strip(),
        reason=reason.strip(), authorization_sha256=digest, paper_only=True, live_authorized=False)
    db.add(approval)
    db.flush()
    return approval


def status(db, account):
    row = db.query(StockPaperResearchQualification).filter_by(account_id=account.id).order_by(
        StockPaperResearchQualification.id.desc()).first() if account else None
    qualified = bool(row and _valid(db, row))
    approval = db.query(StockPaperResearchAuthorization).filter_by(qualification_id=row.id).order_by(
        StockPaperResearchAuthorization.id.desc()).first() if row else None
    activated = bool(qualified and approval and approval.paper_only is True and approval.live_authorized is False
        and approval.authorized_by.strip() and approval.reason.strip()
        and approval.authorized_by.strip() != row.reviewed_by.strip()
        and approval.authorization_sha256 == _digest(_authorization_body(row, approval.authorized_by, approval.reason)))
    return {"version": VERSION, "scope": "closed_inventory_paper_research_start",
            "qualification_status": "current" if qualified else "invalid_or_stale" if row else "missing",
            "qualification_id": row.id if row else None, "report_sha256": row.report_sha256 if row else None,
            "qualified_for_observed_start": qualified, "activation_authorized": activated,
            "venue_ready_for_research_start": activated,
            "costs_verified": False, "legacy_admission_authorized": False, "live_authorized": False,
            "execution_authorized": False,
            "reason": "Research venue evidence and activation do not replace strategy, risk, data and recovery gates"}
