#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_snapshot(api_url: str, timeout: float) -> dict[str, Any]:
    parsed = urlsplit(api_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("API URL must be a server origin without credentials")
    if not parsed.hostname or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1", "backend"})):
        raise ValueError("Remote monitor connections require HTTPS")
    token = os.environ.get("AUTH_VIEWER_KEY", "")
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("A valid AUTH_VIEWER_KEY is required")
    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("Timeout must be between zero and 30 seconds")
    url = api_url.rstrip("/") + "/system/deployment-monitor"
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        response = client.get(url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Deployment monitor response was not a JSON object.")
    return data


def _load_snapshot_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Snapshot file did not contain a JSON object.")
    return data


def _summarize(snapshot: dict[str, Any]) -> dict[str, Any]:
    blockers = snapshot.get("blockers") if isinstance(snapshot.get("blockers"), list) else []
    warnings = snapshot.get("warnings") if isinstance(snapshot.get("warnings"), list) else []
    readiness_blockers = snapshot.get("readiness_blockers") if isinstance(snapshot.get("readiness_blockers"), list) else []
    checks = snapshot.get("checks") if isinstance(snapshot.get("checks"), list) else []
    failed_checks = [
        str(check.get("name", "unknown"))
        for check in checks
        if isinstance(check, dict) and check.get("status") == "blocked"
    ]
    return {
        "checked_at": _now_iso(),
        "status": snapshot.get("status", "unknown"),
        "deployable": bool(snapshot.get("deployable")),
        "paper_trading_allowed": bool(snapshot.get("paper_trading_allowed")),
        "live_trading_allowed": bool(snapshot.get("live_trading_allowed")),
        "readiness_status": snapshot.get("readiness_status", "unknown"),
        "blockers": [str(item) for item in blockers],
        "warnings": [str(item) for item in warnings],
        "readiness_blockers": [str(item) for item in readiness_blockers],
        "failed_checks": failed_checks,
        "message": snapshot.get("message", ""),
    }


def _write_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _print_summary(summary: dict[str, Any]) -> None:
    blockers = ", ".join(summary["blockers"]) or "none"
    readiness_blockers = ", ".join(summary["readiness_blockers"]) or "none"
    failed_checks = ", ".join(summary["failed_checks"]) or "none"
    print(f"deployment_status={summary['status']}")
    print(f"deployable={str(summary['deployable']).lower()}")
    print(f"paper_trading_allowed={str(summary['paper_trading_allowed']).lower()}")
    print(f"live_trading_allowed={str(summary['live_trading_allowed']).lower()}")
    print(f"readiness_status={summary['readiness_status']}")
    print(f"blockers={blockers}")
    print(f"readiness_blockers={readiness_blockers}")
    print(f"failed_checks={failed_checks}")
    if summary["message"]:
        print(f"message={summary['message']}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the Trading App deployment monitor endpoint.")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="Backend API base URL.")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds.")
    parser.add_argument("--jsonl", type=Path, help="Optional path to append a JSONL monitor result.")
    parser.add_argument("--snapshot-file", type=Path, help="Validate an existing deployment-monitor JSON file instead of making HTTP.")
    parser.add_argument("--allow-blocked", action="store_true", help="Exit 0 even when deployable is false. Useful for dry-run logging only.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        snapshot = _load_snapshot_file(args.snapshot_file) if args.snapshot_file else _load_snapshot(args.api_url, args.timeout)
        summary = _summarize(snapshot)
    except (OSError, httpx.HTTPError, TimeoutError, ValueError) as exc:
        summary = {
            "checked_at": _now_iso(),
            "status": "blocked",
            "deployable": False,
            "paper_trading_allowed": False,
            "live_trading_allowed": False,
            "readiness_status": "unknown",
            "blockers": ["Deployment monitor unreachable"],
            "warnings": [],
            "readiness_blockers": [],
            "failed_checks": ["HTTP"],
            "message": f"Monitor failed ({exc.__class__.__name__}); verify endpoint, credentials and service health.",
        }
        _print_summary(summary)
        if args.jsonl:
            _write_jsonl(args.jsonl, summary)
        return 2

    _print_summary(summary)
    if args.jsonl:
        _write_jsonl(args.jsonl, summary)
    return 0 if summary["deployable"] or args.allow_blocked else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
