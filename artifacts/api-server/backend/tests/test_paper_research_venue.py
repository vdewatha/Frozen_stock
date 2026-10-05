from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.services.paper_research_venue import authorize, record_qualification, status
from tests.test_paper_research_accounting import ledger, reviewed_ledger, ready


def test_qualification_and_activation_are_separate_and_never_execution_permission(ready):
    db, account = ready
    assert status(db, account)["qualification_status"] == "missing"
    row = record_qualification(db, account, reviewer="fixture-reviewer")
    assert record_qualification(db, account, reviewer="fixture-reviewer").id == row.id
    report = status(db, account)
    assert report["qualified_for_observed_start"] and not report["activation_authorized"]
    approval = authorize(db, qualification_id=row.id, authorizer="fixture-operator", reason="Fixture only")
    assert authorize(db, qualification_id=row.id, authorizer="fixture-operator", reason="Fixture only").id == approval.id
    report = status(db, account)
    assert report["venue_ready_for_research_start"]
    for key in ("costs_verified", "legacy_admission_authorized", "live_authorized", "execution_authorized"):
        assert report[key] is False
    assert account.status == "halted" and not account.costs_known and not account.accounting_verified


@pytest.mark.parametrize("case", ["stale", "live", "report", "hash", "reviewer", "approval_reason", "approval_identity", "approval_hash"])
def test_changed_evidence_or_authorization_fails_closed(ready, monkeypatch, case):
    db, account = ready
    row = record_qualification(db, account, reviewer="fixture-reviewer")
    approval = authorize(db, qualification_id=row.id, authorizer="fixture-operator", reason="Fixture only")
    if case == "stale":
        account.last_reconciled_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    elif case == "live":
        monkeypatch.setattr(settings, "allow_live_trading", True)
    elif case == "report":
        row.report = {**row.report, "costs_verified": True}
    elif case == "hash":
        row.report_sha256 = "changed"
    elif case == "reviewer":
        row.reviewed_by = "changed"
    elif case == "approval_reason":
        approval.reason = "changed"
    elif case == "approval_identity":
        approval.authorized_by = row.reviewed_by
    elif case == "approval_hash":
        approval.authorization_sha256 = "changed"
    db.flush()
    assert not status(db, account)["activation_authorized"]


@pytest.mark.parametrize("authorizer,reason", [("", "reason"), ("fixture-reviewer", "reason"), ("operator", " ")])
def test_no_empty_or_self_approval(ready, authorizer, reason):
    db, account = ready
    row = record_qualification(db, account, reviewer="fixture-reviewer")
    with pytest.raises(ValueError):
        authorize(db, qualification_id=row.id, authorizer=authorizer, reason=reason)


def test_no_fallback_to_older_authorized_package(ready):
    db, account = ready
    row = record_qualification(db, account, reviewer="fixture-reviewer")
    authorize(db, qualification_id=row.id, authorizer="fixture-operator", reason="Fixture only")
    record_qualification(db, account, reviewer="new-fixture-reviewer")
    assert not status(db, account)["activation_authorized"]


@pytest.mark.parametrize("field,value", [("paper_only", False), ("live_authorized", True)])
def test_database_rejects_nonpaper_authorizations(ready, field, value):
    db, account = ready
    row = record_qualification(db, account, reviewer="fixture-reviewer")
    approval = authorize(db, qualification_id=row.id, authorizer="fixture-operator", reason="Fixture only")
    setattr(approval, field, value)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_blocked_evidence_cannot_be_qualified(ready):
    db, account = ready
    account.reconciliation_required = True
    with pytest.raises(ValueError):
        record_qualification(db, account, reviewer="fixture-reviewer")
