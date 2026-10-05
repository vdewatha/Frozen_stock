"""Versioned research stress costs, never broker fees or trading qualification."""
from decimal import Decimal
import hashlib
import json

from app.services.alpaca_activity_v2 import VERSION as ACTIVITY_VERSION, amount, normalize, replay

ASSUMPTIONS = {
    "version": "paper-turnover-stress-v1",
    "basis": "unrounded_fill_prices",
    "additional_cost_bps_per_side": [0, 1, 5, 10],
    "scope": "closed_inventory_since_observed_baseline",
    "kind": "illustrative_sensitivity_not_estimated_broker_fees",
}


def evaluate(baseline, activities, closing_positions, *, currency):
    assumptions = json.loads(json.dumps(ASSUMPTIONS))
    result = {"assumptions": assumptions,
              "assumptions_sha256": hashlib.sha256(json.dumps(assumptions, sort_keys=True).encode()).hexdigest(),
              "status": "unavailable", "qualifying": False, "launch_authorized": False,
              "costs_verified": False, "scenarios": []}
    try:
        if (currency != "USD" or not isinstance(baseline, dict)
                or baseline.get("version") != ACTIVITY_VERSION
                or not isinstance(baseline.get("positions"), dict)
                or not isinstance(closing_positions, dict)):
            raise ValueError("A versioned USD activity baseline and position maps are required")
        before = replay(baseline["activities"])
        current = replay(activities)
        if baseline.get("journal_sha256") != before["journal_sha256"]:
            raise ValueError("Frozen baseline digest mismatch")
        old = {r["id"]: r for r in baseline["activities"]}
        new = {r["id"]: r for r in activities}
        if any(new.get(identifier) != row for identifier, row in old.items()):
            raise ValueError("Baseline activity missing or changed; correction review required")
        if before["mixed_fee_attribution_requires_review"] or current["mixed_fee_attribution_requires_review"]:
            raise ValueError("Mixed fee attribution requires review")
        if any(amount(v) != 0 for v in baseline["positions"].values()) or any(amount(v) != 0 for v in closing_positions.values()):
            raise ValueError("Open inventory needs a separate valuation and cost-basis contract")
        gross = turnover = commissions = cash_fees = Decimal(0)
        quantities = {}
        fills = unknown = 0
        for identifier, raw in new.items():
            if identifier in old:
                continue
            event = normalize(raw)
            if event["type"] in {"FEE", "CFEE"}:
                cash_fees -= amount(event["gross_cash_delta"])
            if event["type"] != "FILL":
                continue
            fills += 1
            gross += amount(event["gross_cash_delta"])
            turnover += abs(amount(event["gross_cash_delta"]))
            symbol = event["symbol"]
            quantities[symbol] = quantities.get(symbol, Decimal(0)) + amount(event["quantity_delta"])
            if event["reported_fill_commission"] is None:
                unknown += 1
            else:
                commissions += amount(event["reported_fill_commission"])
        if not fills:
            raise ValueError("No new executions to evaluate")
        if any(quantities.values()):
            raise ValueError("Fill inventory is not closed by symbol")
        known_fees = commissions + cash_fees
        scenarios = []
        for bps in assumptions["additional_cost_bps_per_side"]:
            extra = turnover * Decimal(bps) / Decimal(10000)
            scenarios.append({"additional_cost_bps_per_side": bps,
                              "additional_modeled_cost": format(extra, "f"),
                              "modeled_fill_cash_change": format(gross - known_fees - extra, "f")})
        return {**result, "status": "research_only",
                "reason": "Hypothetical additional turnover costs; missing broker fees remain unknown. Not realized net profit.",
                "new_fill_count": fills, "fills_without_reported_commission": unknown,
                "symbols": sorted(quantities), "turnover": format(turnover, "f"),
                "gross_fill_cash_change": format(gross, "f"),
                "reported_fee_subtotal": format(known_fees, "f"),
                "excluded_nonfee_cash_flows": True,
                "journal_sha256": current["journal_sha256"], "scenarios": scenarios}
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        return {**result, "reason": str(exc) if isinstance(exc, ValueError) else "Invalid cost-model evidence"}
