"""Persistent provider-shaped reconciliation, without broker order authority."""
from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models
from app.api.stock_paper import router
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.models.stock_paper import StockPaperAccount, StockPaperBrokerActivity, StockPaperEquitySnapshot, StockPaperFill, StockPaperLedgerEvent, StockPaperOrder
from app.services.alpaca_activity_v2 import VERSION
from app.services.stock_paper_ledger import (
    ACCOUNTING_RESIDUAL_REVIEW_REASON,
    StockPaperError,
    _upsert_orders,
    initialize_stock_paper_account,
    reconcile_stock_paper_account,
    stock_paper_status,
)
from app.services.stock_recovery import attempt_automatic_stock_recovery


def cash_event(**changes):
    return {"id": "deposit", "activity_type": "CSD", "date": "2026-01-01", "net_amount": "1000", **changes}


def fill_event(**changes):
    return {"id": "fill", "activity_type": "FILL", "transaction_time": "2026-01-02T15:00:00Z",
            "symbol": "SPY", "side": "buy", "qty": "2", "price": "100", "order_id": "order", **changes}


class ReadOnlyBroker:
    def __init__(self):
        self.cash = "1000"
        self.rows = [cash_event()]
        self.position_rows = []
        self.order_rows = []
        self.account_id = "test-paper"

    def account(self):
        return {"id": self.account_id, "currency": "USD", "status": "ACTIVE", "cash": self.cash,
                "equity": "1000", "buying_power": "1000", "last_equity": "1000"}

    def positions(self):
        return deepcopy(self.position_rows)

    def orders(self, after=None):
        return deepcopy(self.order_rows)

    def fills(self, after=None):
        return deepcopy(self.rows)

    def order_by_client_id(self, client_id):
        return None


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "active_paper_broker", "alpaca_paper")
    monkeypatch.setattr(settings, "paper_broker_account_id", "")
    engine = create_engine("sqlite:///" + str(tmp_path / "ledger.sqlite"))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db, ReadOnlyBroker()
    engine.dispose()


def initialize(db, broker):
    return initialize_stock_paper_account(db, broker, activity_contract=VERSION)


def report(db):
    return db.query(StockPaperLedgerEvent).filter_by(event_type="activity_reconciliation").order_by(StockPaperLedgerEvent.id.desc()).first().payload


def test_malformed_later_page_halts_without_partial_import(ledger, monkeypatch):
    from app.services.stock_paper_ledger import AlpacaPaperClient

    db, broker = ledger
    initialize(db, broker)
    account = db.query(StockPaperAccount).one()
    baseline = deepcopy(account.activity_baseline)
    reconciled_at = account.last_reconciled_at
    client = AlpacaPaperClient()
    pages = iter([
        ([cash_event(id=f"new-{i}") for i in range(100)], {}),
        ({"message": "temporary broker error"}, {}),
    ])
    monkeypatch.setattr(client, "_request_response", lambda *args, **kwargs: next(pages))
    monkeypatch.setattr(broker, "fills", client.fills)
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "halted"
    db.expire_all()
    account = db.query(StockPaperAccount).one()
    assert account.reconciliation_required is True
    assert account.activity_baseline == baseline
    assert account.last_reconciled_at == reconciled_at
    assert db.query(StockPaperBrokerActivity).count() == 1


def set_position(broker, qty="2"):
    broker.position_rows = [{"symbol": "SPY", "qty": qty, "side": "long", "avg_entry_price": "100",
                             "current_price": "100", "market_value": str(Decimal(qty) * 100)}]


