from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.models import StockPaperAccount, StockPaperFill, StockPaperLedgerEvent, StockPaperOrder
from app.services.paper_research_accounting import assess
from app.services.stock_paper_ledger import stock_paper_status
from tests.test_alpaca_activity_ledger import ledger, reviewed_ledger


@pytest.fixture
def ready(reviewed_ledger):
    db, broker = reviewed_ledger
    account = db.query(StockPaperAccount).one()
    account.raw_payload = {**account.raw_payload, "trading_blocked": False,
                           "account_blocked": False, "trade_suspended_by_user": False}
    db.commit()
    return db, account


def test_missing_commissions_allow_observed_accounting_but_never_verified_costs_or_approval(ready):
    db, account = ready
    baseline = deepcopy(account.activity_baseline)
    result = assess(db, account)
    assert result["accounting_observation_ready"], result
    assert result["status"] == "observed_ready"
    assert result["cost_policy"]["fills_without_reported_commission"] == 2
    assert result["cost_policy"]["missing_fees_treated_as_zero"] is False
    assert not result["costs_verified"] and not result["launch_authorized"] and not result["live_authorized"]
    assert account.status == "halted" and not account.costs_known and not account.accounting_verified
    assert account.activity_baseline == baseline
    assert account.broker_account_id not in str(result)
    assert assess(db, account) == result
    status = stock_paper_status(db)
    assert status["paper_research_accounting"] == result
    assert status["status"] == "halted" and not status["costs_known"]


@pytest.mark.parametrize("case", ["live", "stale", "future", "permission", "identity", "raw_cash",
    "unexplained", "reconcile_required", "pending", "uncertain", "order_quantity", "fill_quantity",
    "fill_link", "invented_fee", "report_hash", "report_stale", "missing_fill", "baseline"])
def test_invalid_or_incomplete_evidence_remains_blocked(ready, monkeypatch, case):
    db, account = ready
    order = db.query(StockPaperOrder).first()
    fill = db.query(StockPaperFill).first()
    report = db.query(StockPaperLedgerEvent).filter_by(event_type="activity_reconciliation").order_by(StockPaperLedgerEvent.id.desc()).first()
    if case == "live":
        monkeypatch.setattr(settings, "allow_live_trading", True)
    elif case in {"stale", "future"}:
        account.last_reconciled_at = datetime.now(timezone.utc) + timedelta(minutes=-3 if case == "stale" else 1)
    elif case == "permission":
        account.raw_payload = {**account.raw_payload, "trading_blocked": True}
    elif case == "identity":
        account.raw_payload = {**account.raw_payload, "id": "other"}
    elif case == "raw_cash":
        account.raw_payload = {**account.raw_payload, "cash": "2"}
    elif case == "unexplained":
        account.unexplained_residual = True
    elif case == "reconcile_required":
        account.reconciliation_required = True
    elif case == "pending":
        order.status = "new"
    elif case == "uncertain":
        order.uncertain_submission = True
    elif case == "order_quantity":
        order.quantity += 1
    elif case == "fill_quantity":
        fill.quantity += 1
    elif case == "fill_link":
        fill.order_id = None
    elif case == "invented_fee":
        fill.fee = 0
        fill.cost_known = True
    elif case == "report_hash":
        report.payload = {**report.payload, "journal_sha256": "changed"}
    elif case == "report_stale":
        report.created_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    elif case == "missing_fill":
        db.delete(fill)
    elif case == "baseline":
        account.activity_baseline = {**account.activity_baseline, "journal_sha256": "changed"}
    db.flush()
    result = assess(db, account)
    assert result["status"] == "blocked", (case, result)
    assert not result["accounting_observation_ready"] and not result["launch_authorized"]
