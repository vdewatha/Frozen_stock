from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models import StockPaperAccount, StockPaperBrokerActivity, StockPaperLedgerEvent, StockPaperOrder, StockPaperRecoveryState
from app.services.paper_cash_policy import CENT, EXACT, MONETARY_REVIEW_REASON, apply_policy, review_cent_residual
from app.services.stock_paper_ledger import ACCOUNTING_RESIDUAL_REVIEW_REASON, PROBE_HALT
from tests.test_alpaca_cash_precision import evidence
from tests.test_paper_cash_policy_adoption import db


@pytest.fixture
def prepared(db):
    account = db.query(StockPaperAccount).one()
    account.cash_policy = CENT
    account.halt_reason = ACCOUNTING_RESIDUAL_REVIEW_REASON
    db.add(StockPaperRecoveryState(id=1, status="cooldown", accounting_review_required=True))
    _, rows, orders = evidence()
    for row in rows:
        db.add(StockPaperBrokerActivity(account_id=account.id, broker_activity_id=row["id"],
                                       activity_type="FILL", raw_payload=row))
    for order in orders:
        db.add(StockPaperOrder(account_id=account.id, client_order_id=order["id"], broker_order_id=order["id"],
            symbol=order["symbol"], side=order["side"], quantity=order["filled_qty"], order_type="market",
            time_in_force="day", reserved_cash=0, status="filled", source="broker_import", raw_payload=order))
    event = db.query(StockPaperLedgerEvent).one()
    event.payload = apply_policy(event.payload, event.payload["cash_precision_diagnostic"], CENT,
                                 broker="alpaca_paper", currency="USD")
    db.commit()
    return db


def test_review_replays_raw_evidence_and_preserves_other_controls(prepared):
    db = prepared
    account = db.query(StockPaperAccount).one()
    baseline = deepcopy(account.activity_baseline)
    journal = [deepcopy(row.raw_payload) for row in db.query(StockPaperBrokerActivity).all()]
    preview = review_cent_residual(db, actor="reviewer")
    assert preview["residual_was_unexplained"] and account.unexplained_residual
    for _ in range(2):
        result = review_cent_residual(db, actor="reviewer", apply=True)
        db.commit()
    assert not account.unexplained_residual
    assert account.status == "halted" and account.reconciliation_required
    assert account.halt_reason == MONETARY_REVIEW_REASON
    assert not account.accounting_verified and not account.costs_known
    recovery = db.get(StockPaperRecoveryState, 1)
    assert recovery.accounting_review_required and recovery.status == "cooldown"
    assert recovery.accounting_reviewed_by is None
    assert account.activity_baseline == baseline
    assert [row.raw_payload for row in db.query(StockPaperBrokerActivity).all()] == journal
    assert not result["launch_authorized"] and not result["recovery_review_cleared"]
    assert db.query(StockPaperLedgerEvent).filter_by(event_type="monetary_residual_review").count() == 1


@pytest.mark.parametrize("cash", ["99999.97", "99999.98", "99999.96"])
def test_delayed_fee_batch_requires_exact_cash_and_new_journal_review(prepared, cash):
    from app.services.alpaca_activity_v2 import reconcile_baseline
    from app.services.alpaca_cash_precision import diagnose

    db = prepared
    account = db.query(StockPaperAccount).one()
    event = db.query(StockPaperLedgerEvent).one()
    prior_hash = event.payload["journal_sha256"]
    for number in range(3):
        raw = {"id": f"delayed-fee-{number}", "activity_type": "FEE",
               "date": "2026-09-29", "net_amount": "-0.01"}
        db.add(StockPaperBrokerActivity(account_id=account.id, broker_activity_id=raw["id"],
                                       activity_type="FEE", raw_payload=raw))
    account.cash = Decimal(cash)
    db.flush()
    activities = db.query(StockPaperBrokerActivity).all()
    raw = [row.raw_payload for row in activities]
    orders = [row.raw_payload for row in db.query(StockPaperOrder).all()]
    replay = reconcile_baseline(account.activity_baseline, raw, account.cash, {},
                                previously_seen_ids={row.broker_activity_id for row in activities})
    diagnostic = diagnose(account.activity_baseline, raw, orders, account.cash, {},
                          broker=account.broker, currency=account.currency)
    event.payload = {**apply_policy(replay, diagnostic, CENT, broker=account.broker, currency=account.currency),
                     "cash_precision_diagnostic": diagnostic}
    db.flush()
    assert event.payload["journal_sha256"] != prior_hash
    if cash != "99999.97":
        with pytest.raises(ValueError):
            review_cent_residual(db, actor="fee-review", apply=True)
        assert account.unexplained_residual
    else:
        result = review_cent_residual(db, actor="fee-review", apply=True)
        assert result["journal_sha256"] == event.payload["journal_sha256"]
        assert not account.unexplained_residual
    assert account.status == "halted"
    assert not account.costs_known and not account.accounting_verified
    assert db.get(StockPaperRecoveryState, 1).accounting_review_required