def test_date_only_seed_baseline_survives_new_session(ledger):
    db, broker = ledger
    result = initialize(db, broker)
    assert result["status"] == "reconciled"
    assert result["activity_contract"] == VERSION
    assert not result["costs_known"] and not result["account"]["accounting_verified"]
    assert result["observed_performance"]["status"] == "provisional"
    assert Decimal(result["observed_performance"]["net_change"]) == 0
    row = db.query(StockPaperBrokerActivity).one()
    assert row.occurred_at is None
    assert row.normalized_payload["effective_date"] == "2026-01-01"
    assert db.query(StockPaperFill).count() == 0
    baseline = deepcopy(db.query(StockPaperAccount).one().activity_baseline)
    with Session(db.bind) as restarted:
        for _ in range(2):
            assert reconcile_stock_paper_account(restarted, broker)["status"] == "reconciled"
            assert report(restarted)["status"] == "matched"
        assert restarted.query(StockPaperBrokerActivity).count() == 1
        assert restarted.query(StockPaperAccount).one().activity_baseline == baseline
        assert not result["observed_performance"]["qualifying"]


def test_observed_change_uses_full_persisted_journal_and_survives_restart(ledger, monkeypatch):
    db, broker = ledger
    initialize(db, broker)
    original_account = broker.account
    monkeypatch.setattr(broker, "account", lambda: {**original_account(), "equity": broker.cash})
    broker.rows.extend(cash_event(id=f"deposit-{index}", net_amount="1") for index in range(401))
    broker.rows.append(cash_event(id="expense", activity_type="FEE", net_amount="-2"))
    broker.cash = "1399"
    result = reconcile_stock_paper_account(db, broker)
    performance = result["observed_performance"]
    assert Decimal(performance["external_net_funding"]) == 401
    assert Decimal(performance["net_change"]) == -2
    assert Decimal(performance["reported_fee_expense"]) == 2
    with Session(db.bind) as restarted:
        assert stock_paper_status(restarted)["observed_performance"] == performance
    assert not result["costs_known"] and not result["account"]["accounting_verified"]


@pytest.mark.parametrize("change", ["baseline_cash", "equity", "source", "halted", "missing_snapshot"])
def test_observed_change_rejects_unmatched_snapshot_boundaries(ledger, change):
    db, broker = ledger
    initialize(db, broker)
    account = db.query(StockPaperAccount).one()
    snapshot = db.query(StockPaperEquitySnapshot).one()
    if change == "baseline_cash":
        snapshot.cash += 1
    elif change == "equity":
        account.equity += 1
    elif change == "source":
        snapshot.source = "other"
    elif change == "halted":
        account.status = "halted"
    else:
        db.delete(snapshot)
    db.flush()
    result = stock_paper_status(db)["observed_performance"]
    assert result["status"] == "unavailable" and result["net_change"] is None


def test_observed_change_requires_the_reconciled_journal_digest(ledger):
    db, broker = ledger
    initialize(db, broker)
    reconcile_stock_paper_account(db, broker)
    event = db.query(StockPaperLedgerEvent).filter_by(event_type="activity_reconciliation").one()
    event.payload = {**event.payload, "journal_sha256": "changed"}
    db.flush()
    result = stock_paper_status(db)["observed_performance"]
    assert result["status"] == "unavailable"
    assert result["reason"] == "Activity journal differs from the reconciled observation"


@pytest.fixture
def reviewed_ledger(ledger, monkeypatch):
    from app.services.paper_cash_policy import adopt_cent_policy, review_cent_residual
    from tests.test_alpaca_cash_precision import evidence

    db, broker = ledger
    monkeypatch.setattr(settings, "allow_live_trading", False)
    initialize(db, broker)
    _, fills, orders = evidence()
    broker.rows.extend(fills)
    broker.order_rows = [{**order, "type": "market", "time_in_force": "day"} for order in orders]
    assert reconcile_stock_paper_account(db, broker)["account"]["unexplained_residual"]
    adopt_cent_policy(db, actor="test", apply=True)
    db.commit()
    reconcile_stock_paper_account(db, broker)
    review_cent_residual(db, actor="test", apply=True)
    db.commit()
    reconcile_stock_paper_account(db, broker)
    return db, broker


