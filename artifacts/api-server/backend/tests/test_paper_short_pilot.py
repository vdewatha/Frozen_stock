"""No real broker calls: exercise the durable short/cover state machine."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.security import AuthenticationMiddleware, required_role
from app.db.base import Base
from app.db.session import get_db
from app.models import RiskRule, StockPaperRecoveryState
from app.models.paper_short_pilot import PaperShortOrder, PaperShortPilot
from app.services import paper_short_pilot as pilot
from app.services.stock_paper_ledger import StockPaperError, StockPaperUnavailable

NOW = datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc)
ACCOUNT = "e6714d68-0429-49bc-aa30-1e2c13467b12"


class Broker:
    base_url = pilot.ALPACA_PAPER_URL

    def __init__(self):
        self.raw_account = {"id": ACCOUNT, "status": "ACTIVE", "currency": "USD",
                            "trading_blocked": False, "account_blocked": False,
                            "shorting_enabled": True, "equity": "100000", "buying_power": "100000"}
        self.raw_asset = {"symbol": "AAPL", "class": "us_equity", "status": "active",
                          "tradable": True, "shortable": True, "easy_to_borrow": True, "marginable": True}
        self.raw_clock = {"timestamp": NOW.isoformat(), "is_open": True}
        self.book, self.raw_positions, self.posts, self.cancels = {}, [], [], []
        self.failure = None
        self.accept_then_timeout = False

    def account(self):
        return deepcopy(self.raw_account)

    def asset(self, symbol):
        return {**self.raw_asset, "symbol": symbol}

    def clock(self):
        return deepcopy(self.raw_clock)

    def orders(self):
        return deepcopy(list(self.book.values()))

    def positions(self):
        return deepcopy(self.raw_positions)

    def order_by_client_id(self, client_id):
        return deepcopy(self.book.get(client_id))

    def submit_order(self, payload):
        self.posts.append(deepcopy(payload))
        if self.failure:
            raise self.failure
        raw = {**payload, "id": f"broker-{len(self.book)}", "status": "new", "filled_qty": "0",
               "filled_avg_price": None, "submitted_at": NOW.isoformat()}
        self.book[payload["client_order_id"]] = raw
        if self.accept_then_timeout:
            raise TimeoutError("No response after broker accepted")
        return deepcopy(raw)

    def fill(self, client_id, qty=None, status="filled", price="100"):
        raw = self.book[client_id]
        raw["filled_qty"], raw["filled_avg_price"], raw["status"] = qty or raw["qty"], price, status
        positions = {}
        for order in self.book.values():
            positions[order["symbol"]] = positions.get(order["symbol"], Decimal("0")) + (
                Decimal(order["filled_qty"]) * (-1 if order["side"] == "sell" else 1))
        self.raw_positions = [{"symbol": s, "qty": str(q), "market_value": str(q * Decimal(price))}
                              for s, q in positions.items() if q]

    def cancel_order(self, broker_id):
        self.cancels.append(broker_id)


@pytest.fixture
def setup(monkeypatch):
    for name, value in {
        "environment": "local", "allow_live_trading": False, "paper_binding_source": "development",
        "paper_workers_enabled": False, "dev_paper_shorts_enabled": True,
        "active_paper_broker": "alpaca_paper", "paper_broker_account_id": ACCOUNT,
        "live_broker_account_id": "", "dev_paper_broker_account_id": SecretStr(ACCOUNT),
    }.items():
        monkeypatch.setattr(pilot.settings, name, value)
    monkeypatch.setattr(pilot, "feed_status", lambda *a, **kw: {"status": "ready", "execution_status": "ready"})
    monkeypatch.setattr(pilot, "_validate_reference_price", lambda *a, **kw: None)
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(RiskRule(name="short pilot policy", value={"kill_switch_enabled": False}, is_active=True))
        db.commit()
        broker = Broker()
        yield db, broker
    engine.dispose()


def authorization(**kwargs):
    return pilot.PilotAuthorization(**({"symbols": ["AAPL", "SPY"], "max_order_notional": "250",
        "max_gross_notional": "1000", "expires_at": NOW + timedelta(hours=2),
        "reason": "Explicit development paper-only pilot"} | kwargs))


def order(**kwargs):
    return pilot.PilotOrderRequest(**({"symbol": "AAPL", "intent": "open_short", "quantity": 1,
        "limit_price": "100.00", "idempotency_key": "short-test-entry-001",
        "reason": "Operator paper-only short test"} | kwargs))


def authorize(db, broker, **kwargs):
    return pilot.authorize_pilot(db, authorization(**kwargs), actor="admin:test", gateway=broker, now=NOW)


def submit(db, broker, request=None, **kwargs):
    return pilot.submit_pilot_order(db, request or order(), actor="operator:test", gateway=broker, now=NOW, **kwargs)


def test_short_then_cover_signed_reconciliation(setup):
    db, broker = setup
    authorize(db, broker)
    opened = submit(db, broker)
    assert broker.posts[0]["side"] == "sell"
    assert broker.posts[0]["position_intent"] == "sell_to_open"
    assert broker.posts[0]["extended_hours"] is False
    assert broker.posts[0]["time_in_force"] == "day"
    broker.fill(opened["client_order_id"])
    result = pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
    assert result["positions"] == {"AAPL": "-1"}
    covered = submit(db, broker, order(intent="cover_short", idempotency_key="short-test-cover-001"))
    assert broker.posts[1]["side"] == "buy"
    assert broker.posts[1]["position_intent"] == "buy_to_close"
    broker.fill(covered["client_order_id"], price="90")
    result = pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
    assert result["positions"] == {}
    assert result["costs_verified"] is False and result["accounting_verified"] is False
    assert result["autonomous"] is False and result["live_trading"] is False


def test_idempotency_does_not_post_twice(setup):
    db, broker = setup
    authorize(db, broker)
    assert submit(db, broker) == submit(db, broker)
    assert len(broker.posts) == 1
    with pytest.raises(StockPaperError, match="different"):
        submit(db, broker, order(quantity=2))
    assert len(broker.posts) == 1


def test_account_wide_pending_order_slot(setup):
    db, broker = setup
    authorize(db, broker)
    submit(db, broker)
    with pytest.raises(StockPaperError, match="unresolved"):
        submit(db, broker, order(symbol="SPY", idempotency_key="another-short-key-001"))
    assert len(broker.posts) == 1


def test_timeout_never_reposts_and_broker_lookup_recovers(setup):
    db, broker = setup
    authorize(db, broker)
    broker.accept_then_timeout = True
    with pytest.raises(StockPaperError, match="unconfirmed"):
        submit(db, broker)
    assert submit(db, broker)["status"] == "uncertain"
    assert len(broker.posts) == 1
    result = pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
    assert result["orders"][0]["status"] == "new"
    assert result["orders"][0]["unresolved"] is True


def test_missing_lookup_does_not_clear_uncertainty(setup):
    db, broker = setup
    authorize(db, broker)
    broker.failure = TimeoutError()
    with pytest.raises(StockPaperError):
        submit(db, broker)
    with pytest.raises(StockPaperError, match="Unresolved"):
        pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
    assert db.query(PaperShortOrder).one().active_slot == 1
    assert len(broker.posts) == 1


@pytest.mark.parametrize("field,value", [("environment", "production"), ("allow_live_trading", True),
    ("paper_workers_enabled", True), ("paper_binding_source", "shared"),
    ("active_paper_broker", "tradier_sandbox"), ("dev_paper_shorts_enabled", False),
    ("paper_broker_account_id", "other"), ("live_broker_account_id", ACCOUNT)])
def test_configuration_fails_closed(setup, monkeypatch, field, value):
    db, broker = setup
    monkeypatch.setattr(pilot.settings, field, value)
    with pytest.raises(StockPaperError):
        authorize(db, broker)
    assert broker.posts == []


def test_live_url_rejected(setup):
    db, broker = setup
    broker.base_url = "https://api.alpaca.markets"
    with pytest.raises(StockPaperError, match="paper endpoint"):
        authorize(db, broker)


@pytest.mark.parametrize("field,value", [("id", "wrong-account"), ("shorting_enabled", False),
    ("shorting_enabled", "true"), ("trading_blocked", True), ("account_blocked", True),
    ("equity", "1999"), ("equity", "NaN"), ("currency", "EUR"), ("status", "CLOSED")])
def test_account_capabilities_are_checked(setup, field, value):
    db, broker = setup
    broker.raw_account[field] = value
    with pytest.raises(StockPaperError):
        authorize(db, broker)
    assert broker.posts == []


@pytest.mark.parametrize("field,value", [("shortable", False), ("easy_to_borrow", False),
    ("marginable", False), ("tradable", False), ("class", "crypto"), ("status", "inactive")])
def test_asset_shortability_checked_before_post(setup, field, value):
    db, broker = setup
    authorize(db, broker)
    broker.raw_asset[field] = value
    with pytest.raises(StockPaperError, match="easy-to-borrow"):
        submit(db, broker)
    assert broker.posts == []
    assert db.query(PaperShortOrder).one().status == "blocked"


def test_kill_switch_blocks_entry_not_cover(setup):
    db, broker = setup
    authorize(db, broker)
    opened = submit(db, broker)
    broker.fill(opened["client_order_id"])
    pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
    db.query(RiskRule).one().value = {"kill_switch_enabled": True}
    db.commit()
    with pytest.raises(StockPaperError, match="kill switch"):
        submit(db, broker, order(idempotency_key="blocked-short-key-001"))
    covered = submit(db, broker, order(intent="cover_short", idempotency_key="cover-short-key-001"))
    assert covered["intent"] == "cover_short"
    assert len(broker.posts) == 2


def test_expired_and_disabled_authorization_still_allows_bounded_cover(setup, monkeypatch):
    db, broker = setup
    authorize(db, broker)
    opened = submit(db, broker)
    broker.fill(opened["client_order_id"])
    pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
    row = db.get(PaperShortPilot, 1)
    row.status, row.expires_at = "paused", NOW - timedelta(seconds=1)
    db.commit()
    monkeypatch.setattr(pilot.settings, "dev_paper_shorts_enabled", False)
    assert submit(db, broker, order(intent="cover_short", idempotency_key="cover-short-key-001"))["status"] == "new"


def test_partial_fill_requires_cancel_confirmation_before_cover(setup):
    db, broker = setup
    authorize(db, broker)
    opened = submit(db, broker, order(quantity=2))
    broker.fill(opened["client_order_id"], qty="1", status="partially_filled")
    pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
    assert pilot.cancel_pilot_order(db, opened["client_order_id"], actor="operator:test", gateway=broker, now=NOW)["unresolved"]
    with pytest.raises(StockPaperError, match="unresolved"):
        submit(db, broker, order(intent="cover_short", idempotency_key="cover-short-key-001"))
    broker.book[opened["client_order_id"]]["status"] = "canceled"
    pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
    with pytest.raises(StockPaperError, match="exceed"):
        submit(db, broker, order(intent="cover_short", quantity=2, idempotency_key="oversized-cover-key-001"))
    assert submit(db, broker, order(intent="cover_short", idempotency_key="valid-cover-key-001"))["status"] == "new"


@pytest.mark.parametrize("mutation", ["feed", "clock", "price", "power", "order_cap", "gross_cap", "symbol", "expiry", "identity", "recovery"])
def test_preflight_blocks_without_post(setup, monkeypatch, mutation):
    db, broker = setup
    authorize(db, broker)
    request = order()
    if mutation == "feed":
        monkeypatch.setattr(pilot, "feed_status", lambda *a, **kw: {"status": "stale"})
    elif mutation == "clock":
        broker.raw_clock["timestamp"] = (NOW - timedelta(seconds=10)).isoformat()
    elif mutation == "price":
        monkeypatch.setattr(pilot, "_validate_reference_price", lambda *a, **kw: (_ for _ in ()).throw(StockPaperError("bad price")))
    elif mutation == "power":
        broker.raw_account["buying_power"] = "1"
    elif mutation == "order_cap":
        request = order(limit_price="300")
    elif mutation == "gross_cap":
        db.get(PaperShortPilot, 1).max_gross_notional = Decimal("100")
    elif mutation == "symbol":
        request = order(symbol="MSFT")
    elif mutation == "expiry":
        db.get(PaperShortPilot, 1).expires_at = NOW
    elif mutation == "identity":
        db.get(PaperShortPilot, 1).authorized_by = "operator:test"
    elif mutation == "recovery":
        db.add(StockPaperRecoveryState(id=1, status="paused", flatten_policy="none", updated_by="test"))
    db.commit()
    with pytest.raises(StockPaperError):
        submit(db, broker, request)
    assert broker.posts == []


def test_external_position_drift_and_foreign_order_block(setup):
    db, broker = setup
    authorize(db, broker)
    broker.raw_positions = [{"symbol": "AAPL", "qty": "-1", "market_value": "-100"}]
    with pytest.raises(StockPaperError, match="reconcile"):
        submit(db, broker)
    broker.raw_positions = []
    broker.book["foreign"] = {"id": "foreign", "client_order_id": "foreign", "status": "new"}
    with pytest.raises(StockPaperError, match="Foreign"):
        submit(db, broker, order(idempotency_key="foreign-block-key-001"))
    assert not broker.posts


def test_definite_rejection_releases_slot_but_not_idempotency_key(setup):
    db, broker = setup
    authorize(db, broker)
    broker.failure = StockPaperUnavailable("rejected", status_code=422)
    with pytest.raises(StockPaperError):
        submit(db, broker)
    assert submit(db, broker)["status"] == "rejected"
    assert not db.query(PaperShortOrder).one().active_slot
    assert len(broker.posts) == 1


@pytest.mark.parametrize("changes", [{"quantity": 0}, {"quantity": -1}, {"quantity": 1.5}, {"quantity": True},
    {"limit_price": "NaN"}, {"limit_price": "Infinity"}, {"limit_price": "100.001"},
    {"intent": "buy"}, {"live": True}, {"idempotency_key": "short"}])
def test_order_schema_rejects_unsafe_inputs(changes):
    with pytest.raises(ValidationError):
        order(**changes)


@pytest.mark.parametrize("changes", [{"symbols": ["TSLA"]}, {"symbols": ["AAPL", "AAPL"]},
    {"max_order_notional": "2000"}, {"max_gross_notional": "100"}, {"expires_at": NOW.replace(tzinfo=None)}])
def test_authorization_schema_rejects_unsafe_inputs(changes):
    with pytest.raises(ValidationError):
        authorization(**changes)


def test_api_roles_and_payload_are_wired(setup, monkeypatch):
    from app.api.stock_paper import router
    db, broker = setup
    app = FastAPI()
    config = Settings(auth_viewer_key="v" * 40, auth_researcher_key="r" * 40,
                      auth_operator_key="o" * 40, auth_admin_key="a" * 40)
    app.add_middleware(AuthenticationMiddleware, configuration=config)
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        base = "/api/stock-paper/short-pilot"
        assert client.get(base).status_code == 401
        assert client.get(base, headers={"Authorization": "Bearer " + "v" * 40}).status_code == 200
        for role, expected in [("v", 403), ("r", 403), ("o", 200), ("a", 200)]:
            with patch.object(pilot, "submit_pilot_order", return_value={"paper_only": True}) as call:
                response = client.post(base + "/orders", json=order().model_dump(mode="json"),
                                       headers={"Authorization": "Bearer " + role * 40})
                assert response.status_code == expected, response.text
                assert call.called == (expected == 200)
                if call.called:
                    assert call.call_args.kwargs["request"].intent == "open_short"
        with patch.object(pilot, "authorize_pilot", return_value={"paper_only": True}) as call:
            assert client.post(base + "/authorize", json=authorization().model_dump(mode="json"),
                               headers={"Authorization": "Bearer " + "o" * 40}).status_code == 403
            assert client.post(base + "/authorize", json=authorization().model_dump(mode="json"),
                               headers={"Authorization": "Bearer " + "a" * 40}).status_code == 200
            assert call.call_count == 1
    assert required_role("POST", "/stock-paper/short-pilot/orders/dps-" + "a" * 48 + "/cancel") == "operator"


def test_real_http_adapter_always_uses_paper_base(monkeypatch):
    import httpx
    from app.services.stock_paper_ledger import settings
    monkeypatch.setattr(type(settings), "paper_broker_credentials", lambda self: ("fake-paper-key", "fake-paper-secret"))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "mock-order"})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(respond)))
    gateway = pilot.ShortPilotGateway()
    gateway.clock()
    gateway.asset("AAPL")
    gateway.submit_order({"symbol": "AAPL", "side": "sell", "position_intent": "sell_to_open"})
    assert len(requests) == 3
    assert all(r.url.host == "paper-api.alpaca.markets" for r in requests)
    assert requests[-1].method == "POST"


def test_concurrent_entries_have_only_one_broker_post(setup, tmp_path):
    _, broker = setup
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(RiskRule(name="policy", value={"kill_switch_enabled": False}, is_active=True))
        db.commit()
        authorize(db, broker)
    entered, release = Event(), Event()
    original = broker.submit_order

    def delayed(payload):
        result = original(payload)
        entered.set()
        assert release.wait(10)
        return result

    broker.submit_order = delayed

    def first():
        with Session(engine) as db:
            return submit(db, broker)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(first)
            try:
                assert entered.wait(10)
                with Session(engine) as db:
                    with pytest.raises(StockPaperError, match="unresolved"):
                        submit(db, broker, order(idempotency_key="concurrent-short-key-002"))
            finally:
                release.set()
            assert future.result(timeout=10)["status"] == "new"
        assert len(broker.posts) == 1
    finally:
        engine.dispose()


def test_restart_preserves_uncertain_intent_without_repost(setup):
    db, broker = setup
    authorize(db, broker)
    broker.accept_then_timeout = True
    with pytest.raises(StockPaperError):
        submit(db, broker)
    engine = db.get_bind()
    with Session(engine) as restarted:
        assert submit(restarted, broker)["status"] == "uncertain"
        result = pilot.reconcile_pilot(restarted, actor="operator:test", gateway=broker, now=NOW)
        assert result["orders"][0]["status"] == "new"
    assert len(broker.posts) == 1


@pytest.mark.parametrize("mutation", ["side", "price", "filled_excess", "filled_nan", "backwards_fills", "broker_id", "status"])
def test_mutated_broker_evidence_fails_closed(setup, mutation):
    db, broker = setup
    authorize(db, broker)
    opened = submit(db, broker)
    raw = broker.book[opened["client_order_id"]]
    if mutation == "side":
        raw["side"] = "buy"
    elif mutation == "price":
        raw["limit_price"] = "999"
    elif mutation == "filled_excess":
        raw["filled_qty"] = "2"
    elif mutation == "filled_nan":
        raw["filled_qty"] = "NaN"
    elif mutation == "backwards_fills":
        broker.fill(opened["client_order_id"], qty="1", status="partially_filled")
        pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
        raw["filled_qty"] = "0"
    elif mutation == "broker_id":
        raw["id"] = "changed-id"
    elif mutation == "status":
        raw["status"] = "unrecognized"
    with pytest.raises(StockPaperError):
        pilot.reconcile_pilot(db, actor="operator:test", gateway=broker, now=NOW)
    db.rollback()
    assert db.get(PaperShortOrder, opened["client_order_id"]).active_slot == 1


def test_long_ledger_cannot_initialize_pilot_owned_account(setup):
    from app.services.stock_paper_ledger import initialize_stock_paper_account
    db, broker = setup
    authorize(db, broker)
    with patch("app.services.stock_paper_ledger._snapshot", return_value=(broker.account(), [], [], [], NOW)):
        with pytest.raises(StockPaperError, match="owned by the isolated short pilot"):
            initialize_stock_paper_account(db, gateway=broker)


def test_pilot_cannot_authorize_long_ledger_owned_account(setup):
    from app.models import StockPaperAccount
    db, broker = setup
    db.add(StockPaperAccount(broker="alpaca_paper", broker_account_id=ACCOUNT, currency="USD",
                            cash=100000, buying_power=100000, equity=100000, raw_payload={}))
    db.commit()
    with pytest.raises(StockPaperError, match="owned by the long-only ledger"):
        authorize(db, broker)


def test_slow_preflight_never_submits(setup, monkeypatch):
    db, broker = setup
    authorize(db, broker)
    ticks = iter([10, 16])
    monkeypatch.setattr(pilot, "monotonic", lambda: next(ticks))
    with pytest.raises(StockPaperError, match="too long"):
        submit(db, broker)
    assert not broker.posts


def test_pause_stops_entries_and_keeps_existing_orders(setup):
    db, broker = setup
    authorize(db, broker)
    result = pilot.pause_pilot(db, actor="operator:test", reason="Pause development short entries")
    assert result["pilot"]["status"] == "paused"
    with pytest.raises(StockPaperError, match="authorization"):
        submit(db, broker)
    assert not broker.posts


def test_canceled_preflight_cannot_resume_and_submit(setup, monkeypatch):
    db, broker = setup
    authorize(db, broker)
    original = pilot._order_checks_excluding

    def cancel_during_check(*args):
        original(*args)
        client_id = args[-1]
        # Model a concurrent cancellation after successful validation but before
        # the durable dispatch claim. Its commit must prevent the broker POST.
        result = pilot.cancel_pilot_order(db, client_id, actor="operator:cancel", gateway=broker, now=NOW)
        assert result["status"] == "blocked" and not result["unresolved"]

    monkeypatch.setattr(pilot, "_order_checks_excluding", cancel_during_check)
    with pytest.raises(StockPaperError, match="canceled before dispatch"):
        submit(db, broker)
    assert not broker.posts


def test_http_full_authorize_submit_fill_cover_flow(setup, monkeypatch):
    from app.api.stock_paper import router
    db, broker = setup

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(pilot, "datetime", Clock)
    monkeypatch.setattr(pilot, "ShortPilotGateway", lambda: broker)
    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware, configuration=Settings(
        auth_admin_key="a" * 40, auth_operator_key="o" * 40,
        auth_viewer_key="v" * 40, auth_researcher_key="r" * 40))
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    admin, operator = {"Authorization": "Bearer " + "a" * 40}, {"Authorization": "Bearer " + "o" * 40}
    base = "/api/stock-paper/short-pilot"
    with TestClient(app) as client:
        response = client.post(base + "/authorize", json=authorization().model_dump(mode="json"), headers=admin)
        assert response.status_code == 200, response.text
        opened = client.post(base + "/orders", json=order().model_dump(mode="json"), headers=operator)
        assert opened.status_code == 200, opened.text
        broker.fill(opened.json()["client_order_id"])
        assert client.post(base + "/reconcile", headers=operator).json()["positions"] == {"AAPL": "-1"}
        covered = client.post(base + "/orders", json=order(intent="cover_short",
            idempotency_key="http-short-cover-001").model_dump(mode="json"), headers=operator)
        assert covered.status_code == 200, covered.text
        broker.fill(covered.json()["client_order_id"])
        assert client.post(base + "/reconcile", headers=operator).json()["positions"] == {}
    assert len(broker.posts) == 2


def test_short_migration_roundtrip_preserves_existing_tables(tmp_path):
    from alembic import command
    from sqlalchemy import inspect
    from app.db.schema import migration_config, assert_schema_current
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    config = migration_config()
    config.set_main_option("sqlalchemy.url", str(engine.url))
    try:
        command.upgrade(config, "0066_live_mandate")
        before = set(inspect(engine).get_table_names())
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert_schema_current(engine)
        assert set(inspect(engine).get_table_names()) - before == {"paper_short_pilot", "paper_short_orders"}
        command.downgrade(config, "0066_live_mandate")
        assert set(inspect(engine).get_table_names()) == before
        command.upgrade(config, "head")
        assert_schema_current(engine)
    finally:
        engine.dispose()
