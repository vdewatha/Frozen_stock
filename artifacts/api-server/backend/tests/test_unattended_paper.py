"""Unattended paper operation: mandate minting, explained-halt auto-resume,
edge-decay re-admission backoff, and external alert dedupe."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.services.stock_recovery as recovery
from app.core.config import settings
from app.core.security import required_role
from app.db.base import Base
from app.models import (
    AuditLog,
    Notification,
    NotificationDelivery,
    RiskRule,
    StockLearningCycle,
    StockPaperModelBinding,
    StockPaperRecoveryEvent,
    StockPaperRecoveryState,
    StockPaperRunApproval,
    StockPaperTrial,
    StockStrategyReadmission,
    Strategy,
)
from app.models.stock_paper import StockPaperAccount, StockPaperOrder, StockPaperPosition, StockPaperStrategyEvidence
from app.services import alert_delivery, paper_mandate, strategy_readmission
from app.services.stock_forward_trial import POLICY
from app.services.stock_learning_cycle import paper_run_approval_projection
from app.services.stock_paper_ledger import active_paper_broker_name

UTC = timezone.utc


def _db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


# --- Standing paper mandate ----------------------------------------------

def _cycle_with_trial(db: Session, *, symbols=("SPY",), policy_overrides=None) -> tuple[StockLearningCycle, StockPaperTrial]:
    cycle_id = "e" * 64
    trial = StockPaperTrial(
        id="00000000-0000-0000-0000-000000000270", binding_id=1, actor="scheduler",
        source_cycle_id=cycle_id, status="approved", policy=dict(POLICY) | (policy_overrides or {}),
        lineage={},
    )
    cycle = StockLearningCycle(
        cycle_id=cycle_id, request_sha256="2" * 64, trigger="scheduled", status="awaiting_preflight",
        stage="preflight", requested_by="scheduler", symbols=list(symbols), cutoff_date=date(2026, 10, 29),
        horizon_days=5, provider="yfinance", seed=42, binding_id=1, trial_id=trial.id,
        gates={}, evidence={}, last_reason="awaiting preflight",
    )
    db.add_all([trial, cycle])
    db.flush()
    return cycle, trial


def _mandate(db: Session, **overrides):
    values = dict(
        actor="admin", symbols=["SPY", "QQQ"],
        exposure_limits={"max_allocated_notional": "20000", "max_risk_per_trade": "0.01"},
        loss_limits={"max_loss": "0.05", "unit": "fraction_of_baseline_equity", "currency": "USD"},
        expires_at=datetime.now(UTC) + timedelta(days=30), max_sessions=60, max_sessions_per_cycle=20,
        reason="standing paper mandate for tests",
    )
    values.update(overrides)
    return paper_mandate.create_paper_mandate(db, **values)


def test_mandate_mints_exact_cycle_approval_that_passes_projection():
    db = _db()
    cycle, trial = _cycle_with_trial(db)
    assert paper_run_approval_projection(db, cycle)["status"] != "pass"
    mandate = _mandate(db)
    approval, reason = paper_mandate.mint_paper_run_approval(db, cycle, trial)
    assert reason is None and approval is not None
    projection = paper_run_approval_projection(db, cycle)
    assert projection["status"] == "pass"
    # The approval authorises the cycle's own bounds, never the mandate's caps.
    assert approval.exposure_limits["max_allocated_notional"] != mandate.exposure_limits["max_allocated_notional"]
    assert f"paper_mandate:{mandate.id}" in approval.approving_actors
    assert paper_mandate.sessions_used(db, mandate.id) == approval.duration_sessions
    # Minting again is idempotent.
    again, _ = paper_mandate.mint_paper_run_approval(db, cycle, trial)
    assert again.id == approval.id
    assert db.query(StockPaperRunApproval).count() == 1
    assert paper_mandate.minted_mandate_for_approval(db, approval.id).id == mandate.id


@pytest.mark.parametrize("overrides,policy,expected", [
    ({"symbols": ["QQQ"]}, {}, "outside the mandate universe"),
    ({"exposure_limits": {"max_allocated_notional": "5000", "max_risk_per_trade": "0.01"}}, {}, "exceeds mandate cap"),
    ({"loss_limits": {"max_loss": "0.01", "unit": "fraction_of_baseline_equity"}}, {}, "exceeds mandate cap"),
    ({"max_sessions_per_cycle": 5, "max_sessions": 60}, {}, "per-cycle limit"),
    ({"max_sessions": 10, "max_sessions_per_cycle": 10}, {}, "per-cycle limit"),
    ({"schedule": {"triggers": ["manual"]}}, {}, "does not cover scheduled"),
    ({"remaining_position_policies": ["flatten"]}, {}, "remaining-position policy"),
])
def test_mandate_mismatch_does_not_mint(overrides, policy, expected):
    db = _db()
    cycle, trial = _cycle_with_trial(db, policy_overrides=policy)
    _mandate(db, **overrides)
    approval, reason = paper_mandate.mint_paper_run_approval(db, cycle, trial)
    assert approval is None
    assert expected in reason
    assert db.query(StockPaperRunApproval).count() == 0


def test_revoked_and_expired_mandates_do_not_mint():
    db = _db()
    cycle, trial = _cycle_with_trial(db)
    mandate = _mandate(db)
    paper_mandate.revoke_paper_mandate(db, mandate.id, actor="admin", reason="stop unattended runs")
    approval, reason = paper_mandate.mint_paper_run_approval(db, cycle, trial)
    assert approval is None and "revoked" in reason.lower()
    # Revocation is idempotent and never rewritten.
    again = paper_mandate.revoke_paper_mandate(db, mandate.id, actor="other", reason="twice")
    assert again.revoked_by == "admin"

    db2 = _db()
    cycle2, trial2 = _cycle_with_trial(db2)
    _mandate(db2)
    later = datetime.now(UTC) + timedelta(days=31)
    approval, reason = paper_mandate.mint_paper_run_approval(db2, cycle2, trial2, now=later)
    assert approval is None and "expired" in reason.lower()
    assert paper_mandate.paper_mandate_projection(db2, now=later)["current"]["status"] == "expired"


def test_mandate_routes_require_admin_writes_and_viewer_reads():
    assert required_role("GET", "/stock/learning-cycles/paper-mandate") == "viewer"
    assert required_role("POST", "/stock/learning-cycles/paper-mandate") == "admin"
    assert required_role("POST", "/stock/learning-cycles/paper-mandate/3/revoke") == "admin"
    assert required_role("GET", "/notifications/delivery-status") == "viewer"


# --- Explained-halt auto-resume ------------------------------------------

def _account(db: Session, now: datetime, *, status="halted", halt_reason=None) -> StockPaperAccount:
    account = StockPaperAccount(
        broker=active_paper_broker_name(), broker_account_id="paper-test", activity_contract="legacy-v1",
        cash=Decimal("10000"), buying_power=Decimal("10000"), equity=Decimal("10000"), status=status,
        costs_known=True, accounting_verified=True, reconciliation_required=False,
        unexplained_residual=False, halt_reason=halt_reason, last_reconciled_at=now - timedelta(minutes=1),
        raw_payload={},
    )
    db.add(account)
    db.add(RiskRule(name="default", value={"kill_switch_enabled": True}, is_active=True))
    db.flush()
    return account


def _paused(db: Session, now: datetime, *, actor: str, payload: dict, reason="pause", paused_minutes_ago=30):
    paused_at = now - timedelta(minutes=paused_minutes_ago)
    db.add(StockPaperRecoveryState(id=1, status="cooldown", flatten_policy="none", updated_by=actor,
                                   cooldown_until=paused_at + timedelta(minutes=15), pause_reason=reason))
    db.add(StockPaperRecoveryEvent(action="pause", status="cooldown", actor=actor, reason=reason,
                                   event_sha256=str(abs(hash((actor, reason)))).ljust(64, "0")[:64],
                                   payload={"prior_status": "armed", "prior_kill_switch": False, **payload},
                                   created_at=paused_at))
    db.flush()


def _resume(db, now):
    with patch.object(recovery, "_fresh_monitoring_is_clear", return_value=(True, "clear")), \
            patch.object(recovery, "_paper_research_policy_ready", return_value=False), \
            patch.object(recovery, "_now", return_value=now):
        return recovery.attempt_explained_halt_auto_resume(db, now=now)


@pytest.mark.parametrize("actor,payload,expected_class", [
    ("stock_watchdog", {"heartbeat_reasons": ["continuous monitor heartbeat is stale"]}, "stale_heartbeat"),
    ("stock_monitor", {"monitor_breach_keys": ["health.worker_scheduler"]}, "stale_heartbeat"),
    ("stock_monitor", {"monitor_breach_keys": ["health.broker", "health.reconciliation"]}, "transient_broker"),
    ("stock_monitor", {"monitor_breach_keys": ["data.freshness_provenance"]}, "feed_gap_repaired"),
])
def test_explained_halt_classes_auto_resume_with_open_positions(actor, payload, expected_class):
    db = _db()
    now = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
    account = _account(db, now, halt_reason="pause")
    _paused(db, now, actor=actor, payload=payload)
    db.add(StockPaperPosition(account_id=account.id, symbol="SPY", quantity=Decimal("3"),
                              average_entry_price=Decimal("500"), observed_at=now, raw_payload={}))
    db.commit()
    result = _resume(db, now)
    assert result["status"] == "resumed", result
    assert result["classification"]["class"] == expected_class
    assert result["evidence"]["flat"] is False
    state = db.get(StockPaperRecoveryState, 1)
    assert state.status == "resumable" and state.updated_by == recovery.AUTO_RESUME_ACTOR
    assert db.get(StockPaperAccount, account.id).status == "reconciled"
    assert db.query(RiskRule).first().value["kill_switch_enabled"] is False
    assert db.scalar(select(AuditLog).where(AuditLog.action == "automatic_resume")) is not None
    preflight = db.scalar(select(StockPaperRecoveryEvent).where(
        StockPaperRecoveryEvent.action == "monitoring_preflight"))
    assert preflight.status == "clear"


def test_drawdown_pause_waits_for_next_ny_session():
    db = _db()
    now = datetime(2026, 10, 30, 19, 0, tzinfo=UTC)  # 3pm New York
    _account(db, now, halt_reason="pause")
    _paused(db, now, actor="stock_monitor", payload={"monitor_breach_keys": ["risk.daily_loss"]})
    db.commit()
    assert _resume(db, now)["status"] == "waiting"
    next_day = now + timedelta(hours=16)
    db.get(StockPaperAccount, 1).last_reconciled_at = next_day - timedelta(minutes=1)
    db.commit()
    assert _resume(db, next_day)["status"] == "resumed"


@pytest.mark.parametrize("actor,payload,account_reason,residual", [
    ("stock_monitor", {"monitor_breach_keys": ["model.realized_performance"]}, "pause", False),
    ("admin", {}, "pause", False),
    ("stock_watchdog", {"heartbeat_reasons": ["continuous monitor heartbeat is missing"]}, "pause", False),
    ("stock_monitor", {"monitor_breach_keys": ["health.broker"]},
     "Order submission outcome is uncertain; read-only broker lookup/reconciliation required", False),
    ("stock_monitor", {"monitor_breach_keys": ["health.broker"]}, "Alpaca paper pagination exceeded safe page limit", False),
    ("stock_monitor", {"monitor_breach_keys": ["health.broker"]}, "pause", True),
])
def test_unexplained_halts_stay_with_operator(actor, payload, account_reason, residual):
    db = _db()
    now = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
    account = _account(db, now, halt_reason=account_reason)
    account.unexplained_residual = residual
    _paused(db, now, actor=actor, payload=payload)
    db.commit()
    result = _resume(db, now)
    assert result["status"] == "needs_operator"
    assert db.get(StockPaperRecoveryState, 1).status == "cooldown"
    assert db.query(RiskRule).first().value["kill_switch_enabled"] is True
    # Repeated runs do not duplicate the outcome or the operator alert.
    _resume(db, now)
    assert db.query(StockPaperRecoveryEvent).filter_by(action="automatic_resume").count() == 1
    assert db.query(Notification).filter_by(source=recovery.AUTO_RESUME_ACTOR).count() == 1


def test_legacy_pause_without_prior_context_fails_closed():
    db = _db()
    now = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
    _account(db, now, halt_reason="pause")
    _paused(db, now, actor="stock_monitor", payload={"monitor_breach_keys": ["health.broker"], "prior_kill_switch": None})
    db.commit()
    assert _resume(db, now)["status"] == "needs_operator"


@pytest.mark.parametrize("mutate,expected", [
    (lambda db, acc, now: setattr(db.get(StockPaperRecoveryState, 1), "cooldown_until", now + timedelta(minutes=5)),
     "cooldown"),
    (lambda db, acc, now: setattr(acc, "reconciliation_required", True), "reconciliation"),
    (lambda db, acc, now: setattr(acc, "last_reconciled_at", now - timedelta(minutes=40)), "reconciliation"),
    (lambda db, acc, now: db.add(StockPaperOrder(
        account_id=acc.id, client_order_id="c1", symbol="SPY", side="buy", order_type="market",
        quantity=Decimal("1"), status="accepted", source="test", raw_payload={})), "In-flight"),
])
def test_explained_halt_waits_for_every_gate(mutate, expected):
    db = _db()
    now = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
    account = _account(db, now, halt_reason="pause")
    _paused(db, now, actor="stock_monitor", payload={"monitor_breach_keys": ["health.broker"]})
    mutate(db, account, now)
    db.commit()
    result = _resume(db, now)
    assert result["status"] == "waiting"
    assert expected.lower() in result["reason"].lower()
    assert db.get(StockPaperRecoveryState, 1).status == "cooldown"


def test_auto_resume_respects_disable_switch():
    db = _db()
    with patch.object(settings, "paper_auto_resume_enabled", False):
        assert recovery.attempt_explained_halt_auto_resume(db)["status"] == "disabled"


# --- Suspended-strategy re-admission --------------------------------------

def _suspended_strategy(db: Session, suspended_at: datetime) -> Strategy:
    strategy = Strategy(name="decayed", strategy_type="rsi2", parameters={}, current_status="paused")
    db.add(strategy)
    db.flush()
    db.add(StockPaperStrategyEvidence(
        strategy_id=strategy.id, evidence_id=f"ev-{strategy.id}", status="suspended",
        verified_drawdown=Decimal("0.01"), observed_at=suspended_at, expires_at=suspended_at + timedelta(days=30),
        provenance={"suspended_at": suspended_at.isoformat(), "suspension_policy": "edge-decay-v1"},
    ))
    db.flush()
    return strategy


def test_readmission_quarantine_backoff_and_retirement():
    db = _db()
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    strategy = _suspended_strategy(db, start)
    db.commit()
    created_cycles = []

    def fake_create(db, **kwargs):
        cycle = StockLearningCycle(
            cycle_id=f"{len(created_cycles):064x}", request_sha256=f"{len(created_cycles) + 100:064x}",
            trigger="scheduled", status="queued", stage="training", requested_by=kwargs["actor"],
            symbols=kwargs["symbols"], cutoff_date=kwargs["cutoff_at"], horizon_days=5, provider="yfinance",
            seed=kwargs["seed"], gates={}, evidence={}, last_reason="queued",
        )
        db.add(cycle)
        db.flush()
        created_cycles.append(cycle)
        return cycle, False

    with patch("app.services.stock_learning_cycle.create_learning_cycle", side_effect=fake_create), \
            patch("app.services.stock_learning_cycle.scheduled_learning_control_projection",
                  return_value={"paused": False}), \
            patch.object(settings, "strategy_readmission_quarantine_hours", 72), \
            patch.object(settings, "strategy_readmission_backoff_factor", 2), \
            patch.object(settings, "strategy_readmission_max_attempts", 2):
        # Inside quarantine nothing is scheduled.
        strategy_readmission.run_strategy_readmission(db, now=start + timedelta(hours=1))
        row = db.scalar(select(StockStrategyReadmission))
        assert row.status == "quarantined" and not created_cycles
        # After quarantine a governed cycle is created.
        now = start + timedelta(hours=73)
        strategy_readmission.run_strategy_readmission(db, now=now)
        assert row.status == "cycle_scheduled" and len(created_cycles) == 1
        # Trained then demoted: failed attempt, backoff = 72h * 2^1.
        created_cycles[0].model_run_id = "a" * 64
        created_cycles[0].status = "demoted"
        db.commit()
        strategy_readmission.run_strategy_readmission(db, now=now)
        assert row.status == "backoff" and row.attempts == 1
        assert strategy_readmission._utc(row.next_attempt_at) == now + timedelta(hours=144)
        # Before backoff expires nothing new happens.
        strategy_readmission.run_strategy_readmission(db, now=now + timedelta(hours=100))
        assert len(created_cycles) == 1
        later = now + timedelta(hours=145)
        strategy_readmission.run_strategy_readmission(db, now=later)
        assert len(created_cycles) == 2
        # A cycle blocked before training is not a strategy failure.
        created_cycles[1].status = "blocked"
        db.commit()
        strategy_readmission.run_strategy_readmission(db, now=later)
        assert row.status == "waiting" and row.attempts == 1
        retry_at = strategy_readmission._utc(row.next_attempt_at)
        # The retry lands inside the next regular NYSE session, not 24h later.
        assert retry_at == strategy_readmission.not_evaluated_retry_at(later)
        assert retry_at.astimezone(ZoneInfo("America/New_York")).strftime("%a %H:%M") == "Mon 09:45"
        strategy_readmission.run_strategy_readmission(db, now=retry_at - timedelta(minutes=1))
        assert len(created_cycles) == 2
        strategy_readmission.run_strategy_readmission(db, now=retry_at)
        assert len(created_cycles) == 3
        created_cycles[2].model_run_id = "b" * 64
        created_cycles[2].status = "complete"
        db.commit()
        strategy_readmission.run_strategy_readmission(db, now=retry_at + timedelta(days=1))
        assert row.status == "retired" and row.attempts == 2
        assert db.get(Strategy, strategy.id).current_status == "retired"
        assert db.query(Notification).filter_by(source="strategy_readmission").count() == 1


def test_promoted_successor_counts_as_readmission():
    db = _db()
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    _suspended_strategy(db, start)
    db.commit()
    strategy_readmission.ensure_readmission_rows(db, now=start)
    row = db.scalar(select(StockStrategyReadmission))
    row.current_cycle_id = "f" * 64
    db.add(StockLearningCycle(
        cycle_id="f" * 64, request_sha256="9" * 64, trigger="scheduled", status="promoted", stage="promotion",
        requested_by="strategy_readmission", symbols=["SPY"], cutoff_date=date(2026, 10, 4), horizon_days=5,
        provider="yfinance", seed=1, model_run_id="c" * 64, gates={}, evidence={}, last_reason="promoted",
    ))
    db.commit()
    with patch("app.services.stock_learning_cycle.scheduled_learning_control_projection",
               return_value={"paused": False}):
        strategy_readmission.run_strategy_readmission(db, now=start + timedelta(days=5))
    assert row.status == "readmitted" and row.successor_cycle_id == "f" * 64


# --- External alert delivery ----------------------------------------------

def _with_webhook():
    from pydantic import SecretStr

    return patch.object(settings, "alert_webhook_url", SecretStr("https://alerts.example.test/hook"))


def test_alerts_dedupe_retry_and_send():
    db = _db()
    now = datetime(2026, 10, 30, 15, tzinfo=UTC)
    with _with_webhook():
        rows = alert_delivery.queue_external_alert(db, severity="critical", title="Paused", message="x",
                                                   dedupe_key="k1", now=now)
        assert len(rows) == 1 and rows[0].channel == "webhook"
        assert alert_delivery.queue_external_alert(db, severity="critical", title="Paused", message="x",
                                                   dedupe_key="k1", now=now + timedelta(minutes=5)) == []
        assert rows[0].suppressed_count == 1
        # Below threshold is not delivered externally.
        assert alert_delivery.queue_external_alert(db, severity="info", title="fyi", message="",
                                                   dedupe_key="k2", now=now) == []
        calls = []

        def flaky(row):
            calls.append(row.id)
            if len(calls) == 1:
                raise RuntimeError("https://secret-url?token=abc")

        first = alert_delivery.deliver_pending_alerts(db, now=now, senders={"webhook": flaky})
        assert first["retrying"] == 1
        row = db.get(NotificationDelivery, rows[0].id)
        assert row.status == "pending" and "secret" not in (row.last_error or "")
        assert alert_delivery.deliver_pending_alerts(db, now=now, senders={"webhook": flaky})["due"] == 0
        second = alert_delivery.deliver_pending_alerts(db, now=now + timedelta(minutes=3), senders={"webhook": flaky})
        assert second["sent"] == 1 and db.get(NotificationDelivery, rows[0].id).status == "sent"
        # Outside the dedupe window a repeat is delivered again.
        again = alert_delivery.queue_external_alert(db, severity="critical", title="Paused", message="x",
                                                    dedupe_key="k1", now=now + timedelta(hours=2))
        assert len(again) == 1


def test_alert_failure_stops_after_max_attempts():
    db = _db()
    now = datetime(2026, 10, 30, 15, tzinfo=UTC)
    with _with_webhook(), patch.object(settings, "alert_max_attempts", 2):
        alert_delivery.queue_external_alert(db, severity="warning", title="t", message="m", dedupe_key="k", now=now)

        def broken(row):
            raise RuntimeError("down")

        alert_delivery.deliver_pending_alerts(db, now=now, senders={"webhook": broken})
        result = alert_delivery.deliver_pending_alerts(db, now=now + timedelta(hours=1), senders={"webhook": broken})
        assert result["failed"] == 1
        assert db.scalar(select(NotificationDelivery)).status == "failed"


def test_notifications_queue_outbox_and_digest_is_once_per_day():
    db = _db()
    now = datetime(2026, 10, 30, 21, tzinfo=UTC)
    from app.services.notifications import create_notification

    create_notification(db, category="c", severity="critical", source="s", title="A", message="m",
                        payload={"dedupe_key": "same"})
    create_notification(db, category="c", severity="critical", source="s", title="A", message="m",
                        payload={"dedupe_key": "same"})
    create_notification(db, category="c", severity="info", source="s", title="B", message="m")
    db.commit()
    rows = db.scalars(select(NotificationDelivery)).all()
    assert len(rows) == 1 and rows[0].status == "unconfigured" and rows[0].suppressed_count == 1
    assert alert_delivery.queue_daily_digest(db, now=now)["status"] == "queued"
    assert alert_delivery.queue_daily_digest(db, now=now + timedelta(hours=2))["status"] == "already_queued"
    status = alert_delivery.alert_delivery_status(db)
    assert status["channels"] == [] and len(status["recent"]) == 2


def _handoff_fixture(db: Session) -> str:
    from app.models import (
        StockDatasetSnapshot, StockModelLifecycleState, StockModelRegistry, StockPaperBindingState,
    )

    snapshot_id, model_id, cycle_id = "c" * 64, "d" * 64, "e" * 64
    db.add(StockDatasetSnapshot(
        snapshot_id=snapshot_id, dataset_sha256="f" * 64, cutoff_date=date(2026, 9, 12),
        universe=["SPY"], provider="yfinance", feature_config_id="features", horizon_days=5,
        artifact_path="/immutable/snapshot", artifact_sha256="a" * 64, metadata_json={"binding_eligible": True},
    ))
    db.add(StockModelRegistry(run_id=model_id, snapshot_id=snapshot_id, manifest_sha256="b" * 64,
                              artifact_path="/immutable/model", training_metadata={}))
    db.add(StockModelLifecycleState(model_run_id=model_id, lifecycle_state="paper_canary",
                                    updated_by="scheduler", reason="canary"))
    binding = StockPaperModelBinding(
        model_run_id=model_id, snapshot_id=snapshot_id, binding_sha256="1" * 64, purpose="scheduled",
        paper_only=True, live_authorized=False, bound_by="scheduler", reason="canary", source_cycle_id=cycle_id,
    )
    db.add(binding)
    db.flush()
    db.add(StockPaperBindingState(id=1, active_binding_id=binding.id, changed_by="scheduler", reason="canary"))
    trial = StockPaperTrial(
        id="00000000-0000-0000-0000-000000000271", binding_id=binding.id, actor="scheduler",
        source_cycle_id=cycle_id, status="approved", policy=dict(POLICY),
        lineage={"model_run_id": model_id, "snapshot_id": snapshot_id},
    )
    db.add(trial)
    db.add(StockLearningCycle(
        cycle_id=cycle_id, request_sha256="2" * 64, trigger="scheduled", status="awaiting_preflight",
        stage="preflight", requested_by="scheduler", symbols=["SPY"], cutoff_date=date(2026, 9, 12),
        horizon_days=5, provider="yfinance", seed=42, snapshot_id=snapshot_id, model_run_id=model_id,
        binding_id=binding.id, trial_id=trial.id, active_binding_id=binding.id, gates={}, evidence={},
        last_reason="awaiting preflight",
    ))
    db.commit()
    return cycle_id


@pytest.mark.parametrize("with_mandate,expected_status", [(True, "running_forward_trial"), (False, "blocked")])
def test_scheduled_handoff_mints_from_mandate_or_blocks_with_reason(with_mandate, expected_status):
    from app.services.stock_learning_cycle import start_scheduled_learning_trial

    db = _db()
    cycle_id = _handoff_fixture(db)
    if with_mandate:
        _mandate(db)
        db.commit()

    def _start(db, trial_id, actor):
        row = db.get(StockPaperTrial, trial_id)
        row.status = "running"
        return row

    ready = {"ready": True, "status": "ready", "regular_session": True, "reason": None,
             "paper_ledger": {"status": "reconciled"}}
    with patch("app.services.stock_learning_cycle.validate_trial_artifact", return_value={}), \
            patch("app.services.stock_learning_cycle.trial_feed_preflight", return_value=ready), \
            patch("app.services.stock_learning_cycle.start_trial", side_effect=_start):
        cycle = start_scheduled_learning_trial(db, cycle_id)
    assert cycle.status == expected_status, cycle.last_reason
    if with_mandate:
        assert cycle.gates["paper_run_approval"]["status"] == "pass"
        assert db.query(StockPaperRunApproval).count() == 1
    else:
        assert "No standing paper mandate is recorded" in cycle.last_reason
        assert db.query(StockPaperRunApproval).count() == 0


def _one_session_cycle(db: Session) -> tuple[StockLearningCycle, StockPaperTrial]:
    from types import SimpleNamespace  # noqa: F401

    from app.services.stock_learning_cycle import ONE_SESSION_SYMBOLS, session_bounds

    session = (datetime.now(UTC) + timedelta(days=3)).date()
    while session_bounds(session) is None:
        session += timedelta(days=1)
    return _cycle_with_trial(db, symbols=ONE_SESSION_SYMBOLS, policy_overrides={
        "regular_sessions": 1,
        "paper_session_date": session.isoformat(),
        "max_order_notional": "1250",
        "max_symbol_notional": "2500",
        "max_aggregate_notional": "7500",
        "max_loss": "0.015",
        "loss_unit": "fraction_of_baseline_equity",
        "stop_authority": "operator_and_system",
        "pending_order_treatment": "cancel",
        "remaining_position_policy": "hold",
    })


def _one_session_mandate(db: Session):
    from app.services.stock_learning_cycle import ONE_SESSION_SYMBOLS

    return _mandate(
        db, symbols=list(ONE_SESSION_SYMBOLS),
        exposure_limits={"max_order_notional": "2000", "max_symbol_notional": "5000",
                         "max_aggregate_notional": "10000"},
        loss_limits={"max_loss": "0.02", "unit": "fraction_of_baseline_equity", "currency": "USD"},
        valid_from=datetime.now(UTC) - timedelta(hours=1),
    )


def test_mandate_minted_one_session_approval_passes_the_order_fence():
    """Minted approvals must hash exactly like operator approvals so the
    order-time fence accepts cycle-owned orders."""
    from types import SimpleNamespace

    from app.services.stock_paper_ledger import _approved_trial_context

    db = _db()
    cycle, trial = _one_session_cycle(db)
    _one_session_mandate(db)
    approval, reason = paper_mandate.mint_paper_run_approval(db, cycle, trial)
    assert reason is None and approval.duration_sessions == 1
    db.commit()
    resolved = _approved_trial_context(db, SimpleNamespace(trial_id=None, trial_lot_id=None), trial_id=trial.id)
    assert resolved is not None and resolved[1].id == approval.id


@pytest.mark.parametrize("withdraw", ["revoke", "expire"])
def test_operator_start_path_honours_mandate_withdrawal(withdraw):
    from app.services.stock_forward_trial import start_trial

    db = _db()
    cycle, trial = _cycle_with_trial(db)
    mandate = _mandate(db)
    approval, _ = paper_mandate.mint_paper_run_approval(db, cycle, trial)
    assert approval is not None
    db.commit()
    if withdraw == "revoke":
        paper_mandate.revoke_paper_mandate(db, mandate.id, actor="admin", reason="withdraw authority")
        row = start_trial(db, trial.id, actor="operator")
    else:
        later = datetime.now(UTC) + timedelta(days=31)
        with patch("app.services.paper_mandate._now", return_value=later):
            row = start_trial(db, trial.id, actor="operator")
    assert row.status == "blocked"
    assert withdraw[:6] in (row.blocked_reason or "").lower()
