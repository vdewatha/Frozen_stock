"""Operations health warnings and the daily operations summary (paper only)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from unittest.mock import patch

from celery.schedules import crontab
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import IntradayBar, Notification, StockPaperRecoveryEvent, StockPaperRecoveryState
from app.models.stock_paper import StockPaperAccount, StockPaperLedgerEvent
from app.services import operations_health as ops
from app.services.paper_trade_scorecard import record_scorecard_snapshot, scorecard_history
from app.services.stock_paper_ledger import active_paper_broker_name
from app.tasks import jobs
from app.tasks.celery_app import celery_app

UTC = timezone.utc
OPEN = datetime(2026, 9, 11, 13, 30, tzinfo=UTC)  # Friday 09:30 ET
CLOSE = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def close(self):
        pass


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def redis_client():
    client = FakeRedis()
    with patch.object(ops, "_redis", return_value=client):
        yield client


def _bar(db, opened_at, symbol="SPY"):
    db.add(IntradayBar(
        symbol=symbol, timeframe="1m", opened_at=opened_at, open=Decimal("1"), high=Decimal("1"),
        low=Decimal("1"), close=Decimal("1"), volume=1, provider=ops._feed_provider(),
        feed_class="iex", exchange_timestamp=opened_at, ingested_at=opened_at,
    ))


def _account(db, last_reconciled_at):
    row = StockPaperAccount(
        broker=active_paper_broker_name(), broker_account_id="paper", cash=Decimal("1"),
        buying_power=Decimal("1"), equity=Decimal("1"), status="reconciled", raw_payload={},
        last_reconciled_at=last_reconciled_at,
    )
    db.add(row)
    db.commit()
    return row


def _recovery(db, status, *, reason="Watchdog: monitor heartbeat is stale"):
    state = db.get(StockPaperRecoveryState, 1) or StockPaperRecoveryState(id=1, updated_by="test")
    state.status = status
    state.pause_reason = None if status in ops.TRADABLE_RECOVERY_STATUSES else reason
    db.add(state)
    db.commit()
    return state


def _event(db, action, at, *, status="cooldown", actor="stock_watchdog", reason="Watchdog: stale"):
    db.add(StockPaperRecoveryEvent(action=action, status=status, actor=actor, reason=reason,
                                   event_sha256=f"{action}-{at.isoformat()}-{actor}", payload={}, created_at=at))
    db.commit()


def _ops_notifications(db):
    return db.query(Notification).filter_by(category=ops.CATEGORY).order_by(Notification.id).all()


def _healthy(db, now, redis_client):
    _bar(db, now - timedelta(minutes=2))
    db.commit()
    _account(db, now - timedelta(minutes=2))
    _recovery(db, "armed")
    ops.record_intraday_import_run(started_at=now - timedelta(seconds=20), finished_at=now - timedelta(seconds=5),
                                   status="ready")


def test_healthy_session_raises_nothing(db, redis_client):
    now = OPEN + timedelta(hours=1)
    _healthy(db, now, redis_client)
    result = ops.run_operations_health_alerts(db, now=now)
    assert result["in_session"] is True
    assert result["active_conditions"] == []
    assert set(result["actions"].values()) == {"clear"}
    assert _ops_notifications(db) == []


def test_slow_import_raises_one_warning_that_refreshes_then_resolves(db, redis_client):
    now = OPEN + timedelta(hours=1)
    _healthy(db, now, redis_client)
    ops.record_intraday_import_run(started_at=now - timedelta(seconds=90), finished_at=now, status="ready")
    first = ops.run_operations_health_alerts(db, now=now)
    second = ops.run_operations_health_alerts(db, now=now + timedelta(minutes=5))
    assert first["actions"]["intraday_feed"] == "raised"
    assert second["actions"]["intraday_feed"] == "refreshed"
    rows = _ops_notifications(db)
    assert len(rows) == 1
    assert rows[0].severity == "warning" and rows[0].status == "open"
    assert "90s" in rows[0].message
    assert rows[0].payload["condition"] == "intraday_feed"

    later = now + timedelta(minutes=10)
    _bar(db, later - timedelta(minutes=2))
    db.commit()
    db.query(StockPaperAccount).one().last_reconciled_at = later - timedelta(minutes=1)
    db.commit()
    ops.record_intraday_import_run(started_at=later - timedelta(seconds=10), finished_at=later, status="ready")
    third = ops.run_operations_health_alerts(db, now=later)
    assert third["actions"]["intraday_feed"] == "resolved"
    db.refresh(rows[0])
    assert rows[0].status == "resolved" and rows[0].resolved_at is not None


def test_stale_spy_bar_only_alerts_during_session(db, redis_client):
    now = OPEN + timedelta(hours=2)
    _healthy(db, now, redis_client)
    stale_now = now + timedelta(minutes=6)
    db.query(StockPaperAccount).one().last_reconciled_at = stale_now
    db.commit()
    result = ops.run_operations_health_alerts(db, now=stale_now)
    assert result["actions"]["intraday_feed"] == "raised"
    feed = next(item for item in result["conditions"] if item["key"] == "intraday_feed")
    assert feed["details"]["spy_bar_age_seconds"] == pytest.approx(7 * 60)
    # After the close the session-only staleness clears and the alert resolves.
    after_close = CLOSE + timedelta(minutes=10)
    db.query(StockPaperAccount).one().last_reconciled_at = after_close
    db.commit()
    result = ops.run_operations_health_alerts(db, now=after_close)
    assert result["in_session"] is False
    assert result["actions"]["intraday_feed"] == "resolved"


def test_stale_reconciliation_raises_and_resolves(db, redis_client):
    now = OPEN + timedelta(hours=1)
    _healthy(db, now, redis_client)
    account = db.query(StockPaperAccount).one()
    account.last_reconciled_at = now - timedelta(minutes=8)
    db.commit()
    result = ops.run_operations_health_alerts(db, now=now)
    assert result["actions"]["broker_reconciliation"] == "raised"
    account.last_reconciled_at = now
    db.commit()
    assert ops.run_operations_health_alerts(db, now=now)["actions"]["broker_reconciliation"] == "resolved"


def test_missing_account_is_unknown_and_leaves_alerts_alone(db, redis_client):
    now = OPEN + timedelta(hours=1)
    _recovery(db, "armed")
    result = ops.run_operations_health_alerts(db, now=now)
    assert result["actions"]["broker_reconciliation"] == "unknown"


def test_recovery_pause_alerts_after_fifteen_session_minutes(db, redis_client):
    now = OPEN + timedelta(hours=1)
    _healthy(db, now, redis_client)
    _recovery(db, "cooldown")
    _event(db, "pause", now - timedelta(minutes=10))
    assert ops.run_operations_health_alerts(db, now=now)["actions"]["recovery_not_tradable"] == "clear"
    result = ops.run_operations_health_alerts(db, now=now + timedelta(minutes=6))
    assert result["actions"]["recovery_not_tradable"] == "raised"
    assert _ops_notifications(db)[-1].severity == "warning"
    _recovery(db, "resumable")
    assert ops.run_operations_health_alerts(db, now=now + timedelta(minutes=7))["actions"]["recovery_not_tradable"] == "resolved"


def test_overnight_pause_counts_only_session_minutes(db, redis_client):
    now = OPEN + timedelta(minutes=10)
    _healthy(db, now, redis_client)
    _recovery(db, "paused")
    _event(db, "pause", OPEN - timedelta(hours=12))
    assert ops.run_operations_health_alerts(db, now=now)["actions"]["recovery_not_tradable"] == "clear"
    result = ops.run_operations_health_alerts(db, now=OPEN + timedelta(minutes=16))
    assert result["actions"]["recovery_not_tradable"] == "raised"


def test_alerts_never_block_readiness_as_critical(db, redis_client):
    now = OPEN + timedelta(hours=1)
    _account(db, now - timedelta(hours=1))
    _recovery(db, "paused")
    _event(db, "pause", now - timedelta(minutes=30))
    ops.run_operations_health_alerts(db, now=now)
    rows = _ops_notifications(db)
    assert len(rows) == 3
    assert {row.severity for row in rows} == {"warning"}


def test_import_timing_record_survives_redis_failure():
    with patch.object(ops, "_redis", side_effect=ConnectionError("down")):
        assert ops.record_intraday_import_run(started_at=OPEN, finished_at=OPEN, status="ready") is False
        assert ops.latest_intraday_import_run() is None


def test_intraday_import_job_records_duration(redis_client):
    with patch.object(jobs, "_run_job", side_effect=lambda _name, work: work(type("DB", (), {"commit": lambda self: None})())), \
            patch.object(jobs, "collect_scheduled_intraday", return_value={"status": "ready"}):
        result = jobs.intraday_market_data_import()
    assert result["status"] == "ready"
    stored = json.loads(redis_client.values[ops.INTRADAY_IMPORT_RUN_KEY])
    assert stored["status"] == "ready" and stored["duration_seconds"] >= 0


def test_intraday_import_job_records_duration_on_failure(redis_client):
    with patch.object(jobs, "_run_job", side_effect=lambda _name, work: work(object())), \
            patch.object(jobs, "collect_scheduled_intraday", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            jobs.intraday_market_data_import()
    assert json.loads(redis_client.values[ops.INTRADAY_IMPORT_RUN_KEY])["status"] == "failed"


def test_beat_entry_is_a_session_crontab_on_the_risk_queue():
    entry = celery_app.conf.beat_schedule["operations-health-alerts"]
    assert entry["task"] == "app.tasks.jobs.operations_health_alert_job"
    assert isinstance(entry["schedule"], crontab)
    assert entry["schedule"].minute == set(range(0, 60, 5))
    assert entry["schedule"].hour == set(range(9, 17))
    assert entry["schedule"].day_of_week == {1, 2, 3, 4, 5}
    assert entry["options"]["queue"] == "risk"
    assert celery_app.conf.task_routes["app.tasks.jobs.operations_health_alert_job"] == {"queue": "risk"}


def test_daily_summary_pause_minutes_reasons_watchdog_and_feed(db):
    # Paused overnight, resumed at 09:40; watchdog pause 11:00-11:30 (auto
    # restore); monitor pause 15:30 still open at the close.
    _event(db, "pause", OPEN - timedelta(hours=10), actor="stock_watchdog", reason="Overnight stale")
    _event(db, "resume", OPEN + timedelta(minutes=10), status="revalidated", actor="operator")
    _event(db, "pause", OPEN + timedelta(minutes=90), actor="stock_watchdog", reason="Watchdog: stale")
    _event(db, "automatic_restore", OPEN + timedelta(minutes=120), status="armed", actor="stock_reconciler")
    _event(db, "pause", CLOSE - timedelta(minutes=30), actor="stock_monitor", reason="Drawdown breach")
    _event(db, "automatic_resume", CLOSE - timedelta(minutes=20), status="blocked", actor="auto")
    for minute in range(0, 390, 2):
        _bar(db, OPEN + timedelta(minutes=minute))
    db.commit()
    summary = ops.daily_operations_summary(db, now=CLOSE + timedelta(minutes=20))
    assert summary["date"] == "2026-09-11"
    assert summary["paused_session_minutes"] == pytest.approx(10 + 30 + 30)
    assert summary["pause_count"] == 2  # the overnight pause belongs to the previous ET day
    assert summary["watchdog_pauses"] == 1
    assert {item["reason"] for item in summary["pause_reasons"]} == {"Watchdog: stale", "Drawdown breach"}
    feed = summary["intraday_feed"]
    assert feed["session_minutes"] == 390 and feed["minutes_with_bar"] == 195
    assert feed["ready_share"] == pytest.approx(0.5)


def test_daily_summary_mid_session_and_holiday(db):
    _event(db, "pause", OPEN + timedelta(minutes=30), actor="stock_watchdog")
    summary = ops.daily_operations_summary(db, now=OPEN + timedelta(minutes=60))
    assert summary["paused_session_minutes"] == pytest.approx(30)
    assert summary["intraday_feed"]["session_minutes"] == 60
    holiday = ops.daily_operations_summary(db, now=datetime(2026, 12, 25, 17, tzinfo=UTC))
    assert holiday["session"]["is_session"] is False
    assert holiday["paused_session_minutes"] == 0 and holiday["intraday_feed"] is None


def test_scorecard_snapshot_records_operations_and_history(db):
    _event(db, "pause", OPEN + timedelta(minutes=30), actor="stock_watchdog", reason="Watchdog: stale")
    _event(db, "automatic_restore", OPEN + timedelta(minutes=45), status="armed", actor="stock_reconciler")
    _bar(db, OPEN)
    db.commit()
    now = CLOSE + timedelta(minutes=20)
    result = record_scorecard_snapshot(db, now=now)
    assert result["recorded"] is True
    assert result["paused_session_minutes"] == pytest.approx(15)
    assert result["watchdog_pauses"] == 1
    event = db.query(StockPaperLedgerEvent).one()
    assert event.payload["operations"]["pause_reasons"] == [{"reason": "Watchdog: stale", "count": 1}]
    history = scorecard_history(db)
    assert history[0]["paused_session_minutes"] == pytest.approx(15)
    assert history[0]["watchdog_pauses"] == 1
    assert history[0]["spy_feed_ready_share"] == pytest.approx(round(1 / 390, 4))


def test_scorecard_snapshot_survives_operations_summary_failure(db):
    with patch.object(ops, "daily_operations_summary", side_effect=RuntimeError("boom")):
        result = record_scorecard_snapshot(db, now=CLOSE + timedelta(minutes=20))
    assert result["recorded"] is True
    assert db.query(StockPaperLedgerEvent).one().payload["operations"]["status"] == "unavailable"
