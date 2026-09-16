"""Publish the blocked-by-default live compliance readiness record."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys

from app.services.compliance_readiness import (
    REQUIRED_EXTERNAL_REVIEWERS,
    REQUIRED_RULE_REVIEWS,
    REQUIRED_SCOPE_EXCLUSIONS,
    default_compliance_readiness,
    evaluate_compliance_readiness,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "live-compliance-readiness-20260915.json"
TESTS = ["tests/test_compliance_readiness.py", "tests/test_live_pilot.py"]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def main() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *TESTS],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "."},
        capture_output=True,
        text=True,
    )
    checks = evaluate_compliance_readiness(default_compliance_readiness())
    passed = result.returncode == 0
    evidence = {
        "schema_version": 1,
        "report_type": "live_compliance_readiness",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "launch_disposition": "blocked",
        "scope": {
            "live_orders_enabled": False,
            "legal_advice_provided": False,
            "tax_advice_provided": False,
            "regulatory_approval_claimed": False,
            "credentials_in_report": False,
            "account_identifiers_in_report": False,
        },
        "intended_scope": {
            "account_owner": "unresolved; must be supplied by the account owner",
            "jurisdictions": "unresolved; qualified compliance review required",
            "broker_account_type": "unresolved; broker confirmation required",
            "permissions": "unresolved; broker account-permission confirmation required",
            "prohibited_activities": [
                "unsupported instruments",
                "leverage",
                "shorts",
                "options",
                "unrestricted automation",
            ],
        },
        "required_review_domains": [
            "broker agreements and account permissions",
            "pattern-day-trading and margin restrictions",
            "market-data terms",
            "record retention",
            "tax and accounting treatment",
            "jurisdictional and disclosure review",
        ],
        "required_external_signoffs": list(REQUIRED_EXTERNAL_REVIEWERS),
        "required_rule_reviews": list(REQUIRED_RULE_REVIEWS),
        "required_scope_exclusions": list(REQUIRED_SCOPE_EXCLUSIONS),
        "default_record_evaluation": checks,
        "responsibility_contract": {
            "approval": "named organizational owner required",
            "monitoring": "named organizational owner required",
            "incident": "named organizational owner required",
            "shutdown": "named organizational owner required",
            "investigation": "named organizational owner required",
            "periodic_review": "named organizational owner required",
        },
        "accounting_contract": {
            "authoritative_records": "broker statements and tax documents, as confirmed by qualified reviewers",
            "supporting_records": "append-only application audit and safety evidence",
        },
        "test_sources": TESTS,
        "test_result": {
            "status": "pass" if passed else "fail",
            "exit_code": result.returncode,
            "summary": next(
                (line.strip() for line in reversed(result.stdout.splitlines()) if "passed" in line or "failed" in line),
                "pytest did not produce a summary",
            ),
        },
        "unresolved_launch_blockers": [
            "Account owner, jurisdictions, broker account type, and permissions are not externally confirmed.",
            "Broker, legal/compliance, and tax reviews are not signed off.",
            "Named owners for approval, monitoring, incidents, shutdown, investigation, and periodic review are not recorded.",
            "Automation, model uncertainty, loss, emergency-control, and jurisdiction-specific disclosures require qualified review.",
        ],
        "certification_status": "blocked" if passed else "blocked_test_failure",
    }
    report = {
        **evidence,
        "report_sha256": _digest(evidence),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"{report['certification_status']}: {OUTPUT.relative_to(ROOT)}")
    if not passed:
        print(result.stdout[-4000:], file=sys.stderr)
        print(result.stderr[-4000:], file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())