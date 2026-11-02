from decimal import Decimal
from types import SimpleNamespace

import pytest
from datetime import timedelta
from test_paper_short_pilot import setup, authorize, submit, order as request, NOW
from app.services import paper_short_pilot as pilot

from app.services.paper_short_performance import observed_short_performance, recorded_performance


def order(intent, quantity, price):
    return SimpleNamespace(symbol="AAPL", intent=intent, filled_quantity=Decimal(str(quantity)), filled_avg_price=price)


def test_no_fills_and_flat_account_is_observed_zero_not_verified_net():
    result = observed_short_performance([], 0)
    assert Decimal(result["gross_pl"]) == 0
    assert result["net_pl"] is None and result["costs_verified"] is False
    assert result["filled_orders"] == 0 and result["status"] == "no_fills"


def test_open_sale_proceeds_are_not_counted_as_profit():
    opened = [order("open_short", 2, "100")]
    assert Decimal(observed_short_performance(opened, "200")["gross_pl"]) == 0
    assert Decimal(observed_short_performance(opened, "180")["gross_pl"]) == 20
    assert Decimal(observed_short_performance(opened, "240")["gross_pl"]) == -40


def test_partial_cover_combines_realized_and_marked_gross_without_double_counting():
    rows = [order("open_short", 2, "100"), order("cover_short", 1, "90")]
    result = observed_short_performance(rows, "95")
    assert Decimal(result["gross_pl"]) == 15
    assert result["filled_orders"] == 2
    assert Decimal(observed_short_performance(rows, "95")["gross_pl"]) == 15


def test_closed_round_trips_exclude_funding_and_keep_losses():
    rows = [order("open_short", 2, "100"), order("cover_short", 2, "90"),
            order("open_short", 1, "150"), order("cover_short", 1, "180")]
    assert Decimal(observed_short_performance(rows, 0)["gross_pl"]) == -10


def test_missing_market_mark_cannot_be_reported_as_profit():
    with pytest.raises(ValueError, match="liability"):
        observed_short_performance([order("open_short", 1, "100")], 0)
    with pytest.raises(ValueError, match="liability"):
        observed_short_performance([], 100)


@pytest.mark.parametrize("quantity,price,liability", [(1, None, 100), (1, "NaN", 100),
    (1, "0", 0), ("NaN", 100, 0), (-1, 100, 0), (1, 100, -100), (1, 100, "NaN")])
def test_invalid_profit_inputs_cannot_become_a_chart_value(quantity, price, liability):
    with pytest.raises((ValueError, ArithmeticError)):
        observed_short_performance([order("open_short", quantity, price)], liability)


def test_old_missing_and_invalid_snapshots_are_not_filled_with_zeros():
    first = observed_short_performance([], 0)
    rows = [SimpleNamespace(id=1, payload={"positions": {}}),
            SimpleNamespace(id=2, payload={"observed_at": "2026-11-02T17:00:00Z", "performance": first}),
            SimpleNamespace(id=3, payload={"observed_at": "2026-11-02T17:01:00Z", "performance": {**first, "gross_pl": "NaN"}})]
    result = recorded_performance(rows)
    assert result["points"] == [{"observed_at": "2026-11-02T17:00:00Z", "gross_pl": "0", "filled_orders": 0, "audit_event_id": 2}]
    assert result["net_pl"] is None and not result["costs_verified"]


def test_broker_reconciliation_persists_profit_and_loss_snapshots(setup):
    db, broker = setup
    authorize(db, broker)
    opened = submit(db, broker)
    broker.fill(opened["client_order_id"], price="100")
    pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
    broker.raw_positions[0]["market_value"] = "-90"
    pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW + timedelta(minutes=1))
    closed = submit(db, broker, request(intent="cover_short", limit_price="110", idempotency_key="performance-cover-001"))
    broker.fill(closed["client_order_id"], price="110")
    result = pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW + timedelta(minutes=2))
    points = result["performance"]["points"]
    assert [Decimal(p["gross_pl"]) for p in points] == [Decimal("0"), Decimal("10"), Decimal("-10")]
    assert result["performance"]["net_pl"] is None
    assert not result["performance"]["costs_verified"]
    assert all(p["audit_event_id"] for p in points)


def test_no_orders_creates_an_observed_flat_point_not_a_fake_trade(setup):
    db, broker = setup
    authorize(db, broker)
    result = pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
    assert result["orders"] == []
    assert Decimal(result["performance"]["latest"]["gross_pl"]) == 0
    assert result["performance"]["latest"]["filled_orders"] == 0
