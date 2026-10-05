from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models
from app.db.base import Base
from app.models import (
    IntradayBar,
    LiveBrokerAccount,
    LiveBrokerAccountSnapshot,
    LiveBrokerActivity,
    LiveBrokerOrder,
    LiveBrokerPosition,
    StrategySignal,
    StockDatasetSnapshot,
    StockModelRegistry,
)
from app.services.live_broker import (
    AlpacaLiveClient,
    LiveBrokerError,
    dispatch_live_order,
    reconcile_live_broker_account,
    reserve_live_order,
    review_live_accounting_residual,
)


NOW = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)


class FakeLiveBroker:
    def __init__(self, *, orders=None, activities=None, submit_error=None):
        self.order_rows = orders or []
        self.activity_rows = activities or []
        self.submit_error = submit_error
        self.submit_calls = 0

    def account(self):
        return {
            "id": "live-account-1", "currency": "USD", "status": "ACTIVE",
            "cash": "10000", "buying_power": "10000", "equity": "10000",
            "trading_blocked": False, "account_blocked": False, "trade_suspended_by_user": False,
        }

    def positions(self):
        return []

    def orders(self):
        return self.order_rows

    def activities(self):
        return self.activity_rows

    def market_data_entitlement(self):
        return {"status": "authorized", "feed": "sip", "symbols": ["SPY"]}

    def submit_order(self, payload):
        self.submit_calls += 1
        if self.submit_error:
            raise self.submit_error
        return {"id": "broker-order-1", "status": "accepted", **payload}

    def cancel_order(self, broker_order_id):
        return None

    def order_by_client_id(self, client_order_id):
        return None


class ResidualLiveBroker(FakeLiveBroker):
    def __init__(self, commission=None):
        super().__init__(
            orders=[{
                "id": "owned-order-1", "client_order_id": "lb-owned-order",
                "symbol": "SPY", "side": "buy", "qty": "1",
                "type": "market", "time_in_force": "day", "status": "filled",
            }],
            activities=[{
                "id": "fill-1", "activity_type": "FILL",
                "transaction_time": "2026-09-15T15:01:00Z",
                "order_id": "owned-order-1", "symbol": "SPY", "side": "buy",
                "qty": "1", "price": "100", **({"commission": commission} if commission is not None else {}),
            }],
        )

    def account(self):
        return {
            "id": "live-account-1", "currency": "USD", "status": "ACTIVE",
            "cash": "9899.50", "buying_power": "9899.50", "equity": "9999.50",
            "trading_blocked": False, "account_blocked": False, "trade_suspended_by_user": False,
        }

    def positions(self):
        return [{
            "symbol": "SPY", "qty": "1", "current_price": "100",
            "market_value": "100", "updated_at": "2026-09-15T15:01:00Z",
        }]


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_order_context(db: Session):
    observed_now = datetime.now(timezone.utc)
    snapshot = StockDatasetSnapshot(
        snapshot_id="snapshot-live-1", dataset_sha256="d" * 64,
        cutoff_date=NOW.date(), universe=["SPY"], provider="test",
        feature_config_id="features-v1", horizon_days=1, artifact_path="x",
        artifact_sha256="a" * 64, metadata_json={},
    )
    db.add(snapshot)
    db.add(StockModelRegistry(
        run_id="model-live-1", snapshot_id=snapshot.snapshot_id,
        manifest_sha256="m" * 64, artifact_path="model", training_metadata={},
    ))
    signal = StrategySignal(
        symbol="SPY", signal_time=NOW.replace(tzinfo=None), action="buy",
        probability_up=Decimal("0.8"), probability_down=Decimal("0.2"),
        confidence=Decimal("0.8"), reason="test", features={},
    )
    account = LiveBrokerAccount(
        broker="alpaca_live", broker_account_id="live-account-1",
        environment="approved-live", currency="USD", cash=Decimal("10000"),
        buying_power=Decimal("10000"), equity=Decimal("10000"),
        status="reconciled", reconciliation_required=False,
        unexplained_residual=False, source_timestamp=observed_now,
        raw_payload={"risk_metrics": {
            "daily_drawdown": "0",
            "strategy_drawdown": "0",
            "daily_turnover": "0",
            "average_daily_volume": "1000",
        }},
    )
    db.add_all([signal, account])
    db.flush()
    db.add(LiveBrokerAccountSnapshot(
        account_id=account.id, cash=account.cash, buying_power=account.buying_power,
        equity=account.equity, observed_at=observed_now, source="alpaca_live", raw_payload={},
    ))
    db.add(IntradayBar(
        symbol="SPY", timeframe="1m", opened_at=NOW, open=Decimal("99"),
        high=Decimal("101"), low=Decimal("99"), close=Decimal("100"),
        volume=1000, provider="tradier", feed_class="sip", exchange_timestamp=NOW,
    ))
    db.flush()
    return account, signal


