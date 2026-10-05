from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.models import StockPaperLedgerEvent
from app.services.paper_cash_policy import MONETARY_REVIEW_REASON
from app.services.paper_research_accounting import TRANSPORT_HALT, assess
from app.services.paper_transport_recovery import review
from app.services.stock_paper_ledger import PROBE_HALT, _event, reconcile_stock_paper_account
from tests.test_paper_research_accounting import ledger, reviewed_ledger, ready


@pytest.fixture
def recovered(ready, reviewed_ledger):
    db, account = ready
    _, broker = reviewed_ledger
    account.halt_reason = TRANSPORT_HALT
    _event(db, account, "reconcile", "unavailable", TRANSPORT_HALT)
    db.commit()
    for _ in range(2):
        reconcile_stock_paper_account(db, broker)
    account.raw_payload = {**account.raw_payload, "trading_blocked": False,
                           "account_blocked": False, "trade_suspended_by_user": False}
    db.commit()
    return db, account


def test_review_restores_only_prior_research_state(recovered):
    db, account = recovered
    assert not assess(db, account)["accounting_observation_ready"]
    result = review(db, actor="test-reviewer")
    assert account.halt_reason == TRANSPORT_HALT
    assert not result["execution_authorized"] and not result["halt_cleared"]
    review(db, actor="test-reviewer", apply=True)
    assert account.status == "halted" and account.halt_reason == MONETARY_REVIEW_REASON
    assert not account.costs_known and not account.accounting_verified
    assert assess(db, account)["accounting_observation_ready"]
    assert db.query(StockPaperLedgerEvent).filter_by(event_type="paper_transport_recovery_review").count() == 1


def test_restored_transport_does_not_discharge_outstanding_probe(recovered):
    db, account = recovered
    reservation = StockPaperLedgerEvent(account_id=account.id, event_type="paper_probe_reservation",
        status="reserved", payload={"run_id": "pending-probe", "symbol": "AAPL"})
    db.add(reservation)
    db.flush()
    preview = review(db, actor="test-reviewer")
    assert account.halt_reason == TRANSPORT_HALT
    assert preview["preserved_probe_reservation_id"] == reservation.id
    result = review(db, actor="test-reviewer", apply=True)
    assert account.halt_reason == PROBE_HALT
    assert not assess(db, account)["accounting_observation_ready"]
    assert not result["execution_authorized"] and not result["halt_cleared"]


@pytest.mark.parametrize("case", ["manual", "other_failure", "stale", "one_observation", "changed_journal",
                                  "unexplained", "live", "permission", "missing_review", "blank_actor"])
def test_incomplete_or_unrelated_recovery_is_rejected(recovered, monkeypatch, case):
    db, account = recovered
    observations = db.query(StockPaperLedgerEvent).filter_by(event_type="activity_reconciliation").order_by(
        StockPaperLedgerEvent.id.desc()).all()
    if case == "manual":
        account.halt_reason = "Operator stop"
    elif case == "other_failure":
        _event(db, account, "manual_halt", "halted", "Operator stop")
    elif case == "stale":
        observations[1].created_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    elif case == "one_observation":
        db.delete(observations[1])
    elif case == "changed_journal":
        observations[1].payload = {**observations[1].payload, "journal_sha256": "changed"}
    elif case == "unexplained":
        account.unexplained_residual = True
    elif case == "live":
        monkeypatch.setattr(settings, "allow_live_trading", True)
    elif case == "permission":
        account.raw_payload = {**account.raw_payload, "trading_blocked": True}
    elif case == "missing_review":
        db.delete(db.query(StockPaperLedgerEvent).filter_by(event_type="monetary_residual_review").one())
    db.flush()
    with pytest.raises(ValueError):
        review(db, actor=" " if case == "blank_actor" else "test-reviewer", apply=True)
    assert account.status == "halted" and account.halt_reason != MONETARY_REVIEW_REASON
    assert db.query(StockPaperLedgerEvent).filter_by(event_type="paper_transport_recovery_review").count() == 0
