"""Provider-shaped activity journal; reconciliation never grants trading authority.

Date-only cash events are not executions. Separate fees are account-level cash
entries, never invented fill commissions. No network calls or order authority.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json

VERSION = "alpaca-activities-v2"
CASH_TYPES = frozenset({"CSD", "CSW", "DIV", "DIVNRA", "FEE", "CFEE", "INT", "JNLC"})


def amount(value):
    if value is None or isinstance(value, bool):
        raise ValueError("Missing or invalid monetary/quantity field")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid numeric field") from None
    if not result.is_finite():
        raise ValueError("Nonfinite numeric field")
    return result


def normalize(row):
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
        raise ValueError("Activity ID required")
    kind = row.get("activity_type")
    base = {"id": row["id"], "type": kind, "version": VERSION}
    if kind == "FILL":
        value = row.get("transaction_time")
        if not isinstance(value, str) or "T" not in value:
            raise ValueError("Fill requires broker execution timestamp")
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Invalid fill timestamp") from None
        if stamp.tzinfo is None:
            raise ValueError("Fill timestamp requires timezone")
        qty, price = amount(row.get("qty")), amount(row.get("price"))
        symbol, order_id, side = row.get("symbol"), row.get("order_id"), row.get("side")
        if qty <= 0 or price <= 0 or not isinstance(symbol, str) or not symbol.strip() or not isinstance(order_id, str) or not order_id.strip() or side not in {"buy", "sell"}:
            raise ValueError("Invalid fill identity, side or quantity/price")
        signed = qty if side == "buy" else -qty
        commission = amount(row["commission"]) if row.get("commission") is not None else None
        if commission is not None and commission < 0:
            raise ValueError("Negative fill commission requires review")
        return {**base, "execution_at": stamp.astimezone(timezone.utc).isoformat(),
                "effective_date": None, "symbol": symbol.upper(), "order_id": order_id,
                "quantity_delta": str(signed), "gross_cash_delta": str(-signed*price),
                "reported_fill_commission": str(commission) if commission is not None else None}
    if kind not in CASH_TYPES:
        raise ValueError("Unsupported activity requires accounting review")
    value = row.get("date")
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError:
        raise ValueError("Cash activity requires exact effective date") from None
    cash = amount(row.get("net_amount"))
    if (kind == "CSD" and cash < 0) or (kind == "CSW" and cash > 0):
        raise ValueError("Cash transfer sign conflicts with activity type")
    return {**base, "execution_at": None, "effective_date": value,
            "symbol": None, "order_id": None, "quantity_delta": "0",
            "gross_cash_delta": str(cash), "reported_fill_commission": None}


def replay(activities, *, opening_cash=None, closing_cash=None, opening_positions=None,
           closing_positions=None, history_complete=False):
    """Compare only explicitly supplied baselines, never assume an empty account."""
    if not isinstance(activities, list):
        raise ValueError("Activity list required")
    seen = {}
    events = []
    for raw in activities:
        event = normalize(raw)
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if event["id"] in seen:
            if seen[event["id"]] != canonical:
                raise ValueError("Conflicting activity ID requires correction review")
            continue
        seen[event["id"]] = canonical
        events.append(event)
    events.sort(key=lambda event: event["id"])
    cash = sum((amount(e["gross_cash_delta"]) for e in events), Decimal(0))
    fees = sum((amount(e["reported_fill_commission"]) for e in events if e["reported_fill_commission"] is not None), Decimal(0))
    separate_fees = any(e["type"] in {"FEE", "CFEE"} for e in events)
    # If both forms exist, attribution may overlap. Do not silently deduct twice.
    ambiguous_fees = separate_fees and any(e["reported_fill_commission"] is not None for e in events)
    positions = {}
    for event in events:
        if event["symbol"]:
            symbol = event["symbol"]
            positions[symbol] = positions.get(symbol, Decimal(0)) + amount(event["quantity_delta"])
    cash_match = None
    inventory_match = None
    if history_complete and not ambiguous_fees and opening_cash is not None and closing_cash is not None:
        cash_match = amount(opening_cash) + cash - fees == amount(closing_cash)
    if history_complete and opening_positions is not None and closing_positions is not None:
        reconstructed = {k: amount(v) for k, v in opening_positions.items()}
        for symbol, delta in positions.items():
            reconstructed[symbol] = reconstructed.get(symbol, Decimal(0)) + delta
        inventory_match = {k: v for k, v in reconstructed.items() if v} == {k: amount(v) for k, v in closing_positions.items() if amount(v)}
    return {"version": VERSION, "event_count": len(events),
            "journal_sha256": hashlib.sha256(json.dumps(events, sort_keys=True).encode()).hexdigest(),
            "gross_reported_cash_delta": str(cash), "reported_fill_commissions": str(fees),
            "mixed_fee_attribution_requires_review": ambiguous_fees,
            "cash_equation_matches": cash_match, "inventory_equation_matches": inventory_match,
            "opening_baseline_supplied": opening_cash is not None and opening_positions is not None,
            "all_in_costs": "unknown", "launch_authorized": False,
            "status": "shadow_replay_only"}


def observed_baseline(activities, cash, positions, observed_at):
    """Freeze an observed account, not an inferred account-opening balance."""
    journal = replay(activities)
    if journal["mixed_fee_attribution_requires_review"]:
        raise ValueError("Mixed fee attribution requires accounting review")
    return {"version": VERSION, "cash": str(amount(cash)),
            "positions": {key: str(amount(value)) for key, value in positions.items()},
            "activities": json.loads(json.dumps(activities, allow_nan=False)),
            "journal_sha256": journal["journal_sha256"], "observed_at": observed_at}


def reconcile_baseline(baseline, activities, cash, positions, *, previously_seen_ids):
    """Replay net changes against the frozen observation, including late events.

    Cash activity dates are settlement/effective dates, not execution watermarks.
    Previously imported IDs must remain visible in the complete broker history.
    Existing payload immutability is enforced by the persistent ledger importer.
    """
    required = {"activities", "cash", "positions", "journal_sha256", "observed_at"}
    if (not isinstance(baseline, dict) or baseline.get("version") != VERSION
            or not required.issubset(baseline) or not isinstance(baseline["positions"], dict)):
        raise ValueError("Missing versioned activity baseline")
    before = replay(baseline["activities"])
    current = replay(activities)
    if before["journal_sha256"] != baseline.get("journal_sha256"):
        raise ValueError("Activity baseline digest changed")
    current_ids = {row["id"] for row in activities}
    required_ids = set(previously_seen_ids) | {row["id"] for row in baseline["activities"]}
    if not required_ids.issubset(current_ids):
        raise ValueError("Previously observed activity missing from broker history")
    if before["mixed_fee_attribution_requires_review"] or current["mixed_fee_attribution_requires_review"]:
        raise ValueError("Mixed fee attribution requires accounting review")
    expected_cash = amount(baseline["cash"])
    expected_cash += amount(current["gross_reported_cash_delta"]) - amount(before["gross_reported_cash_delta"])
    expected_cash -= amount(current["reported_fill_commissions"]) - amount(before["reported_fill_commissions"])
    expected_positions = {key: amount(value) for key, value in baseline["positions"].items()}
    for rows, sign in ((baseline["activities"], -1), (activities, 1)):
        seen = set()
        for raw in rows:
            event = normalize(raw)
            if event["id"] in seen:
                continue
            seen.add(event["id"])
            if event["symbol"]:
                symbol = event["symbol"]
                expected_positions[symbol] = expected_positions.get(symbol, Decimal(0)) + sign * amount(event["quantity_delta"])
    actual_positions = {key: amount(value) for key, value in positions.items()}
    cash_matches = expected_cash == amount(cash)
    inventory_matches = {k: v for k, v in expected_positions.items() if v} == {k: v for k, v in actual_positions.items() if v}
    return {"version": VERSION, "status": "matched" if cash_matches and inventory_matches else "mismatch",
            "event_count": current["event_count"], "journal_sha256": current["journal_sha256"],
            "baseline_journal_sha256": before["journal_sha256"],
            "cash_equation_matches": cash_matches, "inventory_equation_matches": inventory_matches,
            "cash_residual": str(amount(cash) - expected_cash),
            "baseline_scope": "observed_account_not_inception", "all_in_costs": "unknown",
            "launch_authorized": False}
