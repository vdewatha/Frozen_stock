"""Reconciliation imports the full broker history; its query count must not grow per order."""
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  register all tables
from app.core.config import settings
from app.db.base import Base
from app.models.stock_paper import StockPaperBrokerActivity, StockPaperFill, StockPaperOrder
from app.services import stock_paper_ledger as ledger
from tests.test_stock_paper_ledger import FakeAlpaca


def _history(count):
    orders = [{"id": f"o{i}", "client_order_id": f"c{i}", "symbol": "SPY", "side": "buy" if i % 2 == 0 else "sell",
               "qty": "1", "filled_qty": "1", "type": "limit", "limit_price": "100", "time_in_force": "day",
               "status": "filled", "order_class": "simple", "created_at": "2026-01-01T15:00:00Z"}
              for i in range(count)]
    fills = [{"id": f"a{i}", "activity_type": "FILL", "symbol": "SPY", "side": row["side"], "qty": "1",
              "price": "100", "order_id": row["id"], "transaction_time": "2026-01-01T15:00:00Z", "commission": "0"}
             for i, row in enumerate(orders)]
    return orders, fills


def _reconcile_queries(count):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    orders, fills = _history(count)
    with patch.object(settings, "active_paper_broker", "alpaca_paper"), \
            patch.object(settings, "paper_broker_account_id", ""), \
            Session(engine, autoflush=False) as db:  # SessionLocal disables autoflush too
        gateway = FakeAlpaca(orders=orders, activities=fills)
        ledger.initialize_stock_paper_account(db, gateway)
        assert db.query(StockPaperOrder).count() == count
        assert db.query(StockPaperFill).count() == count
        assert db.query(StockPaperBrokerActivity).count() == count
        statements = []
        event.listen(engine, "before_cursor_execute", lambda *args: statements.append(1))
        result = ledger.reconcile_stock_paper_account(db, gateway)
        assert result["status"] == "reconciled"
        # Re-importing the same history created no duplicates.
        assert db.query(StockPaperOrder).count() == count
        assert db.query(StockPaperFill).count() == count
    engine.dispose()
    return len(statements)


def test_reconciliation_queries_do_not_grow_with_order_history():
    small, large = _reconcile_queries(10), _reconcile_queries(150)
    # Was ~4 queries per order+fill (639 for 150); now a fixed set of bulk lookups.
    assert large < 80, large
    assert large - small < 15, (small, large)
