"""Observed gross trading P/L, not broker equity changes or verified net profit."""
from decimal import Decimal
from datetime import datetime


def observed_short_performance(orders, gross_market_value) -> dict:
    liability = Decimal(str(gross_market_value))
    if not liability.is_finite() or liability < 0:
        raise ValueError("Short market liability must be finite and nonnegative")
    trade_cash_flow = Decimal("0")
    filled_orders = 0
    quantities = {}
    for row in orders:
        quantity = Decimal(str(row.filled_quantity))
        if not quantity.is_finite() or quantity < 0:
            raise ValueError("Invalid cumulative short fill quantity")
        if quantity == 0:
            continue
        price = Decimal(str(row.filled_avg_price))
        if not price.is_finite() or price <= 0:
            raise ValueError("Observed fills require a positive finite average price")
        if row.intent not in {"open_short", "cover_short"}:
            raise ValueError("Unknown short fill intent")
        quantities[row.symbol] = quantities.get(row.symbol, Decimal("0")) + quantity * (1 if row.intent == "open_short" else -1)
        trade_cash_flow += quantity * price * (1 if row.intent == "open_short" else -1)
        filled_orders += 1
    if any(q < 0 for q in quantities.values()) or bool(any(quantities.values())) != bool(liability):
        raise ValueError("Owned short fills and market liability do not match")
    # Restricted sale proceeds are not profits: subtract the entire marked
    # liability of still-open shorts. Cash transfers/equity funding are excluded.
    return {"gross_pl": str(trade_cash_flow - liability), "net_pl": None,
            "trade_cash_flow": str(trade_cash_flow), "open_short_liability": str(liability),
            "filled_orders": filled_orders, "currency": "USD", "costs_verified": False,
            "status": "gross_only" if filled_orders else "no_fills",
            "source": "owned_cumulative_broker_fills_and_reconciled_short_market_value"}


def recorded_performance(rows) -> dict:
    points = []
    for row in rows:
        payload = row.payload or {}
        performance = payload.get("performance")
        if not isinstance(performance, dict) or performance.get("source") != "owned_cumulative_broker_fills_and_reconciled_short_market_value":
            continue
        try:
            value = Decimal(str(performance["gross_pl"]))
            if not value.is_finite():
                continue
        except (KeyError, ArithmeticError, ValueError):
            continue
        # Only new, explicitly timestamped observations count. Older audit
        # events with no P/L evidence are not retrospectively filled with zeros.
        observed = payload.get("observed_at")
        if not isinstance(observed, str):
            continue
        try:
            parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                continue
        except ValueError:
            continue
        points.append({"observed_at": observed, "gross_pl": str(value),
                       "filled_orders": performance.get("filled_orders"),
                       "audit_event_id": row.id})
    return {"currency": "USD", "costs_verified": False, "net_pl": None,
            "scope": "latest_1000_reconciliations", "points": points,
            "latest": points[-1] if points else None}
