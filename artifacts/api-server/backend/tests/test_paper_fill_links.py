from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models
from app.db.base import Base
from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder
from app.services.stock_paper_ledger import StockPaperError, _upsert_orders, _upsert_fills

NOW = datetime(2026, 9, 29, 19, tzinfo=timezone.utc)
ORDER = {"id": "order-1", "client_order_id": "client-1", "symbol": "SPY", "side": "buy",
         "qty": "0.013060394", "type": "market", "time_in_force": "day", "status": "filled"}
FILL = {"id": "fill-1", "order_id": "order-1", "activity_type": "FILL", "symbol": "SPY",
        "side": "buy", "qty": "0.013060394", "price": "764.908", "transaction_time": NOW.isoformat()}


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        account = StockPaperAccount(broker_account_id="fixture", cash=1000, buying_power=1000,
                                    equity=1000, raw_payload={})
        session.add(account)
        session.commit()
        yield session
    engine.dispose()


def test_same_batch_links_fill_with_production_autoflush_setting(db):
    account = db.scalar(select(StockPaperAccount))
    _upsert_orders(db, account, [ORDER])
    _upsert_fills(db, account, [FILL], NOW)
    db.commit()
    fill = db.scalar(select(StockPaperFill))
    order = db.scalar(select(StockPaperOrder))
    assert fill.order_id == order.id
    assert str(fill.quantity) == "0.013060394"


@pytest.mark.parametrize("commission", [None, "0.01"])
def test_late_order_links_existing_fill_without_changing_evidence(db, commission):
    account = db.scalar(select(StockPaperAccount))
    _upsert_fills(db, account, [FILL], NOW)
    db.commit()
    assert db.scalar(select(StockPaperFill)).order_id is None
    _upsert_orders(db, account, [ORDER])
    db.commit()
    raw = FILL if commission is None else {**FILL, "commission": commission}
    for _ in range(2):
        _upsert_fills(db, account, [raw], NOW)
        db.commit()
        fill = db.scalar(select(StockPaperFill))
        assert fill.order_id == db.scalar(select(StockPaperOrder)).id
        assert fill.raw_payload == raw


@pytest.mark.parametrize("changes", [{"symbol": "QQQ"}, {"side": "sell"}])
def test_conflicting_order_identity_rejected(db, changes):
    account = db.scalar(select(StockPaperAccount))
    _upsert_orders(db, account, [ORDER])
    db.commit()
    with pytest.raises(StockPaperError):
        _upsert_fills(db, account, [{**FILL, **changes}], NOW)


def test_existing_link_cannot_be_reassigned(db):
    account = db.scalar(select(StockPaperAccount))
    _upsert_orders(db, account, [ORDER, {**ORDER, "id": "order-2", "client_order_id": "client-2"}])
    db.commit()
    _upsert_fills(db, account, [FILL], NOW)
    db.commit()
    other = db.scalar(select(StockPaperOrder).where(StockPaperOrder.broker_order_id == "order-2"))
    db.scalar(select(StockPaperFill)).order_id = other.id
    db.commit()
    with pytest.raises(StockPaperError):
        _upsert_fills(db, account, [FILL], NOW)
