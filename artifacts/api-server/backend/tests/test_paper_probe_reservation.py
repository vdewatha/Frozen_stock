from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models import StockPaperAccount, StockPaperLedgerEvent, StockPaperRecoveryState
from scripts.test_alpaca_paper_roundtrip import PROBE_HALT, reserve_probe
from tests.test_paper_roundtrip_probe import Broker, RUN
from tests.test_paper_research_accounting import ledger, reviewed_ledger, ready

NOW = datetime(2026, 9, 29, 15, tzinfo=timezone.utc)


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allow_live_trading", False)
    monkeypatch.setattr(settings, "active_paper_broker", "alpaca_paper")
    owner = schema = None
    if request.param == "postgresql":
        url = make_url(settings.database_url)
        if url.get_backend_name() != "postgresql":
            pytest.skip("Requires isolated PostgreSQL validation stack")
        owner = create_engine(url)
        schema = "probe_" + uuid4().hex
        with owner.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    else:
        url = "sqlite:///" + str(tmp_path / "probe.sqlite")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(StockPaperAccount(broker="alpaca_paper", broker_account_id="paper-test", currency="USD",
            cash=1000, buying_power=1000, equity=1000, status="reconciled", reconciliation_required=False, raw_payload={}))
        db.commit()
    try:
        yield engine
    finally:
        engine.dispose()
        if owner:
            with owner.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            owner.dispose()


def test_reservation_is_durable_before_any_broker_submission(engine):
    broker = Broker()
    with Session(engine) as db:
        assert reserve_probe(db, broker, RUN, now=NOW) == "paper-test"
    assert not broker.sent
    with Session(engine) as db:
        account = db.query(StockPaperAccount).one()
        assert account.status == "halted" and account.halt_reason == PROBE_HALT
        assert account.reconciliation_required
        assert db.get(StockPaperRecoveryState, 1).accounting_review_required
        event = db.query(StockPaperLedgerEvent).filter_by(event_type="paper_probe_reservation").one()
        assert event.payload["run_id"] == RUN and event.payload["automatic_resume"] is False
        with pytest.raises(RuntimeError, match="reconciled"):
            reserve_probe(db, broker, str(uuid4()), now=NOW)
    assert not broker.sent


def test_closed_session_does_not_reserve_or_mutate_ledger(engine):
    broker = Broker()
    broker.open = False
    with Session(engine) as db:
        with pytest.raises(RuntimeError, match="regular session"):
            reserve_probe(db, broker, RUN, now=NOW)
    with Session(engine) as db:
        assert db.query(StockPaperAccount).one().status == "reconciled"
        assert db.query(StockPaperLedgerEvent).count() == 0
    assert not broker.sent


def test_recovered_ledger_cannot_reuse_a_reserved_run(engine):
    with Session(engine) as db:
        reserve_probe(db, Broker(), RUN, now=NOW)
        account = db.query(StockPaperAccount).one()
        account.status = "reconciled"
        account.reconciliation_required = False
        db.commit()
        with pytest.raises(RuntimeError, match="already reserved"):
            reserve_probe(db, Broker(), RUN, now=NOW)


def test_postgres_serializes_competing_probe_reservations(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock semantics")
    entered, release = Event(), Event()
    broker = Broker()
    account = broker.account
    def hold_preflight():
        entered.set()
        if not release.wait(10):
            raise RuntimeError("Test synchronization timed out")
        return account()
    broker.account = hold_preflight
    def first():
        with Session(engine) as db:
            return reserve_probe(db, broker, RUN, now=NOW)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        try:
            assert entered.wait(5)
            with Session(engine) as db:
                db.execute(text("SET LOCAL lock_timeout = '100ms'"))
                with pytest.raises(DBAPIError) as error:
                    reserve_probe(db, Broker(), str(uuid4()), now=NOW)
                assert error.value.orig.sqlstate == "55P03"
        finally:
            release.set()
        assert future.result(timeout=5) == "paper-test"
    with Session(engine) as db:
        with pytest.raises(RuntimeError, match="reconciled"):
            reserve_probe(db, Broker(), str(uuid4()), now=NOW)
        assert db.query(StockPaperLedgerEvent).filter_by(event_type="paper_probe_reservation").count() == 1


def test_verified_research_account_can_reserve_only_explicit_probe(ready):
    db, account = ready
    broker = Broker()
    raw = broker.account()
    broker.account = lambda: {**raw, "id": account.broker_account_id}
    reserve_probe(db, broker, RUN, now=NOW)
    event = db.query(StockPaperLedgerEvent).filter_by(event_type="paper_probe_reservation").one()
    assert event.payload["research_accounting_sha256"]
    assert event.payload["scope"] == "explicit_bounded_integration_probe"
    assert not account.costs_known and not account.accounting_verified
    assert account.status == "halted" and account.halt_reason == PROBE_HALT
    assert not broker.sent


def test_unrelated_halt_is_never_overridden(ready):
    db, account = ready
    account.halt_reason = "Operator stop"
    db.commit()
    broker = Broker()
    with pytest.raises(RuntimeError, match="verified research"):
        reserve_probe(db, broker, RUN, now=NOW)
    assert account.halt_reason == "Operator stop"
    assert not broker.sent
