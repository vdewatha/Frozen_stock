"""2026-11-09 full-day log scan follow-ups.

- Learning-scope alerts no rerun can clear (unknown strategy, deactivated
  symbol) stayed open and held "Scheduler health" blocked.
- Each signal cycle logged only ``blocked=N``; why was not in the logs.
- A whole session of held-back trading raised no alarm.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db.base import Base
from app.models import Asset, Notification
from app.models.stock_paper import StockPaperOrder
from app.services import held_back_alarm as alarm
from app.services.notifications import resolve_obsolete_learning_scope_alerts
from app.services.stock_paper_autotrader import block_reasons, format_block_reasons
from app.tasks.celery_app import celery_app

UTC = timezone.utc
# Tuesday 2026-11-10: the session opens 14:30 UTC (09:30 ET).
NOON_ET = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def alert(db, source, *, category="scheduled_job", severity="critical", status="open"):
    row = Notification(category=category, severity=severity, source=source, status=status,
                       title=f"Scheduled job failed: {source}", message="x")
    db.add(row)
    return row


# ------------------------------------------------------------ 1. obsolete alerts

def test_obsolete_learning_scope_alerts_are_resolved_and_others_kept(db):
    db.add_all([Asset(symbol="AAPL", name="Apple", is_active=True),
                Asset(symbol="ALGN", name="Align", is_active=False)])
    unknown = alert(db, "strategy_learning_scope_job:AAPL:passive_arm")
    acknowledged = alert(db, "strategy_learning_scope_job:SPY:defensive_tsmom_iv", status="acknowledged")
    inactive = alert(db, "strategy_learning_scope_job:ALGN:ensemble")
    missing = alert(db, "strategy_learning_scope_job:ZZZZ:rsi2_pullback")
    live = alert(db, "strategy_learning_scope_job:AAPL:ensemble")  # can still succeed on a rerun
    other = alert(db, "stock_paper_broker_reconciliation_job")
    db.commit()

    result = resolve_obsolete_learning_scope_alerts(db)
    db.commit()

    assert result == {"resolved": 4, "unknown_strategy": 2, "inactive_symbol": 2}
    for row in (unknown, acknowledged, inactive, missing):
        db.refresh(row)
        assert row.status == "resolved" and row.resolved_at is not None
    for row in (live, other):
        db.refresh(row)
        assert row.status == "open"


def test_unknown_strategy_scope_skip_closes_its_alerts(db):
    from app.tasks import jobs

    source = "strategy_learning_scope_job:AAPL:passive_arm"
    alert(db, source)
    alert(db, source)
    db.commit()
    dispatch = MagicMock()
    with patch.object(jobs, "_run_job", side_effect=lambda name, work: work(db)), \
            patch.object(jobs, "_claim_learning_dispatch", return_value=(dispatch, "claimed")), \
            patch.object(jobs, "run_strategy_experiments") as experiments:
        result = jobs.strategy_learning_scope_job.run(symbol="AAPL", strategy_slug="passive_arm", dispatch_id=1)
    experiments.assert_not_called()
    assert result["status"] == "skipped" and result["resolved_alerts"] == 2
    assert dispatch.status == "succeeded"
    assert db.query(Notification).filter(Notification.status == "open").count() == 0


def test_reaper_resolves_obsolete_learning_alerts(db):
    from app.tasks import jobs

    alert(db, "strategy_learning_scope_job:AAPL:passive_arm")
    db.commit()
    with patch.object(jobs, "_run_job", side_effect=lambda name, work: work(db)):
        result = jobs.reap_stale_jobs_job.run()
    assert result["resolved_learning_alerts"]["resolved"] == 1
    assert db.query(Notification).filter(Notification.status == "open").count() == 0


# ------------------------------------------------------------ 2. block reasons

def test_block_reasons_group_by_reason_with_numbers_folded():
    results = [
        {"status": "blocked", "reason": "Scheduled paper execution blocked by launch admission: notifications"},
        {"status": "blocked", "reason": "Scheduled paper execution blocked by launch admission: notifications"},
        {"status": "blocked", "reason": "position cap 12 of 12 reached"},
        {"status": "blocked", "reason": "position cap 11 of 12 reached"},
        {"status": "failed", "reason": "KeyError: 'x'"},
        {"status": "submitted"},
        {"status": "skipped", "reason": "no signal"},
    ]
    reasons = block_reasons(results)
    assert reasons == [
        {"reason": "Scheduled paper execution blocked by launch admission: notifications", "count": 2},
        {"reason": "position cap # of # reached", "count": 2},
        {"reason": "KeyError: 'x'", "count": 1},
    ]
    assert format_block_reasons(reasons[:2]).startswith("2x Scheduled paper execution blocked")
    assert format_block_reasons([]) == "-"


# ------------------------------------------------------------ 3. held-back alarm

class FakePipeline:
    def __init__(self, client):
        self.client, self.ops = client, []

    def __getattr__(self, name):
        def op(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return op

    def execute(self):
        for name, args, kwargs in self.ops:
            getattr(self.client, name)(*args, **kwargs)


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    def pipeline(self):
        return FakePipeline(self)

    def hincrby(self, key, field, amount):
        h = self.hashes.setdefault(key, {})
        h[field] = str(int(h.get(field, 0)) + amount)

    def hsetnx(self, key, field, value):
        self.hashes.setdefault(key, {}).setdefault(field, value)

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def expire(self, key, seconds):
        pass

    def hgetall(self, key):
        return {k.encode(): v.encode() for k, v in self.hashes.get(key, {}).items()}


def test_signal_cycle_gate_refusals_are_counted_but_rule_refusals_are_not():
    result = {"status": "partially_complete", "results": [
        {"status": "blocked", "reason": "Scheduled paper execution blocked by launch admission: notifications"},
        {"status": "blocked", "reason": "Scheduled paper execution blocked by launch admission: notifications"},
        {"status": "blocked", "reason": "Stock paper exposure is blocked until reconciled"},
        {"status": "blocked", "reason": "position cap reached"},
        {"status": "submitted"},
    ]}
    assert alarm.signal_cycle_held_back(result) == (
        3, "Scheduled paper execution blocked by launch admission: notifications")
    assert alarm.signal_cycle_held_back({"status": "complete", "results": [
        {"status": "blocked", "reason": "position cap reached"}]}) == (0, None)
    assert alarm.signal_cycle_held_back({"status": "skipped", "reason": "stock paper account is not reconciled"})[0] == 1
    assert alarm.signal_cycle_held_back({"status": "skipped", "reason": "regular market session is closed"})[0] == 0


@pytest.mark.parametrize("result,held", [
    ({"status": "blocked", "reason": "no fresh reference price for SGOV, VOO"}, True),
    ({"status": "skipped", "reason": "stock paper account is not reconciled"}, True),
    ({"status": "skipped", "reason": "engine is paused: watchdog"}, True),
    ({"status": "skipped", "reason": "no tranche is due this week"}, False),
    ({"status": "skipped", "reason": "outside the rebalance window (regular session, clear of the auctions)"}, False),
    ({"status": "skipped", "reason": "tranche 1 already rebalanced on 2026-11-10"}, False),
    ({"status": "skipped", "reason": "kill switch is on"}, False),
    ({"status": "complete"}, False),
])
def test_target_weight_held_back_classification(result, held):
    assert bool(alarm.target_weight_held_back(result)) is held


def order(db, *, source, submitted_at):
    db.add(StockPaperOrder(account_id=1, client_order_id=f"c-{source}-{submitted_at}", symbol="VOO", side="buy",
                           quantity=Decimal("1"), status="filled", source=source, submitted_at=submitted_at))


def test_held_back_session_opens_one_warning_then_resolves_when_an_order_is_sent(db):
    redis = FakeRedis()
    alarm.record_held_back("target_weight", "no fresh reference price for SGOV, VOO",
                           now=datetime(2026, 11, 10, 18, 0, tzinfo=UTC), client=redis)
    alarm.record_held_back("cost_probe", "launch admission: notifications", now=NOON_ET, client=redis)
    alarm.record_held_back("cost_probe", "launch admission: notifications", now=NOON_ET, client=redis)
    # A manual order does not count as the bot trading.
    order(db, source="manual", submitted_at=NOON_ET)
    db.commit()

    first = alarm.check_held_back_session(db, now=NOON_ET, client=redis)
    db.commit()
    assert first["alarm"] == "opened" and first["orders_sent"] == 0
    rows = db.query(Notification).filter(Notification.source == alarm.NOTIFICATION_SOURCE).all()
    assert len(rows) == 1
    warning = rows[0]
    assert warning.severity == "warning" and warning.category != "scheduled_job"
    assert "cost probes: 2x" in warning.message and "slow book (VOO/SGOV): 1x" in warning.message

    again = alarm.check_held_back_session(db, now=datetime(2026, 11, 10, 17, 30, tzinfo=UTC), client=redis)
    db.commit()
    assert again["alarm"] == "updated"
    assert db.query(Notification).filter(Notification.source == alarm.NOTIFICATION_SOURCE).count() == 1

    order(db, source="cost_probe", submitted_at=datetime(2026, 11, 10, 17, 40, tzinfo=UTC))
    db.commit()
    done = alarm.check_held_back_session(db, now=datetime(2026, 11, 10, 18, 0, tzinfo=UTC), client=redis)
    db.commit()
    assert done["resolved_alarm"] is True
    db.refresh(warning)
    assert warning.status == "resolved"


def test_no_alarm_when_nothing_was_held_back_or_outside_the_session(db):
    redis = FakeRedis()
    assert alarm.check_held_back_session(db, now=NOON_ET, client=redis)["held_back"] == {}
    alarm.record_held_back("signals", "x", now=NOON_ET, client=redis)
    early = alarm.check_held_back_session(db, now=datetime(2026, 11, 10, 15, 0, tzinfo=UTC), client=redis)
    assert early["reason"] == "too early in the session"
    weekend = alarm.check_held_back_session(db, now=datetime(2026, 11, 8, 17, 0, tzinfo=UTC), client=redis)
    assert weekend["reason"] == "not a trading session"
    assert db.query(Notification).count() == 0


def test_a_warning_never_blocks_trading(db):
    from app.services.notifications import trading_blocking_critical_filter

    redis = FakeRedis()
    alarm.record_held_back("signals", "Scheduled paper execution blocked by launch admission: x",
                           count=290, now=NOON_ET, client=redis)
    alarm.check_held_back_session(db, now=NOON_ET, client=redis)
    db.commit()
    assert db.query(Notification).filter(trading_blocking_critical_filter()).count() == 0


def test_recording_survives_a_redis_outage():
    with patch.object(alarm, "_redis", side_effect=ConnectionError("down")):
        alarm.record_held_back("signals", "x")  # must not raise


def test_jobs_record_held_back_attempts():
    from app.tasks import jobs

    recorded = []
    with patch.object(jobs, "_run_job", side_effect=lambda name, work: work(MagicMock())), \
            patch("app.services.held_back_alarm.record_held_back",
                  side_effect=lambda source, reason, **kw: recorded.append((source, reason, kw.get("count", 1)))), \
            patch("app.services.target_weight_book.run_rebalance",
                  return_value={"status": "blocked", "reason": "no fresh reference price for VOO"}), \
            patch("app.services.cost_probes.run_probe_cycle",
                  return_value={"status": "blocked", "reason": "launch admission"}), \
            patch("app.services.cost_probes.record_tick"), \
            patch("app.services.stock_paper_autotrader.run_stock_paper_signal_cycle",
                  return_value={"status": "partially_complete", "results": [
                      {"status": "blocked", "reason": "Scheduled paper execution blocked by launch admission: n"}]}):
        jobs.target_weight_rebalance_job.run()
        jobs.cost_probe_job.run()
        jobs.paper_trading_signal_job.run()
    assert recorded == [("target_weight", "no fresh reference price for VOO", 1),
                        ("cost_probe", "launch admission", 1),
                        ("signals", "Scheduled paper execution blocked by launch admission: n", 1)]


def test_alarm_is_scheduled_in_session_on_the_fast_lane():
    entry = celery_app.conf.beat_schedule["held-back-session-alarm"]
    assert entry["task"] == "app.tasks.jobs.held_back_session_alarm_job"
    assert celery_app.conf.task_routes["app.tasks.jobs.held_back_session_alarm_job"]["queue"] == "learning_fast"
