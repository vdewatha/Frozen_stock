from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register tables
from app.core.config import settings
from app.db.base import Base
from app.models import Notification, RiskRule, StockPaperRecoveryEvent, StockPaperRecoveryState
from app.services import auto_revalidation as auto
from app.services.audit import write_audit_log
from app.services.stock_paper_ledger import initialize_stock_paper_account, reconcile_stock_paper_account
from app.services.stock_recovery import enter_stock_recovery
from tests.test_stock_paper_ledger import FakeAlpaca

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'auto.sqlite'}")
    Base.metadata.create_all(engine)
    with patch.object(settings, "active_paper_broker", "alpaca_paper"), \
         patch.object(settings, "paper_broker_account_id", ""), \
         patch.object(settings, "allow_live_trading", False), \
         Session(engine) as session:
        session.add(RiskRule(name="auto-risk", value={"kill_switch_enabled": False, "paper_only": True}))
        session.commit()
        initialize_stock_paper_account(session, FakeAlpaca(), activity_contract="alpaca-activities-v2")
        yield session
    engine.dispose()


def _pause(db, actor="stock_monitor", minutes_ago=30):
    enter_stock_recovery(db, reason="Automatic stock-paper pause after persistent monitoring breach", actor=actor)
    event = db.query(StockPaperRecoveryEvent).filter_by(action="pause").order_by(StockPaperRecoveryEvent.id.desc()).first()
    event.created_at = NOW - timedelta(minutes=minutes_ago)
    state = db.query(StockPaperRecoveryState).one()
    state.cooldown_until = NOW - timedelta(minutes=1)
    db.commit()


def _run(db, *, clear=True, resume_result=None):
    with patch.object(auto, "_fresh_monitoring_is_clear", return_value=(clear, "test monitoring")), \
         patch.object(auto, "resume_stock_paper_after_revalidation",
                      return_value=resume_result or {"status": "resumable"}) as resume:
        result = auto.run_auto_revalidation(db, now=NOW)
    return result, resume


def test_system_pause_is_re_armed_automatically(db):
    _pause(db)
    result, resume = _run(db)
    assert result["status"] == "resumed"
    assert resume.call_args.kwargs["actor"] == auto.AUTO_REVALIDATION_ACTOR


def test_operator_pause_waits_for_a_human(db):
    _pause(db, actor="operator-1")
    result, resume = _run(db)
    assert result["status"] == "operator_hold"
    resume.assert_not_called()


def test_manual_kill_switch_after_the_pause_waits_for_a_human(db):
    _pause(db)
    write_audit_log(db, event_type="safety_control", entity_type="risk_rule", entity_id=1,
                    action="set_kill_switch", status="enabled", message="Operator stop", payload={})
    db.commit()
    result, resume = _run(db)
    assert result["status"] == "operator_hold"
    resume.assert_not_called()


def test_active_cooldown_and_unclear_monitoring_wait(db):
    _pause(db)
    state = db.query(StockPaperRecoveryState).one()
    state.cooldown_until = NOW + timedelta(minutes=5)
    db.commit()
    assert _run(db)[0]["status"] == "cooldown"
    state.cooldown_until = NOW - timedelta(minutes=1)
    db.commit()
    result, resume = _run(db, clear=False)
    assert result["status"] == "waiting"
    resume.assert_not_called()


def test_daily_budget_stops_a_pause_loop_and_raises_one_warning(db):
    _pause(db)
    for index in range(settings.paper_auto_revalidation_max_per_day):
        db.add(StockPaperRecoveryEvent(action="resume", status="revalidated", actor=auto.AUTO_REVALIDATION_ACTOR,
                                       reason="earlier", event_sha256=f"{index:064d}", payload={}))
    db.commit()
    for _ in range(2):
        result, resume = _run(db)
        assert result["status"] == "budget_exhausted"
        resume.assert_not_called()
    assert db.query(Notification).filter_by(category=auto.BUDGET_CATEGORY).count() == 1


def test_never_runs_when_live_trading_is_allowed(db):
    _pause(db)
    with patch.object(settings, "allow_live_trading", True):
        result, resume = _run(db)
    assert result["status"] == "disabled"
    resume.assert_not_called()


def test_real_gates_re_arm_and_clear_the_kill_switch(db):
    _pause(db, actor="stock_watchdog")
    reconcile_stock_paper_account(db, FakeAlpaca())
    with patch("app.services.stock_recovery._paper_research_policy_ready", return_value=True), \
         patch.object(auto, "_fresh_monitoring_is_clear", return_value=(True, "fresh")), \
         patch("app.services.stock_recovery._fresh_monitoring_is_clear", return_value=(True, "fresh")):
        result = auto.run_auto_revalidation(db, now=NOW)
    assert result["status"] == "resumed"
    db.expire_all()
    assert db.query(StockPaperRecoveryState).one().status == "resumable"
    assert db.query(RiskRule).one().value["kill_switch_enabled"] is False
