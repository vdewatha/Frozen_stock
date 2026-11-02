"""Exercise the running loopback paper API without submitting broker orders."""
from datetime import datetime, timezone
import json
from pathlib import Path

from dotenv import load_dotenv
import httpx


def main() -> None:
    root = Path(__file__).resolve().parents[4]
    load_dotenv(root / ".env")
    from app.core.config import settings
    if settings.environment != "local" or settings.allow_live_trading or settings.paper_binding_source != "development":
        raise SystemExit("Requires local, dedicated development paper configuration")
    results = []
    with httpx.Client(base_url="http://127.0.0.1:8120", timeout=60) as client:
        admin = {"Authorization": f"Bearer {settings.auth_admin_key.get_secret_value()}"}
        researcher = {"Authorization": f"Bearer {settings.auth_researcher_key.get_secret_value()}"}
        operator = {"Authorization": f"Bearer {settings.auth_operator_key.get_secret_value()}"}
        viewer = {"Authorization": f"Bearer {settings.auth_viewer_key.get_secret_value()}"}

        def check(name, response, expected):
            row = {"check": name, "http_status": response.status_code, "expected": expected,
                   "passed": response.status_code == expected}
            results.append(row)
            print(json.dumps(row), flush=True)
            return response

        status = client.get("/api/stock-paper/status", headers=viewer)
        if status.status_code == 200 and status.json().get("status") == "uninitialized":
            # Import-only: captures the broker snapshot, never creates an order.
            check("initialize_local_snapshot", client.post("/api/stock-paper/initialize", headers=admin, json={}), 200)
        for path in ("/health", "/auth/config", "/auth/session", "/dashboard", "/assets", "/strategies",
                     "/risk-rules", "/stock-paper/status", "/stock-paper/overview", "/stock-paper/scorecard",
                     "/market-data/iex/status", "/research/scalp/runs"):
            check(f"viewer_get{path}", client.get(f"/api{path}", headers=viewer), 200)
        check("reconcile_local_snapshot", client.post("/api/stock-paper/reconcile", headers=operator), 200)
        for headers, name, expected in (({}, "anonymous", 401), (viewer, "viewer", 403)):
            check(f"{name}_cannot_run_short_research", client.post("/api/stock-paper/short-simulation",
                  headers=headers, json={"symbol": "SPY"}), expected)
        check("researcher_can_simulate", client.post("/api/stock-paper/short-simulation", headers=researcher,
              json={"symbol": "SPY", "limit": 1500}), 200)
        check("reject_synthetic", client.post("/api/stock-paper/short-simulation", headers=researcher,
              json={"symbol": "SPY", "source": "synthetic_fallback"}), 422)
        check("reject_live_flag", client.post("/api/stock-paper/short-simulation", headers=researcher,
              json={"symbol": "SPY", "live": True}), 422)
        check("reject_invalid_config", client.post("/api/stock-paper/short-simulation", headers=researcher,
              json={"symbol": "SPY", "config": {"allocation": .9}}), 422)
        check("reject_invalid_credentials", client.get("/api/stock-paper/status",
              headers={"Authorization": "Bearer invalid-test-credential"}), 401)
    report = {"at": datetime.now(timezone.utc).isoformat(), "paper_only": True,
              "broker_orders_submitted": 0, "checks": results, "passed": all(r["passed"] for r in results)}
    destination = root / ".paper-training/local-short-session-20261030/api-checks.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