def test_reviewed_monetary_halt_shows_observed_change_without_approving_trading(reviewed_ledger):
    db, _ = reviewed_ledger
    result = stock_paper_status(db)
    performance = result["observed_performance"]
    assert result["status"] == "halted"
    assert not result["account"]["accounting_verified"] and not result["costs_known"]
    assert performance["status"] == "provisional" and Decimal(performance["net_change"]) == 0
    assert not performance["qualifying"] and not performance["costs_complete"]
    assert performance["new_fills_without_commission"] == 2
    assert "account remains halted" in performance["reason"]
    with Session(db.bind) as restarted:
        assert stock_paper_status(restarted)["observed_performance"] == performance


@pytest.mark.parametrize("change", ["no_review", "review_status", "review_baseline", "review_scope",
                                  "policy", "halt_reason", "residual", "required", "report_status", "equity"])
def test_reviewed_halt_still_requires_valid_boundaries_and_evidence(reviewed_ledger, change):
    from app.services.paper_cash_policy import EXACT

    db, _ = reviewed_ledger
    account = db.query(StockPaperAccount).one()
    review = db.query(StockPaperLedgerEvent).filter_by(event_type="monetary_residual_review").one()
    if change == "no_review":
        db.delete(review)
    elif change == "review_status":
        review.status = "blocked"
    elif change == "review_baseline":
        review.payload = {**review.payload, "baseline_journal_sha256": "other"}
    elif change == "review_scope":
        review.payload = {**review.payload, "scope": "other"}
    elif change == "policy":
        account.cash_policy = EXACT
    elif change == "halt_reason":
        account.halt_reason = "Corporate action requires accounting review"
    elif change == "residual":
        account.unexplained_residual = True
    elif change == "required":
        account.reconciliation_required = True
    elif change == "report_status":
        event = db.query(StockPaperLedgerEvent).filter_by(event_type="activity_reconciliation").order_by(StockPaperLedgerEvent.id.desc()).first()
        event.payload = {**event.payload, "status": "mismatch"}
    elif change == "equity":
        account.equity += 1
    db.flush()
    assert stock_paper_status(db)["observed_performance"]["status"] == "unavailable"


def test_new_cash_discrepancy_withholds_previously_reviewed_performance(reviewed_ledger):
    db, broker = reviewed_ledger
    broker.cash = "999.99"
    result = reconcile_stock_paper_account(db, broker)
    assert result["account"]["unexplained_residual"]
    assert result["observed_performance"]["status"] == "unavailable"


@pytest.mark.parametrize("kind,delta", [("CSD", "20"), ("CSW", "-20"), ("DIV", "2"), ("DIVNRA", "-1"), ("FEE", "-1"), ("INT", "1"), ("JNLC", "3")])
def test_cash_changes_reconcile_without_execution_timestamp(ledger, kind, delta):
    db, broker = ledger
    initialize(db, broker)
    broker.rows.append(cash_event(id="later", activity_type=kind, net_amount=delta))
    broker.cash = str(Decimal(1000) + Decimal(delta))
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "reconciled"
    assert report(db)["cash_equation_matches"]
    assert not result["costs_known"]
    assert not result["account"]["accounting_verified"]


def test_legacy_review_halt_reclassifies_imported_cash_activity_after_exact_match(ledger):
    db, broker = ledger
    broker.rows = []
    initialize_stock_paper_account(db, broker, activity_contract="legacy-v1")
    account = db.query(StockPaperAccount).one()
    account.status = "halted"
    account.halt_reason = ACCOUNTING_RESIDUAL_REVIEW_REASON
    account.reconciliation_required = True
    account.unexplained_residual = True
    db.commit()

    broker.rows.append(cash_event(id="fee", activity_type="FEE", date="2026-10-07", net_amount="-1"))
    broker.cash = "999"
    result = reconcile_stock_paper_account(db, broker)

    assert result["status"] == "reconciled"
    assert result["account"]["unexplained_residual"] is False
    assert result["account"]["reconciliation_required"] is False
    assert result["account"]["accounting_verified"] is False
    assert result["costs_known"] is False


