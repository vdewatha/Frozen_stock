"""A pause that lands during a pause joins it; auto-resume must explain the whole chain.

2026-11-06: a 03:00 ET monitor pause (broker/reconciliation lag) was still in
cooldown when deploy restarts made the watchdog pause again for stale
heartbeats.  The watchdog recorded "not tradable before the pause", so
auto-resume asked for an operator although both causes had cleared.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import app.services.stock_recovery as recovery
from app.models import RiskRule, StockPaperRecoveryEvent, StockPaperRecoveryState
from app.models.stock_paper import StockPaperAccount

from tests.test_unattended_paper import _account, _db, _paused, _resume

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 17, 0, tzinfo=UTC)
WATCHDOG = {"heartbeat_reasons": ["continuous monitor heartbeat is stale"]}


def _second_pause(db, at, *, actor="stock_watchdog", reason="Independent watchdog: continuous monitor heartbeat is stale",
                  context=None):
    state = db.get(StockPaperRecoveryState, 1)
    rule = db.query(RiskRule).first()
    context = context if context is not None else {
        "prior_status": state.status,
        "prior_kill_switch": bool(rule.value.get("kill_switch_enabled")),
        **WATCHDOG,
    }
    with patch.object(recovery, "_now", return_value=at):
        recovery.enter_stock_recovery(db, reason=reason, actor=actor, context=context)
    event = db.query(StockPaperRecoveryEvent).filter_by(action="pause").order_by(StockPaperRecoveryEvent.id.desc()).first()
    event.created_at = at  # the row default reads the real clock
    db.commit()
    return event


def test_a_pause_during_a_pause_inherits_the_original_state_and_auto_resumes_when_both_clear():
    db = _db()
    _account(db, NOW, halt_reason="pause")
    _paused(db, NOW, actor="stock_monitor", payload={"monitor_breach_keys": ["health.broker", "health.reconciliation"]},
            paused_minutes_ago=540)
    first = db.query(StockPaperRecoveryEvent).filter_by(action="pause").one()
    db.commit()
    second = _second_pause(db, NOW - timedelta(minutes=30))
    payload = second.payload
    assert payload["chained_pause_event_ids"] == [first.id]
    assert (payload["prior_status"], payload["prior_kill_switch"]) == ("armed", False)
    assert (payload["observed_prior_status"], payload["observed_prior_kill_switch"]) == ("cooldown", True)

    classification = recovery.classify_recovery_halt(db, now=NOW)
    assert classification["explained"] is True, classification
    assert classification["class"] == "stale_heartbeat+transient_broker"
    assert classification["chained_pause_event_ids"] == [first.id]

    # A clean reconciliation after the second pause (the account halt it set).
    account = db.get(StockPaperAccount, 1)
    account.halted_at = NOW - timedelta(minutes=30)
    account.reconciliation_required = False
    account.last_reconciled_at = NOW - timedelta(minutes=1)
    db.commit()
    result = _resume(db, NOW)
    assert result["status"] == "resumed", result
    assert db.get(StockPaperRecoveryState, 1).status == "resumable"
    assert db.query(RiskRule).first().value["kill_switch_enabled"] is False


def test_an_unexplained_earlier_pause_still_needs_the_operator():
    db = _db()
    _account(db, NOW, halt_reason="pause")
    _paused(db, NOW, actor="admin", payload={}, paused_minutes_ago=120)
    db.commit()
    _second_pause(db, NOW - timedelta(minutes=30))
    classification = recovery.classify_recovery_halt(db, now=NOW)
    assert classification["explained"] is False
    assert classification["reason"].startswith("Earlier pause") and "admin" in classification["reason"]
    assert _resume(db, NOW)["status"] == "needs_operator"


def test_an_unexplained_new_pause_still_needs_the_operator():
    db = _db()
    _account(db, NOW, halt_reason="pause")
    _paused(db, NOW, actor="stock_monitor", payload={"monitor_breach_keys": ["health.broker"]}, paused_minutes_ago=120)
    db.commit()
    _second_pause(db, NOW - timedelta(minutes=30), context={
        "prior_status": "cooldown", "prior_kill_switch": True,
        "heartbeat_reasons": ["broker reconciliation heartbeat is future-dated"],
    })
    classification = recovery.classify_recovery_halt(db, now=NOW)
    assert classification["explained"] is False and "future-dated" in classification["reason"]


def test_a_missing_earlier_pause_fails_closed():
    db = _db()
    _account(db, NOW, halt_reason="pause")
    _paused(db, NOW, actor="stock_watchdog", payload={**WATCHDOG, "chained_pause_event_ids": [999]})
    db.commit()
    classification = recovery.classify_recovery_halt(db, now=NOW)
    assert classification["explained"] is False and "missing" in classification["reason"]


def test_operator_pauses_and_pauses_from_an_armed_state_do_not_chain():
    db = _db()
    _account(db, NOW, halt_reason="pause")
    _paused(db, NOW, actor="stock_monitor", payload={"monitor_breach_keys": ["health.broker"]}, paused_minutes_ago=120)
    db.commit()
    operator = _second_pause(db, NOW - timedelta(minutes=30), actor="admin", reason="Operator pause", context={})
    assert "chained_pause_event_ids" not in (operator.payload or {})

    fresh = _db()
    _account(fresh, NOW, status="reconciled")
    fresh.add(StockPaperRecoveryState(id=1, status="armed", flatten_policy="none", updated_by="test"))
    fresh.query(RiskRule).first().value = {"kill_switch_enabled": False}
    fresh.commit()
    pause = _second_pause(fresh, NOW - timedelta(minutes=30))
    assert "chained_pause_event_ids" not in pause.payload
    assert (pause.payload["prior_status"], pause.payload["prior_kill_switch"]) == ("armed", False)


def test_the_heartbeat_restore_shortcut_never_restores_a_chain():
    db = _db()
    account = _account(db, NOW, halt_reason="pause")
    _paused(db, NOW, actor="stock_monitor", payload={"monitor_breach_keys": ["health.broker"]}, paused_minutes_ago=120)
    db.commit()
    _second_pause(db, NOW - timedelta(minutes=30))
    state = db.get(StockPaperRecoveryState, 1)
    state.last_monitor_heartbeat_at = NOW
    db.commit()
    with patch.object(recovery, "_now", return_value=NOW):
        assert recovery.restore_after_transient_heartbeat_pause(db, account) is False
    assert db.get(StockPaperRecoveryState, 1).status == "cooldown"
