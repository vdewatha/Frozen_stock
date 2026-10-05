import pytest

from app.services.alpaca_activity_v2 import normalize, replay


def fill(**changes):
    return {"id": "fill1", "activity_type": "FILL", "symbol": "SPY", "order_id": "order1",
            "side": "buy", "qty": "2", "price": "100", "transaction_time": "2026-09-28T15:00:00Z", **changes}


def cash(**changes):
    return {"id": "cash1", "activity_type": "FEE", "date": "2026-09-28", "net_amount": "-1", **changes}


def test_separate_fees_and_date_only_cash_reconcile_without_fabricated_commission():
    rows = [fill(), cash()]
    result = replay(rows, opening_cash="1000", closing_cash="799", opening_positions={},
                    closing_positions={"SPY": "2"}, history_complete=True)
    assert result["cash_equation_matches"] is True
    assert result["inventory_equation_matches"] is True
    assert result["all_in_costs"] == "unknown"
    assert not result["launch_authorized"]
    assert normalize(rows[0])["reported_fill_commission"] is None
    assert normalize(rows[1])["execution_at"] is None


def test_reordered_duplicate_replay_idempotent():
    rows = [fill(), cash()]
    assert replay(rows) == replay([cash(), fill(), fill()])
    with pytest.raises(ValueError, match="Conflicting"):
        replay([fill(), fill(price="101")])


def test_missing_baseline_or_incomplete_history_never_passes():
    assert replay([fill()])["cash_equation_matches"] is None
    result = replay([fill()], opening_cash="1000", closing_cash="800", opening_positions={}, closing_positions={"SPY": "2"})
    assert result["cash_equation_matches"] is None
    assert result["inventory_equation_matches"] is None


def test_mixed_fee_representation_needs_review():
    result = replay([fill(commission="1"), cash()], opening_cash="1000", closing_cash="798", history_complete=True)
    assert result["mixed_fee_attribution_requires_review"]
    assert result["cash_equation_matches"] is None


def test_partial_fills_sales_and_dividends():
    rows = [fill(qty="1"), fill(id="fill2", qty="1"), fill(id="fill3", side="sell", qty="1", price="110"), cash(activity_type="DIV", net_amount="2")]
    report = replay(rows, opening_cash="1000", closing_cash="912", opening_positions={}, closing_positions={"SPY": "1"}, history_complete=True)
    assert report["cash_equation_matches"] is True
    assert report["inventory_equation_matches"] is True
    report = replay(rows, opening_cash="1000", closing_cash="913", opening_positions={}, closing_positions={}, history_complete=True)
    assert report["cash_equation_matches"] is False
    assert report["inventory_equation_matches"] is False


@pytest.mark.parametrize("change", [{"transaction_time": "2026-09-28"}, {"transaction_time": "2026-09-28T15:00:00"}, {"qty": "NaN"}, {"qty": 0}, {"price": True}, {"order_id": None}, {"commission": "-1"}, {"side": "bad"}, {"activity_type": "SPLIT"}])
def test_malformed_or_unsupported_fill_fails_closed(change):
    with pytest.raises(ValueError):
        normalize(fill(**change))


@pytest.mark.parametrize("change", [{"date": "2026-09-28T00:00:00Z"}, {"net_amount": "Infinity"}, {"activity_type": "CSD"}, {"id": ""}])
def test_bad_cash_requires_review(change):
    with pytest.raises(ValueError):
        normalize(cash(**change))