def test_fill_and_separate_fee_replay_do_not_invent_commission(ledger):
    db, broker = ledger
    initialize(db, broker)
    broker.rows.extend([fill_event(), cash_event(id="fee", activity_type="FEE", net_amount="-1")])
    broker.cash = "799"
    set_position(broker)
    for _ in range(2):
        result = reconcile_stock_paper_account(db, broker)
        assert result["status"] == "reconciled"
        assert result["last_activity_reconciliation"]["status"] == "matched"
        assert report(db)["inventory_equation_matches"]
        assert not result["costs_known"]
    fill = db.query(StockPaperFill).one()
    assert fill.fee is None and not fill.cost_known
    broker.rows.reverse()
    broker.rows.append(deepcopy(broker.rows[0]))
    assert reconcile_stock_paper_account(db, broker)["status"] == "reconciled"
    assert db.query(StockPaperBrokerActivity).count() == 3
    assert db.query(StockPaperFill).count() == 1


def test_nonempty_opening_inventory_is_not_assumed_zero(ledger):
    db, broker = ledger
    broker.rows.append(fill_event())
    broker.cash = "800"
    set_position(broker)
    initialize(db, broker)
    broker.rows.append(fill_event(id="sale", side="sell", qty="1", price="110"))
    broker.cash = "910"
    set_position(broker, "1")
    assert reconcile_stock_paper_account(db, broker)["status"] == "reconciled"
    assert report(db)["status"] == "matched"


@pytest.mark.parametrize("change", ["missing", "changed", "unsupported", "mixed_fees", "duplicate_conflict", "wrong_identity", "duplicate_position"])
def test_invalid_provider_evidence_halts_and_does_not_rebase(ledger, change):
    db, broker = ledger
    initialize(db, broker)
    baseline = deepcopy(db.query(StockPaperAccount).one().activity_baseline)
    if change == "missing":
        broker.rows = []
    elif change == "changed":
        broker.rows[0]["net_amount"] = "999"
    elif change == "unsupported":
        broker.rows.append(cash_event(id="split", activity_type="SSP"))
    elif change == "mixed_fees":
        broker.rows.extend([fill_event(commission="1"), cash_event(id="fee", activity_type="FEE", net_amount="-1")])
    elif change == "duplicate_conflict":
        broker.rows.append(cash_event(net_amount="999"))
    elif change == "wrong_identity":
        broker.account_id = "other-account"
    else:
        set_position(broker)
        broker.position_rows *= 2
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "halted"
    assert not result["costs_known"]
    assert db.query(StockPaperAccount).one().activity_baseline == baseline
    assert db.query(StockPaperBrokerActivity).count() == 1


@pytest.mark.parametrize("kind", ["cash", "inventory"])
def test_unexplained_residual_is_sticky(ledger, kind):
    db, broker = ledger
    initialize(db, broker)
    if kind == "cash":
        broker.cash = "999"
    else:
        set_position(broker)
    for _ in range(2):
        result = reconcile_stock_paper_account(db, broker)
        assert result["status"] == "halted"
        assert result["account"]["unexplained_residual"]
        assert report(db)["status"] == "mismatch"
    broker.cash, broker.position_rows = "1000", []
    assert reconcile_stock_paper_account(db, broker)["status"] == "halted"
    assert report(db)["status"] == "matched"


def test_late_fee_enrichment_cannot_qualify_v2(ledger):
    db, broker = ledger
    initialize(db, broker)
    broker.rows.append(fill_event())
    broker.cash = "799"
    set_position(broker)
    assert reconcile_stock_paper_account(db, broker)["status"] == "halted"
    broker.rows[-1]["commission"] = "1"
    result = reconcile_stock_paper_account(db, broker)
    assert report(db)["status"] == "matched"
    assert result["status"] == "halted"
    assert not result["costs_known"] and not result["account"]["accounting_verified"]
    assert attempt_automatic_stock_recovery(db, candidate=True, evidence={"enriched_activity_ids": ["fill"]})["status"] == "blocked"


