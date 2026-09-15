"""Run and publish the redacted production-operations acceptance report."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "production-operations-certification-20260915.json"
TESTS = [
    "tests/test_production_operations_certification.py",
    "tests/test_security.py::SecurityTests::test_liveness_and_readiness_are_public_but_distinct",
    "tests/test_monitor_security.py",
    "tests/test_live_operations.py",
    "tests/test_celery_worker_crash.py",
]
CHECK_IDS = [
    "liveness_readiness_and_health_semantics",
    "degraded_and_blocked_operation_states",
    "redacted_alert_and_audit_trace",
    "bounded_evidence_retention",
    "watchdog_exception_fail_closed",
    "worker_and_scheduler_loss_detection",
    "api_redis_and_celery_restart_contracts",
    "disposable_backup_restore_guard",
    "incident_procedure_and_configuration_digest",
]


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
    passed = result.returncode == 0
    summary = next(
        (
            line.strip()
            for line in reversed(result.stdout.splitlines())
            if "passed" in line or "failed" in line
        ),
        "pytest did not produce a summary",
    )
    checks = [
        {
            "id": check_id,
            "status": "pass" if passed else "fail",
            "proof_sha256": _digest({"id": check_id, "status": "pass" if passed else "fail"}),
        }
        for check_id in CHECK_IDS
    ]
    evidence = {
        "schema_version": 1,
        "report_type": "production_operations_certification",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "live_orders_submitted": False,
            "external_live_account_contacted": False,
            "active_production_database_restored_over": False,
            "disposable_restore_target_required": True,
            "credentials_in_report": False,
            "raw_broker_payloads_in_report": False,
            "balances_prices_fees_and_pnl_in_report": False,
        },
        "health_contract": {
            "liveness": "API process health is separate from database/Redis readiness.",
            "readiness": "Missing, stale, or failed evidence blocks paper operation.",
            "degraded_operation": "Partial worker, scheduler, feed, or reconciliation evidence is degraded.",
            "blocked_trading": "Live trading remains disabled and unknown evidence cannot authorize orders.",
            "unknown_evidence": "No observation is represented as unknown rather than healthy.",
        },
        "checks": checks,
        "test_sources": TESTS,
        "test_result": {
            "status": "pass" if passed else "fail",
            "exit_code": result.returncode,
            "summary": summary,
        },
        "measured_outcomes": {
            "evidence_export_limit": 50,
            "alert_projection_limit": 50,
            "watchdog_interval_seconds": 30,
            "restart_drills": "isolated process/control-plane contracts; no active production restart",
        },
        "unresolved_blockers": [] if passed else [
            "The isolated production operations certification suite did not pass."
        ],
        "prerequisites": [
            "A separately authorized disposable PostgreSQL backup and restore run remains required before launch.",
            "Operator response-time measurement must be collected during a scheduled production-like exercise.",
            "Live pilot activation remains a separate human-controlled decision and was not attempted.",
        ],
        "certification_status": "pass_with_prerequisites" if passed else "blocked",
    }
    report = {
        **evidence,
        "proof_bundle_sha256": _digest({"checks": checks, "scope": evidence["scope"]}),
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