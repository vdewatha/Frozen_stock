"""Account-specific qualification and activation boundary for paper venues.

Qualification is evidence, not configuration.  A provider becomes eligible for
paper admission only after a redacted package proves the complete activity
contract and a separate operator authorizes activation of that exact package.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StockPaperAccount, StockPaperVenueAuthorization, StockPaperVenueQualification

QUALIFICATION_VERSION = "paper-venue-v1"
SELECTED_PAPER_PROVIDER = "alpaca_paper"

_REQUIRED_BOOLEAN_PATHS = (
    ("account_identity", "present"),
    ("account_identity", "matches_configured_binding"),
    ("orders", "complete"),
    ("executions", "complete"),
    ("commissions", "complete"),
    ("cash_activities", "complete"),
    ("timestamps", "precise_utc"),
    ("pagination", "orders_complete"),
    ("pagination", "activities_complete"),
    ("delayed_events", "captured"),
    ("restart_replay", "activities_preserved"),
    ("restart_replay", "no_duplicates"),
    ("session_expiry_replay", "activities_preserved"),
    ("session_expiry_replay", "no_duplicates"),
)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _account_digest(account_id: str) -> str:
    return hashlib.sha256(account_id.encode()).hexdigest()


def _safe_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Keep only reviewable metadata; never persist broker payloads or IDs."""
    allowed = {
        "account_identity", "orders", "executions", "commissions",
        "cash_activities", "timestamps", "pagination", "report_period",
        "delayed_events", "restart_replay", "session_expiry_replay",
        "counts", "provider_contract",
    }
    redacted_keys = {
        "account_id", "account_number", "activity_id", "broker_activity_id",
        "broker_order_id", "client_order_id", "execution_id", "id",
        "order_id", "raw_payload", "payload", "description", "net_amount",
    }

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(child): redact(child_value)
                for child, child_value in value.items()
                if str(child).lower() not in redacted_keys
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return None

    result: dict[str, Any] = {}
    for key in allowed:
        value = evidence.get(key)
        if isinstance(value, dict):
            result[key] = redact(value)
        elif isinstance(value, (str, int, float, bool, type(None), list)):
            result[key] = redact(value)
    return result


