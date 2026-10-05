from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.models import StockPaperAccount, StockPaperLedgerEvent, StockPaperOrder, StockPaperRecoveryState
from app.services.paper_cash_policy import MONETARY_REVIEW_REASON, review_cent_residual
from app.services.paper_probe_review import review, pending_probe_reservation
from app.services.paper_research_accounting import assess, _assess
from app.services.stock_paper_ledger import ALPACA_PAPER_URL, PROBE_HALT
from tests.test_alpaca_activity_ledger import ledger, reviewed_ledger
from tests.test_paper_research_accounting import ready
from tests.test_paper_roundtrip_probe import RUN


@pytest.fixture
def prepared(ready):
    db, account = ready
    account.halt_reason = PROBE_HALT
    events = [{"event": "probe_reserved", "run_id": RUN, "symbol": "SPY",
               "paper_only": True, "automatic_resume": False}]
    raw_orders = {}
    for order in db.query(StockPaperOrder).all():
        identifier = "qa-" + RUN.replace("-", "") + "-" + order.side
        order.client_order_id = identifier
        order.raw_payload = {**order.raw_payload, "client_order_id": identifier}
        raw_orders[identifier] = deepcopy(order.raw_payload)
        payload = {"client_order_id": identifier, "symbol": "SPY", "side": order.side,
                   "type": "market", "time_in_force": "day", "extended_hours": False}
        payload.update({"notional": "10"} if order.side == "buy" else {"qty": "0.013060394"})
        events.append({"event": "before_submit", "payload": payload})
    reservation = StockPaperLedgerEvent(account_id=account.id, event_type="paper_probe_reservation",
        status="reserved", reason=PROBE_HALT, payload={"run_id": RUN, "symbol": "SPY",
        "buy_notional_cap": "10", "paper_only": True, "live_authorized": False})
    db.add(reservation)
    db.commit()
    class Broker:
        base_url = ALPACA_PAPER_URL
        def account(self):
            return deepcopy(account.raw_payload)
        def positions(self):
            return []
        def orders(self):
            return list(deepcopy(raw_orders).values())
        def order_by_client_id(self, identifier):
            return deepcopy(raw_orders.get(identifier))
        def submit_order(self, *args):
            raise AssertionError("Review cannot submit")
        def cancel_order(self, *args):
            raise AssertionError("Review cannot cancel")
    return db, account, Broker(), events, reservation


def invoke(prepared, *, apply=False, content=None):
    db, _, broker, events, _ = prepared
    content = content if content is not None else "".join(json.dumps(row)+"\n" for row in events)
    return review(db, broker, content, run_id=RUN, symbol="SPY", actor="reviewer", apply=apply)


def test_completed_probe_preview_and_apply_never_grant_trading(prepared):
    db, account, _, _, reservation = prepared
    baseline = deepcopy(account.activity_baseline)
    recovery = db.get(StockPaperRecoveryState, 1)
    recovery_before = (recovery.status, recovery.accounting_review_required, recovery.accounting_reviewed_by)
    assert not assess(db, account)["accounting_observation_ready"]
    result = invoke(prepared)
    assert result["status"] == "preview" and account.halt_reason == PROBE_HALT
    assert pending_probe_reservation(db, account).id == reservation.id
    assert not db.query(StockPaperLedgerEvent).filter_by(event_type="paper_probe_completion_review").count()
    result = invoke(prepared, apply=True)
    db.commit()
    assert result["status"] == "reviewed" and result["report"]["orders"]
    assert account.halt_reason == MONETARY_REVIEW_REASON and account.status == "halted"
    assert pending_probe_reservation(db, account) is None
    assert not account.costs_known and not account.accounting_verified
    assert assess(db, account)["accounting_observation_ready"]
    assert account.activity_baseline == baseline
    assert (recovery.status, recovery.accounting_review_required, recovery.accounting_reviewed_by) == recovery_before
    account.halt_reason = "New operator halt"
    assert invoke(prepared, apply=True)["status"] == "already_reviewed"
    assert account.halt_reason == "New operator halt"
    assert db.query(StockPaperLedgerEvent).filter_by(event_type="paper_probe_completion_review").count() == 1


