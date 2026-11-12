"""2026-11-11: the QQQ cost probe's sell was refused 51 times with "Sell would create a short position".

The exit re-arm had put an OCO on the probe's share on 11-10, before it learned
to skip probes.  Those resting legs commit the share, so the probe could never
sell and never measured a round trip.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import cost_probes
from app.services import stock_paper_ledger as ledger

UTC = timezone.utc
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)          # 10:00 ET, the day after the probe bought


def _probe(sell=None):
    return {"symbol": "QQQ", "buy_key": "cost-probe:2026-11-10:QQQ:buy", "sell_key": "cost-probe:2026-11-10:QQQ:sell",
            "buy_order_id": 7, "buy": SimpleNamespace(id=7, status="filled"), "sell": sell,
            "recorded": False, "closed_elsewhere": False}


class Ledger:
    """The ledger calls ``_close_probe`` makes, with the short-position rule the real reservation applies."""

    def __init__(self, *, held="1", legs=1, cancel=None):
        self.held = Decimal(held)
        self.legs = [SimpleNamespace(id=100 + i, status="accepted", leg_role="take_profit") for i in range(legs)]
        self.cancel = cancel or {"cancelled_order_ids": [leg.id for leg in self.legs], "still_live_order_ids": [],
                                 "leg_filled": False, "clear": True}
        self.reserved, self.dispatched, self.released, self.audits = [], [], [], []

    def reserve(self, db, **kwargs):
        committed = sum(1 for leg in self.legs if leg.id not in set(kwargs.get("replacing_exit_order_ids") or ()))
        if kwargs["quantity"] + committed > self.held:
            raise ledger.StockPaperError("Sell would create a short position")
        self.reserved.append(kwargs)
        return SimpleNamespace(id=55)

    def patches(self):
        fill = {"quantity": Decimal("1"), "filled_at": NOW - timedelta(days=1)}
        return [
            patch.object(cost_probes, "_fills", return_value=fill),
            patch.object(cost_probes, "_position_quantity", return_value=self.held),
            patch.object(cost_probes, "_latest_close", return_value=Decimal("650")),
            patch.object(cost_probes, "_audit", side_effect=lambda db, action, payload, message: self.audits.append(action)),
            patch.object(ledger, "live_exit_orders", side_effect=lambda db, account_id, symbol: list(self.legs)),
            patch.object(ledger, "reserve_stock_paper_order", side_effect=self.reserve),
            patch.object(ledger, "cancel_broker_exit_orders", side_effect=lambda db, orders: self.cancel),
            patch.object(ledger, "dispatch_reserved_order",
                         side_effect=lambda db, order_id: self.dispatched.append(order_id) or SimpleNamespace(id=order_id, status="accepted")),
            patch.object(ledger, "release_unsubmitted_reservation",
                         side_effect=lambda db, order_id, reason: self.released.append(order_id)),
        ]

    def close(self, probe=None):
        stack = self.patches()
        for item in stack:
            item.start()
        try:
            return cost_probes._close_probe(SimpleNamespace(commit=lambda: None), SimpleNamespace(id=1), probe or _probe(), NOW)
        finally:
            for item in stack:
                item.stop()


def test_the_probe_replaces_exit_legs_resting_on_its_own_share_and_sells():
    book = Ledger(held="1", legs=2)
    result = book.close()
    assert result["status"] == "sell_submitted" and book.dispatched == [55]
    assert book.reserved[0]["replacing_exit_order_ids"] == {100, 101} and book.audits == [cost_probes.ACTION_SELL]


def test_a_strategys_legs_on_a_larger_position_are_never_touched():
    """Two shares held, one of them a strategy's with its own stop: the probe must not strip that protection."""
    book = Ledger(held="2", legs=2)
    result = book.close()
    assert result == {"symbol": "QQQ", "status": "blocked", "reason": "Sell would create a short position"}
    assert book.reserved == [] and book.dispatched == []


def test_the_sell_waits_for_the_broker_to_confirm_the_cancel_then_dispatches():
    waiting = {"cancelled_order_ids": [100], "still_live_order_ids": [100], "leg_filled": False, "clear": False}
    book = Ledger(held="1", legs=1, cancel=waiting)
    result = book.close()
    assert result["status"] == "waiting_for_exit_cancel" and book.dispatched == [] and book.audits == []
    # Next tick: the reserved sell is still the open probe, and the cancel is now confirmed.
    reserved_sell = SimpleNamespace(id=55, status="reserved", broker_order_id=None)
    assert cost_probes._sell_awaits_dispatch(_probe(reserved_sell))
    assert not cost_probes._sell_awaits_dispatch(_probe(SimpleNamespace(id=55, status="accepted", broker_order_id="b1")))
    book.cancel = {"cancelled_order_ids": [], "still_live_order_ids": [], "leg_filled": False, "clear": True}
    assert book.close(_probe(reserved_sell))["status"] == "sell_submitted" and book.dispatched == [55]


def test_a_leg_that_already_sold_the_share_closes_the_probe_without_a_second_sell():
    filled = {"cancelled_order_ids": [], "still_live_order_ids": [], "leg_filled": True, "clear": False}
    book = Ledger(held="1", legs=1, cancel=filled)
    result = book.close()
    assert result == {"symbol": "QQQ", "status": "closed_elsewhere"}
    assert book.released == [55] and book.dispatched == [] and book.audits == [cost_probes.ACTION_CLOSED_ELSEWHERE]


def test_a_probe_with_no_resting_legs_sells_as_before():
    book = Ledger(held="1", legs=0)
    assert book.close()["status"] == "sell_submitted"
    assert "replacing_exit_order_ids" not in book.reserved[0]


@pytest.mark.parametrize("status,broker_id,expected", [("reserved", None, True), ("reserved", "b9", False),
                                                       ("filled", "b9", False)])
def test_only_an_unsubmitted_reservation_counts_as_awaiting_dispatch(status, broker_id, expected):
    sell = SimpleNamespace(id=1, status=status, broker_order_id=broker_id)
    assert cost_probes._sell_awaits_dispatch(_probe(sell)) is expected
