"""2026-11-09: research-job alerts blocked all paper trading; cost probes failed silently.

strategy_learning_scope_job kept retrying symbols deactivated after their
scopes were planned (ALGN, ABNB). Each failure opened a critical alert, and
any open critical alert failed the launch-admission "notifications" gate, so
every autonomous order (signals, exploration, the slow book's rebalance, cost
probes) was refused. No probe ever ran and nothing recorded why.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Asset, Notification
from app.services import cost_probes
from app.services.notifications import trading_blocking_critical_filter


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Notification.__table__.create(engine)
    Asset.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def alert(db, source, *, category="scheduled_job", severity="critical"):
    row = Notification(category=category, severity=severity, source=source, status="open",
                       title=f"Scheduled job failed: {source}", message="x")
    db.add(row)
    return row


def blocking(db):
    return db.query(Notification).filter(trading_blocking_critical_filter()).count()


def test_research_scope_failures_do_not_block_trading(db):
    for strategy in ("ensemble", "rsi2_pullback"):
        alert(db, f"strategy_learning_scope_job:ALGN:{strategy}")
    db.commit()
    assert blocking(db) == 0


def test_other_critical_alerts_still_block(db):
    alert(db, "strategy_learning_scope_job:ALGN:ensemble")
    alert(db, "stock_paper_broker_reconciliation_job")
    alert(db, "risk_monitor", category="risk")
    alert(db, "deploy", category="deployment_monitor")  # excluded as before
    alert(db, "strategy_learning_scope_job:X:y", category="risk")  # only scheduled_job rows are exempt
    alert(db, "other_job", severity="warning")
    db.commit()
    assert blocking(db) == 3


def test_scope_job_on_inactive_symbol_skips_and_closes_its_alerts(db):
    from app.tasks import jobs

    db.add(Asset(symbol="ALGN", name="Align", is_active=False))
    source = "strategy_learning_scope_job:ALGN:ensemble"
    alert(db, source)
    alert(db, source)
    alert(db, "strategy_learning_scope_job:ABNB:ensemble")
    db.commit()
    dispatch = MagicMock()
    with patch.object(jobs, "_run_job", side_effect=lambda name, work: work(db)), \
            patch.object(jobs, "_claim_learning_dispatch", return_value=(dispatch, "claimed")), \
            patch.object(jobs, "run_strategy_experiments") as experiments:
        result = jobs.strategy_learning_scope_job.run(symbol="ALGN", strategy_slug="ensemble", dispatch_id=1)
    experiments.assert_not_called()
    assert result["status"] == "skipped" and result["resolved_alerts"] == 2
    assert dispatch.status == "succeeded"  # recovery will not republish it
    still_open = db.query(Notification).filter(Notification.status == "open").all()
    assert [row.source for row in still_open] == ["strategy_learning_scope_job:ABNB:ensemble"]


def test_scope_job_on_unknown_symbol_also_skips(db):
    from app.tasks import jobs

    with patch.object(jobs, "_run_job", side_effect=lambda name, work: work(db)), \
            patch.object(jobs, "_claim_learning_dispatch", return_value=(None, "claimed")), \
            patch.object(jobs, "run_strategy_experiments") as experiments:
        result = jobs.strategy_learning_scope_job.run(symbol="ZZZZ", strategy_slug="ensemble")
    experiments.assert_not_called()
    assert result["reason"] == "instrument is not active"


@pytest.mark.parametrize("result,expected", [
    ({"status": "blocked", "reason": "Scheduled paper execution blocked by launch admission: x"}, True),
    ({"status": "skipped", "reason": "no eligible probe symbol"}, True),
    ({"status": "skipped", "reason": "17 of 20 open-position slots in use; a probe needs 2 free"}, True),
    ({"status": "skipped", "reason": "paper account is not reconciled"}, True),
    ({"status": "skipped", "reason": "outside the regular session"}, False),
    ({"status": "skipped", "reason": "outside the probe start window (10:30-14:00 ET)"}, False),
    ({"status": "skipped", "reason": "3 probes this week (target 3)"}, False),
    ({"status": "skipped", "reason": "today's probe already ran"}, False),
    ({"status": "buy_submitted", "symbol": "QQQ"}, False),
])
def test_held_back_classification(result, expected):
    assert cost_probes.held_back(result) is expected


class FakeRedis:
    def __init__(self, store):
        self.store = store

    def set(self, key, value, ex=None):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def close(self):
        pass


def test_ticks_are_kept_for_probe_status():
    store = {}
    now = datetime(2026, 11, 10, 15, 35, tzinfo=timezone.utc)
    with patch.object(cost_probes, "_redis", side_effect=lambda: FakeRedis(store)):
        cost_probes.record_tick({"status": "blocked", "reason": "launch admission: notifications",
                                 "paper_only": True}, now=now)
        cost_probes.record_tick({"status": "skipped", "reason": "today's probe already ran"}, now=now)
        ticks = cost_probes.recent_ticks()
    assert ticks["last_tick"]["reason"] == "today's probe already ran"
    assert ticks["last_held_back"]["status"] == "blocked"
    assert ticks["last_held_back"]["at"] == now.isoformat()
    assert "paper_only" not in ticks["last_held_back"]


def test_tick_recording_survives_redis_outage():
    with patch.object(cost_probes, "_redis", side_effect=ConnectionError("down")):
        cost_probes.record_tick({"status": "blocked", "reason": "x"})
        assert cost_probes.recent_ticks()["error"] == "ConnectionError"


def test_public_ready_reads_the_cached_snapshot_and_hides_details():
    from fastapi import HTTPException

    from app.api import routes

    blocked = {"overall_status": "blocked", "generated_at": datetime(2026, 11, 9, 21, 0),
               "checks": [{"name": "Critical notifications", "status": "blocked", "details": {"secret": 1}},
                          {"name": "Market data", "status": "ready"}]}
    with patch.object(routes, "system_readiness", return_value=blocked), \
            patch.object(routes, "readiness_snapshot", side_effect=AssertionError("built per request")):
        with pytest.raises(HTTPException) as raised:
            routes.ready()
    detail = raised.value.detail
    assert raised.value.status_code == 503
    assert detail["checks"] == [{"name": "Critical notifications", "status": "blocked"}]
    assert "secret" not in str(detail)
    ready = {"overall_status": "ready", "generated_at": "t", "checks": []}
    with patch.object(routes, "system_readiness", return_value=ready):
        assert routes.ready()["status"] == "ready"
