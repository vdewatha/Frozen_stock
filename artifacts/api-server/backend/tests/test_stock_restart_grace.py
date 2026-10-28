"""Bounded post-restart grace: defer restart staleness, never other breaches."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models import RiskRule, StockPaperRecoveryEvent, StockPaperRecoveryState
from app.models.stock_paper import StockPaperAccount
from app.services import stock_monitoring, stock_recovery, stock_restart_grace
from app.services.stock_paper_ledger import active_paper_broker_name
from app.services.stock_recovery import recovery_status, run_stock_watchdog
from app.services.stock_restart_grace import restart_warmup_status

UTC = timezone.utc
BOOT = datetime(2026, 10, 28, 14, 0, tzinfo=UTC)  # worker start of the new generation
BOOT_ENV = {
    "PAPER_BOOT_ID": "boot-under-test",
    "PAPER_BOOT_AT": (BOOT - timedelta(minutes=3)).isoformat(),
    "PAPER_WORKERS_STARTED_AT": BOOT.isoformat(),
    "PAPER_BUILD_ID": "build-123",
}


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(RiskRule(name="grace-risk", value={"kill_switch_enabled": False, "paper_only": True}))
        session.add(StockPaperAccount(
            broker=active_paper_broker_name(), broker_account_id="paper-test",
            cash=Decimal("1000"), buying_power=Decimal("1000"), equity=Decimal("1000"),
            status="reconciled", reconciliation_required=False, unexplained_residual=False,
            # Last written by the previous generation, 11 minutes before BOOT+1m.
            last_reconciled_at=BOOT - timedelta(minutes=10), raw_payload={},
        ))
        session.add(StockPaperRecoveryState(
            id=1, status="resumable", flatten_policy="none", updated_by="test",
            last_monitor_heartbeat_at=BOOT - timedelta(minutes=10),
            last_watchdog_heartbeat_at=BOOT - timedelta(minutes=4),
        ))
        session.commit()
        yield session
    engine.dispose()


@contextmanager
def clock(now, env=BOOT_ENV):
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        for module in (stock_recovery, stock_restart_grace, stock_monitoring):
            stack.enter_context(patch.object(module, "_now", return_value=now))
        yield


def _kill_switch(db) -> bool:
    return bool(db.query(RiskRule).one().value.get("kill_switch_enabled"))


def _check(key, status="clear", scope="safety", details=None, category="health"):
    return {
        "key": key, "category": category, "metric": key, "status": status, "message": key,
        "value": {}, "threshold": {}, "details": details or {}, "action_scope": scope,
    }


def _stale_feed(failure_class="stale_data", ingestion=None):
    return _check("data.freshness_provenance", "breach", category="data", details={
        "failures": {"SPY": "stale"},
        "symbols": {"SPY": {"status": "stale", "failure_class": failure_class,
                            "ingestion_timestamp": ingestion}},
        "historical_warnings": {},
    })


def _run_monitor(db, *, feed, risk=None, health=None):
    clear_model = _check("model.realized_performance", scope="model", category="model")
    with patch.object(stock_monitoring, "_distribution_drift", return_value=[]), \
         patch.object(stock_monitoring, "_performance_drift", return_value=clear_model), \
         patch.object(stock_monitoring, "_freshness_and_provenance", return_value=feed), \
         patch.object(stock_monitoring, "_execution_divergence", return_value=_check("execution.fill_divergence")), \
         patch.object(stock_monitoring, "_stock_risk_metrics", return_value=risk or []), \
         patch.object(stock_monitoring, "_broker_reconciliation_health", return_value=health or []), \
         patch.object(stock_monitoring, "_worker_scheduler_health", return_value=[]):
        return stock_monitoring.run_stock_monitoring(db)


# --- watchdog ---------------------------------------------------------------

def test_watchdog_restart_within_grace_warms_up_and_blocks_entries(db):
    with clock(BOOT + timedelta(minutes=1)):
        result = run_stock_watchdog(db)
        status = recovery_status(db)
        warmup = restart_warmup_status(db)

    assert result["status"] == "warming_up"
    assert result["reasons"] == []
    assert set(result["deferred"]) == {
        "broker reconciliation heartbeat is stale", "continuous monitor heartbeat is stale",
    }
    state = db.get(StockPaperRecoveryState, 1)
    account = db.query(StockPaperAccount).one()
    assert state.status == "resumable" and state.cooldown_until is None
    assert account.status == "reconciled" and account.halt_reason is None
    assert _kill_switch(db) is False
    # Entries are blocked: readiness requires resumable/not_required.
    assert status["status"] == "warming_up"
    assert status["persisted_status"] == "resumable"
    assert warmup["active"] is True and warmup["boot_id"] == "boot-under-test"
    opened = db.query(StockPaperRecoveryEvent).filter_by(action="restart_grace", status="opened").one()
    assert opened.payload["build_id"] == "build-123"
    assert opened.payload["grace_until"] == (BOOT + timedelta(minutes=5)).isoformat()


def test_launch_admission_blocks_new_entries_while_warming_up(db):
    from app.services import stock_learning_cycle

    state = db.get(StockPaperRecoveryState, 1)
    state.status = "armed"
    db.commit()
    now = BOOT + timedelta(minutes=1)
    with clock(now):
        run_stock_watchdog(db)
        state.last_monitor_heartbeat_at = now
        db.commit()
        gates = stock_learning_cycle.evaluate_launch_admission_prerequisites(db, now=now)
    gate = gates["recovery_state"]
    assert gate["status"] == "fail"
    assert "warming up" in gate["reason"]
    assert gate["evidence"]["restart_warmup"]["active"] is True


def test_watchdog_stale_beyond_grace_pauses_exactly_as_before(db):
    with clock(BOOT + timedelta(minutes=1)):
        assert run_stock_watchdog(db)["status"] == "warming_up"
    # Same boot (a respawned watchdog keeps the marker) past the window.
    with clock(BOOT + timedelta(minutes=6)):
        result = run_stock_watchdog(db)
        status = recovery_status(db)
    assert result["status"] == "paused"
    state = db.get(StockPaperRecoveryState, 1)
    assert state.status == "cooldown"
    assert state.pause_reason.startswith("Independent watchdog:")
    assert _kill_switch(db) is True
    assert db.query(StockPaperAccount).one().status == "halted"
    assert status["status"] == "cooldown"


def test_unmarked_process_has_no_grace(db):
    with clock(BOOT + timedelta(minutes=1), env={"PAPER_BOOT_ID": "", "PAPER_WORKERS_STARTED_AT": ""}):
        result = run_stock_watchdog(db)
    assert result["status"] == "paused"
    assert db.query(StockPaperRecoveryEvent).filter_by(action="restart_grace").count() == 0


def test_long_outage_is_not_a_planned_restart(db):
    db.get(StockPaperRecoveryState, 1).last_watchdog_heartbeat_at = BOOT - timedelta(hours=2)
    db.commit()
    with clock(BOOT + timedelta(minutes=1)):
        result = run_stock_watchdog(db)
    assert result["status"] == "paused"
    denied = db.query(StockPaperRecoveryEvent).filter_by(action="restart_grace", status="denied").one()
    assert "outage" in denied.reason


def test_restart_already_paused_gets_no_grace(db):
    db.get(StockPaperRecoveryState, 1).status = "revalidation_required"
    db.commit()
    with clock(BOOT + timedelta(minutes=1)):
        run_stock_watchdog(db)
        warmup = restart_warmup_status(db)
    assert warmup["active"] is False
    assert db.query(StockPaperRecoveryEvent).filter_by(action="restart_grace", status="denied").count() == 1


def test_restart_storm_is_denied(db):
    for index in range(3):
        anchor = BOOT - timedelta(minutes=10 * (index + 1))
        db.add(StockPaperRecoveryEvent(
            action="restart_grace", status="opened", actor="stock_restart_grace",
            reason="earlier boot", event_sha256=f"{index:064d}",
            payload={"boot_id": f"earlier-{index}", "anchor": anchor.isoformat(),
                     "grace_until": (anchor + timedelta(minutes=5)).isoformat()},
        ))
    db.commit()
    with clock(BOOT + timedelta(minutes=1)):
        result = run_stock_watchdog(db)
    assert result["status"] == "paused"
    denied = db.query(StockPaperRecoveryEvent).filter_by(action="restart_grace", status="denied").one()
    assert "Restart-storm" in denied.reason


def test_future_dated_heartbeat_pauses_even_during_grace(db):
    db.get(StockPaperRecoveryState, 1).last_monitor_heartbeat_at = BOOT + timedelta(hours=1)
    db.commit()
    with clock(BOOT + timedelta(minutes=1)):
        result = run_stock_watchdog(db)
    assert result["status"] == "paused"
    assert "continuous monitor heartbeat is future-dated" in result["reasons"]


def test_halted_account_breach_is_not_deferred(db):
    db.query(StockPaperAccount).one().reconciliation_required = True
    db.commit()
    with clock(BOOT + timedelta(minutes=1)):
        run_stock_watchdog(db)  # opens the window
        _run_monitor(db, feed=_check("data.freshness_provenance", category="data"),
                     health=[_check("health.reconciliation", "breach")])
        result = _run_monitor(db, feed=_check("data.freshness_provenance", category="data"),
                              health=[_check("health.reconciliation", "breach")])
    pause = next(action for action in result["actions"] if action["action"] == "pause_stock_path")
    assert pause["reasons"] == ["health.reconciliation"]
    assert _kill_switch(db) is True


# --- continuous monitor -----------------------------------------------------

def test_monitor_stale_data_within_grace_does_not_pause(db):
    with clock(BOOT + timedelta(minutes=1)):
        _run_monitor(db, feed=_stale_feed())
    with clock(BOOT + timedelta(minutes=2)):
        result = _run_monitor(db, feed=_stale_feed())
        status = recovery_status(db)
    assert [action["action"] for action in result["actions"]] == ["restart_warmup"]
    assert result["actions"][0]["deferred"] == ["data.freshness_provenance"]
    assert db.get(StockPaperRecoveryState, 1).status == "resumable"
    assert _kill_switch(db) is False
    assert status["status"] == "warming_up"
    assert db.query(StockPaperRecoveryEvent).filter_by(action="restart_grace", status="deferred").count() == 1


def test_monitor_stale_data_beyond_grace_pauses_as_before(db):
    with clock(BOOT + timedelta(minutes=3)):
        _run_monitor(db, feed=_stale_feed())
    with clock(BOOT + timedelta(minutes=4)):
        _run_monitor(db, feed=_stale_feed())
    with clock(BOOT + timedelta(minutes=5, seconds=30)):
        result = _run_monitor(db, feed=_stale_feed())
    pause = next(action for action in result["actions"] if action["action"] == "pause_stock_path")
    assert pause["reasons"] == ["data.freshness_provenance"]
    state = db.get(StockPaperRecoveryState, 1)
    assert state.status == "cooldown"
    assert "persistent monitoring breach: data.freshness_provenance" in state.pause_reason
    assert _kill_switch(db) is True


def test_entitlement_proof_aged_across_restart_is_deferred(db):
    feed = _stale_feed("entitlement", ingestion=(BOOT - timedelta(minutes=9)).isoformat())
    with clock(BOOT + timedelta(minutes=1)):
        _run_monitor(db, feed=feed)
        result = _run_monitor(db, feed=feed)
    assert [action["action"] for action in result["actions"]] == ["restart_warmup"]


@pytest.mark.parametrize("failure_class", ["configuration", "availability", None])
def test_non_staleness_feed_failures_still_pause_during_grace(db, failure_class):
    with clock(BOOT + timedelta(minutes=1)):
        _run_monitor(db, feed=_stale_feed(failure_class))
        result = _run_monitor(db, feed=_stale_feed(failure_class))
    assert any(action["action"] == "pause_stock_path" for action in result["actions"])
    assert _kill_switch(db) is True


def test_drawdown_during_grace_pauses_immediately(db):
    drawdown = _check("risk.daily_loss", "breach", category="risk")
    with clock(BOOT + timedelta(minutes=1)):
        _run_monitor(db, feed=_stale_feed(), risk=[drawdown])
        result = _run_monitor(db, feed=_stale_feed(), risk=[drawdown])
    pause = next(action for action in result["actions"] if action["action"] == "pause_stock_path")
    assert pause["reasons"] == ["risk.daily_loss"]
    warmup = next(action for action in result["actions"] if action["action"] == "restart_warmup")
    assert warmup["deferred"] == ["data.freshness_provenance"]
    assert db.get(StockPaperRecoveryState, 1).status == "cooldown"
    assert _kill_switch(db) is True


def test_unexplained_residual_during_grace_pauses(db):
    db.query(StockPaperAccount).one().unexplained_residual = True
    db.commit()
    stale = [_check("health.reconciliation", "breach")]
    with clock(BOOT + timedelta(minutes=1)):
        _run_monitor(db, feed=_stale_feed(), health=stale)
        result = _run_monitor(db, feed=_stale_feed(), health=stale)
    pause = next(action for action in result["actions"] if action["action"] == "pause_stock_path")
    assert pause["reasons"] == ["health.reconciliation"]
    assert _kill_switch(db) is True


def test_stale_reconciliation_on_reconciled_account_is_deferred(db):
    stale = [_check("health.reconciliation", "breach")]
    with clock(BOOT + timedelta(minutes=1)):
        _run_monitor(db, feed=_stale_feed(), health=stale)
        result = _run_monitor(db, feed=_stale_feed(), health=stale)
    assert [action["action"] for action in result["actions"]] == ["restart_warmup"]
    assert sorted(result["actions"][0]["deferred"]) == ["data.freshness_provenance", "health.reconciliation"]
    assert _kill_switch(db) is False


def test_clean_monitor_run_completes_warmup_early(db):
    with clock(BOOT + timedelta(minutes=1)):
        _run_monitor(db, feed=_stale_feed())
    with clock(BOOT + timedelta(minutes=2)):
        result = _run_monitor(db, feed=_check("data.freshness_provenance", category="data"))
        status = recovery_status(db)
    assert result["actions"] == []
    assert status["status"] == "resumable"
    assert status["restart_warmup"]["status"] == "complete"
    # A later stale finding in the same boot is no longer deferred.
    with clock(BOOT + timedelta(minutes=3)):
        _run_monitor(db, feed=_stale_feed())
        result = _run_monitor(db, feed=_stale_feed())
    assert any(action["action"] == "pause_stock_path" for action in result["actions"])


# --- persistence must restart after an operator revalidation ----------------

def _operator_resume(db, at):
    """The durable effects of resume_stock_paper_after_revalidation."""
    state = db.get(StockPaperRecoveryState, 1)
    state.status, state.last_revalidation_at, state.pause_reason, state.cooldown_until = "resumable", at, None, None
    rule = db.query(RiskRule).one()
    rule.value = (rule.value or {}) | {"kill_switch_enabled": False}
    account = db.query(StockPaperAccount).one()
    account.status, account.halt_reason, account.reconciliation_required = "reconciled", None, False
    account.last_reconciled_at = at
    db.commit()


def test_breaches_observed_while_halted_do_not_repause_after_resume(db):
    no_boot = {"PAPER_BOOT_ID": "", "PAPER_WORKERS_STARTED_AT": ""}
    halted = [_check("health.broker", "breach"), _check("health.reconciliation", "breach")]
    clear_feed = _check("data.freshness_provenance", category="data")
    start = BOOT + timedelta(hours=1)
    db.get(StockPaperRecoveryState, 1).last_monitor_heartbeat_at = start
    db.commit()
    # Breach snapshots while the account is halted: the path pauses.
    for minute in (0, 1):
        with clock(start + timedelta(minutes=minute), env=no_boot):
            _run_monitor(db, feed=clear_feed, health=halted)
    assert db.get(StockPaperRecoveryState, 1).status == "cooldown"
    with clock(start + timedelta(minutes=2), env=no_boot):
        _run_monitor(db, feed=clear_feed, health=halted)  # still halted during cooldown

    resumed_at = start + timedelta(minutes=17)
    _operator_resume(db, resumed_at)
    # Production race: an evaluation that observed the halted account just
    # before the resume committed, plus the first one after it.
    with clock(resumed_at - timedelta(seconds=5), env=no_boot):
        result = _run_monitor(db, feed=clear_feed, health=halted)
    assert not any(action["action"] == "pause_stock_path" for action in result["actions"])
    with clock(resumed_at + timedelta(seconds=6), env=no_boot):
        result = _run_monitor(db, feed=clear_feed, health=halted)
    assert not any(action["action"] == "pause_stock_path" for action in result["actions"])
    assert db.get(StockPaperRecoveryState, 1).status == "resumable"
    assert _kill_switch(db) is False

    with clock(resumed_at + timedelta(minutes=1), env=no_boot):
        result = _run_monitor(db, feed=clear_feed, health=[_check("health.broker"), _check("health.reconciliation")])
    assert result["actions"] == []

    # A genuine new breach after resume still pauses once it persists.
    drawdown = [_check("risk.daily_loss", "breach", category="risk")]
    with clock(resumed_at + timedelta(minutes=2), env=no_boot):
        result = _run_monitor(db, feed=clear_feed, risk=drawdown)
    assert not any(action["action"] == "pause_stock_path" for action in result["actions"])
    with clock(resumed_at + timedelta(minutes=3), env=no_boot):
        result = _run_monitor(db, feed=clear_feed, risk=drawdown)
    pause = next(action for action in result["actions"] if action["action"] == "pause_stock_path")
    assert pause["reasons"] == ["risk.daily_loss"]
    assert db.get(StockPaperRecoveryState, 1).status == "cooldown"
    assert _kill_switch(db) is True


def test_breach_observations_from_previous_generation_do_not_count(db):
    """One stale-generation observation plus one new one is not 'persistent'."""
    drawdown = [_check("risk.daily_loss", "breach", category="risk")]
    clear_feed = _check("data.freshness_provenance", category="data")
    with clock(BOOT - timedelta(minutes=1), env={"PAPER_BOOT_ID": "", "PAPER_WORKERS_STARTED_AT": ""}):
        _run_monitor(db, feed=clear_feed, risk=drawdown)
    with clock(BOOT + timedelta(minutes=1)):
        result = _run_monitor(db, feed=clear_feed, risk=drawdown)
    assert not any(action["action"] == "pause_stock_path" for action in result["actions"])
    with clock(BOOT + timedelta(minutes=2)):
        result = _run_monitor(db, feed=clear_feed, risk=drawdown)
    assert any(action["action"] == "pause_stock_path" for action in result["actions"])
