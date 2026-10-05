from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models import StockPaperAccount, StockPaperLedgerEvent, StockPaperOrder
from app.services.paper_cash_policy import EXACT, CENT, adopt_cent_policy
from tests.test_alpaca_cash_precision import evidence
from tests.test_paper_cash_policy import reports


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(settings, "allow_live_trading", False)
    monkeypatch.setattr(settings, "active_paper_broker", "alpaca_paper")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        now = datetime.now(timezone.utc)
        baseline, _, _ = evidence()
        account = StockPaperAccount(broker="alpaca_paper", broker_account_id="fixture", currency="USD",
            cash=100000, equity=100000, buying_power=100000, raw_payload={}, status="halted",
            activity_contract=baseline["version"], activity_baseline=baseline,
            unexplained_residual=True, last_reconciled_at=now)
        session.add(account)
        session.flush()
        report, diagnostic = reports()
        session.add(StockPaperLedgerEvent(account_id=account.id, event_type="activity_reconciliation",
            status="mismatch", actor="test", created_at=now,
            payload={**report, "cash_precision_diagnostic": diagnostic}))
        session.commit()
        yield session
    engine.dispose()


def test_preview_and_idempotent_adoption_preserve_halts_costs_and_baseline(db):
    account = db.query(StockPaperAccount).one()
    baseline = dict(account.activity_baseline)
    adopt_cent_policy(db, actor="test")
    assert account.cash_policy == EXACT
    for _ in range(2):
        adopt_cent_policy(db, actor="test", apply=True)
        db.commit()
    assert account.cash_policy == CENT
    assert account.status == "halted" and account.unexplained_residual
    assert not account.costs_known and not account.accounting_verified
    assert account.activity_baseline == baseline
    assert db.query(StockPaperLedgerEvent).filter_by(event_type="cash_policy_adopted").count() == 1


@pytest.mark.parametrize("case", ["stale", "future", "ambiguous", "baseline", "live", "pending", "actor"])
def test_unsupported_adoption_does_not_change_policy(db, monkeypatch, case):
    event = db.query(StockPaperLedgerEvent).one()
    account = db.query(StockPaperAccount).one()
    if case == "stale":
        event.created_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    elif case == "future":
        account.last_reconciled_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    elif case == "ambiguous":
        event.payload = {**event.payload, "cash_precision_diagnostic": {"status": "unavailable"}}
    elif case == "baseline":
        event.payload = {**event.payload, "baseline_journal_sha256": "changed"}
    elif case == "live":
        monkeypatch.setattr(settings, "allow_live_trading", True)
    elif case == "pending":
        db.add(StockPaperOrder(account_id=account.id, client_order_id="pending", symbol="SPY", side="buy",
            quantity=1, order_type="market", time_in_force="day", reserved_cash=0, status="new", source="broker_import"))
    db.flush()
    with pytest.raises(ValueError):
        adopt_cent_policy(db, actor="" if case == "actor" else "test", apply=True)
    assert account.cash_policy == EXACT