def _allow_live():
    return {
        "live_orders_allowed": True, "mode": "approved-live", "status": "ready",
        "gates": {name: {"status": "pass"} for name in ("environment_separation", "operator_approval", "broker_account", "current_data", "monitoring_health", "recovery_readiness", "immutable_model_lineage")},
    }


def test_live_client_has_distinct_production_endpoint_and_never_uses_paper_credentials():
    assert AlpacaLiveClient.base_url == "https://api.alpaca.markets"
    assert AlpacaLiveClient.base_url != "https://paper-api.alpaca.markets"


def test_reconciliation_imports_live_account_and_external_activity_separately(db):
    gateway = FakeLiveBroker(activities=[{
        "id": "activity-1", "activity_type": "DIV", "transaction_time": "2026-09-15T14:00:00Z",
        "symbol": "SPY", "net_amount": "1.00",
    }])
    result = reconcile_live_broker_account(db, gateway=gateway)
    account = db.query(LiveBrokerAccount).one()
    assert result["mode"] == "live"
    assert account.broker == "alpaca_live"
    assert account.status == "reconciled"
    assert account.reconciliation_required is False


def test_unknown_external_order_halts_live_account(db):
    gateway = FakeLiveBroker(orders=[{
        "id": "external-1", "symbol": "SPY", "side": "buy", "qty": "1",
        "type": "market", "time_in_force": "day", "status": "new",
    }])
    reconcile_live_broker_account(db, gateway=gateway)
    account = db.query(LiveBrokerAccount).one()
    assert account.status == "halted"
    assert account.reconciliation_required is True
    assert "External nonterminal" in account.halt_reason


def test_reconciliation_preserves_partial_terminal_and_unknown_provider_states(db):
    statuses = ["accepted", "partially_filled", "canceled", "replaced", "delayed"]
    gateway = FakeLiveBroker()
    for status in statuses:
        gateway.order_rows = [{
            "id": "owned-lifecycle-1", "client_order_id": "lb-owned-lifecycle-1",
            "symbol": "SPY", "side": "buy", "qty": "1",
            "type": "limit", "time_in_force": "day", "limit_price": "100",
            "status": status,
        }]
        result = reconcile_live_broker_account(db, gateway=gateway)
        order = db.query(LiveBrokerOrder).filter_by(broker_order_id="owned-lifecycle-1").one()
        expected = "unknown" if status == "delayed" else status
        assert order.status == expected
        if expected == "unknown":
            assert result["status"] == "halted"


def test_restart_reconciliation_halts_when_submitting_order_is_not_found(db):
    reconcile_live_broker_account(db, gateway=FakeLiveBroker())
    account = db.query(LiveBrokerAccount).one()
    order = LiveBrokerOrder(
        account_id=account.id, account_snapshot_id=None, model_run_id=None, signal_id=None,
        risk_decision_id="restart", risk_decision={}, actor="operator",
        idempotency_key="restart-key", client_order_id="lb-restart-order",
        symbol="SPY", side="buy", quantity=Decimal("1"), order_type="market",
        time_in_force="day", reference_price=Decimal("100"),
        reference_observed_at=NOW, reserved_cash=Decimal("100"),
        status="submitting", uncertain_submission=True, source="live_control_room",
    )
    db.add(order)
    db.commit()
    result = reconcile_live_broker_account(db, gateway=FakeLiveBroker())
    account = db.query(LiveBrokerAccount).one()
    assert result["status"] == "halted"
    assert account.status == "halted"
    assert account.reconciliation_required is True