@pytest.mark.parametrize("case", ["stale", "residual", "required", "other_halt", "cash_changed",
    "missing_order", "pending_order", "ledger_mismatch", "position", "run", "symbol", "scope", "journal_reservation",
    "new_terminal_order", "duplicate_order", "permission_changed"])
def test_incomplete_or_changed_probe_cannot_be_reviewed(prepared, case):
    db, account, broker, events, reservation = prepared
    if case == "stale":
        account.last_reconciled_at = datetime.now(timezone.utc)-timedelta(minutes=3)
    elif case == "residual":
        account.unexplained_residual = True
    elif case == "required":
        account.reconciliation_required = True
    elif case == "other_halt":
        account.halt_reason = "Unrelated operator halt"
    elif case == "cash_changed":
        original = broker.account
        broker.account = lambda: {**original(), "cash": "900"}
    elif case in {"missing_order", "pending_order", "ledger_mismatch"}:
        original = broker.order_by_client_id
        def changed(identifier):
            row = original(identifier)
            if row is None or case == "missing_order":
                return None
            return {**row, **({"status": "new"} if case == "pending_order" else {"extra_field": "changed"})}
        broker.order_by_client_id = changed
    elif case == "position":
        broker.positions = lambda: [{"symbol": "SPY", "qty": "1"}]
    elif case in {"new_terminal_order", "duplicate_order"}:
        original = broker.orders
        broker.orders = lambda: original() + [{**original()[0], **({"id": "new-terminal"} if case == "new_terminal_order" else {})}]
    elif case == "permission_changed":
        original = broker.account
        broker.account = lambda: {**original(), "trading_blocked": True}
    elif case in {"run", "symbol", "scope"}:
        key, value = {"run": ("run_id", "other"), "symbol": ("symbol", "AAPL"), "scope": ("live_authorized", True)}[case]
        reservation.payload = {**reservation.payload, key: value}
    else:
        events[0]["symbol"] = "AAPL"
    db.flush()
    with pytest.raises((ValueError, RuntimeError)):
        invoke(prepared, apply=True)
    assert not db.query(StockPaperLedgerEvent).filter_by(event_type="paper_probe_completion_review").count()
    assert account.status == "halted" and not account.costs_known


@pytest.mark.parametrize("content", ["", "{}", "{}\nBROKEN\n", "[]\n"])
def test_incomplete_journal_rejected(prepared, content):
    with pytest.raises((ValueError, RuntimeError)):
        invoke(prepared, content=content, apply=True)


def test_corrupted_completion_cannot_discharge_reservation(prepared):
    db, account, _, _, reservation = prepared
    invoke(prepared, apply=True)
    row = db.query(StockPaperLedgerEvent).filter_by(event_type="paper_probe_completion_review").one()
    row.payload = {**row.payload, "journal_sha256": "changed"}
    db.flush()
    assert pending_probe_reservation(db, account).id == reservation.id


def test_completed_probe_does_not_return_after_later_monetary_review(prepared):
    db, account, _, _, _ = prepared
    invoke(prepared, apply=True)
    db.commit()
    review_cent_residual(db, actor="monetary-review", apply=True)
    assert account.halt_reason == MONETARY_REVIEW_REASON
    assert pending_probe_reservation(db, account) is None


def test_new_reservation_is_not_discharged_by_old_completion(prepared):
    db, account, _, _, reservation = prepared
    invoke(prepared, apply=True)
    newer = StockPaperLedgerEvent(account_id=account.id, event_type="paper_probe_reservation",
        status="reserved", payload={**reservation.payload, "run_id": "new-run"})
    db.add(newer)
    db.flush()
    assert pending_probe_reservation(db, account).id == newer.id


def test_internal_probe_scope_cannot_be_combined_with_transport_scope(prepared):
    db, account, *_ = prepared
    assert not _assess(db, account, probe_review=True, transport_review=True)["accounting_observation_ready"]
