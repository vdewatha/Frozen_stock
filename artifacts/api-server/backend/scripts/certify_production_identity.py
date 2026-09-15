"""Exercise and publish bounded, redacted production identity certification evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "production-identity-certification-20260915.json"
TESTS = [
    "tests/test_production_security.py",
    "tests/test_secondary_approval_identity.py",
    "tests/test_live_pilot.py::test_activation_requires_distinct_approval_actors_and_launch_evidence",
]


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *TESTS],
        cwd=ROOT,
        env={**__import__("os").environ, "PYTHONPATH": "."},
        capture_output=True,
        text=True,
    )
    passed = result.returncode == 0
    checks = [
        {"id": "server_side_role_mapping", "status": "pass" if passed else "fail"},
        {"id": "forged_role_rejection", "status": "pass" if passed else "fail"},
        {"id": "expiration_and_lifetime", "status": "pass" if passed else "fail"},
        {"id": "signing_key_rotation", "status": "pass" if passed else "fail"},
        {"id": "global_revocation_cutoff", "status": "pass" if passed else "fail"},
        {"id": "mutation_replay_rejection", "status": "pass" if passed else "fail"},
        {"id": "local_role_key_production_rejection", "status": "pass" if passed else "fail"},
        {"id": "issuer_and_audience_binding", "status": "pass" if passed else "fail"},
        {"id": "distinct_secondary_signed_approver", "status": "pass" if passed else "fail"},
        {"id": "paper_live_credential_separation", "status": "pass" if passed else "fail"},
        {"id": "paper_live_account_binding_separation", "status": "pass" if passed else "fail"},
        {"id": "production_startup_fail_closed", "status": "pass" if passed else "fail"},
    ]
    evidence = {
        "schema_version": 1,
        "report_type": "production_identity_boundary_certification",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "live_orders_enabled": False,
            "credentials_in_report": False,
            "account_identifiers_in_report": False,
            "authorization_decisions_server_side": True,
        },
        "principal_inventory": [
            {"type": "viewer", "maximum_role": "viewer"},
            {"type": "researcher", "maximum_role": "researcher"},
            {"type": "operator", "maximum_role": "operator"},
            {"type": "reviewer", "maximum_role": "admin"},
            {"type": "service", "maximum_role": "researcher"},
            {"type": "worker", "maximum_role": "researcher"},
            {"type": "scheduler", "maximum_role": "researcher"},
            {"type": "emergency", "maximum_role": "operator"},
        ],
        "configuration_contract": {
            "authentication": "signed_identity_assertion",
            "role_source": "server_side_subject_mapping",
            "required_claims": ["sub", "iat", "exp", "iss", "aud"],
            "maximum_token_lifetime_seconds": 900,
            "step_up": ["explicit_confirmation", "reason", "idempotency_key"],
            "dual_approval": "two_distinct_server_mapped_signed_identities",
            "paper_live_credentials": "separate_explicit_pairs",
            "paper_live_accounts": "separate_explicit_bindings",
            "local_role_keys_in_production": "rejected",
        },
        "checks": checks,
        "test_sources": TESTS,
        "test_result": {
            "status": "pass" if passed else "fail",
            "exit_code": result.returncode,
            "summary": next(
                (line.strip() for line in reversed(result.stdout.splitlines()) if "passed" in line or "failed" in line),
                "pytest did not produce a summary",
            ),
        },
        "unresolved_blockers": [] if passed else ["The controlled identity certification suite did not pass"],
        "certification_status": "pass" if passed else "blocked",
    }
    report = {**evidence, "report_sha256": digest(evidence)}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"{report['certification_status']}: {OUTPUT.relative_to(ROOT)}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())