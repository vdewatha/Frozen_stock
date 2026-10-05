from copy import deepcopy

import pytest

from app.services.alpaca_activity_v2 import observed_baseline
from app.services.alpaca_cash_precision import diagnose


def evidence():
    baseline = observed_baseline([], "100000", {}, "2026-09-29T19:00:00Z")
    rows = [{"id": side, "activity_type": "FILL", "symbol": "SPY", "order_id": side,
             "side": side, "qty": "0.013060394", "price": price,
             "transaction_time": "2026-09-29T19:07:49Z"}
            for side, price in [("buy", "764.908"), ("sell", "764.902")]]
    orders = [{"id": r["order_id"], "symbol": "SPY", "side": r["side"], "status": "filled",
               "filled_qty": r["qty"], "filled_avg_price": r["price"]} for r in rows]
    return baseline, rows, orders


def run(baseline, rows, orders, cash="100000", positions=None):
    return diagnose(baseline, rows, orders, cash, positions or {}, currency="USD", broker="alpaca_paper")


def test_observed_roundtrip_explained_without_qualification_or_mutation():
    data = evidence()
    original = deepcopy(data)
    report = run(*data)
    assert report["status"] == "consistent"
    assert report["rounding_adjustment_since_baseline"] == "0.000078362364"
    assert report["cash_residual_after_rounding"] == "0.000000000000"
    assert not report["costs_verified"] and not report["qualifying"] and not report["launch_authorized"]
    assert data == original


def test_cannot_hide_a_cent_or_inventory_difference():
    assert run(*evidence(), cash="99999.99")["status"] == "mismatch"
    assert run(*evidence(), positions={"SPY": "0.000000001"})["status"] == "mismatch"


@pytest.mark.parametrize("change", [{"status": "partially_filled"}, {"filled_qty": "0.013"},
                                    {"symbol": "QQQ"}, {"side": "sell"}, {"filled_avg_price": "700"}])
def test_missing_or_conflicting_order_evidence_rejected(change):
    baseline, rows, orders = evidence()
    orders[0].update(change)
    assert run(baseline, rows, orders)["status"] == "unavailable"


def test_missing_duplicate_orders_and_conflicting_activities_rejected():
    baseline, rows, orders = evidence()
    assert run(baseline, rows, orders[1:])["status"] == "unavailable"
    assert run(baseline, rows, orders + orders)["status"] == "unavailable"
    assert run(baseline, rows + [{**rows[0], "price": "700"}], orders)["status"] == "unavailable"
    assert run(baseline, rows + rows, orders) == run(baseline, rows, orders)


@pytest.mark.parametrize("price", ["1.005", "1.015"])
def test_half_cent_rounding_ambiguity_rejected(price):
    baseline, rows, orders = evidence()
    rows = [{**rows[0], "qty": "1", "price": price}]
    orders = [{**orders[0], "filled_qty": "1", "filled_avg_price": price}]
    assert run(baseline, rows, orders)["status"] == "unavailable"


def test_missing_fill_for_reported_filled_order_rejected():
    baseline, rows, orders = evidence()
    assert run(baseline, rows[:1], orders)["status"] == "unavailable"


def test_partial_fill_rounding_ambiguity_rejected():
    baseline, rows, orders = evidence()
    orders = orders[:1]
    rows = [{**rows[0], "id": name, "qty": "1", "price": "1.004"} for name in ("a", "b")]
    orders[0].update(filled_qty="2", filled_avg_price="1.004")
    report = run(baseline, rows, orders)
    assert report["status"] == "unavailable" and "Per-fill" in report["reason"]


def test_frozen_baseline_adjustment_is_subtracted_and_cash_fees_preserved():
    _, rows, orders = evidence()
    baseline = observed_baseline(rows[:1], "99990.01", {"SPY": "0.013060394"}, "2026-09-29T19:07:49Z")
    rows.append({"id": "fee", "activity_type": "FEE", "date": "2026-09-29", "net_amount": "-0.01"})
    assert run(baseline, rows, orders, cash="99999.99")["status"] == "consistent"
    assert run(baseline, rows, orders)["status"] == "mismatch"


@pytest.mark.parametrize("currency,broker", [("EUR", "alpaca_paper"), ("USD", "tradier_sandbox"), ("USD", "live")])
def test_only_paper_usd_scope(currency, broker):
    baseline, rows, orders = evidence()
    assert diagnose(baseline, rows, orders, "100000", {}, currency=currency, broker=broker)["status"] == "unavailable"
