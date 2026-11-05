"""Unfilled app-side exit sells are cancelled and re-issued under numbered keys."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  register all tables
from app.db.base import Base
from app.models.stock_paper import StockPaperAccount, StockPaperOrder
from app.services import entry_protections, stock_paper_autotrader as autotrader
from app.services.stock_paper_ledger import StockPaperError, active_paper_broker_name, cancel_aged_open_exits

UTC = timezone.utc
NOW = datetime(2026, 11, 5, 16, 40, tzinfo=UTC)  # 11:40 New York
OPEN = (datetime(2026, 11, 5, 14, 30, tzinfo=UTC), datetime(2026, 11, 5, 21, 0, tzinfo=UTC))


class _Gateway:
    def __init__(self):
        self.cancelled = []

    def cancel_order(self, broker_order_id):
        self.cancelled.append(broker_order_id)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(StockPaperAccount(
            broker=active_paper_broker_name(), broker_account_id="paper-test", activity_contract="legacy-v1",
            cash=Decimal("10000"), buying_power=Decimal("10000"), equity=Decimal("10000"), status="reconciled",
            raw_payload={}))
        session.commit()
        yield session


def _order(db, client, *, side="sell", status="new", minutes_ago=20, source=autotrader.SCHEDULED_SOURCE,
           created=None, **extra):
    order = StockPaperOrder(
        account_id=1, client_order_id=client, broker_order_id=extra.pop("broker_order_id", f"b-{client}"),
        symbol="AAPL", side=side, order_type="limit", quantity=Decimal("4"), limit_price=Decimal("352.76"),
        status=status, source=source, raw_payload={}, submitted_at=NOW - timedelta(minutes=minutes_ago),
        created_at=created or NOW - timedelta(minutes=minutes_ago), **extra)
    db.add(order)
    db.commit()
    return order


def test_only_aged_autonomous_app_side_sells_are_cancelled(db):
    stale = _order(db, "stale")
    young = _order(db, "young", minutes_ago=3)
    leg = _order(db, "leg", status="held", leg_role="stop_loss")
    manual = _order(db, "manual", source="manual_close")
    buy = _order(db, "buy", side="buy")
    partial = _order(db, "partial", status="partially_filled")
    gateway = _Gateway()
    with patch("app.services.stock_paper_ledger.session_bounds", return_value=OPEN):
        cancelled = cancel_aged_open_exits(db, db.get(StockPaperAccount, 1), NOW, gateway)
    assert cancelled == [stale.id] and gateway.cancelled == ["b-stale"]
    assert db.get(StockPaperOrder, stale.id).status == "pending_cancel"
    for keep in (young, leg, manual, buy, partial):
        assert db.get(StockPaperOrder, keep.id).status == keep.status


def test_the_sweep_can_be_disabled(db):
    _order(db, "stale")
    with patch("app.services.stock_paper_ledger.session_bounds", return_value=OPEN):
        assert cancel_aged_open_exits(db, db.get(StockPaperAccount, 1), NOW, _Gateway(), max_open_minutes=0) == []


def test_a_cancelled_exit_is_reissued_under_the_next_numbered_key(db):
    base = "scheduled-paper:2026-11-05:AAPL:rsi_mean_reversion:sell"
    assert autotrader._sell_key(db, 1, "AAPL", base, NOW) == base
    _order(db, "c1", status="canceled")  # Alpaca's spelling after reconciliation
    assert autotrader._sell_key(db, 1, "AAPL", base, NOW) == base + ":retry1"
    _order(db, "c2", status="cancelled")
    _order(db, "y", status="cancelled", created=NOW - timedelta(days=1))  # earlier session: not counted
    _order(db, "legx", status="cancelled", leg_role="take_profit")      # broker leg: not counted
    assert autotrader._sell_key(db, 1, "AAPL", base, NOW) == base + ":retry2"


def test_reissues_stop_at_the_retry_limit(db):
    for n in range(entry_protections.SELL_RETRY_LIMIT + 1):
        _order(db, f"c{n}", status="cancelled")
    with pytest.raises(StockPaperError, match="re-issued"):
        autotrader._sell_key(db, 1, "AAPL", "scheduled-paper:2026-11-05:AAPL:x:sell", NOW)


def test_a_reissued_protective_stop_is_still_attributed_to_the_stop():
    key = entry_protections.sell_retry_key(entry_protections.protective_stop_idempotency_key("2026-11-05", "AAPL"), 3)
    client = entry_protections._client_order_id(key)
    assert entry_protections._is_protective_stop(client, "AAPL", NOW)
    assert not entry_protections._is_protective_stop(client, "MSFT", NOW)


def test_a_reissued_signal_sell_keeps_its_strategy_in_the_scorecard():
    from types import SimpleNamespace

    from app.services.paper_trade_scorecard import _ExitAttribution

    base = "scheduled-paper:2026-11-05:AAPL:rsi_mean_reversion:sell"
    order = SimpleNamespace(
        leg_role=None, source=autotrader.SCHEDULED_SOURCE, trial_id=None, symbol="AAPL",
        client_order_id=entry_protections._client_order_id(entry_protections.sell_retry_key(base, 2)),
        created_at=NOW, submitted_at=NOW, submission_attempted_at=NOW)
    assert _ExitAttribution(["rsi_mean_reversion"]).classify(order) == ("sell_signal", "rsi_mean_reversion")
