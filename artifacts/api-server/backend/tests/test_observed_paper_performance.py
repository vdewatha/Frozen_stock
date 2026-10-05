from copy import deepcopy
from decimal import Decimal

import pytest

from app.services.alpaca_activity_v2 import observed_baseline
from app.services.stock_paper_performance import calculate_observed_change


def cash(kind, value, identifier="cash"):
    return {"id": identifier, "activity_type": kind, "date": "2026-09-29", "net_amount": value}


def fill(**changes):
    return {"id": "fill", "activity_type": "FILL", "transaction_time": "2026-09-29T15:00:00Z",
            "symbol": "SPY", "side": "buy", "qty": "1", "price": "100", "order_id": "order", **changes}


def calculate(rows, closing_cash, closing_equity=None, positions=None):
    seed = [cash("CSD", "1000", "seed")]
    baseline = observed_baseline(seed, "1000", {}, "2026-09-29T14:00:00Z")
    return calculate_observed_change(
        baseline, seed + rows, opening_equity="1000", closing_equity=closing_equity or closing_cash,
        closing_cash=closing_cash, closing_positions=positions or {},
    )


@pytest.mark.parametrize("kind,value,expected", [
    ("CSD", "100", "0"), ("CSW", "-100", "0"),
    ("DIV", "5", "5"), ("DIVNRA", "-1", "-1"),
    ("INT", "2", "2"), ("FEE", "-3", "-3"), ("CFEE", "-2", "-2"),
])
def test_funding_is_not_profit_and_expenses_are_not_deducted_twice(kind, value, expected):
    result = calculate([cash(kind, value)], str(Decimal(1000) + Decimal(value)))
    assert result["status"] == "provisional"
    assert Decimal(result["net_change"]) == Decimal(expected)
    assert result["qualifying"] is False and result["costs_complete"] is False


def test_execution_prices_and_separate_fees_are_already_in_equity():
    result = calculate([fill(), cash("FEE", "-1")], "899", "1009", {"SPY": "1"})
    assert result["net_change"] == "9"
    assert result["reported_fee_expense"] == "1"
    assert result["new_fills_without_commission"] == 1


def test_explicit_commission_is_not_subtracted_from_equity_twice():
    result = calculate([fill(commission="1")], "899", "1009", {"SPY": "1"})
    assert result["net_change"] == "9"
    assert result["reported_fee_expense"] == "1"
    assert result["new_fills_without_commission"] == 0
    assert not result["costs_complete"]


def test_duplicates_do_not_double_count_external_flows():
    row = cash("CSD", "25")
    result = calculate([row, deepcopy(row)], "1025")
    assert result["external_net_funding"] == "25"
    assert result["net_change"] == "0"


@pytest.mark.parametrize("rows,cash_balance,positions", [
    ([cash("JNLC", "3")], "1003", {}),
    ([fill(commission="1"), cash("FEE", "-1")], "898", {"SPY": "1"}),
    ([cash("CSD", "25")], "1026", {}),
    ([fill()], "900", {}),
    ([cash("CSD", "NaN")], "1000", {}),
    ([cash("CSD", "-1")], "999", {}),
    ([cash("OTHER", "1")], "1001", {}),
])
def test_ambiguous_or_invalid_evidence_withholds_the_calculation(rows, cash_balance, positions):
    result = calculate(rows, cash_balance, positions=positions)
    assert result["status"] == "unavailable"
    assert result["net_change"] is None and result["qualifying"] is False


def test_baseline_activities_and_costs_are_not_counted_as_new():
    rows = [cash("CSD", "1000"), cash("FEE", "-2", "fee")]
    baseline = observed_baseline(rows, "998", {}, "2026-09-29T14:00:00Z")
    result = calculate_observed_change(baseline, rows, opening_equity="998", closing_equity="998",
                                       closing_cash="998", closing_positions={})
    assert result["net_change"] == "0"
    assert result["external_net_funding"] == "0"
    assert result["reported_fee_expense"] == "0"


def test_missing_baseline_activity_and_changed_digest_fail_closed():
    rows = [cash("CSD", "1000")]
    baseline = observed_baseline(rows, "1000", {}, "2026-09-29T14:00:00Z")
    for data, current in [(baseline, []), ({**baseline, "journal_sha256": "invalid"}, rows)]:
        result = calculate_observed_change(data, current, opening_equity="1000", closing_equity="1000",
                                           closing_cash="1000", closing_positions={})
        assert result["status"] == "unavailable"


def test_date_only_late_deposit_uses_journal_membership_not_invented_timestamp():
    row = {**cash("CSD", "20"), "date": "2026-09-28"}
    assert calculate([row], "1020")["net_change"] == "0"


def test_nonfinite_equity_is_not_reported():
    assert calculate([], "1000", "Infinity")["status"] == "unavailable"
