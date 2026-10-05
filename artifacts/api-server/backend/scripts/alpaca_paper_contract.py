"""Redacted, read-only compatibility assessment; never a launch authorization."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def precise_activity_time(row):
    """Return only a timezone-qualified broker time, never date/observation time."""
    value = row.get("transaction_time") or row.get("created_at")
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def reported_commission(row):
    value = row.get("commission")
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def assess_documented_activity_schema(activities):
    """Describe Alpaca's actual schema separately from our stricter ledger gate.

    TradeActivity has a transaction time but no guaranteed commission field.
    NonTradeActivity can have a settlement date, and fees are separate activities.
    Absence of FEE rows is not evidence of zero fees or complete pagination.
    """
    if activities is None:
        return {"status": "unavailable", "launch_authorized": False}
    fills = [row for row in activities if row.get("activity_type") == "FILL"]
    cash = [row for row in activities if row.get("activity_type") != "FILL"]
    fees = [row for row in cash if row.get("activity_type") in {"FEE", "CFEE"}]

    def has_date(row):
        try:
            value = row.get("date")
            return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False

    return {
        "status": "observed_schema_only" if activities else "no_activities",
        "fill_count": len(fills),
        "fills_with_precise_time": sum(precise_activity_time(row) is not None for row in fills),
        "cash_activity_count": len(cash),
        "cash_activities_with_settlement_date": sum(has_date(row) for row in cash),
        "separate_fee_activity_count": len(fees),
        "fee_total": None,
        "all_in_costs": "unknown",
        "launch_authorized": False,
        "contract_limitations": [
            "Alpaca TradeActivity does not guarantee per-fill commission",
            "NonTradeActivity settlement dates are not precise execution timestamps",
            "Separate fee activities require pagination, replay and cash reconciliation",
            "Modeled slippage and paper omissions are not broker-reported costs",
        ],
    }


def assess_cost_timestamp_contract(activities):
    """Assess returned evidence only; empty or unavailable evidence is not a pass.

    Stable IDs, paging, order linkage, accounting, and recovery remain separate
    requirements even when these two field-level contracts are satisfied.
    """
    if activities is None:
        return {
            "result": "not_established",
            "cost_evidence": "unknown",
            "activity_timestamp_evidence": "unknown",
            "reasons": ["activity_evidence_unavailable"],
            "launch_authorized": False,
        }
    fills = [row for row in activities if row.get("activity_type") == "FILL"]
    missing_costs = sum(reported_commission(row) is None for row in fills)
    missing_times = sum(precise_activity_time(row) is None for row in activities)
    reasons = []
    if not fills:
        reasons.append("no_fill_cost_evidence")
    elif missing_costs:
        reasons.append("fill_commissions_absent_or_invalid")
    if not activities:
        reasons.append("no_activity_timestamp_evidence")
    elif missing_times:
        reasons.append("precise_broker_activity_timestamps_absent_or_invalid")
    return {
        "result": "not_established" if reasons else "field_contract_satisfied_only",
        "cost_evidence": "unknown" if not fills or missing_costs else "explicitly_reported",
        "activity_timestamp_evidence": "unknown" if not activities or missing_times else "precise_broker_reported",
        "fill_count": len(fills),
        "fills_without_valid_commission": missing_costs,
        "activities_without_precise_broker_time": missing_times,
        "reasons": reasons,
        "launch_authorized": False,
    }
