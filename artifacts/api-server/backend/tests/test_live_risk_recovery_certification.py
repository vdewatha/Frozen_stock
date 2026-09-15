"""Isolated live-boundary certification drills.

These tests use database fixtures and provider-shaped gateways only.  They
must never require live credentials or submit to an external broker.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import (
    LiveBrokerAccount,
    LiveBrokerOrder,
    LiveBrokerPosition,
    LiveSafetyState,
    RiskRule,
)
from app.services.live_broker import (
    LiveBrokerError,
    _live_risk_gate,
    cancel_live_order,
    flatten_live_positions,
)


NOW = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)


def _db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _account(db: Session, *, metrics: dict | None = None) -> LiveBrokerAccount:
    account = LiveBrokerAccount(
        broker="alpaca_live",
        broker_account_id="isolated-certification-account",
        environment="approved-live",
        currency="USD",
        cash=Decimal("10000"),
        buying_power=Decimal("10000"),
        equity=Decimal("10000"),
        status="reconciled",
        reconciliation_required=False,
        unexplained_residual=False,
        source_timestamp=datetime.now(timezone.utc),
        raw_payload={"risk_metrics": metrics} if metrics is not None else {},
    )
    db.add(account)
    db.flush()
    return account


def _metrics(**overrides: str) -> dict[str, str]:
    values = {
        "daily_drawdown": "0",
        "strategy_drawdown": "0",
        "daily_turnover": "0",
        "average_daily_volume": "1000",
    }
    values.update(overrides)
    return values


def _gate(db: Session, *, metrics: dict | None = None, rule: dict | None = None) -> tuple[Session, LiveBrokerAccount]:
    account = _account(db, metrics=metrics if metrics is not None else _metrics())
    if rule is not None:
        db.add(RiskRule(name="certification-rule", value=rule, is_active=True))
        db.flush()
    return db, account


def test_pretrade_risk_dimensions_are_fail_closed():
    cases = [
        ("missing risk evidence", {}, {}, "daily drawdown evidence"),
        ("daily loss", _metrics(daily_drawdown="0.04"), {}, "daily drawdown"),
        ("strategy drawdown", _metrics(strategy_drawdown="0.11"), {}, "strategy drawdown"),
        ("turnover", _metrics(daily_turnover="0.50"), {}, "turnover"),
        ("liquidity", _metrics(average_daily_volume="5"), {}, "liquidity"),
    ]
    for _, metrics, rule, reason in cases:
        db = _db()
        try:
            _, account = _gate(db, metrics=metrics, rule=rule)
            with pytest.raises(LiveBrokerError, match=reason):
                _live_risk_gate(
                    db, account, "SPY", "buy", Decimal("1"), Decimal("100")
                )
        finally:
            db.close()


def test_concentration_and_leverage_limits_are_checked_against_projected_exposure():
    db = _db()
    try:
        _, account = _gate(
            db,
            rule={"max_total_exposure": "2.0", "max_symbol_exposure": "0.10", "max_live_leverage": "0.05"},
        )
        db.add(LiveBrokerPosition(
            account_id=account.id,
            symbol="SPY",
            quantity=Decimal("100"),
            current_price=Decimal("100"),
            market_value=Decimal("10000"),
            observed_at=NOW,
            raw_payload={},
        ))
        db.commit()
        with pytest.raises(LiveBrokerError, match="symbol exposure"):
            _live_risk_gate(db, account, "SPY", "buy", Decimal("1"), Decimal("100"))

        db.rollback()
        db.query(LiveBrokerPosition).delete()
        db.commit()
        with pytest.raises(LiveBrokerError, match="leverage"):
            _live_risk_gate(db, account, "SPY", "buy", Decimal("10"), Decimal("100"))
    finally:
        db.close()


def test_shorting_and_open_order_limits_are_blocked():
    db = _db()
    try:
        _, account = _gate(db, rule={"max_live_open_orders": 1})
        with pytest.raises(LiveBrokerError, match="long inventory"):
            _live_risk_gate(db, account, "SPY", "sell", Decimal("1"), Decimal("100"))

        db.add(LiveBrokerOrder(
            account_id=account.id,
            risk_decision_id="certification-pending",
            risk_decision={},
            actor="certification",
            idempotency_key="certification-pending",
            client_order_id="certification-pending",
            symbol="SPY",
            side="buy",
            quantity=Decimal("1"),
            order_type="limit",
            time_in_force="day",
            reference_price=Decimal("100"),
            reference_observed_at=NOW,
            reserved_cash=Decimal("100"),
            status="accepted",
            raw_payload={},
        ))
        db.commit()
        with pytest.raises(LiveBrokerError, match="order count"):
            _live_risk_gate(db, account, "SPY", "buy", Decimal("1"), Decimal("100"))
    finally:
        db.close()


def test_kill_switch_blocks_even_with_complete_risk_evidence():
    db = _db()
    try:
        _, account = _gate(db, rule={"kill_switch_enabled": True})
        with pytest.raises(LiveBrokerError, match="kill switch"):
            _live_risk_gate(db, account, "SPY", "buy", Decimal("1"), Decimal("100"))
    finally:
        db.close()


class _RecoveryGateway:
    def __init__(self, error: Exception | None = None):
        self.calls = 0
        self.error = error

    def submit_order(self, payload: dict) -> dict:
        self.calls += 1
        if self.error:
            raise self.error
        return {"id": "recovery-provider-order", "status": "accepted", **payload}

    def cancel_order(self, broker_order_id: str) -> None:
        self.calls += 1


def test_flatten_is_idempotent_and_never_claims_success_for_nonterminal_broker_state():
    db = _db()
    try:
        account = _account(db, metrics=None)
        db.add(LiveSafetyState(id=1, mode="canary-live", last_reason="certification"))
        db.add(LiveBrokerPosition(
            account_id=account.id,
            symbol="SPY",
            quantity=Decimal("2"),
            current_price=Decimal("100"),
            market_value=Decimal("200"),
            observed_at=NOW,
            raw_payload={},
        ))
        db.commit()
        gateway = _RecoveryGateway()
        with patch("app.services.live_broker._fresh_market_data", return_value={"status": "pass"}):
            first = flatten_live_positions(
                db, actor="risk-operator", reason="Contain live exposure", gateway=gateway
            )
            second = flatten_live_positions(
                db, actor="risk-operator", reason="Repeat containment", gateway=gateway
            )
        assert first["status"] == "halted"
        assert first["unresolved_order_ids"]
        assert second["status"] == "halted"
        assert len(db.query(LiveBrokerOrder).all()) == 1
        assert gateway.calls == 1
    finally:
        db.close()


def test_cancel_does_not_repeat_a_pending_provider_request():
    db = _db()
    try:
        account = _account(db)
        order = LiveBrokerOrder(
            account_id=account.id,
            broker_order_id="provider-order",
            risk_decision_id="cancel-certification",
            risk_decision={},
            actor="operator",
            idempotency_key="cancel-certification",
            client_order_id="cancel-certification",
            symbol="SPY",
            side="buy",
            quantity=Decimal("1"),
            order_type="limit",
            time_in_force="day",
            reference_price=Decimal("100"),
            reference_observed_at=NOW,
            reserved_cash=Decimal("100"),
            status="accepted",
            raw_payload={},
        )
        db.add(order)
        db.commit()
        gateway = _RecoveryGateway()
        first = cancel_live_order(
            db, order.id, actor="operator", reason="Contain order", gateway=gateway
        )
        second = cancel_live_order(
            db, order.id, actor="operator", reason="Repeat containment", gateway=gateway
        )
        assert first.status == "pending_cancel"
        assert second.status == "pending_cancel"
        assert gateway.calls == 1
    finally:
        db.close()