def assess_paper_venue_evidence(
    *,
    provider: str,
    account_id: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Return a redacted, deterministic package assessment.

    Missing, malformed, or unknown evidence is a blocker.  This function does
    not contact a provider and does not authorize a provider switch.
    """
    safe = _safe_evidence(evidence)
    blockers: list[str] = []
    if provider != SELECTED_PAPER_PROVIDER:
        blockers.append(f"Selected paper provider must be {SELECTED_PAPER_PROVIDER}")
    if not account_id.strip():
        blockers.append("Account identity is missing")
    for section, field in _REQUIRED_BOOLEAN_PATHS:
        section_value = safe.get(section)
        value = section_value.get(field) if isinstance(section_value, dict) else None
        if value is not True:
            blockers.append(f"{section}.{field} is not affirmatively proven")
    period = safe.get("report_period")
    if not isinstance(period, dict) or not period.get("start") or not period.get("end"):
        blockers.append("A bounded provider report period is required")
    if not isinstance(safe.get("provider_contract"), dict):
        blockers.append("Provider contract metadata is missing")

    report = {
        "qualification_version": QUALIFICATION_VERSION,
        "provider": provider,
        "account_id_sha256": _account_digest(account_id) if account_id.strip() else None,
        "evidence": safe,
        "status": "qualified" if not blockers else "blocked",
        "blockers": blockers,
        "paper_only": True,
        "live_authorized": False,
    }
    report["report_sha256"] = _digest(report)
    return report


def record_paper_venue_qualification(
    db: Session,
    *,
    provider: str,
    account_id: str,
    evidence: dict[str, Any],
    reviewer: str,
) -> StockPaperVenueQualification:
    if not reviewer.strip():
        raise ValueError("A qualification reviewer is required")
    report = assess_paper_venue_evidence(
        provider=provider, account_id=account_id, evidence=evidence
    )
    row = StockPaperVenueQualification(
        provider=provider,
        account_id_sha256=report["account_id_sha256"] or "",
        status=report["status"],
        qualification_version=QUALIFICATION_VERSION,
        report=report,
        report_sha256=report["report_sha256"],
        reviewed_by=reviewer.strip(),
        reviewed_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def _qualification_is_current(db: Session, qualification: StockPaperVenueQualification, provider: str) -> bool:
    account = db.scalar(select(StockPaperAccount).where(
        StockPaperAccount.broker == provider, StockPaperAccount.archived_at.is_(None)
    ))
    if (not account or qualification.provider != provider
            or qualification.status != "qualified" or not qualification.reviewed_by.strip()
            or qualification.qualification_version != QUALIFICATION_VERSION
            or not isinstance(qualification.report, dict)
            or not isinstance(qualification.report.get("evidence"), dict)):
        return False
    # Reassess the package, not just its mutable status flag, against today's account.
    expected = assess_paper_venue_evidence(provider=provider, account_id=account.broker_account_id,
                                          evidence=qualification.report["evidence"])
    return (expected["status"] == "qualified" and qualification.report == expected
            and qualification.account_id_sha256 == expected["account_id_sha256"]
            and qualification.report_sha256 == expected["report_sha256"])


def _authorization_payload(qualification, provider, authorizer, reason):
    return {
        "provider": provider, "qualification_id": qualification.id,
        "qualification_report_sha256": qualification.report_sha256,
        "authorizer": authorizer, "reason": reason,
        "paper_only": True, "live_authorized": False,
    }


def authorize_paper_venue_activation(
    db: Session,
    *,
    qualification_id: int,
    provider: str,
    authorizer: str,
    reason: str,
) -> StockPaperVenueAuthorization:
    qualification = db.get(StockPaperVenueQualification, qualification_id)
    if not qualification or qualification.provider != provider:
        raise ValueError("The selected paper venue qualification was not found")
    if qualification.status != "qualified":
        raise ValueError("Only a passing qualification package can be activated")
    if not _qualification_is_current(db, qualification, provider):
        raise ValueError("Qualification evidence must match the current initialized paper account")
    if not authorizer.strip():
        raise ValueError("A non-empty activation authorizer is required")
    if not reason.strip():
        raise ValueError("A separate activation reason is required")
    if authorizer.strip() == qualification.reviewed_by:
        raise ValueError("Venue activation requires a separate authorizer")
    payload = _authorization_payload(qualification, provider, authorizer.strip(), reason.strip())
    row = StockPaperVenueAuthorization(
        provider=provider,
        qualification_id=qualification.id,
        authorized_by=authorizer.strip(),
        reason=reason.strip(),
        authorization_sha256=_digest(payload),
        paper_only=True,
        live_authorized=False,
    )
    db.add(row)
    db.flush()
    return row


def paper_venue_qualification_status(db: Session, provider: str) -> dict[str, Any]:
    qualification = db.scalar(
        select(StockPaperVenueQualification)
        .where(StockPaperVenueQualification.provider == provider)
        .order_by(StockPaperVenueQualification.reviewed_at.desc(), StockPaperVenueQualification.id.desc())
    )
    authorization = None
    if qualification:
        authorization = db.scalar(
            select(StockPaperVenueAuthorization)
            .where(
                StockPaperVenueAuthorization.provider == provider,
                StockPaperVenueAuthorization.qualification_id == qualification.id,
            )
            .order_by(StockPaperVenueAuthorization.created_at.desc(), StockPaperVenueAuthorization.id.desc())
        )
    qualified = bool(qualification and _qualification_is_current(db, qualification, provider))
    activated = bool(qualified and authorization
        and authorization.paper_only is True and authorization.live_authorized is False
        and authorization.authorized_by.strip() and authorization.reason.strip()
        and authorization.authorized_by.strip() != qualification.reviewed_by.strip()
        and authorization.authorization_sha256 == _digest(_authorization_payload(
            qualification, provider, authorization.authorized_by, authorization.reason)))
    return {
        "provider": provider,
        "selected_provider": provider == SELECTED_PAPER_PROVIDER,
        "qualification_status": (
            "invalid" if qualification and qualification.status == "qualified" and not qualified
            else qualification.status if qualification else "missing"
        ),
        "evidence_verified_for_current_account": qualified,
        "qualified": qualified,
        "activation_authorized": activated,
        "ready_for_paper_admission": activated,
        "qualification_id": qualification.id if qualification else None,
        "report_sha256": qualification.report_sha256 if qualification else None,
        "reviewed_by": qualification.reviewed_by if qualification else None,
        "authorized_by": authorization.authorized_by if authorization else None,
        "reason": (
            "Account-specific paper venue qualification and separate activation authorization passed"
            if activated else
            "Stored qualification or activation evidence failed current-account validation"
            if qualification and qualification.status == "qualified" and (not qualified or authorization is not None) else
            "A passing account-specific qualification and separate activation authorization are required"
        ),
    }
