"""Explain cent rounding without treating a matching hypothesis as qualification."""
from decimal import Decimal, ROUND_HALF_EVEN

from app.services.alpaca_activity_v2 import amount, normalize, reconcile_baseline

POLICY = "alpaca-usd-cent-diagnostic-v1"


def _cent(value):
    if abs(value * 100) % 1 == Decimal("0.5"):
        raise ValueError("Half-cent tie needs an evidenced rounding rule")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def _adjustment(rows, orders):
    groups = {}
    seen = set()
    for raw in rows:
        event = normalize(raw)
        if event["id"] in seen:
            continue  # reconcile_baseline already rejects conflicting duplicates.
        seen.add(event["id"])
        if event["type"] != "FILL":
            continue
        group = groups.setdefault(event["order_id"], [])
        group.append(event)
    adjustment = Decimal(0)
    details = []
    for identifier, events in sorted(groups.items()):
        order = orders.get(identifier)
        if not order or order.get("status") not in {"filled", "canceled", "expired"}:
            raise ValueError("Every included order needs terminal broker evidence")
        side = order.get("side")
        if side not in {"buy", "sell"}:
            raise ValueError("Invalid order side")
        sign = Decimal(1) if side == "buy" else Decimal(-1)
        if any(e["symbol"] != order.get("symbol") or amount(e["quantity_delta"]) * sign <= 0 for e in events):
            raise ValueError("Fill and order identities differ")
        quantity = sum((amount(e["quantity_delta"]) * sign for e in events), Decimal(0))
        if quantity != amount(order.get("filled_qty")):
            raise ValueError("Baseline or current journal contains an incomplete order")
        gross = sum((amount(e["gross_cash_delta"]) for e in events), Decimal(0))
        per_order = _cent(gross)
        per_fill = sum((_cent(amount(e["gross_cash_delta"])) for e in events), Decimal(0))
        average = amount(order.get("filled_avg_price"))
        if average <= 0 or _cent(-sign * quantity * average) != per_order:
            raise ValueError("Order average price does not corroborate fill proceeds")
        if per_fill != per_order:
            raise ValueError("Per-fill and per-order rounding disagree")
        adjustment += per_order - gross
        details.append({"order_id": identifier, "fill_count": len(events),
                        "unrounded_cash_delta": format(gross, "f"), "rounded_cash_delta": format(per_order, "f"),
                        "rounding_adjustment": format(per_order - gross, "f")})
    return adjustment, details


def diagnose(baseline, activities, orders, cash, positions, *, currency, broker):
    result = {"policy": POLICY, "qualifying": False, "launch_authorized": False,
              "costs_verified": False, "status": "unavailable"}
    try:
        if broker != "alpaca_paper" or currency != "USD":
            raise ValueError("Diagnostic requires Alpaca paper USD evidence")
        replay = reconcile_baseline(baseline, activities, cash, positions,
                                    previously_seen_ids={r["id"] for r in baseline["activities"]})
        index = {}
        for order in orders:
            if not isinstance(order, dict):
                raise ValueError("Invalid broker order evidence")
            identifier = order.get("id")
            if not isinstance(identifier, str) or not identifier or identifier in index:
                raise ValueError("Missing or duplicate broker order ID")
            index[identifier] = order
        represented = {r.get("order_id") for r in activities if r.get("activity_type") == "FILL"}
        if any(amount(order.get("filled_qty")) > 0 and identifier not in represented
               for identifier, order in index.items()):
            raise ValueError("Filled broker order is missing from the activity journal")
        old, _ = _adjustment(baseline["activities"], index)
        new, details = _adjustment(activities, index)
        adjustment = new - old
        raw_residual = amount(replay["cash_residual"])
        residual = raw_residual - adjustment
        consistent = residual == 0 and replay["inventory_equation_matches"] is True
        return {**result, "status": "consistent" if consistent else "mismatch",
                "reason": "Rounding hypothesis only; no accounting approval or cost completeness implied",
                "unrounded_cash_residual": format(raw_residual, "f"),
                "rounding_adjustment_since_baseline": format(adjustment, "f"),
                "cash_residual_after_rounding": format(residual, "f"),
                "inventory_matches": replay["inventory_equation_matches"],
                "orders": details}
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        return {**result, "reason": str(exc) if isinstance(exc, ValueError) else "Invalid numeric or structural evidence"}
