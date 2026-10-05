import pytest

from app.services.stock_training_jobs import holdout_assessment
from scripts.alpaca_paper_contract import assess_documented_activity_schema


@pytest.mark.parametrize("model,baseline,status", [
    ((.26, .71), (.25, .69), "did_not_beat_baseline"),
    ((.24, .67), (.25, .69), "lower_prediction_error"),
    ((.24, .70), (.25, .69), "did_not_beat_baseline"),
    ((.25, .69), (.25, .69), "did_not_beat_baseline"),
    ((None, .71), (.25, .69), "not_comparable"),
    ((float("nan"), .71), (.25, .69), "not_comparable"),
])
def test_holdout_completion_is_not_a_pass(model, baseline, status):
    keys = ("brier_score", "log_loss")
    result = holdout_assessment({"final_holdout_metrics": dict(zip(keys, model)), "final_holdout_baseline": dict(zip(keys, baseline))})
    assert result["status"] == status
    assert result["profitability_established"] is False
    assert result["execution_authorized"] is False


def test_provider_schema_preserves_date_precision_and_unknown_fees():
    result = assess_documented_activity_schema([
        {"activity_type": "FILL", "transaction_time": "2026-09-28T13:40:00Z"},
        {"activity_type": "FEE", "date": "2026-09-28", "net_amount": "-0.01"},
    ])
    assert result["fills_with_precise_time"] == 1
    assert result["cash_activities_with_settlement_date"] == 1
    assert result["separate_fee_activity_count"] == 1
    assert result["fee_total"] is None
    assert result["all_in_costs"] == "unknown"
    assert result["launch_authorized"] is False
