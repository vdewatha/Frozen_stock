"""Fail-closed, non-code launch compliance readiness contract.

This is a checklist evaluator, not legal, tax, broker, or investment advice.
It intentionally requires external signoff before a live-pilot launch can pass.
The returned evidence is limited to statuses and safe counts; callers must not
put credentials, account numbers, or sensitive personal data in the checklist.
"""
from __future__ import annotations

from typing import Any


REQUIRED_DOCUMENTED_FIELDS = (
    "account_owner",
    "jurisdictions",
    "broker_account_type",
    "broker_permissions",
    "tax_accounting_records",
    "retention_policy",
    "user_disclosures",
    "operator_disclosures",
)
REQUIRED_RULE_REVIEWS = (
    "broker_agreement",
    "account_permissions",
    "pattern_day_trading",
    "margin_restrictions",
    "market_data_terms",
    "record_retention",
)
REQUIRED_RESPONSIBILITY_OWNERS = (
    "approval",
    "monitoring",
    "incident",
    "shutdown",
    "investigation",
    "periodic_review",
)
REQUIRED_EXTERNAL_REVIEWERS = ("legal", "tax", "compliance", "broker")
REQUIRED_SCOPE_EXCLUSIONS = (
    "unsupported_instruments",
    "leverage",
    "shorts",
    "options",
    "unrestricted_automation",
)


def default_compliance_readiness() -> dict:
    """Return a safe blocked template for a new or incomplete launch record."""
    return {
        "status": "blocked",
        "launch_disposition": "blocked",
        "account_owner": None,
        "jurisdictions": [],
        "broker_account_type": None,
        "broker_permissions": [],
        "prohibited_activities": [
            "unsupported instruments",
            "leverage",
            "shorts",
            "options",
            "unrestricted automation",
        ],
        "rule_reviews": {name: "unreviewed" for name in REQUIRED_RULE_REVIEWS},
        "tax_accounting_records": {
            "authoritative_broker_statements": None,
            "supporting_app_audit_evidence": None,
        },
        "retention_policy": None,
        "user_disclosures": None,
        "operator_disclosures": None,
        "responsibility_owners": {name: None for name in REQUIRED_RESPONSIBILITY_OWNERS},
        "external_reviews": {name: "required" for name in REQUIRED_EXTERNAL_REVIEWERS},
        "initial_live_scope": {
            name: False
            for name in REQUIRED_SCOPE_EXCLUSIONS
        },
    }


def _nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return value is not None


def evaluate_compliance_readiness(record: Any) -> dict:
    """Evaluate a caller-supplied compliance record without trusting its status."""
    value = record if isinstance(record, dict) else {}
    missing: list[str] = []
    for field in REQUIRED_DOCUMENTED_FIELDS:
        if not _nonempty(value.get(field)):
            missing.append(field)

    rule_reviews = value.get("rule_reviews")
    unreviewed_rules = [
        name for name in REQUIRED_RULE_REVIEWS
        if not isinstance(rule_reviews, dict) or rule_reviews.get(name) != "reviewed"
    ]
    owner_values = value.get("responsibility_owners")
    missing_owners = [
        name for name in REQUIRED_RESPONSIBILITY_OWNERS
        if not isinstance(owner_values, dict) or not _nonempty(owner_values.get(name))
    ]
    external_reviews = value.get("external_reviews")
    missing_external_signoffs = [
        name for name in REQUIRED_EXTERNAL_REVIEWERS
        if not isinstance(external_reviews, dict) or external_reviews.get(name) != "signed_off"
    ]
    scope = value.get("initial_live_scope")
    missing_exclusions = [
        name for name in REQUIRED_SCOPE_EXCLUSIONS
        if not isinstance(scope, dict) or scope.get(name) is not True
    ]
    accounting = value.get("tax_accounting_records")
    if not isinstance(accounting, dict) or not _nonempty(accounting.get("authoritative_broker_statements")):
        missing.append("tax_accounting_records.authoritative_broker_statements")
    if not isinstance(accounting, dict) or not _nonempty(accounting.get("supporting_app_audit_evidence")):
        missing.append("tax_accounting_records.supporting_app_audit_evidence")
    if not _nonempty(value.get("prohibited_activities")):
        missing.append("prohibited_activities")

    unresolved = [
        *missing,
        *(f"rule_reviews.{name}" for name in unreviewed_rules),
        *(f"responsibility_owners.{name}" for name in missing_owners),
        *(f"external_reviews.{name}" for name in missing_external_signoffs),
        *(f"initial_live_scope.{name}" for name in missing_exclusions),
    ]
    disposition = value.get("launch_disposition")
    passed = not unresolved and disposition == "approved"
    return {
        "status": "pass" if passed else "blocked",
        "launch_disposition": "approved" if passed else "blocked",
        "reason": (
            "All compliance items are documented and required external reviews are signed off."
            if passed
            else "Live pilot is blocked until every compliance item is documented and externally signed off."
        ),
        "unresolved_items": unresolved,
        "reviewed_rule_count": len(REQUIRED_RULE_REVIEWS) - len(unreviewed_rules),
        "required_rule_count": len(REQUIRED_RULE_REVIEWS),
        "signed_external_review_count": len(REQUIRED_EXTERNAL_REVIEWERS) - len(missing_external_signoffs),
        "required_external_review_count": len(REQUIRED_EXTERNAL_REVIEWERS),
        "documented_owner_count": len(REQUIRED_RESPONSIBILITY_OWNERS) - len(missing_owners),
        "required_owner_count": len(REQUIRED_RESPONSIBILITY_OWNERS),
        "scope_exclusions_confirmed": len(REQUIRED_SCOPE_EXCLUSIONS) - len(missing_exclusions),
        "required_scope_exclusions": len(REQUIRED_SCOPE_EXCLUSIONS),
        "fail_closed": True,
    }