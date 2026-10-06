"""Explicit Alpaca paper cost assumptions.

This module describes a provider contract for research accounting only.  It
does not turn missing broker fields into observed facts and never grants order
authority.
"""

from app.core.config import settings


def assess(*, provider: str, activity_contract: str, activities: list[dict]) -> dict:
    """Return a transparent, non-authorizing cost-policy assessment."""
    fills = [row for row in activities if str(row.get("activity_type") or "").upper() == "FILL"]
    missing_commissions = [row for row in fills if row.get("commission") is None]
    separate_fees = [row for row in activities
                     if str(row.get("activity_type") or "").upper() in {"FEE", "CFEE"}]
    enabled = bool(
        settings.alpaca_paper_zero_commission_contract
        and provider == "alpaca_paper"
        and activity_contract == "alpaca-activities-v2"
        and fills
        and len(missing_commissions) == len(fills)
    )
    return {
        "provider": provider,
        "activity_contract": activity_contract,
        "enabled": enabled,
        "fill_commissions_assumed_zero": enabled,
        "reported_fill_commissions_missing": len(missing_commissions),
        "account_level_fee_events": len(separate_fees),
        "regulatory_and_other_costs_simulated": False,
        "costs_verified": False,
        "launch_authorized": False,
        "live_authorized": False,
        "reason": (
            "Provider contract is enabled for modeled research only; account-level "
            "fees and unmodeled spread/slippage remain outside the contract."
            if enabled else
            "No provider zero-commission assumption is active; missing costs remain unknown."
        ),
    }
