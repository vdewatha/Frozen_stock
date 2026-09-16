from app.services.compliance_readiness import (
    REQUIRED_EXTERNAL_REVIEWERS,
    REQUIRED_RULE_REVIEWS,
    default_compliance_readiness,
    evaluate_compliance_readiness,
)


def _complete_record() -> dict:
    return {
        "launch_disposition": "approved",
        "account_owner": "organization owner",
        "jurisdictions": ["to be confirmed by compliance reviewer"],
        "broker_account_type": "cash",
        "broker_permissions": ["read-only verification pending pilot signoff"],
        "prohibited_activities": ["unsupported instruments", "leverage", "shorts", "options", "unrestricted automation"],
        "rule_reviews": {name: "reviewed" for name in REQUIRED_RULE_REVIEWS},
        "tax_accounting_records": {
            "authoritative_broker_statements": "broker statements",
            "supporting_app_audit_evidence": "append-only audit chain",
        },
        "retention_policy": "retain broker statements and audit evidence under approved policy",
        "user_disclosures": "automation, uncertainty, losses, and emergency controls disclosed",
        "operator_disclosures": "operator stop, review, and escalation duties disclosed",
        "responsibility_owners": {
            "approval": "named approval owner",
            "monitoring": "named monitoring owner",
            "incident": "named incident owner",
            "shutdown": "named shutdown owner",
            "investigation": "named investigation owner",
            "periodic_review": "named review owner",
        },
        "external_reviews": {name: "signed_off" for name in REQUIRED_EXTERNAL_REVIEWERS},
        "initial_live_scope": {
            "unsupported_instruments": True,
            "leverage": True,
            "shorts": True,
            "options": True,
            "unrestricted_automation": True,
        },
    }


def test_default_compliance_record_is_explicitly_blocked():
    result = evaluate_compliance_readiness(default_compliance_readiness())
    assert result["status"] == "blocked"
    assert result["launch_disposition"] == "blocked"
    assert result["fail_closed"] is True
    assert "account_owner" in result["unresolved_items"]
    assert "external_reviews.legal" in result["unresolved_items"]


def test_compliance_requires_all_external_reviews_and_scope_exclusions():
    record = _complete_record()
    result = evaluate_compliance_readiness(record)
    assert result["status"] == "pass"
    assert result["signed_external_review_count"] == 4
    record["external_reviews"]["tax"] = "required"
    result = evaluate_compliance_readiness(record)
    assert result["status"] == "blocked"
    assert "external_reviews.tax" in result["unresolved_items"]


def test_compliance_record_does_not_trust_client_supplied_pass_status():
    record = _complete_record()
    record["status"] = "pass"
    record["initial_live_scope"]["options"] = False
    result = evaluate_compliance_readiness(record)
    assert result["status"] == "blocked"
    assert "initial_live_scope.options" in result["unresolved_items"]