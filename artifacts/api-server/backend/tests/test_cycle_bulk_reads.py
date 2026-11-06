"""The order phase reads positions and fills once per cycle, not per symbol."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  register all tables
from app.db.base import Base
from app.models import Strategy
from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder, StockPaperPosition
from app.services import stock_paper_autotrader as autotrader
from app.services.stock_paper_ledger import active_paper_broker_name

SYMBOLS = [f"S{i}" for i in range(12)]
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(StockPaperAccount(
            broker=active_paper_broker_name(), broker_account_id="paper-test", activity_contract="legacy-v1",
            cash=Decimal("10000"), buying_power=Decimal("10000"), equity=Decimal("10000"), status="reconciled",
            raw_payload={}))
        strategy = Strategy(name="RSI", strategy_type="rsi_mean_reversion", parameters={}, is_active=True,
                            current_status="research")
        session.add(strategy)
        session.flush()
        for index, symbol in enumerate(SYMBOLS):
            order = StockPaperOrder(account_id=1, client_order_id=f"c{index}", broker_order_id=f"b{index}",
                                    symbol=symbol, side="buy", order_type="limit", quantity=Decimal("2"),
                                    status="filled", source="scheduled_paper_signal", raw_payload={},
                                    strategy_id=strategy.id)
            session.add(order)
            session.flush()
            session.add(StockPaperFill(account_id=1, order_id=order.id, broker_activity_id=f"a{index}",
                                       broker_order_id=f"b{index}", symbol=symbol, side="buy",
                                       quantity=Decimal("2"), price=Decimal("100"), fee=Decimal("0"),
                                       cost_known=True, filled_at=NOW, raw_payload={}))
            session.add(StockPaperPosition(account_id=1, symbol=symbol, quantity=Decimal("2"),
                                           average_entry_price=Decimal("100"), observed_at=NOW, raw_payload={}))
        session.commit()
        session.type_by_id = {strategy.id: "rsi_mean_reversion"}
        yield session


def _queries(db, work):
    count = []
    listener = lambda *args: count.append(1)  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", listener)
    try:
        work()
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", listener)
    return len(count)


def _every_symbol(db):
    for symbol in SYMBOLS:
        assert autotrader._position(db, 1, symbol).quantity == Decimal("2")
        holdings, attributed = autotrader._strategy_holdings(db, 1, symbol, db.type_by_id)
        assert holdings == {"rsi_mean_reversion": Decimal("2")} and attributed
        assert autotrader._lot_entry_times(db, 1, symbol, db.type_by_id)["buyers"] == {"rsi_mean_reversion"}


def test_inside_a_cycle_the_reads_do_not_grow_with_symbols(db):
    uncached = _queries(db, lambda: _every_symbol(db))
    db.info[autotrader.CYCLE_READS] = {}
    cached = _queries(db, lambda: _every_symbol(db))
    assert uncached >= 5 * len(SYMBOLS)  # position + fills + orders, twice over, per symbol
    assert cached == 3  # positions, fills, orders: once each


def test_the_cache_is_dropped_after_the_cycle(db, monkeypatch):
    seen = {}

    def inner(session):
        seen["reads"] = isinstance(session.info.get(autotrader.CYCLE_READS), dict)
        return {"status": "complete"}

    monkeypatch.setattr(autotrader, "_run_signal_cycle", inner)
    autotrader.run_stock_paper_signal_cycle(db)
    assert seen["reads"] is True and autotrader.CYCLE_READS not in db.info
