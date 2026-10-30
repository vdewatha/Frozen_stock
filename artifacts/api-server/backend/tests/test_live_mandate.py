"""Standing two-person live mandate: renewal gating, lapse, promotion, revocation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
import app.models.stock_paper  # noqa: F401  (register paper tables for create_all)
from app.models import (
    LiveBrokerAccount,
    LiveBrokerAccountSnapshot,
    LiveBrokerOrder,
    LiveMandateEvent,
    LivePilot,
    LiveSafetyState,
    Notification,
)
from app.services.compliance_readiness import REQUIRED_EXTERNAL_REVIEWERS, REQUIRED_RULE_REVIEWS
from app.services.live_mandate import (
    MANDATE_ACTOR,
    LiveMandateError,
    create_live_mandate,
    live_mandate_projection,
    mandate_state,
    record_compliance_block,
    reinstate_live_mandate,
    revoke_live_mandate,
    run_live_mandate_cycle,
)
from app.services.live_pilot import ensure_live_pilot, pilot_order_decision, stop_live_pilot

UTC = timezone.utc


def _db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _compliance() -> dict:
    return {
        "launch_disposition": "approved",
        "account_owner": "organization owner",
        "jurisdictions": ["confirmed"],
        "broker_account_type": "cash",
        "broker_permissions": ["equities"],
        "prohibited_activities": ["leverage", "shorts", "options"],
        "rule_reviews": {name: "reviewed" for name in REQUIRED_RULE_REVIEWS},
        "tax_accounting_records": {
            "authoritative_broker_statements": "broker statements",
            "supporting_app_audit_evidence": "append-only audit chain",
        },
        "retention_policy": "approved policy",
        "user_disclosures": "disclosed",
        "operator_disclosures": "disclosed",
        "responsibility_owners": {
            name: f"named {name} owner"
            for name in ("approval", "monitoring", "incident", "shutdown", "investigation", "periodic_review")
        },
        "external_reviews": {name: "signed_off" for name in REQUIRED_EXTERNAL_REVIEWERS},
        "initial_live_scope": {
            "unsupported_instruments": True, "leverage": True, "shorts": True,
            "options": True, "unrestricted_automation": True,
        },
    }


def _checklist() -> dict:
    return {
        "drills": {name: True for name in (
            "emergency_stop", "rollback", "broker_uncertainty", "worker_loss", "model_demotion",
        )},
        "evidence_references": ["drill-bundle-1"],
        "compliance_readiness": _compliance(),
    }


def _launch(status: str = "pass"):
    def evaluate(db, *, model_run_id, checklist):
        return {
            "status": status,
            "gates": {"paper_evidence": {"status": status, "reason": None if status == "pass" else "red", "evidence": {}}},
            "required_gates": ["paper_evidence"],
            "fail_closed": True,
        }
    return evaluate


def _transition(db, *, target_mode, **_kwargs):
    from app.services.live_safety import ensure_live_safety_state

    state = ensure_live_safety_state(db)
    state.mode = target_mode
    return state


@pytest.fixture
def gates():
    state = {"status": "pass"}

    def evaluate(db, *, model_run_id, checklist):
        return _launch(state["status"])(db, model_run_id=model_run_id, checklist=checklist)

    with patch("app.services.live_pilot.evaluate_live_pilot_launch", side_effect=evaluate), \
            patch("app.services.live_safety.transition_live_safety", side_effect=_transition):
        yield state


def _seed(db: Session, *, mode: str = "shadow", equity: str = "10000", drawdown: str = "0") -> LiveBrokerAccount:
    db.add(LiveSafetyState(id=1, mode=mode, last_reason="test", updated_by="test"))
    account = LiveBrokerAccount(
        broker="alpaca_live", broker_account_id="acct", environment="approved-live", currency="USD",
        cash=Decimal(equity), buying_power=Decimal(equity), equity=Decimal(equity), status="reconciled",
        reconciliation_required=False, unexplained_residual=False,
        # The ordinary reconciled Alpaca /v2/account payload: no precomputed
        # risk metrics, so drawdown must come from durable equity snapshots.
        raw_payload={"equity": equity, "status": "ACTIVE"},
    )
    db.add(account)
    db.commit()
    return account


def _snapshot(db: Session, account: LiveBrokerAccount, equity: str, *, minutes: int) -> None:
    db.add(LiveBrokerAccountSnapshot(
        account_id=account.id, cash=Decimal(equity), buying_power=Decimal(equity), equity=Decimal(equity),
        observed_at=datetime.now(UTC) + timedelta(minutes=minutes), raw_payload={},
    ))
    db.commit()


def _mandate(db: Session, **overrides):
    values = dict(
        actor="admin-a", secondary_actor="admin-b", reason="standing live mandate",
        symbols=["SPY"], max_notional=Decimal("5000"), max_order_notional=Decimal("500"),
        max_pilot_days=7, renewal_lead_hours=24, observation_window_sessions=2,
        max_cumulative_loss=Decimal("500"), max_drawdown=Decimal("0.05"), rollback_target="shadow",
        model_run_id="model-1", checklist=_checklist(), expires_at=datetime.now(UTC) + timedelta(days=120),
    )
    values.update(overrides)
    mandate = create_live_mandate(db, **values)
    db.commit()
    return mandate


def _actions(db: Session) -> list[str]:
    return [row.action for row in db.query(LiveMandateEvent).order_by(LiveMandateEvent.id).all()]


def test_mandate_creation_is_two_person_bounded_and_audited(gates):
    db = _db()
    try:
        _seed(db)
        with pytest.raises(LiveMandateError, match="two distinct"):
            _mandate(db, secondary_actor="admin-a")
        with pytest.raises(LiveMandateError, match="180 days"):
            _mandate(db, expires_at=datetime.now(UTC) + timedelta(days=200))
        blocked = _checklist()
        blocked["compliance_readiness"]["external_reviews"]["legal"] = "required"
        with pytest.raises(LiveMandateError, match="compliance"):
            _mandate(db, checklist=blocked)
        mandate = _mandate(db)
        assert mandate.allowed_order_types == ["limit"]
        assert mandate_state(db, mandate)[0] == "active"
        with pytest.raises(LiveMandateError, match="already exists"):
            _mandate(db)
        amended = _mandate(db, amend=True, max_notional=Decimal("4000"))
        assert amended.supersedes_mandate_id == mandate.id
        assert mandate_state(db, mandate)[0] == "superseded"
        assert _actions(db) == ["create", "amend"]
    finally:
        db.close()


def test_green_gates_mint_a_canary_attributed_to_mandate_approvers(gates):
    db = _db()
    try:
        account = _seed(db)
        mandate = _mandate(db)
        _snapshot(db, account, "10000", minutes=1)
        result = run_live_mandate_cycle(db)
        db.commit()
        pilot = ensure_live_pilot(db)
        assert result["actions"][0]["action"] == "activate"
        assert pilot.status == "canary"
        assert pilot.mandate_id == mandate.id
        assert (pilot.primary_approval_actor, pilot.secondary_approval_actor) == ("admin-a", "admin-b")
        assert pilot.updated_by == MANDATE_ACTOR
        assert db.get(LiveSafetyState, 1).mode == "canary-live"
        window = pilot.expires_at.replace(tzinfo=UTC) - pilot.starts_at.replace(tzinfo=UTC)
        assert window <= timedelta(days=7)
    finally:
        db.close()


def test_red_gate_blocks_activation_and_alerts_once(gates):
    db = _db()
    try:
        account = _seed(db)
        _mandate(db)
        _snapshot(db, account, "10000", minutes=1)
        gates["status"] = "unknown"
        run_live_mandate_cycle(db)
        run_live_mandate_cycle(db)
        db.commit()
        assert ensure_live_pilot(db).status == "inactive"
        assert _actions(db).count("activation_blocked") == 1
        assert db.query(Notification).filter_by(category="live_mandate", severity="warning").count() == 1
    finally:
        db.close()


def _operating(db: Session, gates) -> tuple:
    account = _seed(db)
    mandate = _mandate(db)
    _snapshot(db, account, "10000", minutes=1)
    run_live_mandate_cycle(db)
    db.commit()
    return account, mandate, ensure_live_pilot(db)


def test_renewal_inside_lead_window_mints_a_new_bounded_window(gates):
    db = _db()
    try:
        _, mandate, pilot = _operating(db, gates)
        first_expiry = pilot.expires_at.replace(tzinfo=UTC)
        now = first_expiry - timedelta(hours=2)
        result = run_live_mandate_cycle(db, now=now)
        db.commit()
        assert [row["action"] for row in result["actions"]][-1] == "renew"
        renewed = ensure_live_pilot(db).expires_at.replace(tzinfo=UTC)
        assert renewed > first_expiry
        assert renewed <= now + timedelta(days=mandate.max_pilot_days)
        assert "renew" in _actions(db)
    finally:
        db.close()


def test_red_gate_holds_renewal_then_lapses_at_expiry_with_alert(gates):
    db = _db()
    try:
        _, _, pilot = _operating(db, gates)
        expiry = pilot.expires_at.replace(tzinfo=UTC)
        gates["status"] = "fail"
        held = run_live_mandate_cycle(db, now=expiry - timedelta(hours=2))
        db.commit()
        assert held["actions"][-1]["action"] == "renewal_blocked"
        assert ensure_live_pilot(db).status == "canary"
        lapsed = run_live_mandate_cycle(db, now=expiry + timedelta(minutes=1))
        db.commit()
        assert lapsed["actions"][-1]["action"] == "lapse"
        assert ensure_live_pilot(db).status == "rolled_back"
        assert db.get(LiveSafetyState, 1).mode == "shadow"
        assert db.query(Notification).filter_by(category="live_mandate", severity="critical").count() == 1
    finally:
        db.close()


def test_canary_promotes_automatically_after_clean_observation_sessions(gates):
    db = _db()
    try:
        _, mandate, pilot = _operating(db, gates)
        now = pilot.approved_at.replace(tzinfo=UTC) + timedelta(days=6)
        result = run_live_mandate_cycle(db, now=now)
        db.commit()
        assert "auto_promote" in [row["action"] for row in result["actions"]]
        pilot = ensure_live_pilot(db)
        assert pilot.status == "active"
        assert pilot.secondary_approval_actor == mandate.secondary_approval_actor
        assert db.get(LiveSafetyState, 1).mode == "approved-live"
    finally:
        db.close()


def test_canary_stays_until_sessions_complete_and_rolls_back_on_rejections(gates):
    db = _db()
    try:
        account, _, pilot = _operating(db, gates)
        early = run_live_mandate_cycle(db, now=pilot.approved_at.replace(tzinfo=UTC) + timedelta(minutes=5))
        assert early["actions"][0]["action"] == "observing"
        assert ensure_live_pilot(db).status == "canary"
        db.add(LiveBrokerOrder(
            account_id=account.id, risk_decision_id="r1", risk_decision={}, actor="test", idempotency_key="k1",
            client_order_id="c1", symbol="SPY", side="buy", quantity=Decimal("1"), order_type="limit",
            reference_price=Decimal("100"), reference_observed_at=datetime.now(UTC), status="rejected",
            created_at=datetime.now(UTC) + timedelta(minutes=1),
        ))
        db.commit()
        result = run_live_mandate_cycle(db, now=pilot.approved_at.replace(tzinfo=UTC) + timedelta(days=6))
        db.commit()
        assert result["actions"][0]["action"] == "lapse"
        assert ensure_live_pilot(db).status == "rolled_back"
    finally:
        db.close()


@pytest.mark.parametrize("trigger", [
    "cumulative_loss", "drawdown", "broker_ambiguity", "reconciliation_failure",
])
def test_market_and_broker_triggers_emergency_stop_and_suspend(gates, trigger):
    db = _db()
    try:
        account, mandate, _ = _operating(db, gates)
        if trigger == "cumulative_loss":
            _snapshot(db, account, "9400", minutes=2)
        elif trigger == "drawdown":
            # Peak 10800 then 10200: a 5.6% drawdown with no loss vs baseline.
            _snapshot(db, account, "10800", minutes=2)
            _snapshot(db, account, "10200", minutes=3)
        elif trigger == "broker_ambiguity":
            db.add(LiveBrokerOrder(
                account_id=account.id, risk_decision_id="r1", risk_decision={}, actor="test", idempotency_key="k1",
                client_order_id="c1", symbol="SPY", side="buy", quantity=Decimal("1"), order_type="limit",
                reference_price=Decimal("100"), reference_observed_at=datetime.now(UTC), status="unknown",
                uncertain_submission=True,
            ))
        else:
            account.reconciliation_required = True
        db.commit()
        result = run_live_mandate_cycle(db)
        db.commit()
        assert result["status"] == "suspended"
        assert trigger in result["actions"][0]["triggers"]
        assert ensure_live_pilot(db).status == "halted"
        assert db.get(LiveSafetyState, 1).mode == "emergency-stop"
        assert mandate_state(db, mandate)[0] == "suspended"
        assert db.query(Notification).filter_by(category="live_mandate", severity="critical").count() >= 1
        # A suspended mandate never re-mints authority by itself.
        assert run_live_mandate_cycle(db)["status"] == "suspended"
        with pytest.raises(LiveMandateError, match="emergency stop"):
            reinstate_live_mandate(db, actor="admin-a", secondary_actor="admin-b", reason="resume")
    finally:
        db.close()


def test_compliance_block_lapses_and_needs_two_people_to_reinstate(gates):
    db = _db()
    try:
        _, mandate, _ = _operating(db, gates)
        record_compliance_block(db, actor="operator-1", reason="compliance status changed to blocked")
        db.commit()
        assert mandate_state(db, mandate)[0] == "suspended"
        assert ensure_live_pilot(db).status == "rolled_back"
        assert db.get(LiveSafetyState, 1).mode == "shadow"
        with pytest.raises(LiveMandateError, match="two distinct"):
            reinstate_live_mandate(db, actor="admin-a", secondary_actor="admin-a", reason="resume")
        reinstate_live_mandate(db, actor="admin-a", secondary_actor="admin-b", reason="compliance cleared")
        db.commit()
        assert mandate_state(db, mandate)[0] == "active"
    finally:
        db.close()


def test_manual_emergency_stop_suspends_mandate_and_denies_orders(gates):
    db = _db()
    try:
        _, mandate, pilot = _operating(db, gates)
        stop_live_pilot(db, actor="operator-1", reason="operator emergency stop")
        db.commit()
        assert mandate_state(db, mandate)[0] == "suspended"
        assert "suspend" in _actions(db)
        projection = live_mandate_projection(db)
        assert projection["current"]["state"] == "suspended"
        assert projection["revocation_history"][0]["action"] == "suspend"
        assert projection["contract"]["graduation_grants_live_authority"] is False
    finally:
        db.close()


def test_order_fence_denies_orders_once_the_mandate_is_revoked(gates):
    db = _db()
    try:
        _, mandate, pilot = _operating(db, gates)
        pilot.status = "active"
        db.commit()
        observed = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
        with patch("app.services.live_mandate.mandate_state", return_value=("revoked", "test")):
            pilot.starts_at, pilot.expires_at = observed - timedelta(hours=1), observed + timedelta(days=1)
            decision = pilot_order_decision(
                db, symbol="SPY", side="buy", quantity=Decimal("1"), reference_price=Decimal("50"),
                order_type="limit", time_in_force="day", now=observed,
            )
        assert decision["allowed"] is False
        assert "mandate" in decision["reason"]
        revoke_live_mandate(db, actor="admin-a", secondary_actor="admin-b", reason="no longer needed")
        db.commit()
        assert mandate_state(db, mandate)[0] == "revoked"
        assert ensure_live_pilot(db).status == "rolled_back"
    finally:
        db.close()


def test_no_mandate_leaves_manual_pilots_untouched(gates):
    db = _db()
    try:
        _seed(db)
        assert run_live_mandate_cycle(db)["status"] == "no_mandate"
        assert ensure_live_pilot(db).status == "inactive"
    finally:
        db.close()


def test_mandate_routes_have_explicit_roles():
    from app.core.security import required_role

    assert required_role("GET", "/system/live-pilot/mandate") == "viewer"
    for path in ("", "/amend", "/revoke", "/reinstate"):
        assert required_role("POST", f"/system/live-pilot/mandate{path}") == "admin"
    assert required_role("POST", "/system/live-pilot/mandate/compliance-block") == "operator"


def test_drawdown_is_derived_from_reconciled_snapshots_and_fails_closed():
    from app.services.live_mandate import live_drawdown

    db = _db()
    try:
        with patch("app.services.live_pilot.evaluate_live_pilot_launch", side_effect=_launch("pass")):
            account = _seed(db)
            mandate = _mandate(db)
            assert live_drawdown(db, mandate)["status"] == "unknown"
            _snapshot(db, account, "10000", minutes=1)
            _snapshot(db, account, "10500", minutes=2)
            _snapshot(db, account, "10290", minutes=3)
            measured = live_drawdown(db, mandate)
            assert measured["status"] == "measured"
            assert Decimal(measured["strategy_drawdown"]) == Decimal("0.02")
            assert measured["breached"] is False
            account.raw_payload = {"risk_metrics": {"daily_drawdown": "not-a-number"}}
            db.commit()
            assert live_drawdown(db, mandate)["status"] == "unknown"
    finally:
        db.close()


@pytest.mark.parametrize("human_action", ["rollback", "emergency_stop"])
def test_human_stop_after_amendment_suspends_successor_and_is_not_undone(gates, human_action):
    from app.services.live_pilot import rollback_live_pilot

    db = _db()
    try:
        _, original, pilot = _operating(db, gates)
        assert pilot.mandate_id == original.id
        amended = _mandate(db, amend=True, max_notional=Decimal("4000"))
        assert mandate_state(db, amended)[0] == "active"
        if human_action == "rollback":
            rollback_live_pilot(db, actor="operator-1", reason="operator rollback")
        else:
            stop_live_pilot(db, actor="operator-1", reason="operator emergency stop")
            db.commit()
            # The existing two-person emergency-stop reset returns to shadow.
            rollback_live_pilot(db, actor="admin-a", secondary_actor="admin-b", reason="reset after review")
        db.commit()
        assert mandate_state(db, amended)[0] == "suspended"
        assert db.get(LiveSafetyState, 1).mode in {"shadow", "paper"}
        for _ in range(2):
            assert run_live_mandate_cycle(db)["status"] == "suspended"
            db.commit()
        assert ensure_live_pilot(db).status == "rolled_back"
    finally:
        db.close()


def test_approved_live_requires_a_verified_secondary_identity():
    from types import SimpleNamespace
    from fastapi import HTTPException
    import app.api.live_pilot as api

    request = SimpleNamespace(headers={}, state=SimpleNamespace(actor="admin-a"))
    with patch.object(api.settings, "environment", "approved-live"), \
            patch.object(api.settings, "live_environment_name", "approved-live"):
        with pytest.raises(HTTPException) as denied:
            api._secondary(request, "admin-b", "admin")
        assert denied.value.status_code == 403
    with patch.object(api.settings, "environment", "local"):
        assert api._secondary(request, "admin-b", "admin") == "admin-b"