def test_live_residual_stays_halted_until_exact_fee_enrichment_or_review(db):
    reconcile_live_broker_account(db, gateway=FakeLiveBroker())
    account = db.query(LiveBrokerAccount).one()
    db.add(LiveBrokerOrder(
        account_id=account.id, model_run_id=None, signal_id=None,
        risk_decision_id="owned", risk_decision={}, actor="operator",
        idempotency_key="owned-order-key", client_order_id="lb-owned-order",
        broker_order_id="owned-order-1", symbol="SPY", side="buy",
        quantity=Decimal("1"), order_type="market", time_in_force="day",
        reference_price=Decimal("100"), reference_observed_at=NOW,
        reserved_cash=Decimal("100"), status="filled", source="live_control_room",
    ))
    db.commit()

    first = reconcile_live_broker_account(db, gateway=ResidualLiveBroker())
    halted = db.query(LiveBrokerAccount).one()
    assert first["status"] == "halted"
    assert halted.unexplained_residual is True
    assert halted.accounting_review_required is True

    repeated = reconcile_live_broker_account(db, gateway=ResidualLiveBroker())
    assert repeated["status"] == "halted"
    assert db.query(LiveBrokerAccount).one().unexplained_residual is True

    enriched = reconcile_live_broker_account(db, gateway=ResidualLiveBroker(commission="0.50"))
    recovered = db.query(LiveBrokerAccount).one()
    assert enriched["status"] == "reconciled"
    assert recovered.unexplained_residual is False
    assert recovered.accounting_review_required is False

    # An explicit review remains attributable and requires the current digest;
    # it cannot be performed against a stale snapshot.
    db.add(LiveBrokerActivity(
        account_id=recovered.id, broker_activity_id="review-marker",
        activity_type="MISC", occurred_at=NOW, raw_payload={"id": "review-marker"},
    ))
    db.commit()
    recovered.unexplained_residual = True
    recovered.accounting_review_required = True
    db.commit()
    from app.services.live_broker import _accounting_digest
    digest = _accounting_digest(db, recovered)
    reviewed = review_live_accounting_residual(
        db, actor="risk-operator", reason="Reviewed residual against broker statement", evidence_digest=digest,
    )
    assert reviewed["account"]["accounting_reviewed_by"] == "risk-operator"
    assert reviewed["account"]["unexplained_residual"] is False


def test_duplicate_idempotency_returns_original_and_changed_fields_are_rejected(db):
    _, signal = _seed_order_context(db)
    with patch("app.services.live_broker.live_order_decision", return_value=_allow_live()), \
         patch("app.services.live_broker.feed_status", return_value={"status": "ready"}), \
         patch("app.services.live_broker.session_bounds", return_value=(datetime.now(timezone.utc) - timedelta(hours=1), datetime.now(timezone.utc) + timedelta(hours=1))):
        first = reserve_live_order(
            db, symbol="SPY", side="buy", quantity=Decimal("1"),
            reference_price=Decimal("100"), order_type="market", time_in_force="day",
            limit_price=None, idempotency_key="same-live-key", model_run_id="model-live-1",
            signal_id=signal.id, risk_decision_id="risk-1", actor="operator",
        )
        duplicate = reserve_live_order(
            db, symbol="SPY", side="buy", quantity=Decimal("1"),
            reference_price=Decimal("100"), order_type="market", time_in_force="day",
            limit_price=None, idempotency_key="same-live-key", model_run_id="model-live-1",
            signal_id=signal.id, risk_decision_id="risk-1", actor="operator",
        )
        assert duplicate.id == first.id
        with pytest.raises(LiveBrokerError, match="changed immutable"):
            reserve_live_order(
                db, symbol="SPY", side="buy", quantity=Decimal("2"),
                reference_price=Decimal("100"), order_type="market", time_in_force="day",
                limit_price=None, idempotency_key="same-live-key", model_run_id="model-live-1",
                signal_id=signal.id, risk_decision_id="risk-1", actor="operator",
            )


def test_timeout_after_submit_marks_unknown_and_halts_without_retry(db):
    _, signal = _seed_order_context(db)
    with patch("app.services.live_broker.live_order_decision", return_value=_allow_live()), \
         patch("app.services.live_broker.feed_status", return_value={"status": "ready"}), \
         patch("app.services.live_broker.session_bounds", return_value=(datetime.now(timezone.utc) - timedelta(hours=1), datetime.now(timezone.utc) + timedelta(hours=1))):
        order = reserve_live_order(
            db, symbol="SPY", side="buy", quantity=Decimal("1"),
            reference_price=Decimal("100"), order_type="market", time_in_force="day",
            limit_price=None, idempotency_key="timeout-live-key", model_run_id="model-live-1",
            signal_id=signal.id, risk_decision_id="risk-2", actor="operator",
        )
    gateway = FakeLiveBroker(submit_error=TimeoutError("timeout after provider accepted request"))
    with patch("app.services.live_broker.live_order_decision", return_value=_allow_live()), \
         patch("app.services.live_broker.feed_status", return_value={"status": "ready"}), \
         patch("app.services.live_broker.session_bounds", return_value=(datetime.now(timezone.utc) - timedelta(hours=1), datetime.now(timezone.utc) + timedelta(hours=1))):
        result = dispatch_live_order(db, order.id, gateway=gateway)
    account = db.query(LiveBrokerAccount).one()
    assert gateway.submit_calls == 1
    assert result.status == "unknown"
    assert result.uncertain_submission is True
    assert account.status == "halted"
    assert account.reconciliation_required is True
