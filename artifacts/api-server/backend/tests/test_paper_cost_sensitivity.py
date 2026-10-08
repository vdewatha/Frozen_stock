from copy import deepcopy
from decimal import Decimal

import pytest

from app.services.alpaca_activity_v2 import observed_baseline
from app.services.paper_cost_sensitivity import evaluate as _evaluate


def evaluate(baseline, activities, positions):
    return _evaluate(baseline, activities, positions, currency="USD")


def evidence():
    rows = [{"id": side, "activity_type": "FILL", "symbol": "SPY", "order_id": side,
             "side": side, "qty": "2", "price": price, "transaction_time": "2026-09-29T19:00:00Z"}
            for side, price in [("buy", "100"), ("sell", "101")]]
    return observed_baseline([], "1000", {}, "2026-09-29T18:00:00Z"), rows


def test_costs_scale_with_both_sides_and_remain_hypotheses():
    baseline, rows = evidence()
    original = deepcopy((baseline, rows))
    report = evaluate(baseline, rows, {})
    assert report["status"] == "research_only"
    assert report["turnover"] == "402" and report["gross_fill_cash_change"] == "2"
    assert [Decimal(r["modeled_fill_cash_change"]) for r in report["scenarios"]] == [Decimal(v) for v in ("2", "1.9598", "1.799", "1.598")]
    assert report["fills_without_reported_commission"] == 2
    assert not report["costs_verified"] and not report["qualifying"] and not report["launch_authorized"]
    assert original == (baseline, rows)
    assert report == evaluate(baseline, list(reversed(rows)) + rows, {})


def test_reported_commissions_deducted_once():
    baseline, rows = evidence()
    rows = [{**r, "commission": "0.10"} for r in rows]
    report = evaluate(baseline, rows, {})
    assert report["reported_fee_subtotal"] == "0.20"
    assert Decimal(report["scenarios"][0]["modeled_fill_cash_change"]) == Decimal("1.8")
    assert not report["costs_verified"]


def test_transport_enrichment_does_not_change_economic_baseline():
    baseline, rows = evidence()
    enriched = [
        {**row, "activity_source": "alpaca_activity_sse",
         "activity_event_id": f"event-{row['id']}",
         "net_amount": "-200" if row["side"] == "buy" else "202"}
        for row in rows
    ]
    report = evaluate(baseline, enriched, {})
    assert report["status"] == "research_only"
    assert report["fills_without_reported_commission"] == 2


def test_separate_fees_included_but_funding_and_dividends_not_trade_profit():
    baseline, rows = evidence()
    for kind, value in [("FEE", "-0.2"), ("CSD", "1000"), ("DIV", "20")]:
        rows.append({"id": kind, "activity_type": kind, "date": "2026-09-29", "net_amount": value})
    report = evaluate(baseline, rows, {})
    assert Decimal(report["scenarios"][0]["modeled_fill_cash_change"]) == Decimal("1.8")
    assert report["excluded_nonfee_cash_flows"]


@pytest.mark.parametrize("case", ["open", "unbalanced", "mixed_fees", "changed", "missing", "digest", "empty", "nonfinite"])
def test_unsupported_or_corrupted_evidence_never_produces_scenarios(case):
    baseline, rows = evidence()
    positions = {}
    if case == "open":
        positions = {"SPY": "1"}
    elif case == "unbalanced":
        rows[1]["qty"] = "1"
    elif case == "mixed_fees":
        rows[0]["commission"] = "0.1"
        rows.append({"id": "fee", "activity_type": "FEE", "date": "2026-09-29", "net_amount": "-0.1"})
    elif case in {"changed", "missing"}:
        baseline = observed_baseline(rows, "1002", {}, "2026-09-29T19:01:00Z")
        if case == "changed":
            rows[0]["price"] = "99"
        else:
            rows.pop()
    elif case == "digest":
        baseline["journal_sha256"] = "broken"
    elif case == "empty":
        rows = []
    elif case == "nonfinite":
        rows[0]["price"] = "NaN"
    report = evaluate(baseline, rows, positions)
    assert report["status"] == "unavailable" and report["scenarios"] == []
    assert not report["qualifying"]


def test_baseline_fills_do_not_count_twice_and_each_symbol_must_close():
    _, old = evidence()
    baseline = observed_baseline(old, "1002", {}, "2026-09-29T19:01:00Z")
    rows = old + [{**r, "id": "new-" + r["id"], "order_id": "new-" + r["order_id"], "symbol": "QQQ"} for r in old]
    report = evaluate(baseline, rows, {})
    assert report["new_fill_count"] == 2 and report["symbols"] == ["QQQ"]
    rows[-1]["symbol"] = "AAPL"
    assert evaluate(baseline, rows, {})["status"] == "unavailable"


def test_currency_and_contract_scope_are_not_assumed():
    baseline, rows = evidence()
    assert _evaluate(baseline, rows, {}, currency="EUR")["status"] == "unavailable"
    baseline["version"] = "unknown"
    assert evaluate(baseline, rows, {})["status"] == "unavailable"
