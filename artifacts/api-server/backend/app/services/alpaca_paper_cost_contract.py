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


def execution_policy_status(
    *,
    provider: str,
    activity_contract: str,
    account_status: str | None,
    reconciliation_required: bool,
    unexplained_residual: bool,
    reconciliation_payload: dict | None,
    venue_activation_authorized: bool,
) -> dict:
    """Describe whether paper orders may use the explicit broker policy.

    This is deliberately narrower than ``costs_verified``.  It admits only a
    reconciled, residual-free Alpaca paper account whose latest observed
    baseline matches and whose separate venue activation is present.  It does
    not claim that spread, slippage, or all-in P/L costs are known.
    """
    policy_enabled = bool(
        settings.alpaca_paper_zero_commission_contract
        and provider == "alpaca_paper"
        and activity_contract == "alpaca-activities-v2"
    )
    try:
        cash_residual = Decimal(str(reconciliation_payload.get("cash_residual", "1"))) \
            if isinstance(reconciliation_payload, dict) else Decimal("1")
    except (InvalidOperation, TypeError, ValueError):
        cash_residual = Decimal("1")
    reconciliation_matched = bool(
        isinstance(reconciliation_payload, dict)
        and reconciliation_payload.get("status") == "matched"
        and reconciliation_payload.get("cash_equation_matches") is True
        and reconciliation_payload.get("inventory_equation_matches") is True
        and cash_residual == 0
    )
    ready = bool(
        policy_enabled
        and account_status == "reconciled"
        and not reconciliation_required
        and not unexplained_residual
        and reconciliation_matched
        and venue_activation_authorized
    )
    blockers = []
    if not policy_enabled:
        blockers.append("explicit Alpaca paper commission policy is disabled")
    if account_status != "reconciled" or reconciliation_required:
        blockers.append("paper account reconciliation is not current")
    if unexplained_residual:
        blockers.append("unexplained broker residual remains")
    if not reconciliation_matched:
        blockers.append("latest broker activity reconciliation is not an exact match")
    if not venue_activation_authorized:
        blockers.append("separate paper venue activation is missing")
    return {
        "ready": ready,
        "policy_active": policy_enabled,
        "commission_policy": "eligible Alpaca paper U.S. equity API trades modeled as zero broker commission",
        "all_in_costs_verified": False,
        "research_only_costs": True,
        "venue_activation_authorized": bool(venue_activation_authorized),
        "reconciliation_matched": reconciliation_matched,
        "blockers": blockers,
    }
