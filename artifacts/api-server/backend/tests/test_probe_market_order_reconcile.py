"""2026-11-10: the first cost probe halted the paper account.

Probe orders are market orders that keep their decision price in
limit_price (the cost calibration's benchmark). Alpaca reports a market order
with no limit price, so reconciliation called the order "corrupted evidence"
and halted the account at 10:33 ET.
"""
from decimal import Decimal

import pytest

from app.models.stock_paper import StockPaperAccount, StockPaperOrder
from app.services.stock_paper_ledger import StockPaperError, _upsert_orders
from tests.test_alpaca_activity_ledger import ledger  # noqa: F401  (fixture)
from tests.test_alpaca_activity_ledger import initialize


def _local(db, account, order_type, limit):
    row = StockPaperOrder(account_id=account.id, client_order_id="sp-probe-1", broker_order_id="b-1", symbol="QQQ",
                          side="buy", quantity=Decimal("1"), order_type=order_type, time_in_force="day",
                          limit_price=Decimal(limit), reserved_cash=Decimal("0"), status="pending_new",
                          source="cost_probe")
    db.add(row)
    db.flush()
    return row


def _broker(order_type, limit=None, status="filled"):
    return {"id": "b-1", "client_order_id": "sp-probe-1", "symbol": "QQQ", "side": "buy", "qty": "1",
            "type": order_type, "time_in_force": "day", "status": status, "limit_price": limit}


def test_probe_market_order_reconciles_and_keeps_its_decision_price(ledger):  # noqa: F811
    db, broker = ledger
    initialize(db, broker)
    account = db.query(StockPaperAccount).one()
    order = _local(db, account, "market", "676.715")
    _upsert_orders(db, account, [_broker("market")])
    assert order.status == "filled"
    assert order.limit_price == Decimal("676.715")  # the calibration benchmark survives


def test_a_changed_limit_still_halts(ledger):  # noqa: F811
    db, broker = ledger
    initialize(db, broker)
    account = db.query(StockPaperAccount).one()
    _local(db, account, "limit", "676.71")
    with pytest.raises(StockPaperError, match="immutable fields changed"):
        _upsert_orders(db, account, [_broker("limit", "680.00")])


def test_a_market_order_that_became_a_limit_still_halts(ledger):  # noqa: F811
    db, broker = ledger
    initialize(db, broker)
    account = db.query(StockPaperAccount).one()
    _local(db, account, "market", "676.715")
    with pytest.raises(StockPaperError, match="immutable fields changed"):
        _upsert_orders(db, account, [_broker("limit", "676.715")])
