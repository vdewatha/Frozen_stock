"""Read current paper-learning evidence without approving, starting, or changing a run."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

import httpx

from check_deployment_monitor import _load_json

INFRASTRUCTURE_CHECKS = (
    "Database", "Redis", "Scheduled job definitions", "Celery workers", "Celery beat scheduler",
)


def summarize(runtime: dict, preflight: dict, schedule: dict, cycles: list[dict], training_jobs: list[dict] | None = None) -> dict:
    checks = {item["name"]: item for item in runtime.get("checks", [])}
    infrastructure = {
        name: checks.get(name, {}).get("status") == "ready"
        for name in INFRASTRUCTURE_CHECKS
    }
    safety = checks.get("Live trading safety", {}).get("status") == "ready"
    gates = preflight.get("gates") or {}
    blocked_gates = {
        name: {"status": gate.get("status", "unknown"), "reason": gate.get("reason")}
        for name, gate in gates.items() if gate.get("status") != "pass"
    }
    blockers = [f"Infrastructure unavailable: {name}" for name, ready in infrastructure.items() if not ready]
    if not safety or runtime.get("live_trading_allowed") is not False:
        blockers.append("Live-trading-disabled invariant is not proven")
    if schedule.get("paused") is not False:
        blockers.append("Scheduled learning is paused or its state is unknown")
    if preflight.get("status") != "ready" or not gates or blocked_gates:
        blockers.append("Launch prerequisites are not all passing")
    if not preflight.get("cycle_id"):
        blockers.append("No exact cycle was selected for launch evidence")
    if preflight.get("eligible_for_approval") is not True:
        blockers.append("This snapshot does not establish eligibility for a new paper approval")
    observed = []
    for cycle in cycles:
        handoff = cycle.get("handoff") or {}
        promotion = cycle.get("automatic_promotion") or {}
        observed.append({
            "cycle_id": cycle.get("cycle_id"), "symbols": cycle.get("symbols"),
            "stage": cycle.get("stage"), "status": cycle.get("status"),
            "model_run_id": cycle.get("model_run_id"), "trial_id": cycle.get("trial_id"),
            "trial_status": handoff.get("trial_status"),
            "approval_status": (handoff.get("approval") or {}).get("status", "missing"),
            "promotion_decision": promotion.get("decision"),
            "promotion_decision_id": promotion.get("id"),
            "last_reason": cycle.get("last_reason"),
        })
    return {
        "schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "read_only_current_api_projection_not_independent_artifact_certification",
        "infrastructure_ready": all(infrastructure.values()),
        "infrastructure_checks": infrastructure,
        "paper_only_invariant_proven": safety and runtime.get("live_trading_allowed") is False,
        "scheduled_learning_paused": schedule.get("paused"),
        "launch": {
            "cycle_id": preflight.get("cycle_id"), "symbols": preflight.get("symbols"),
            "provider": preflight.get("provider"), "checked_at": preflight.get("checked_at"),
            "status": preflight.get("status", "unknown"),
            "eligible_for_new_approval": not blockers,
            "blocked_gates": blocked_gates,
            "reason": preflight.get("reason"),
        },
        "observed_cycles": observed,
        "observed_training_jobs": [
            {key: row.get(key) for key in (
                "job_id", "status", "snapshot_id", "result_run_id", "report_available",
                "failure_code", "created_at", "completed_at",
            )}
            for row in (training_jobs or [])
        ],
        "cycle_list_limit": 100,
        "new_approval_blockers": blockers,
        "profitability_proven": False,
        "live_trading_authorized": False,
    }


def collect(api_url: str, timeout: float, cycle_id: str | None = None) -> dict:
    if cycle_id is not None and not re.fullmatch(r"[0-9a-f]{64}", cycle_id):
        raise ValueError("cycle-id must be a 64-character lowercase hex identifier")
    runtime = _load_json(api_url, "/api/system/deployment-monitor", timeout)
    schedule = _load_json(api_url, "/api/stock/learning-cycles/schedule-control", timeout)
    suffix = f"?cycle_id={cycle_id}" if cycle_id else ""
    preflight = _load_json(api_url, "/api/stock/learning-cycles/launch-prerequisites" + suffix, timeout)
    cycles = _load_json(api_url, "/api/stock/learning-cycles?limit=100", timeout)
    training = _load_json(api_url, "/api/stock/training/jobs?limit=100", timeout)
    if not all(isinstance(item, dict) for item in (runtime, schedule, preflight)):
        raise ValueError("Runtime, schedule, and preflight must be JSON objects")
    if not isinstance(cycles, list) or not all(isinstance(item, dict) for item in cycles):
        raise ValueError("Cycle list is unavailable")
    if not isinstance(training, dict) or not isinstance(training.get("items"), list) or not all(isinstance(item, dict) for item in training["items"]):
        raise ValueError("Training-job list is unavailable")
    if cycle_id and preflight.get("cycle_id") != cycle_id:
        raise ValueError("Preflight does not match the requested cycle")
    return summarize(runtime, preflight, schedule, cycles, training["items"])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--cycle-id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = collect(args.api_url, args.timeout, args.cycle_id)
        code = 0 if report["launch"]["eligible_for_new_approval"] else 1
    except (OSError, httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
        report = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "unavailable", "error_type": type(exc).__name__,
            "profitability_proven": False, "live_trading_authorized": False,
        }
        code = 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
