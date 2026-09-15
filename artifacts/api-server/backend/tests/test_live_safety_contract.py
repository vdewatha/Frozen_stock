from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.security import required_role
from app.db.base import Base
from app.models import AuditLog, LiveSafetyEvent, LiveSafetyState
from app.services.live_safety import (
    LiveSafetyError,
    evaluate_live_safety,
    live_order_decision,
    transition_live_safety,
)


def _db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _passing_evaluation() -> dict:
    gates = {
        name: {"status": "pass"}
        for name in (
            "environment_separation",
            "operator_approval",
            "broker_account",
            "current_data",
            "monitoring_health",
            "recovery_readiness",
            "immutable_model_lineage",
        )
    }
    return {"gates": gates, "status": "ready", "live_orders_allowed": False}


def test_default_state_is_paper_safe_and_live_order_gate_is_blocked():
    db = _db()
    try:
        result = evaluate_live_safety(db)
        assert result["mode"] == "research"
        assert result["status"] == "blocked"
        assert result["paper_only"] is True
        assert result["live_authorized"] is False
        assert result["live_orders_allowed"] is False
        assert result["gates"]["environment_separation"]["status"] == "fail"
        assert result["gates"]["broker_account"]["status"] == "unknown"
        assert live_order_decision(db)["live_orders_allowed"] is False
    finally:
        db.close()


def test_allowed_paper_and_shadow_transitions_and_denied_live_transition_are_audited():
    db = _db()
    try:
        transition_live_safety(db, target_mode="paper", actor="admin", reason="Paper review")
        transition_live_safety(db, target_mode="shadow", actor="admin", reason="Shadow review")
        with pytest.raises(LiveSafetyError):
            transition_live_safety(
                db,
                target_mode="canary-live",
                actor="admin",
                approval_actor="operator",
                reason="Attempt live canary",
            )
        db.commit()

        state = db.get(LiveSafetyState, 1)
        assert state.mode == "shadow"
        denied = db.scalars(
            select(LiveSafetyEvent).where(LiveSafetyEvent.action == "transition_denied")
        ).all()
        assert len(denied) == 1
        assert denied[0].to_mode == "canary-live"
        assert db.scalars(select(AuditLog).where(AuditLog.event_type == "live_safety")).first()
    finally:
        db.close()


def test_live_progression_requires_gates_and_distinct_approvals():
    db = _db()
    try:
        transition_live_safety(db, target_mode="paper", actor="admin", reason="Paper review")
        transition_live_safety(db, target_mode="shadow", actor="admin", reason="Shadow review")
        with patch(
            "app.services.live_safety.evaluate_live_safety",
            return_value=_passing_evaluation(),
        ):
            transition_live_safety(
                db,
                target_mode="canary-live",
                actor="admin",
                approval_actor="operator",
                reason="Bounded canary approval",
            )
            with pytest.raises(LiveSafetyError):
                transition_live_safety(
                    db,
                    target_mode="approved-live",
                    actor="admin",
                    approval_actor="operator",
                    secondary_approval_actor="operator",
                    reason="Invalid dual approval",
                )
            transition_live_safety(
                db,
                target_mode="approved-live",
                actor="admin",
                approval_actor="operator",
                secondary_approval_actor="reviewer",
                reason="Dual approval complete",
            )
        db.commit()
        assert db.get(LiveSafetyState, 1).mode == "approved-live"
    finally:
        db.close()


def test_emergency_stop_requires_revalidation_before_resumption():
    db = _db()
    try:
        state = LiveSafetyState(id=1, mode="canary-live", last_reason="Canary")
        db.add(state)
        db.commit()
        transition_live_safety(db, target_mode="emergency-stop", actor="operator", reason="Stop now")
        with pytest.raises(LiveSafetyError):
            transition_live_safety(db, target_mode="paper", actor="operator", reason="Resume paper")
        transition_live_safety(
            db,
            target_mode="paper",
            actor="operator",
            reason="Revalidated paper",
            evidence={"revalidation_digest": "a" * 64},
        )
        db.commit()
        assert db.get(LiveSafetyState, 1).mode == "paper"
    finally:
        db.close()


def test_live_safety_routes_have_read_only_viewer_and_admin_transition_roles():
    assert required_role("GET", "/system/live-safety") == "viewer"
    assert required_role("POST", "/system/live-safety/transition") == "admin"