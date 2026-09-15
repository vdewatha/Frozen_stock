"""Certify external paper-account activity handling without inventing evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "external-account-activity-certification-20260915.json"
TESTS = [
    "tests/test_stock_paper_ledger.py",
    "tests/test_external_account_activity_certification.py",
]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *TESTS],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "."},
        capture_output=True,
        text=True,
    )
    passed = result.returncode == 0
    evidence = {
        "schema_version": 1,
        "report_type": "external_account_activity_certification",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "external_broker_account_contacted": False,
            "live_orders_submitted": False,
            "raw_broker_payloads_in_report": False,
            "invented_costs_or_activity": False,
            "paper_or_live_record_mixing": False,
        },
        "activity_classes": {
            "supported": [
                "FILL with immutable broker identifiers and stable broker timestamp",
                "Late commission enrichment for an existing immutable fill only",
            ],
            "review_required": [
                "DIV and DIVNRA dividends",
                "SPLIT, REORG, ROC, and SPIN corporate actions",
                "FEE, TAF, and TAX fee activity",
                "TRANS, ACATC, ACATS, CSD, CSW, DEPOSIT, and WITHDRAWAL cash movement",
                "JNL, JNLC, JNLS, MISC, NC, PTC, MA, SUB, and SSO broker adjustments",
                "Unknown or malformed provider activity",
                "Externally originated nonterminal orders and fills without strategy ownership",
            ],
        },
        "checks": [
            {"id": "historical_activity_import_is_classified", "status": "pass" if passed else "fail"},
            {"id": "unsupported_activity_blocks_accounting_and_recovery", "status": "pass" if passed else "fail"},
            {"id": "external_orders_and_fills_remain_unowned", "status": "pass" if passed else "fail"},
            {"id": "corporate_action_snapshot_preserves_immutable_history", "status": "pass" if passed else "fail"},
            {"id": "late_commission_enrichment_requires_exact_immutable_match", "status": "pass" if passed else "fail"},
            {"id": "post_pause_activity_cannot_clear_recovery", "status": "pass" if passed else "fail"},
            {"id": "repeated_activity_is_idempotent", "status": "pass" if passed else "fail"},
            {"id": "unknown_costs_withhold_pl_and_performance_fields", "status": "pass" if passed else "fail"},
            {"id": "raw_payloads_and_credentials_are_redacted_from_evidence_export", "status": "pass" if passed else "fail"},
        ],
        "recovery_boundary": {
            "resume_requires": [
                "fresh broker reconciliation",
                "resolved accounting review",
                "known applicable costs",
                "terminal in-flight orders",
                "fresh post-pause monitoring evidence",
                "authorized operator revalidation",
            ],
            "automatic_acceptance_of_external_activity": False,
        },
        "test_sources": TESTS,
        "test_result": {
            "status": "pass" if passed else "fail",
            "exit_code": result.returncode,
            "summary": next(
                (
                    line.strip()
                    for line in reversed(result.stdout.splitlines())
                    if "passed" in line or "failed" in line
                ),
                "pytest did not produce a summary",
            ),
        },
        "unresolved_blockers": [] if passed else [
            "External account activity certification tests did not pass"
        ],
        "certification_status": "pass" if passed else "blocked",
    }
    report = {**evidence, "report_sha256": _digest(evidence)}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"{report['certification_status']}: {OUTPUT.relative_to(ROOT)}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())