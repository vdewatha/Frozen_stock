from app.core.config import settings
from app.services.alpaca_paper_cost_contract import assess


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
