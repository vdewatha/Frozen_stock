from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import LiveBrokerAccount, LivePilot, LiveSafetyState
from app.services.live_pilot import (
    LivePilotError,
    activate_live_pilot,
    ensure_live_pilot,
    pilot_order_decision,
)


def _db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _checklist() -> dict:
    return {
        "drills": {
            "emergency_stop": True,
            "rollback": True,
            "broker_uncertainty": True,
            "worker_loss": True,
            "model_demotion": True,
        },
        "evidence_references": ["drill-bundle-1"],
    }


def test_pilot_is_inactive_and_live_order_is_denied_by_default():
    db = _db()
    try:
        assert ensure_live_pilot(db).status == "inactive"
        result = pilot_order_decision(
            db,
            symbol="SPY",
            side="buy",
            quantity=Decimal("1"),
            reference_price=Decimal("100"),
            order_type="limit",
            time_in_force="day",
        )
        assert result["allowed"] is False
        assert "not active" in result["reason"]
    finally:
        db.close()


def test_activation_requires_distinct_approval_actors_and_launch_evidence():
    db = _db()
    try:
        db.add(LiveSafetyState(id=1, mode="shadow", last_reason="test", updated_by="test"))
        db.commit()
        with pytest.raises(LivePilotError, match="two distinct"):
            activate_live_pilot(
                db,
                actor="admin",
                secondary_actor="admin",
                reason="start canary",
                symbols=["SPY"],
                max_notional=Decimal("10000"),
                max_order_notional=Decimal("1000"),
                starts_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(days=5),
                observation_window_sessions=5,
                rollback_target="paper",
                model_run_id="model-1",
                paper_expectations={"accuracy": "0.60"},
                checklist=_checklist(),
            )
    finally:
        db.close()


def test_active_pilot_enforces_allowlist_order_type_and_budget():
    db = _db()
    try:
        observed = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)
        db.add(LivePilot(
            id=1,
            status="active",
            symbols=["SPY"],
            max_notional=Decimal("1000"),
            max_order_notional=Decimal("100"),
            allowed_order_types=["limit"],
            time_in_force="day",
            session_policy="regular",
            starts_at=observed - timedelta(minutes=5),
            expires_at=observed + timedelta(days=1),
            observation_window_sessions=1,
            rollback_target="paper",
            model_run_id="model-1",
            paper_expectations={},
            launch_checklist={},
            updated_by="test",
        ))
        db.add(LiveBrokerAccount(
            broker="alpaca_live",
            broker_account_id="acct",
            environment="approved-live",
            currency="USD",
            cash=Decimal("10000"),
            buying_power=Decimal("10000"),
            equity=Decimal("10000"),
            status="reconciled",
            reconciliation_required=False,
            unexplained_residual=False,
            raw_payload={"risk_metrics": {
                "daily_drawdown": "0",
                "strategy_drawdown": "0",
                "daily_turnover": "0",
                "average_daily_volume": "1000",
            }},
        ))
        db.commit()
        allowed = pilot_order_decision(
            db, symbol="SPY", side="buy", quantity=Decimal("1"),
            reference_price=Decimal("50"), order_type="limit", time_in_force="day", now=observed,
        )
        assert allowed["allowed"] is True
        denied_symbol = pilot_order_decision(
            db, symbol="QQQ", side="buy", quantity=Decimal("1"),
            reference_price=Decimal("50"), order_type="limit", time_in_force="day", now=observed,
        )
        assert denied_symbol["allowed"] is False
        denied_budget = pilot_order_decision(
            db, symbol="SPY", side="buy", quantity=Decimal("3"),
            reference_price=Decimal("50"), order_type="limit", time_in_force="day", now=observed,
        )
        assert denied_budget["allowed"] is False
        assert "per-order" in denied_budget["reason"]
        denied_market = pilot_order_decision(
            db, symbol="SPY", side="buy", quantity=Decimal("1"),
            reference_price=Decimal("50"), order_type="market", time_in_force="day", now=observed,
        )
        assert denied_market["allowed"] is False
    finally:
        db.close()