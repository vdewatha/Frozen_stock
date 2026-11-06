"""Self-healing: detect the known stalls, fix the safe ones within cooldowns and caps, and say so."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

from celery.schedules import crontab
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.security import required_role
from app.db.base import Base
from app.models import AuditLog, IntradayBar, Notification, StockLearningWorkerDispatch, StockPaperRecoveryEvent, StockPaperRecoveryState
from app.services import self_healing as heal
from app.services.audit import write_audit_log
from app.services.intraday_data import MARKET_DATA_PROVIDER
from app.tasks import jobs
from app.tasks.celery_app import celery_app

UTC = timezone.utc
OPEN = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)   # Friday 09:30 ET
IN_SESSION = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)   # 10:00 ET
AFTER_CLOSE = datetime(2026, 11, 6, 22, 0, tzinfo=UTC)  # 17:00 ET


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "self_healing_enabled", True)
    monkeypatch.setattr(settings, "paper_meta_label_mode", "off")  # each test opts into the cases it exercises
    monkeypatch.setattr(heal, "_traded_symbols", lambda: ["AAPL", "SOFI", "SPY"])
    with Session(engine) as session:
        yield session


def _bar(db, symbol, opened_at):
    db.add(IntradayBar(symbol=symbol, timeframe="1m", opened_at=opened_at, open=Decimal("1"), high=Decimal("1"),
                       low=Decimal("1"), close=Decimal("1"), volume=1, provider=MARKET_DATA_PROVIDER,
                       feed_class="iex", exchange_timestamp=opened_at, ingested_at=opened_at))
    db.commit()


def _fresh_feed(db, now):
    for symbol in ("SPY", "AAPL", "SOFI"):
        _bar(db, symbol, now - timedelta(minutes=2))


def _outcome(result, key):
    return next(item for item in result["cases"] if item["case"] == key)


def _case_rows(db, key):
    return db.query(AuditLog).filter(AuditLog.event_type == heal.EVENT, AuditLog.action == key).order_by(AuditLog.id).all()


def _warnings(db):
    return db.query(Notification).filter_by(category=heal.CATEGORY).order_by(Notification.id).all()


# ------------------------------------------------------------ a clear run

def test_a_healthy_session_records_one_run_row_and_nothing_else(db):
    _fresh_feed(db, IN_SESSION)
    write_audit_log(db, event_type=heal.NEWS_EVENT, action="run_news_discovery", status="complete", message="t",
                    payload={}).created_at = (IN_SESSION - timedelta(minutes=10)).replace(tzinfo=None)
    db.commit()
    result = heal.run_self_healing(db, now=IN_SESSION)
    assert result["status"] == "complete" and result["outcome"] == "clear" and result["in_session"] is True
    assert {item["status"] for item in result["cases"]} == {"clear"}
    rows = db.query(AuditLog).filter_by(event_type=heal.EVENT).all()
    assert [row.action for row in rows] == ["run"] and rows[0].message == "Self-healing found nothing to fix."
    assert rows[0].created_at == IN_SESSION.replace(tzinfo=None)
    assert _warnings(db) == []


def test_disabled_runs_write_nothing(db, monkeypatch):
    monkeypatch.setattr(settings, "self_healing_enabled", False)
    assert heal.run_self_healing(db, now=IN_SESSION)["status"] == "disabled"
    assert db.query(AuditLog).count() == 0


# ------------------------------------------------------------ stale intraday symbols

def test_stale_symbols_are_backfilled_within_the_repair_budget(db, monkeypatch):
    monkeypatch.setattr(settings, "intraday_backfill_time_budget_seconds", 25.0)
    _bar(db, "SPY", IN_SESSION - timedelta(minutes=2))
    _bar(db, "AAPL", IN_SESSION - timedelta(minutes=30))  # stale
    ingest = Mock(return_value={"results": [{"symbol": "SOFI", "status": "complete"},
                                            {"symbol": "AAPL", "status": "incomplete"}],
                                "backfill_requests_remaining": 31})
    with patch("app.services.intraday_data.ingest_intraday", ingest):
        result = heal.run_self_healing(db, now=IN_SESSION)
    outcome = _outcome(result, "stale_intraday_symbols")
    assert outcome["status"] == "remediated" and result["outcome"] == "acted"
    # SOFI (never ingested) comes before AAPL (30 minutes stale); SPY is the feed reference, never a target.
    ingest.assert_called_once()
    assert ingest.call_args.args[1] == ["SOFI", "AAPL"]
    assert ingest.call_args.kwargs == {"now": IN_SESSION, "backfill_time_budget_seconds": 25.0}
    assert outcome["result"]["complete"] == ["SOFI"]
    (row,) = _case_rows(db, "stale_intraday_symbols")
    assert row.status == "remediated"
    assert row.message == ("Self-healing found 2 traded symbols have no minute bar from the last 15 minutes while SPY "
                           "is current (the minutely import ran out of its repair budget): SOFI, AAPL; it re-ran "
                           "the intraday backfill for SOFI, AAPL within the 25-second repair budget; 1 of 2 came "
                           "back complete.")
    assert row.payload["evidence"]["stale"] == [{"symbol": "SOFI", "minutes_since_bar": None},
                                                {"symbol": "AAPL", "minutes_since_bar": 30.0}]
    assert row.payload["paper_only"] is True and row.created_at == IN_SESSION.replace(tzinfo=None)


def test_stale_symbol_fix_waits_for_its_cooldown_then_stops_at_the_daily_cap(db, monkeypatch):
    ingest = Mock(return_value={"results": [], "backfill_requests_remaining": 40})
    case = heal.CASE_BY_KEY["stale_intraday_symbols"]

    def run(now):  # SPY stays current at every run; AAPL and SOFI never get a bar
        _bar(db, "SPY", now - timedelta(minutes=2))
        return heal.run_self_healing(db, now=now)

    with patch("app.services.intraday_data.ingest_intraday", ingest):
        first = run(IN_SESSION)
        again = run(IN_SESSION + timedelta(minutes=15))
        later = run(IN_SESSION + case.cooldown + timedelta(minutes=1))
    assert _outcome(first, "stale_intraday_symbols")["status"] == "remediated"
    cooled = _outcome(again, "stale_intraday_symbols")
    assert cooled["status"] == "cooldown" and "cooldown is 20 minutes" in cooled["done"]
    assert _outcome(later, "stale_intraday_symbols")["status"] == "remediated"
    assert ingest.call_count == 2
    # A cooldown is noted in the run row only; the ledger keeps the actions.
    assert [row.status for row in _case_rows(db, "stale_intraday_symbols")] == ["remediated", "remediated"]
    assert _warnings(db) == []

    # Past the daily cap the job raises one warning instead of acting, and refreshes it while it persists.
    monkeypatch.setattr(heal, "CASES", tuple(
        heal.Case(**{**c.__dict__, "daily_cap": 2}) if c.key == case.key else c for c in heal.CASES))
    monkeypatch.setattr(heal, "CASE_BY_KEY", {c.key: c for c in heal.CASES})
    with patch("app.services.intraday_data.ingest_intraday", ingest):
        capped = run(IN_SESSION + timedelta(hours=2))
        still = run(IN_SESSION + timedelta(hours=3))
    assert _outcome(capped, "stale_intraday_symbols")["status"] == "capped"
    assert _outcome(still, "stale_intraday_symbols")["status"] == "capped"
    assert ingest.call_count == 2
    assert capped["notifications"] == {"stale_intraday_symbols": "raised"}
    assert still["notifications"] == {"stale_intraday_symbols": "refreshed"}
    (warning,) = _warnings(db)
    assert warning.severity == "warning" and warning.status == "open"
    assert warning.title == "Self-healing needs attention: Traded symbols get minute bars"
    assert "today's cap of 2" in warning.message and warning.payload["case"] == "stale_intraday_symbols"
    assert _case_rows(db, "stale_intraday_symbols")[-1].status == "capped"

    # A new day resets the cap; the fix runs and the warning resolves.
    next_day = IN_SESSION + timedelta(days=3)  # Monday 10:00 ET
    with patch("app.services.intraday_data.ingest_intraday", ingest):
        monday = run(next_day)
    assert _outcome(monday, "stale_intraday_symbols")["status"] == "remediated"
    assert monday["notifications"] == {"stale_intraday_symbols": "resolved"}
    assert _warnings(db)[0].status == "resolved"


def test_a_feed_outage_is_reported_not_backfilled(db):
    _bar(db, "SPY", IN_SESSION - timedelta(minutes=12))
    with patch("app.services.intraday_data.ingest_intraday") as ingest:
        result = heal.run_self_healing(db, now=IN_SESSION)
    outcome = _outcome(result, "stale_intraday_symbols")
    assert outcome["status"] == "manual" and "SPY itself has no minute bar for 12 minutes" in outcome["found"]
    ingest.assert_not_called()
    assert _case_rows(db, "stale_intraday_symbols") == [] and _warnings(db) == []


def test_stale_symbols_are_only_checked_in_session_after_the_open_grace(db):
    with patch("app.services.intraday_data.ingest_intraday") as ingest:
        early = heal.run_self_healing(db, now=OPEN + timedelta(minutes=5))
        closed = heal.run_self_healing(db, now=AFTER_CLOSE)
    assert _outcome(early, "stale_intraday_symbols")["status"] == "clear"
    assert "first 20 minutes" in _outcome(early, "stale_intraday_symbols")["found"]
    assert _outcome(closed, "stale_intraday_symbols")["status"] == "clear"
    ingest.assert_not_called()


def test_a_failing_fix_counts_toward_the_budget_and_is_recorded(db):
    _bar(db, "SPY", IN_SESSION - timedelta(minutes=2))
    with patch("app.services.intraday_data.ingest_intraday", side_effect=RuntimeError("provider 503")):
        result = heal.run_self_healing(db, now=IN_SESSION)
        again = heal.run_self_healing(db, now=IN_SESSION + timedelta(minutes=3))
    assert _outcome(result, "stale_intraday_symbols")["status"] == "failed"
    assert _outcome(again, "stale_intraday_symbols")["status"] == "cooldown"
    (row,) = _case_rows(db, "stale_intraday_symbols")
    assert row.status == "failed" and row.message.endswith("its fix failed: RuntimeError: provider 503.")


# ------------------------------------------------------------ meta-label artifact

def test_a_missing_meta_label_model_is_retrained_off_hours_only(db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "paper_meta_label_mode", "shadow")
    monkeypatch.setattr(settings, "meta_label_retrain_enabled", True)
    monkeypatch.setattr("app.services.meta_label_shadow.model_path", lambda: tmp_path / "meta_label_model.joblib")
    with patch.object(jobs.meta_label_retrain_job, "apply_async", return_value=Mock(id="task-1")) as enqueue:
        during = heal.run_self_healing(db, now=IN_SESSION)
        after = heal.run_self_healing(db, now=AFTER_CLOSE)
        soon = heal.run_self_healing(db, now=AFTER_CLOSE + timedelta(hours=1))
    waiting = _outcome(during, "meta_label_artifact")
    assert waiting["status"] == "waiting" and "shadow mode fails open" in waiting["found"]
    assert "deferred until the close" in waiting["found"]
    fixed = _outcome(after, "meta_label_artifact")
    assert fixed["status"] == "remediated" and fixed["result"] == {"task_id": "task-1"}
    assert _outcome(soon, "meta_label_artifact")["status"] == "cooldown"  # six hours between retrains
    enqueue.assert_called_once_with(expires=6 * 60 * 60)
    (row,) = _case_rows(db, "meta_label_artifact")
    assert row.message.startswith("Self-healing found the meta-label model is missing at ")
    assert row.message.endswith("it enqueued the meta-label retrain on the learning queue instead of waiting "
                                "for the 20:15 run.")


def test_a_present_model_or_an_off_filter_is_clear(db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "paper_meta_label_mode", "shadow")
    path = tmp_path / "meta_label_model.joblib"
    path.write_bytes(b"model")
    (tmp_path / "meta_label_model.joblib.json").write_text("{}")
    monkeypatch.setattr("app.services.meta_label_shadow.model_path", lambda: path)
    assert heal.detect_meta_label_artifact(db, AFTER_CLOSE).found is False
    monkeypatch.setattr(settings, "paper_meta_label_mode", "off")
    monkeypatch.setattr("app.services.meta_label_shadow.model_path", lambda: tmp_path / "missing.joblib")
    assert heal.detect_meta_label_artifact(db, AFTER_CLOSE).found is False


def test_a_missing_model_with_retraining_disabled_is_reported_only(db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "paper_meta_label_mode", "enforce")
    monkeypatch.setattr(settings, "meta_label_retrain_enabled", False)
    monkeypatch.setattr("app.services.meta_label_shadow.model_path", lambda: tmp_path / "missing.joblib")
    with patch.object(jobs.meta_label_retrain_job, "apply_async") as enqueue:
        result = heal.run_self_healing(db, now=AFTER_CLOSE)
    outcome = _outcome(result, "meta_label_artifact")
    assert outcome["status"] == "manual" and "enforce mode skips every BUY" in outcome["found"]
    enqueue.assert_not_called()


# ------------------------------------------------------------ expired learning leases

def _dispatch(db, symbol, *, lease_expires_at, attempts=1, status="running"):
    row = StockLearningWorkerDispatch(batch_id="batch-1", symbol=symbol, strategy_slug="rsi2_pullback",
                                      max_candidates=3, status=status, task_id=f"old-{symbol}",
                                      attempt_count=attempts, claimed_at=lease_expires_at - timedelta(minutes=15),
                                      lease_expires_at=lease_expires_at)
    db.add(row)
    db.commit()
    return row


def test_an_expired_learning_lease_is_requeued_like_the_recovery_job(db):
    expired = _dispatch(db, "AAPL", lease_expires_at=IN_SESSION - timedelta(minutes=10))
    fresh = _dispatch(db, "MSFT", lease_expires_at=IN_SESSION + timedelta(minutes=5))
    recent = _dispatch(db, "NVDA", lease_expires_at=IN_SESSION - timedelta(minutes=2))  # inside the grace
    with patch.object(jobs.strategy_learning_scope_job, "apply_async", return_value=Mock(id="task-9")) as enqueue:
        result = heal.run_self_healing(db, now=IN_SESSION)
    outcome = _outcome(result, "expired_learning_lease")
    assert outcome["status"] == "remediated"
    enqueue.assert_called_once_with(args=["AAPL", "rsi2_pullback", 3, expired.id], queue="learning")
    db.refresh(expired)
    assert (expired.status, expired.task_id, expired.lease_expires_at, expired.error_type) == ("published", "task-9", None, None)
    assert fresh.status == "running" and recent.status == "running"
    (row,) = _case_rows(db, "expired_learning_lease")
    assert row.message == ("Self-healing found 1 learning scopes are marked running but their worker lease expired "
                           "more than 5 minutes ago without a result, and the minutely recovery job has not "
                           "requeued them (it waits on the learning queue behind the long jobs): "
                           "AAPL/rsi2_pullback; it requeued 1 learning scopes on the learning queue "
                           "(AAPL/rsi2_pullback).")
    assert row.payload["result"]["requeued"] == [{"dispatch_id": expired.id, "scope": "AAPL/rsi2_pullback",
                                                  "task_id": "task-9"}]


def test_a_scope_that_already_ran_three_times_is_left_for_the_batch(db):
    _dispatch(db, "AAPL", lease_expires_at=IN_SESSION - timedelta(minutes=10), attempts=3)
    with patch.object(jobs.strategy_learning_scope_job, "apply_async") as enqueue:
        result = heal.run_self_healing(db, now=IN_SESSION)
    outcome = _outcome(result, "expired_learning_lease")
    assert outcome["status"] == "manual" and "already ran 3 times" in outcome["found"]
    enqueue.assert_not_called()


def test_a_requeue_that_cannot_reach_the_broker_is_a_failed_fix(db):
    row = _dispatch(db, "AAPL", lease_expires_at=IN_SESSION - timedelta(minutes=10))
    with patch.object(jobs.strategy_learning_scope_job, "apply_async", side_effect=RuntimeError("redis down")):
        result = heal.run_self_healing(db, now=IN_SESSION)
    assert _outcome(result, "expired_learning_lease")["status"] == "failed"
    db.refresh(row)
    assert row.status == "failed" and row.error_type == "lease_expired"  # the recovery job can still pick it up


# ------------------------------------------------------------ reported-only cases

def test_a_starved_fast_lane_raises_one_warning_and_resolves_when_it_runs_again(db, monkeypatch):
    monkeypatch.setattr(settings, "news_discovery_enabled", True)
    _fresh_feed(db, IN_SESSION)
    starved = heal.run_self_healing(db, now=IN_SESSION)
    outcome = _outcome(starved, "learning_lane_starved")
    assert outcome["status"] == "manual" and "has not run at all" in outcome["found"]
    assert starved["notifications"] == {"learning_lane_starved": "raised"}
    (warning,) = _warnings(db)
    assert warning.title == "Self-healing needs attention: Fast learning lane is served"
    assert "reported only" in warning.message
    assert _case_rows(db, "learning_lane_starved") == []  # nothing was done, so no action row

    write_audit_log(db, event_type=heal.NEWS_EVENT, action="run_news_discovery", status="complete", message="t",
                    payload={}).created_at = (IN_SESSION + timedelta(minutes=20)).replace(tzinfo=None)
    db.commit()
    served = heal.run_self_healing(db, now=IN_SESSION + timedelta(minutes=30))
    assert _outcome(served, "learning_lane_starved")["status"] == "clear"
    assert served["notifications"] == {"learning_lane_starved": "resolved"}
    assert _warnings(db)[0].status == "resolved"
    # Outside the discovery window, and when discovery is off, there is nothing to check.
    assert heal.detect_learning_lane_starved(db, AFTER_CLOSE).found is False
    monkeypatch.setattr(settings, "news_discovery_enabled", False)
    assert heal.detect_learning_lane_starved(db, IN_SESSION).found is False


def test_heartbeat_and_monitor_pauses_are_reported_but_never_touched(db):
    state = StockPaperRecoveryState(id=1, updated_by="test", status="cooldown",
                                    pause_reason="Watchdog: monitor heartbeat is stale")
    db.add(state)
    db.add(StockPaperRecoveryEvent(action="pause", status="cooldown", actor="stock_watchdog",
                                   reason="Watchdog: monitor heartbeat is stale", event_sha256="p1", payload={},
                                   created_at=IN_SESSION - timedelta(minutes=30)))
    db.commit()
    _fresh_feed(db, IN_SESSION)
    result = heal.run_self_healing(db, now=IN_SESSION)
    heartbeat = _outcome(result, "heartbeat_pause")
    assert heartbeat["status"] == "manual" and "self-healing never resumes trading" in heartbeat["found"]
    pauses = _outcome(result, "monitor_pauses")
    assert pauses["status"] == "manual" and pauses["found"].startswith("1 pause(s) today by the watchdog or monitor")
    db.refresh(state)
    assert state.status == "cooldown"  # untouched
    assert _warnings(db) == []  # these pauses already raise their own alerts
    assert result["outcome"] == "attention"
    run = db.query(AuditLog).filter_by(event_type=heal.EVENT, action="run").one()
    assert run.message == ("Self-healing run: No heartbeat pause after a publish: manual; "
                           "Monitoring did not pause trading: manual.")


def test_a_broken_detector_does_not_stop_the_others(db, monkeypatch):
    _fresh_feed(db, IN_SESSION)
    broken = heal.Case("broken", "Broken check", lambda db, now: (_ for _ in ()).throw(ValueError("boom")), "none")
    monkeypatch.setattr(heal, "CASES", (broken, *heal.CASES))
    result = heal.run_self_healing(db, now=IN_SESSION)
    assert _outcome(result, "broken")["status"] == "manual"
    assert "the check itself failed (ValueError: boom)" in _outcome(result, "broken")["found"]
    assert _outcome(result, "stale_intraday_symbols")["status"] == "clear"


# ------------------------------------------------------------ surface and wiring

def test_summary_lists_every_case_with_its_budget_and_latest_outcome(db):
    _bar(db, "SPY", IN_SESSION - timedelta(minutes=2))
    with patch("app.services.intraday_data.ingest_intraday", return_value={"results": []}):
        heal.run_self_healing(db, now=IN_SESSION)
    summary = heal.self_healing_summary(db, now=IN_SESSION + timedelta(minutes=5))
    assert summary["enabled"] is True and summary["paper_only"] is True
    assert "kill switch" in " ".join(summary["never_touches"])
    assert [case["key"] for case in summary["cases"]] == [case.key for case in heal.CASES]
    stale = next(case for case in summary["cases"] if case["key"] == "stale_intraday_symbols")
    assert stale["automatic"] is True and stale["actions_today"] == 1 and stale["daily_cap"] == 12
    assert stale["cooldown_until"] == (IN_SESSION + timedelta(minutes=20)).isoformat()
    assert stale["latest"]["status"] == "remediated"
    manual = next(case for case in summary["cases"] if case["key"] == "heartbeat_pause")
    assert manual["automatic"] is False and manual["latest"]["status"] == "clear"
    assert summary["last_run"]["status"] == "acted" and len(summary["last_run"]["cases"]) == len(heal.CASES)
    assert summary["recent"][0]["case"] == "stale_intraday_symbols" and summary["open_warnings"] == []


def test_route_is_viewer_readable_and_the_job_runs_on_the_fast_lane_every_quarter_hour():
    assert required_role("GET", "/stock-paper/self-healing") == "viewer"
    entry = celery_app.conf.beat_schedule["self-healing"]
    assert entry["task"] == "app.tasks.jobs.self_healing_job"
    assert isinstance(entry["schedule"], crontab)
    assert entry["schedule"].minute == set(range(0, 60, 15))
    assert entry["schedule"].hour == set(range(7, 21))
    assert entry["schedule"].day_of_week == {1, 2, 3, 4, 5}
    assert entry["options"]["expires"] == 10 * 60
    assert celery_app.conf.task_routes["app.tasks.jobs.self_healing_job"] == {"queue": "learning_fast"}
    assert jobs.self_healing_job.soft_time_limit == 120


def test_job_runs_the_service_under_the_shared_job_wrapper():
    seen = {}

    def fake_run_job(job_name, work):
        seen["job"] = job_name
        with patch("app.services.self_healing.run_self_healing", return_value={"status": "complete"}):
            return work(None)

    with patch.object(jobs, "_run_job", fake_run_job):
        result = jobs.self_healing_job.run()
    assert seen["job"] == "self_healing_job" and result == {"job": "self_healing_job", "status": "complete"}
