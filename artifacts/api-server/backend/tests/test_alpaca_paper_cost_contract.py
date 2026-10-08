from app.core.config import settings
from app.services.alpaca_paper_cost_contract import assess, execution_policy_status


def test_contract_is_disabled_by_default_and_keeps_costs_unknown(monkeypatch):
    monkeypatch.setattr(settings, "alpaca_paper_zero_commission_contract", False)
    result = assess(provider="alpaca_paper", activity_contract="alpaca-activities-v2",
                    activities=[{"activity_type": "FILL"}])
    assert result["enabled"] is False
    assert result["costs_verified"] is False
    assert result["live_authorized"] is False


def test_contract_is_research_only_and_does_not_hide_account_fees(monkeypatch):
    monkeypatch.setattr(settings, "alpaca_paper_zero_commission_contract", True)
    result = assess(provider="alpaca_paper", activity_contract="alpaca-activities-v2", activities=[
        {"activity_type": "FILL"}, {"activity_type": "FEE"},
    ])
    assert result["enabled"] is True
    assert result["fill_commissions_assumed_zero"] is True
    assert result["account_level_fee_events"] == 1
    assert result["costs_verified"] is False
    assert result["launch_authorized"] is False


def test_execution_policy_requires_exact_reconciliation_and_separate_activation(monkeypatch):
    monkeypatch.setattr(settings, "alpaca_paper_zero_commission_contract", True)
    matched = {
        "status": "matched",
        "cash_equation_matches": True,
        "inventory_equation_matches": True,
        "cash_residual": "0.00",
    }
    result = execution_policy_status(
        provider="alpaca_paper",
        activity_contract="alpaca-activities-v2",
        account_status="reconciled",
        reconciliation_required=False,
        unexplained_residual=False,
        reconciliation_payload=matched,
        venue_activation_authorized=True,
    )
    assert result["ready"] is True
    assert result["research_only_costs"] is True
    assert result["all_in_costs_verified"] is False


def test_execution_policy_stays_blocked_without_activation_or_exact_match(monkeypatch):
    monkeypatch.setattr(settings, "alpaca_paper_zero_commission_contract", True)
    result = execution_policy_status(
        provider="alpaca_paper",
        activity_contract="alpaca-activities-v2",
        account_status="reconciled",
        reconciliation_required=False,
        unexplained_residual=False,
        reconciliation_payload={"status": "matched", "cash_equation_matches": True},
        venue_activation_authorized=False,
    )
    assert result["ready"] is False
    assert "separate paper venue activation is missing" in result["blockers"]
