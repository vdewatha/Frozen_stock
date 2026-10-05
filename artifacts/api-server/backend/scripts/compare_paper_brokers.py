"""Probe both paper adapters with read-only account requests; never switch or qualify a venue."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from app.core.config import settings
from app.services.stock_paper_ledger import AlpacaPaperClient, TradierPaperClient


def compare() -> dict:
    key, secret = settings.paper_broker_credentials()
    candidates = (
        ("alpaca_paper", bool(key and secret), AlpacaPaperClient),
        ("tradier_sandbox", bool(settings.tradier_api_key.get_secret_value() and settings.tradier_account_id), TradierPaperClient),
    )
    results = []
    for name, configured, factory in candidates:
        result = {
            "provider": name, "credentials_configured": configured,
            "account_read": "not_tested", "positions_read": "not_tested",
            "qualified_for_execution": False, "activated": False,
            "costs_verified": False, "history_completeness_verified": False,
        }
        if not configured:
            result.update(status="blocked", reason="Paper credentials/account binding unavailable")
        else:
            try:
                client = factory()
                account = client.account()
                if not isinstance(account, dict) or not account.get("id"):
                    raise ValueError("Invalid paper account response")
                result["account_read"] = "pass"
                positions = client.positions()
                if not isinstance(positions, list) or not all(isinstance(row, dict) for row in positions):
                    raise ValueError("Invalid paper positions response")
                result.update(
                    positions_read="pass", position_count=len(positions),
                    status="reachable_not_qualified",
                    reason="Read connectivity does not establish complete accounting or execution qualification",
                )
            except Exception as exc:
                result.update(status="unavailable", error_type=type(exc).__name__, reason="Read-only paper probe failed")
        if name == "tradier_sandbox":
            result["adapter_limitation"] = "Current adapter cannot establish complete historical accounting"
        results.append(result)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "read_only_connectivity_not_venue_qualification",
        "configured_active_provider": settings.active_paper_broker,
        "provider_changed": False, "orders_submitted": 0,
        "live_trading_authorized": False, "candidates": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    raise SystemExit(0 if any(row["status"] == "reachable_not_qualified" for row in report["candidates"]) else 1)