def test_unstable_baseline_is_rejected_without_persisting(ledger):
    db, broker = ledger
    original = broker.account
    calls = 0
    def changing_account():
        nonlocal calls
        calls += 1
        return {**original(), "cash": str(1000 + calls)}
    broker.account = changing_account
    with pytest.raises(StockPaperError, match="changed during baseline"):
        initialize(db, broker)
    assert db.query(StockPaperAccount).count() == 0


def test_incomplete_persisted_baseline_halts_instead_of_crashing(ledger):
    db, broker = ledger
    initialize(db, broker)
    db.query(StockPaperAccount).one().activity_baseline = {"version": VERSION}
    db.commit()
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "halted"
    assert "baseline" in result["reason"]


def test_legacy_account_is_not_silently_migrated(ledger):
    db, broker = ledger
    assert initialize_stock_paper_account(db, broker)["status"] == "halted"
    assert db.query(StockPaperAccount).one().activity_contract == "legacy-v1"
    with pytest.raises(StockPaperError, match="already initialized"):
        initialize(db, broker)


def test_activity_id_collision_cannot_merge_accounts(ledger):
    db, broker = ledger
    initialize(db, broker)
    other = StockPaperAccount(broker="other", broker_account_id="other", cash=1000, equity=1000, buying_power=1000, raw_payload={})
    db.add(other)
    db.flush()
    db.query(StockPaperBrokerActivity).one().account_id = other.id
    db.commit()
    result = reconcile_stock_paper_account(db, broker)
    assert result["status"] == "halted"
    assert "another ledger account" in result["reason"]


def test_order_id_collision_cannot_merge_accounts(ledger):
    db, broker = ledger
    initialize(db, broker)
    account = db.query(StockPaperAccount).one()
    other = StockPaperAccount(broker="other", broker_account_id="other", cash=1000, equity=1000, buying_power=1000, raw_payload={})
    db.add(other)
    db.flush()
    row = {"id": "order", "client_order_id": "client", "symbol": "SPY", "side": "buy", "qty": "1", "type": "market", "time_in_force": "day", "status": "filled"}
    _upsert_orders(db, other, [row])
    db.flush()
    with pytest.raises(StockPaperError, match="another ledger account"):
        _upsert_orders(db, account, [row])
    assert db.query(StockPaperOrder).one().account_id == other.id


def test_api_explicit_version_and_invalid_options(ledger, monkeypatch):
    db, broker = ledger
    import app.api.stock_paper as api
    received = []
    def capture(session, **kwargs):
        received.append(kwargs)
        return {"status": "test"}
    monkeypatch.setattr(api, "initialize_stock_paper_account", capture)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        assert client.post("/stock-paper/initialize").status_code == 200
        assert received[-1] == {"activity_contract": VERSION}
        assert client.post("/stock-paper/initialize", json={"activity_contract": VERSION}).status_code == 200
        assert received[-1] == {"activity_contract": VERSION}
        assert client.post("/stock-paper/initialize", json={"activity_contract": "guess"}).status_code == 422
        assert client.post("/stock-paper/initialize", json={"allow_live": True}).status_code == 422


def test_api_omitted_contract_defaults_to_alpaca_v2(monkeypatch):
    import app.api.stock_paper as api
    received = []
    def capture(session, **kwargs):
        received.append(kwargs)
        return {"status": "test"}
    monkeypatch.setattr(api, "initialize_stock_paper_account", capture)
    monkeypatch.setattr(api, "active_paper_broker_name", lambda: "alpaca_paper")
    app = FastAPI()
    app.include_router(router)
    class FakeDB:
        info = {}
    app.dependency_overrides[get_db] = lambda: FakeDB()
    with TestClient(app) as client:
        assert client.post("/stock-paper/initialize").status_code == 200
        assert received[-1] == {"activity_contract": "alpaca-activities-v2"}
        assert client.post("/stock-paper/initialize", json={}).status_code == 200
        assert received[-1] == {"activity_contract": "alpaca-activities-v2"}
