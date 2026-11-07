"""Drift-triggered retraining: a persistent model.* breach starts one governed
challenger cycle (trigger "drift") within the guard rails; every gate intact."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import stock_learning_cycle as learning_cycle_api
from app.core.config import settings
from app.db.base import Base
from app.models import (
    AuditLog,
    Notification,
    StockLearningCycle,
    StockLearningCycleEvent,
    StockMonitoringBreach,
    StockMonitoringSnapshot,
)
from app.services import drift_retraining as drift
from app.services import paper_mandate, stock_learning_cycle as cycles, stock_training_jobs
from app.services.stock_learning_cycle import StockTrainingError, create_learning_cycle
from app.services.stock_monitoring import MONITOR_KEY
from app.tasks import jobs
from app.tasks.celery_app import celery_app

UTC = timezone.utc
IN_WINDOW = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)      # Friday 10:00 ET
BEFORE_WINDOW = datetime(2026, 11, 6, 14, 40, tzinfo=UTC)  # 09:40 ET, inside the 15-minute open offset
LATE_SESSION = datetime(2026, 11, 6, 20, 45, tzinfo=UTC)   # 15:45 ET, inside the 30-minute close margin
AFTER_CLOSE = datetime(2026, 11, 6, 22, 0, tzinfo=UTC)     # 17:00 ET
MONDAY_OPEN = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)
PAUSED = {"paused": False}


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "drift_retrain_enabled", True)
    monkeypatch.setattr(settings, "drift_retrain_cooldown_hours", 48)
    monkeypatch.setattr(settings, "drift_retrain_max_per_day", 1)
    monkeypatch.setattr(settings, "drift_retrain_backoff_factor", 2)
    with patch("app.services.stock_learning_cycle.scheduled_learning_control_projection", return_value=PAUSED):
        with Session(engine) as session:
            yield session


def _snapshot(db, at, status="breach"):
    row = StockMonitoringSnapshot(monitor_key=MONITOR_KEY, status=status, generated_at=at, checks=[], actions=[],
                                  source="test")
    db.add(row)
    db.commit()
    return row


def _breach(db, key, status, *, count, at, first=None, category="model"):
    row = db.scalar(select(StockMonitoringBreach).where(StockMonitoringBreach.breach_key == key))
    if row is None:
        row = StockMonitoringBreach(breach_key=key, category=category, metric=key.split(".")[-1], status=status,
                                    severity="critical", last_observed_at=at, details={})
        db.add(row)
    row.status, row.consecutive_count, row.last_observed_at = status, count, at
    row.first_observed_at = first or row.first_observed_at or at
    row.observed_value, row.threshold = {"absolute_mean_shift": 0.25}, {"breach_mean_shift": 0.2}
    db.commit()
    return row


def _persistent_breach(db, at, key="model.prediction_distribution", first=None):
    _snapshot(db, at)
    return _breach(db, key, "persistent", count=3, at=at, first=first or at - timedelta(minutes=10))


def _drift_cycle(db, *, cycle_id, status, stage, created_at, updated_at=None, episodes=(), model_run_id=None,
                 training_job_id=None, gates=None, last_reason="test"):
    row = StockLearningCycle(
        cycle_id=cycle_id, request_sha256=cycle_id, trigger="drift", status=status, stage=stage,
        requested_by=drift.ACTOR, symbols=["AAPL", "MSFT", "QQQ", "SPY"], cutoff_date=created_at.date(),
        horizon_days=5, provider="yfinance", seed=1, model_run_id=model_run_id, training_job_id=training_job_id,
        gates=gates or {}, evidence={"request": {"lineage": {"episodes": list(episodes), "breach_keys": ["model.x"],
                                                              "monitoring_snapshot_id": 1}}},
        last_reason=last_reason, created_at=created_at, updated_at=updated_at or created_at,
    )
    db.add(row)
    db.commit()
    return row


class _FakeCreate:
    """Stands in for create_learning_cycle so guard tests need no feed or dataset."""

    def __init__(self, status="queued", stage="training", raise_error=None):
        self.calls: list[dict] = []
        self.status, self.stage, self.raise_error = status, stage, raise_error

    def __call__(self, db, **kwargs):
        self.calls.append(kwargs)
        if self.raise_error:
            raise StockTrainingError(self.raise_error)
        cycle = StockLearningCycle(
            cycle_id=f"{len(self.calls):064x}", request_sha256=f"{len(self.calls) + 500:064x}",
            trigger=kwargs["trigger"], status=self.status, stage=self.stage, requested_by=kwargs["actor"],
            symbols=kwargs["symbols"], cutoff_date=kwargs["cutoff_at"], horizon_days=kwargs["horizon_days"],
            provider=kwargs["provider"], seed=kwargs["seed"], gates={},
            evidence={"request": {"lineage": kwargs.get("lineage") or {}}}, last_reason="queued",
        )
        db.add(cycle)
        db.flush()
        return cycle, False


def _rows(db, action=None):
    query = db.query(AuditLog).filter(AuditLog.event_type == drift.EVENT)
    if action:
        query = query.filter(AuditLog.action == action)
    return query.order_by(AuditLog.id).all()


def _run(db, now, fake=None):
    fake = fake or _FakeCreate()
    with patch("app.services.stock_learning_cycle.create_learning_cycle", fake):
        return drift.run_drift_retraining(db, now=now), fake


# ------------------------------------------------------------------ 1. trigger

def test_trigger_requires_a_persistent_model_breach_in_the_latest_snapshot(db):
    now = IN_WINDOW
    assert drift.evaluate_drift_trigger(db, now=now)["triggered"] is False
    _snapshot(db, now - timedelta(minutes=2))
    # Observed once is not a trigger; the second consecutive observation is.
    _breach(db, "model.prediction_distribution", "observed", count=1, at=now - timedelta(minutes=2))
    assert drift.evaluate_drift_trigger(db, now=now)["triggered"] is False
    _breach(db, "model.prediction_distribution", "observed", count=2, at=now - timedelta(minutes=2))
    assert drift.evaluate_drift_trigger(db, now=now)["triggered"] is True
    first = now - timedelta(minutes=7)
    _breach(db, "model.prediction_distribution", "persistent", count=3, at=now - timedelta(minutes=2), first=first)
    # A persistent safety breach is not a retraining reason.
    _breach(db, "health.broker", "persistent", count=5, at=now - timedelta(minutes=2), category="health")
    # A model breach the latest snapshot cleared does not count.
    _breach(db, "model.input_features", "cleared", count=0, at=now - timedelta(minutes=2))
    trigger = drift.evaluate_drift_trigger(db, now=now)
    assert trigger["triggered"] is True
    assert trigger["breach_keys"] == ["model.prediction_distribution"]
    assert trigger["episodes"] == [f"model.prediction_distribution@{first.isoformat()}"]
    assert trigger["snapshot_id"] is not None
    assert {b["key"] for b in trigger["breaches"]} == {"model.prediction_distribution", "model.input_features"}
    # A persistent breach that does not belong to the latest snapshot (an older run) is not current evidence.
    later = now + drift.SNAPSHOT_BREACH_TOLERANCE + timedelta(minutes=1)
    _snapshot(db, later)
    assert drift.evaluate_drift_trigger(db, now=later)["triggered"] is False
    # Stale monitoring is not evidence either.
    _breach(db, "model.prediction_distribution", "persistent", count=4, at=later, first=first)
    assert drift.evaluate_drift_trigger(db, now=later)["triggered"] is True
    stale = drift.evaluate_drift_trigger(db, now=later + drift.EVIDENCE_MAX_AGE + timedelta(minutes=1))
    assert stale["triggered"] is False and "stale" in stale["reason"]


# ------------------------------------------------------------------ 2. the cycle

def test_persistent_breach_creates_a_drift_cycle_with_lineage(db):
    first = IN_WINDOW - timedelta(minutes=10)
    breach = _persistent_breach(db, IN_WINDOW - timedelta(minutes=1), first=first)
    snapshot_id = db.scalar(select(StockMonitoringSnapshot.id))
    result, fake = _run(db, IN_WINDOW)

    assert result["status"] == "scheduled", result
    assert result["paper_only"] is True and result["live_authorized"] is False
    call = fake.calls[0]
    assert call["trigger"] == "drift" and call["actor"] == drift.ACTOR
    assert call["symbols"] == list(settings.stock_learning_default_symbols)
    assert call["cutoff_at"] == date(2026, 11, 6) and call["horizon_days"] == 5
    lineage = call["lineage"]
    assert lineage["monitoring_snapshot_id"] == snapshot_id
    assert lineage["breach_keys"] == ["model.prediction_distribution"]
    assert lineage["episodes"] == [drift.episode_key(breach)]
    assert lineage["breaches"][0]["observed_value"] == {"absolute_mean_shift": 0.25}
    assert lineage["attempt"] == 1
    cycle = db.get(StockLearningCycle, result["cycle_id"])
    assert cycle.trigger == "drift"
    assert cycle.evidence["request"]["lineage"]["monitoring_snapshot_id"] == snapshot_id
    scheduled = _rows(db, "schedule_cycle")
    assert len(scheduled) == 1 and scheduled[0].status == "scheduled"
    assert scheduled[0].payload["cycle_id"] == cycle.cycle_id
    assert scheduled[0].payload["breach_keys"] == ["model.prediction_distribution"]
    assert scheduled[0].payload["monitoring_snapshot_id"] == snapshot_id
    notice = db.query(Notification).filter_by(source=drift.ACTOR).one()
    assert notice.payload["cycle_id"] == cycle.cycle_id

    # The same breach cannot start a second cycle while the first is open.
    again, fake2 = _run(db, IN_WINDOW + timedelta(minutes=15))
    assert again["status"] == "waiting" and again["cycle_id"] == cycle.cycle_id
    assert fake2.calls == []


def test_real_create_learning_cycle_accepts_drift_as_an_automated_trigger(db):
    gates = {name: {"status": "pass"} for name in
             ("verified_feed", "scheduler_health", "paper_ledger", "dataset_provenance", "readiness_history")}
    job = SimpleNamespace(id="00000000-0000-0000-0000-000000000901", snapshot_id="s" * 64, status="queued",
                          queue_error=None)
    lineage = {"source": drift.ACTOR, "breach_keys": ["model.input_features"], "monitoring_snapshot_id": 7}
    with patch.object(cycles, "evaluate_cycle_prerequisites", return_value=gates), \
            patch.object(cycles, "create_stock_training_job", return_value=(job, False)) as create_job:
        cycle, duplicate = create_learning_cycle(
            db, symbols=["SPY"], cutoff_at=date(2026, 11, 6), horizon_days=5, provider="yfinance",
            actor=drift.ACTOR, trigger="drift", seed=7, lineage=lineage,
        )
    assert duplicate is False and cycle.trigger == "drift"
    assert cycle.status == "queued" and cycle.stage == "training"
    assert create_job.call_args.kwargs["trigger"] == "drift"
    assert cycle.evidence["request"]["lineage"] == lineage
    assert [e.stage for e in db.scalars(select(StockLearningCycleEvent)).all()] == ["preflight", "dataset", "training"]

    # Unknown prerequisite evidence defers a drift cycle like a scheduled one (never "blocked" as manual).
    unknown = {**gates, "verified_feed": {"status": "unknown", "reason": "regular session required"}}
    with patch.object(cycles, "evaluate_cycle_prerequisites", return_value=unknown):
        deferred, _ = create_learning_cycle(
            db, symbols=["SPY"], cutoff_at=date(2026, 11, 6), horizon_days=5, provider="yfinance",
            actor=drift.ACTOR, trigger="drift", seed=8, lineage=lineage,
        )
    assert deferred.status == "deferred" and deferred.stage == "preflight"
    with pytest.raises(StockTrainingError, match="Only manual, scheduled, or drift"):
        create_learning_cycle(db, symbols=["SPY"], cutoff_at=date(2026, 11, 6), horizon_days=5,
                              provider="yfinance", actor="x", trigger="bogus")


def test_training_job_validation_accepts_drift(db):
    with pytest.raises(StockTrainingError, match="Only manual, scheduled, or drift"):
        stock_training_jobs.create_stock_training_job(
            db, symbols=["SPY"], horizon_bars=5, actor="x", cutoff_at=date(2026, 11, 6), trigger="bogus")
    # Past trigger validation the dataset build is the next step; it is rejected here, not the trigger.
    with patch.object(stock_training_jobs, "_lock_training_admission"), \
            patch.object(stock_training_jobs, "create_stock_dataset", side_effect=ValueError("no history")):
        with pytest.raises(StockTrainingError, match="Verified dataset snapshot rejected"):
            stock_training_jobs.create_stock_training_job(
                db, symbols=["SPY"], horizon_bars=5, actor="x", cutoff_at=date(2026, 11, 6), trigger="drift")


def test_automation_handles_drift_cycles_like_scheduled_ones(db):
    cycle = _drift_cycle(db, cycle_id="a" * 64, status="awaiting_admission", stage="admission",
                         created_at=IN_WINDOW, training_job_id=None)
    # Admission no longer rejects the trigger; it reports the missing training job instead.
    with patch.object(cycles, "create_notification"):
        admitted = cycles.admit_scheduled_learning_cycle(db, cycle.cycle_id)
    assert admitted.status == "deferred" and "not yet a successfully completed training job" in admitted.last_reason
    # Promotion rejects only manual cycles on the trigger; a drift cycle is evaluated like a scheduled one.
    manual = StockLearningCycle(
        cycle_id="d" * 64, request_sha256="d" * 64, trigger="manual", status="running_forward_trial", stage="forward_trial",
        requested_by="op", symbols=["SPY"], cutoff_date=date(2026, 11, 6), horizon_days=5, provider="yfinance",
        seed=1, gates={}, evidence={}, last_reason="x",
    )
    db.add(manual)
    db.commit()
    with pytest.raises(StockTrainingError, match="limited to scheduled and drift"):
        cycles.automate_paper_promotion(db, manual.cycle_id)
    # The handoff job picks drift cycles up (here: defers them while learning is paused).
    job_cycle = _drift_cycle(db, cycle_id="e" * 64, status="awaiting_admission", stage="admission",
                             created_at=IN_WINDOW, training_job_id="00000000-0000-0000-0000-000000000902")
    with patch.object(cycles, "_scheduled_learning_pause_reason", return_value="operator pause"):
        result = cycles.run_scheduled_paper_trial_handoff_job(db)
    assert result["status"] == "paused"
    db.refresh(job_cycle)
    assert job_cycle.status == "deferred" and job_cycle.last_reason == "operator pause"


def test_mandate_scheduled_trigger_covers_drift_cycles():
    mandate = SimpleNamespace(
        id=1, execution_provider="alpaca", symbols=["SPY"], exposure_limits={}, loss_limits={"unit": "u", "max_loss": "1"},
        max_sessions_per_cycle=20, max_sessions=60, schedule={"triggers": ["scheduled"]}, stop_conditions=[],
        valid_from=IN_WINDOW - timedelta(days=1), expires_at=IN_WINDOW + timedelta(days=30),
        remaining_position_policies=["hold"], stop_authority="operator_and_system", pending_order_treatment="cancel",
    )
    expected = {"environment": "paper", "execution_provider": "alpaca", "symbols": ["SPY"], "exposure_limits": {},
                "loss_limits": {"unit": "u", "max_loss": "1"}, "duration_sessions": 5, "schedule": {},
                "stop_conditions": [], "remaining_position_policy": "hold", "stop_authority": "operator_and_system",
                "pending_order_treatment": "cancel"}
    with patch.object(paper_mandate, "mandate_state", return_value=("active", None)), \
            patch.object(paper_mandate, "sessions_used", return_value=0):
        covered = paper_mandate.mandate_fit(None, mandate, expected, trigger="drift", now=IN_WINDOW)
        manual = paper_mandate.mandate_fit(None, mandate, expected, trigger="manual", now=IN_WINDOW)
    assert not any("does not cover" in reason for reason in covered), covered
    assert any("does not cover manual" in reason for reason in manual)


# ------------------------------------------------------------------ 3. guard rails

def test_open_cycle_guard_records_one_waiting_decision(db):
    _persistent_breach(db, IN_WINDOW - timedelta(minutes=1))
    _drift_cycle(db, cycle_id="a" * 64, status="running_forward_trial", stage="forward_trial",
                 created_at=IN_WINDOW - timedelta(days=3))
    first, fake = _run(db, IN_WINDOW)
    assert first["status"] == "waiting" and fake.calls == []
    second, _ = _run(db, IN_WINDOW + timedelta(minutes=15))
    assert second["status"] == "waiting"
    rows = _rows(db, "evaluate")
    assert len(rows) == 1 and rows[0].status == "waiting" and rows[0].payload["cycle_id"] == "a" * 64


def test_dead_deferred_or_abandoned_cycles_do_not_hold_the_open_slot(db):
    now = IN_WINDOW
    # Deferred at preflight without a model: nothing retries it, so it is ended ("not evaluated").
    dead = _drift_cycle(db, cycle_id="a" * 64, status="deferred", stage="preflight", created_at=now - timedelta(days=3))
    assert drift.cycle_outcome(dead, now=now) == "not_evaluated"
    live = _drift_cycle(db, cycle_id="b" * 64, status="deferred", stage="admission", created_at=now - timedelta(days=3),
                        training_job_id="00000000-0000-0000-0000-000000000903")
    assert drift.cycle_outcome(live, now=now) is None
    old = _drift_cycle(db, cycle_id="c" * 64, status="running_forward_trial", stage="forward_trial",
                       created_at=now - drift.OPEN_CYCLE_MAX_AGE - timedelta(days=1))
    assert drift.cycle_outcome(old, now=now) == "abandoned"
    blocked_after_training = _drift_cycle(db, cycle_id="d" * 64, status="blocked", stage="admission",
                                          created_at=now - timedelta(days=3), model_run_id="m" * 64)
    assert drift.cycle_outcome(blocked_after_training, now=now) == "failed"
    assert drift.cycle_outcome(_drift_cycle(db, cycle_id="e" * 64, status="promoted", stage="promotion",
                                            created_at=now - timedelta(days=3)), now=now) == "promoted"


def test_cooldown_and_exponential_backoff_after_failed_cycles(db):
    now = IN_WINDOW
    _persistent_breach(db, now - timedelta(minutes=1))
    base = timedelta(hours=48)
    ended = now - timedelta(hours=10)
    _drift_cycle(db, cycle_id="1" * 64, status="demoted", stage="promotion", created_at=ended - timedelta(days=5),
                 updated_at=ended)
    state = drift.guard_state(db, now=now)
    assert state["consecutive_failures"] == 1 and state["cooldown_active"] is True
    assert state["cooldown_until"] == (ended + base).isoformat()
    result, fake = _run(db, now)
    assert result["status"] == "cooldown" and fake.calls == []
    assert "back-off after 1 consecutive failure" in result["reason"]
    # Past the cooldown the same persistent breach starts the next attempt.
    later = ended + base + timedelta(minutes=1)
    _persistent_breach(db, later - timedelta(minutes=1))
    result, fake = _run(db, later + timedelta(hours=3) if drift.trigger_window(later)["open"] is False else later)
    assert result["status"] in {"scheduled", "deferred"}
    # Two consecutive failures double the wait; three quadruple it; a promotion resets it.
    db.query(StockLearningCycle).delete()
    db.commit()
    for index in range(2):
        _drift_cycle(db, cycle_id=str(index + 2) * 64, status="failed", stage="training",
                     created_at=ended - timedelta(days=4 - index), updated_at=ended - timedelta(days=2 - index),
                     model_run_id="m" * 64)
    state = drift.guard_state(db, now=now)
    assert state["consecutive_failures"] == 2
    assert state["cooldown_until"] == (ended - timedelta(days=1) + base * 2).isoformat()
    assert drift.backoff_delay(3) == base * 4
    assert drift.backoff_delay(1) == base
    assert drift.backoff_delay(drift.MAX_BACKOFF_EXPONENT + 5) == base * 2 ** drift.MAX_BACKOFF_EXPONENT
    _drift_cycle(db, cycle_id="5" * 64, status="promoted", stage="promotion", created_at=ended - timedelta(hours=12),
                 updated_at=ended - timedelta(hours=6))
    state = drift.guard_state(db, now=now)
    assert state["consecutive_failures"] == 0
    assert state["cooldown_until"] == (ended - timedelta(hours=6) + base).isoformat()
    assert state["last_cycle"]["outcome"] == "promoted"


def test_promoted_cycle_answers_its_breach_episode_but_not_a_new_one(db):
    now = IN_WINDOW
    first = now - timedelta(days=10)
    breach = _persistent_breach(db, now - timedelta(minutes=1), first=first)
    episode = drift.episode_key(breach)
    _drift_cycle(db, cycle_id="a" * 64, status="promoted", stage="promotion", created_at=now - timedelta(days=9),
                 updated_at=now - timedelta(days=5), episodes=[episode])
    result, fake = _run(db, now)
    assert result["status"] == "answered" and fake.calls == []
    assert drift.guard_state(db, now=now, trigger=drift.evaluate_drift_trigger(db, now=now))["answered_episodes"] == [episode]
    # The breach clears and comes back: a new episode, past the cooldown, retrains again.
    _breach(db, breach.breach_key, "persistent", count=2, at=now - timedelta(minutes=1), first=now - timedelta(hours=1))
    result, fake = _run(db, now)
    assert result["status"] == "scheduled" and len(fake.calls) == 1


def test_daily_cap(db, monkeypatch):
    now = IN_WINDOW
    monkeypatch.setattr(settings, "drift_retrain_cooldown_hours", 1)
    _persistent_breach(db, now - timedelta(minutes=1))
    _drift_cycle(db, cycle_id="a" * 64, status="blocked", stage="preflight", created_at=now - timedelta(hours=3),
                 updated_at=now - timedelta(hours=3))  # not evaluated, cooldown (1h) elapsed, created today
    result, fake = _run(db, now)
    assert result["status"] == "capped" and fake.calls == []
    assert "drift_retrain_max_per_day=1" in result["reason"]
    monkeypatch.setattr(settings, "drift_retrain_max_per_day", 2)
    result, fake = _run(db, now)
    assert result["status"] == "scheduled" and len(fake.calls) == 1
    assert fake.calls[0]["lineage"]["attempt"] == 1  # a not-evaluated cycle is not a failed attempt


def test_cycle_creation_waits_for_the_session_window(db):
    window = drift.trigger_window(AFTER_CLOSE)
    assert window["open"] is False
    assert window["next_at"] == (MONDAY_OPEN + drift.OPEN_OFFSET).isoformat()
    before = drift.trigger_window(BEFORE_WINDOW)
    assert before["open"] is False
    assert before["next_at"] == (datetime(2026, 11, 6, 14, 30, tzinfo=UTC) + drift.OPEN_OFFSET).isoformat()
    assert drift.trigger_window(LATE_SESSION)["open"] is False
    assert drift.trigger_window(datetime(2026, 11, 7, 15, 0, tzinfo=UTC))["open"] is False  # Saturday
    assert drift.trigger_window(IN_WINDOW)["open"] is True

    _persistent_breach(db, AFTER_CLOSE - timedelta(minutes=1))
    result, fake = _run(db, AFTER_CLOSE)
    assert result["status"] == "deferred" and fake.calls == []
    assert result["next_window_at"] == window["next_at"]
    rows = _rows(db, "evaluate")
    assert len(rows) == 1 and rows[0].status == "deferred" and rows[0].payload["next_window_at"] == window["next_at"]
    # Monday inside the window the standing breach starts the cycle.
    monday = MONDAY_OPEN + timedelta(minutes=20)
    _persistent_breach(db, monday - timedelta(minutes=1))
    result, fake = _run(db, monday)
    assert result["status"] == "scheduled" and len(fake.calls) == 1
    assert fake.calls[0]["cutoff_at"] == date(2026, 11, 9)


def test_disabled_paused_and_clear_write_no_rows(db, monkeypatch):
    _persistent_breach(db, IN_WINDOW - timedelta(minutes=1))
    monkeypatch.setattr(settings, "drift_retrain_enabled", False)
    result, fake = _run(db, IN_WINDOW)
    assert result["status"] == "disabled" and fake.calls == []
    monkeypatch.setattr(settings, "drift_retrain_enabled", True)
    with patch("app.services.stock_learning_cycle.scheduled_learning_control_projection",
               return_value={"paused": True, "pause_reason": "operator pause"}):
        result, fake = _run(db, IN_WINDOW)
    assert result["status"] == "paused" and result["reason"] == "operator pause" and fake.calls == []
    _breach(db, "model.prediction_distribution", "cleared", count=0, at=IN_WINDOW - timedelta(minutes=1))
    result, fake = _run(db, IN_WINDOW)
    assert result["status"] == "clear" and fake.calls == []
    assert _rows(db) == []


def test_blocked_creation_is_recorded_and_waits_one_cooldown(db):
    _persistent_breach(db, IN_WINDOW - timedelta(minutes=1))
    result, fake = _run(db, IN_WINDOW, _FakeCreate(raise_error="At least one stock symbol is required"))
    assert result["status"] == "blocked" and "could not be created" in result["reason"]
    blocked = _rows(db, "schedule_blocked")
    assert len(blocked) == 1 and blocked[0].payload["reason"] == "At least one stock symbol is required"
    result, fake = _run(db, IN_WINDOW + timedelta(minutes=15))
    assert result["status"] == "cooldown" and fake.calls == []
    assert "cycle creation was blocked" in result["reason"]


# ------------------------------------------------------------------ 4. surfaces

def test_projection_and_route_expose_the_drift_state(db):
    breach = _persistent_breach(db, IN_WINDOW - timedelta(minutes=1))
    _drift_cycle(db, cycle_id="a" * 64, status="running_forward_trial", stage="forward_trial",
                 created_at=IN_WINDOW - timedelta(days=2))
    _run(db, IN_WINDOW)
    projection = drift.drift_retraining_projection(db, now=IN_WINDOW)
    assert projection["enabled"] is True
    assert projection["policy"] == {"cooldown_hours": 48, "max_per_day": 1, "backoff_factor": 2,
                                    "window": {"open_offset_minutes": 15, "close_margin_minutes": 30}}
    assert projection["trigger"]["triggered"] is True
    assert projection["trigger"]["episodes"] == [drift.episode_key(breach)]
    assert projection["open_cycle"]["cycle_id"] == "a" * 64
    assert projection["window"]["open"] is True
    assert projection["last_decision"]["status"] == "waiting"
    assert projection["paper_only"] is True and projection["live_authorized"] is False

    app = FastAPI()
    app.include_router(learning_cycle_api.router)
    app.dependency_overrides[learning_cycle_api.get_db] = lambda: db
    with patch.object(learning_cycle_api, "scheduled_learning_control_projection",
                      return_value={"paused": False, "paper_only": True}), \
            patch.object(drift, "_now", return_value=IN_WINDOW):
        with TestClient(app) as client:
            body = client.get("/stock/learning-cycles/schedule-control").json()
    assert body["paused"] is False
    assert body["drift_retraining"]["open_cycle"]["cycle_id"] == "a" * 64
    assert body["drift_retraining"]["trigger"]["breach_keys"] == ["model.prediction_distribution"]
    assert body["drift_retraining"]["last_decision"]["status"] == "waiting"


def test_job_wiring_and_task_result():
    assert celery_app.conf.task_routes["app.tasks.jobs.drift_retrain_job"] == {"queue": "learning"}
    entry = celery_app.conf.beat_schedule["drift-retrain"]
    assert entry["task"] == "app.tasks.jobs.drift_retrain_job"
    assert entry["schedule"] == 60 * 15
    assert entry["options"] == {"expires": 10 * 60}
    assert "app.tasks.jobs.drift_retrain_job" in celery_app.tasks
    assert jobs.drift_retrain_job.soft_time_limit == 120
    # The calendar retrain stays as the fallback.
    assert "scheduled-stock-challenger-retraining" in celery_app.conf.beat_schedule

    with patch.object(jobs, "_run_job", side_effect=lambda name, work: {"name": name, **work(object())}), \
            patch("app.services.drift_retraining.run_drift_retraining",
                  return_value={"status": "clear", "reason": "none"}) as run:
        result = jobs.drift_retrain_job()
    assert result == {"name": "drift_retrain_job", "job": "drift_retrain_job", "status": "clear", "reason": "none"}
    assert run.call_count == 1
