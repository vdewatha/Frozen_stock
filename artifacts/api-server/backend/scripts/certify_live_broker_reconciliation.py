"""Run provider-shaped live reconciliation certification without submitting orders."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "live-broker-reconciliation-certification-20260915.json"
TESTS = [
    "tests/test_live_broker.py",
    "tests/test_live_broker_certification.py",
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
        "report_type": "live_broker_reconciliation_certification",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "live_orders_submitted": False,
            "external_broker_account_contacted": False,
            "credentials_in_report": False,
            "raw_broker_payloads_in_report": False,
            "paper_ledger_reused": False,
        },
        "checks": [
            {"id": "isolated_live_endpoint_and_credentials", "status": "pass" if passed else "fail"},
            {"id": "read_only_account_permission_validation", "status": "pass" if passed else "fail"},
            {"id": "read_only_market_data_entitlement", "status": "pass" if passed else "fail"},
            {"id": "complete_order_pagination", "status": "pass" if passed else "fail"},
            {"id": "complete_activity_pagination", "status": "pass" if passed else "fail"},
            {"id": "ambiguous_cursor_fail_closed", "status": "pass" if passed else "fail"},
            {"id": "durable_intent_and_idempotency", "status": "pass" if passed else "fail"},
            {"id": "partial_terminal_and_unknown_lifecycle_states", "status": "pass" if passed else "fail"},
            {"id": "timeout_after_submit_blocks_retry", "status": "pass" if passed else "fail"},
            {"id": "fee_enrichment_and_unsupported_activity_handling", "status": "pass" if passed else "fail"},
            {"id": "external_order_and_activity_isolation", "status": "pass" if passed else "fail"},
        ],
        "provider_prerequisites": [
            "Explicit authorization is still required before a real live-account read-only probe.",
            "A real live-account probe must verify the configured account binding and broker permissions.",
            "No certification scenario submits, cancels, or replaces a live order.",
        ],
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
            "The provider-shaped reconciliation certification suite did not pass"
        ],
        "certification_status": "pass_with_prerequisites" if passed else "blocked",
    }
    report = {**evidence, "report_sha256": _digest(evidence)}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"{report['certification_status']}: {OUTPUT.relative_to(ROOT)}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())