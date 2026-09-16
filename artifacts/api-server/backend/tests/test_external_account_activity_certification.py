from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models
from app.core.config import settings
from app.db.base import Base
from app.models import CorporateAction, StockPaperRecoveryState
from app.models.stock_paper import (
    StockPaperAccount,
    StockPaperBrokerActivity,
    StockPaperFill,
    StockPaperLedgerEvent,
    StockPaperOrder,
    StockPaperPosition,
)
from app.services.stock_paper_ledger import (
    initialize_stock_paper_account,
    reconcile_stock_paper_account,
    stock_paper_status,
)
from app.services.stock_recovery import enter_stock_recovery


UTC = timezone.utc
OBSERVED = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def legacy_paper_broker(monkeypatch):
    monkeypatch.setattr(settings, "active_paper_broker", "alpaca_paper")


class ActivityGateway:
    def __init__(self, *, activities=None, positions=None, orders=None, cash="1000"):
        self.activity_rows = activities or []
        self.position_rows = positions or []
        self.order_rows = orders or []
        self.cash = cash
        self.lookup_calls = 0

    def account(self):
        return {
            "id": "paper-account",
            "currency": "USD",
            "status": "ACTIVE",
            "cash": self.cash,
            "buying_power": self.cash,
            "equity": self.cash,
            "last_equity": self.cash,
        }

    def positions(self):
        return self.position_rows

    def orders(self, after=None):
        return self.order_rows

    def fills(self, after=None):
        return self.activity_rows

    def order_by_client_id(self, client_order_id):
        self.lookup_calls += 1
        return None

    def submit_order(self, payload):
        raise AssertionError("Certification must not submit an order")

    def cancel_order(self, broker_order_id):
        raise AssertionError("Certification must not cancel an order")


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _activity(activity_type: str, activity_id: str = "activity-1") -> dict:
    return {
        "id": activity_id,
        "activity_type": activity_type,
        "transaction_time": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "symbol": "SPY",
        "net_amount": "1",
    }


@pytest.mark.parametrize(
    "activity_type",
    [
        "DIV",
        "SPLIT",
        "FEE",
        "TRANS",
        "CSD",
        "CSW",
        "DEPOSIT",
        "WITHDRAWAL",
        "JNLC",
        "MISC",
        "UNKNOWN",
    ],
)
def test_initial_external_activity_classes_are_review_blockers(db, activity_type):
    result = initialize_stock_paper_account(
        db,
        ActivityGateway(activities=[_activity(activity_type)]),
    )

    assert result["status"] == "halted"
    assert result["account"]["reconciliation_required"] is True
    assert result["account"]["unexplained_residual"] is True
    state = db.query(StockPaperRecoveryState).one()
    assert state.accounting_review_required is True
    event = db.query(StockPaperLedgerEvent).filter_by(event_type="initialize").one()
    assert activity_type in event.payload["activity_types"]
    assert result["account"]["accounting_verified"] is False
    assert result["costs_known"] is False


def test_external_terminal_order_and_fill_remain_unowned_by_strategy(db):
    initialize_stock_paper_account(db, ActivityGateway())
    gateway = ActivityGateway(
        positions=[{"symbol": "SPY", "qty": "1", "market_value": "100"}],
        orders=[{
            "id": "external-order-1",
            "client_order_id": "outside-app-1",
            "symbol": "SPY",
            "side": "buy",
            "qty": "1",
            "type": "market",
            "time_in_force": "day",
            "status": "filled",
        }],
        activities=[{
            "id": "external-fill-1",
            "activity_type": "FILL",
            "order_id": "external-order-1",
            "symbol": "SPY",
            "side": "buy",
            "qty": "1",
            "price": "100",
            "commission": "0",
            "transaction_time": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        }],
        cash="900",
    )

    result = reconcile_stock_paper_account(db, gateway)

    order = db.query(StockPaperOrder).filter_by(broker_order_id="external-order-1").one()
    fill = db.query(StockPaperFill).filter_by(broker_activity_id="external-fill-1").one()
    assert result["status"] == "reconciled"
    assert order.source == "broker_import"
    assert order.strategy_id is None
    assert fill.order_id == order.id
    assert result["account"]["accounting_verified"] is False
    assert result["positions"][0]["unrealized_pl"] is None


def test_corporate_action_updates_broker_snapshot_but_preserves_history_and_blocks_recovery(db):
    initialize_stock_paper_account(
        db,
        ActivityGateway(
            positions=[{"symbol": "SPY", "qty": "1", "market_value": "100"}],
        ),
    )
    enter_stock_recovery(db, reason="Pause before corporate action", actor="operator-test")
    db.add(CorporateAction(
        symbol="SPY",
        action_type="SPLIT",
        ex_date=date(2026, 9, 15),
        value=Decimal("2"),
        provider="alpaca",
        raw_payload={"id": "split-1"},
    ))
    db.commit()

    result = reconcile_stock_paper_account(
        db,
        ActivityGateway(
            positions=[{"symbol": "SPY", "qty": "2", "market_value": "200"}],
            cash="1000",
        ),
    )

    account = db.query(StockPaperAccount).one()
    position = db.query(StockPaperPosition).filter_by(symbol="SPY").one()
    assert result["status"] == "halted"
    assert "supported reported activity" in result["reason"].lower()
    assert position.quantity == Decimal("2")
    assert account.cash == Decimal("1000")
    assert db.query(StockPaperOrder).count() == 0
    assert db.query(StockPaperFill).count() == 0
    assert db.query(StockPaperBrokerActivity).count() == 0
    assert db.query(StockPaperRecoveryState).one().status == "cooldown"


def test_repeated_external_activity_does_not_duplicate_reconciliation_notice(db):
    initialize_stock_paper_account(db, ActivityGateway())
    gateway = ActivityGateway(activities=[_activity("DIV")], cash="1000")

    first = reconcile_stock_paper_account(db, gateway)
    first_count = db.query(StockPaperLedgerEvent).filter_by(event_type="reconcile").count()
    second = reconcile_stock_paper_account(db, gateway)
    second_count = db.query(StockPaperLedgerEvent).filter_by(event_type="reconcile").count()

    assert first["status"] == "halted"
    assert second["status"] == "halted"
    assert first_count == second_count == 1
    assert stock_paper_status(db)["account"]["unexplained_residual"] is True