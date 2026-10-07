"""Explicit Alpaca paper cost assumptions.

This module describes a provider contract for research accounting only.  It
does not turn missing broker fields into observed facts and never grants order
authority.
"""

from decimal import Decimal, InvalidOperation

from app.core.config import settings


def assess(*, provider: str, activity_contract: str, activities: list[dict]) -> dict:
    """Return a transparent, non-authorizing cost-policy assessment."""
    fills = [row for row in activities if str(row.get("activity_type") or "").upper() == "FILL"]
    missing_commissions = [row for row in fills if row.get("commission") is None]
    separate_fees = [row for row in activities
                     if str(row.get("activity_type") or "").upper() in {"FEE", "CFEE"}]
    observed_account_fee_total = Decimal("0")
    for row in separate_fees:
        if row.get("net_amount") is None:
            continue
        try:
            observed_account_fee_total += Decimal(str(row["net_amount"]))
        except (InvalidOperation, TypeError, ValueError):
            pass
    enabled = bool(
        settings.alpaca_paper_zero_commission_contract
        and provider == "alpaca_paper"
        and activity_contract == "alpaca-activities-v2"
        and fills
        and len(missing_commissions) == len(fills)
    )
    return {
        "policy_name": "alpaca_paper_commission_free_plus_regulatory_fees",
        "commission_policy": "Eligible U.S. equity API trades have no broker commission.",
        "account_fee_policy": "SEC/FINRA and other account-level fees may apply and are recorded separately.",
        "spread_slippage_policy": "Spread and slippage are not broker-reported costs and remain unverified.",
        "official_policy_sources": [
            "https://alpaca.markets/support/types-accounts-alpaca-offers",
            "https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf",
        ],
        "provider": provider,
        "activity_contract": activity_contract,
        "enabled": enabled,
        "fill_commissions_assumed_zero": enabled,
        "reported_fill_commissions_missing": len(missing_commissions),
        "account_level_fee_events": len(separate_fees),
        "observed_account_fee_total": format(observed_account_fee_total, ".2f"),
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