@pytest.mark.parametrize("reason", [ACCOUNTING_RESIDUAL_REVIEW_REASON, MONETARY_REVIEW_REASON, PROBE_HALT])
def test_monetary_review_preserves_reserved_probe_even_if_reason_was_overwritten(prepared, reason):
    db = prepared
    account = db.query(StockPaperAccount).one()
    account.halt_reason = reason
    reservation = StockPaperLedgerEvent(account_id=account.id, event_type="paper_probe_reservation",
        status="reserved", reason=PROBE_HALT, payload={"run_id": "probe-test", "symbol": "AAPL"})
    db.add(reservation)
    db.commit()
    preview = review_cent_residual(db, actor="reviewer")
    assert account.halt_reason == reason and account.unexplained_residual
    assert preview["preserved_probe_reservation_id"] == reservation.id
    for _ in range(2):
        review_cent_residual(db, actor="reviewer", apply=True)
        db.commit()
    assert account.halt_reason == PROBE_HALT and account.status == "halted"
    assert not account.unexplained_residual
    assert not account.accounting_verified and not account.costs_known
    assert db.get(StockPaperRecoveryState, 1).accounting_review_required
    assert db.query(StockPaperLedgerEvent).filter_by(event_type="monetary_residual_review").count() == 1


def test_probe_reason_without_reservation_is_never_downgraded(prepared):
    account = prepared.query(StockPaperAccount).one()
    account.halt_reason = PROBE_HALT
    review_cent_residual(prepared, actor="reviewer", apply=True)
    assert account.halt_reason == PROBE_HALT


@pytest.mark.parametrize("case", ["wrong_policy", "other_halt", "cash", "raw_fill", "missing_fill",
                                  "raw_order", "uncertain", "newer_failure", "stale", "report"])
def test_review_rejects_changed_or_incomplete_evidence(prepared, case):
    db = prepared
    account = db.query(StockPaperAccount).one()
    event = db.query(StockPaperLedgerEvent).one()
    row = db.query(StockPaperBrokerActivity).first()
    order = db.query(StockPaperOrder).first()
    if case == "wrong_policy":
        account.cash_policy = EXACT
    elif case == "other_halt":
        account.halt_reason = "Corporate action requires review"
    elif case == "cash":
        account.cash -= 1
    elif case == "raw_fill":
        row.raw_payload = {**row.raw_payload, "price": "700"}
    elif case == "missing_fill":
        db.delete(row)
    elif case == "raw_order":
        order.raw_payload = {**order.raw_payload, "filled_qty": "1"}
    elif case == "uncertain":
        order.uncertain_submission = True
    elif case == "newer_failure":
        db.add(StockPaperLedgerEvent(account_id=account.id, event_type="reconcile", status="unavailable"))
    elif case == "stale":
        account.last_reconciled_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    elif case == "report":
        event.payload = {**event.payload, "journal_sha256": "altered"}
    db.flush()
    with pytest.raises(ValueError):
        review_cent_residual(db, actor="reviewer", apply=True)
    assert account.unexplained_residual
    assert not db.query(StockPaperLedgerEvent).filter_by(event_type="monetary_residual_review").count()
