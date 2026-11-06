"""The three triggers that kept flipping the kill switch (2026-11-03..06).

1. A ``model.*`` breach held every strategy's resume; it now pauses only the
   strategies that trade that model.
2. Broker/reconciliation lag while the market is closed (02:45 ET retrain)
   paused the stock path at ~03:00 ET every night; it now waits for the open.
3. Missing current-session minute bars at 09:31 ET read as a broad outage;
   the first minutes after the open are a warning.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.core.config import Settings, settings
from app.db.base import Base
from app.models import Asset, StockMonitoringBreach, StockMonitoringSnapshot
from app.services import stock_monitoring, stock_recovery
from app.services.entry_protections import MODEL_DEPENDENT_STRATEGIES, load_entry_protections, model_monitoring_block

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
# Thursday 2026-11-05: regular session 09:30-16:00 ET.
SESSION = (datetime(2026, 11, 5, 9, 30, tzinfo=NY).astimezone(UTC), datetime(2026, 11, 5, 16, 0, tzinfo=NY).astimezone(UTC))


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _check(key, status, scope, category=None):
    return {"key": key, "category": category or key.split(".")[0], "metric": key, "status": status,
            "message": status, "value": {}, "threshold": {}, "details": {}, "action_scope": scope}


def test_defaults():
    defaults = Settings()
    assert defaults.paper_monitor_offhours_deferral_enabled is True
    assert defaults.paper_monitor_offhours_resume_minutes_before_open == 30
    assert defaults.paper_monitor_open_feed_grace_minutes == 10
    assert MODEL_DEPENDENT_STRATEGIES == {"model_predictive_long"}


# ------------------------------------------------------------------ 1. model scope

def test_model_breach_no_longer_blocks_the_resume_preflight_but_safety_breaches_still_do(db):
    now = datetime(2026, 11, 6, 16, 29, tzinfo=UTC)
    checks = [_check("model.realized_performance", "breach", "model"),
              _check("model.input_features", "breach", "model"), _check("risk.exposure", "clear", "safety")]
    db.add(StockMonitoringSnapshot(monitor_key="stock_continuous_monitor", status="breach", generated_at=now,
                                   checks=checks, actions=[], source="test"))
    db.commit()
    ok, reason = stock_recovery._fresh_monitoring_is_clear(db, now)
    assert ok, reason

    later = now.replace(minute=30)
    db.add(StockMonitoringSnapshot(monitor_key="stock_continuous_monitor", status="breach", generated_at=later,
                                   checks=checks + [_check("risk.drawdown", "breach", "safety")], actions=[],
                                   source="test"))
    db.commit()
    ok, reason = stock_recovery._fresh_monitoring_is_clear(db, later)
    assert not ok and "risk.drawdown=breach" in reason and "model." not in reason


def test_model_breach_withholds_only_model_dependent_entries(db):
    assert model_monitoring_block([], "model_predictive_long") is None
    assert model_monitoring_block(["model.realized_performance"], "rsi2_pullback") is None
    block = model_monitoring_block(["model.realized_performance"], "model_predictive_long")
    assert block is not None and "model.realized_performance" in block["reason"]

    now = datetime(2026, 11, 6, 16, 0, tzinfo=UTC)
    db.add_all([
        StockMonitoringBreach(breach_key="model.realized_performance", category="model", metric="m",
                              status="persistent", severity="critical", last_observed_at=now, details={}),
        StockMonitoringBreach(breach_key="model.input_features", category="model", metric="m",
                              status="cleared", severity="clear", last_observed_at=now, details={}),
        StockMonitoringBreach(breach_key="health.broker", category="health", metric="m",
                              status="persistent", severity="critical", last_observed_at=now, details={}),
    ])
    db.commit()
    account = SimpleNamespace(id=1, equity=Decimal("100000"), last_equity=Decimal("100000"))
    protections = load_entry_protections(db, account, now=now, rules={}, type_by_id={}, bounds_for=lambda _d: None)
    assert protections.load_error is None
    assert protections.model_breaches == ["model.realized_performance"]
    assert "model monitoring breach" in protections.check("XOM", "model_predictive_long")["reason"]
    assert protections.check("XOM", "rsi2_pullback") is None
    assert protections.summary()["model_monitoring_lock"]["breaches"] == ["model.realized_performance"]


# ------------------------------------------------------------------ 2. off-hours broker lag

def _run_at(db, when, broker_status):
    checks = [_check("health.broker", broker_status, "safety"), _check("health.reconciliation", broker_status, "safety")]
    with patch.object(stock_monitoring, "_now", return_value=when), \
         patch.object(stock_monitoring, "_session_bounds", return_value=SESSION), \
         patch.object(stock_monitoring, "_distribution_drift", return_value=[]), \
         patch.object(stock_monitoring, "_performance_drift", return_value=_check("model.realized_performance", "clear", "model")), \
         patch.object(stock_monitoring, "_freshness_and_provenance", return_value=_check("data.freshness_provenance", "clear", "safety")), \
         patch.object(stock_monitoring, "_execution_divergence", return_value=_check("execution.fill_divergence", "clear", "safety")), \
         patch.object(stock_monitoring, "_stock_risk_metrics", return_value=[]), \
         patch.object(stock_monitoring, "_broker_reconciliation_health", return_value=checks), \
         patch.object(stock_monitoring, "_worker_scheduler_health", return_value=[]), \
         patch.object(stock_monitoring, "_restart_warmup_action", return_value=None), \
         patch.object(stock_monitoring, "_pause_stock_path", return_value={"action": "pause_stock_path"}) as pause:
        result = stock_monitoring.run_stock_monitoring(db)
    return result, pause


def test_overnight_broker_lag_waits_for_the_open_and_still_pauses_if_it_persists(db):
    with patch.object(settings, "paper_monitor_offhours_deferral_enabled", True):
        for minute in (0, 1, 2):  # 03:00-03:02 ET: persistent, but the market is closed
            result, pause = _run_at(db, datetime(2026, 11, 5, 3, minute, tzinfo=NY).astimezone(UTC), "breach")
            pause.assert_not_called()
        deferral = next(action for action in result["actions"] if action["action"] == "offhours_deferral")
        assert sorted(deferral["deferred"]) == ["health.broker", "health.reconciliation"]

        # Still breaching 30 minutes before the open: the path pauses exactly as before.
        _run_at(db, datetime(2026, 11, 5, 8, 59, tzinfo=NY).astimezone(UTC), "breach")
        _result, pause = _run_at(db, datetime(2026, 11, 5, 9, 0, tzinfo=NY).astimezone(UTC), "breach")
        pause.assert_called_once()
        assert sorted(pause.call_args.args[1]) == ["health.broker", "health.reconciliation"]


def test_overnight_lag_that_clears_before_the_open_never_pauses(db):
    with patch.object(settings, "paper_monitor_offhours_deferral_enabled", True):
        for minute in (0, 1, 2):
            _run_at(db, datetime(2026, 11, 5, 3, minute, tzinfo=NY).astimezone(UTC), "breach")
        _result, pause = _run_at(db, datetime(2026, 11, 5, 3, 20, tzinfo=NY).astimezone(UTC), "clear")
        pause.assert_not_called()
        assert db.query(StockMonitoringBreach).filter_by(breach_key="health.broker").one().status == "cleared"


def test_in_session_broker_breach_still_pauses_and_the_switch_can_turn_deferral_off(db):
    with patch.object(settings, "paper_monitor_offhours_deferral_enabled", True):
        _run_at(db, datetime(2026, 11, 5, 11, 0, tzinfo=NY).astimezone(UTC), "breach")
        _result, pause = _run_at(db, datetime(2026, 11, 5, 11, 1, tzinfo=NY).astimezone(UTC), "breach")
        pause.assert_called_once()
    db.query(StockMonitoringBreach).delete()
    db.commit()
    # Disabled (as in the shared test fixture): overnight breaches pause as before.
    _run_at(db, datetime(2026, 11, 5, 3, 0, tzinfo=NY).astimezone(UTC), "breach")
    _result, pause = _run_at(db, datetime(2026, 11, 5, 3, 1, tzinfo=NY).astimezone(UTC), "breach")
    pause.assert_called_once()


# ------------------------------------------------------------------ 3. opening feed grace

def _freshness_at(db, when):
    stale = {"status": "stale", "execution_status": "stale", "entitlement_state": "verified",
             "exchange_timestamp": None}
    with patch.object(settings, "paper_execution_symbols", ["SPY", "QQQ"]), \
         patch.object(stock_monitoring, "feed_status", return_value=stale), \
         patch.object(stock_monitoring, "_session_bounds", return_value=SESSION):
        return stock_monitoring._freshness_and_provenance(db, when)


def test_broad_feed_gap_in_the_first_minutes_after_the_open_is_a_warning(db):
    db.add_all([Asset(symbol="SPY", asset_type="stock", is_active=True),
                Asset(symbol="QQQ", asset_type="stock", is_active=True)])
    db.commit()
    with patch.object(settings, "paper_monitor_open_feed_grace_minutes", 10):
        at_open = _freshness_at(db, datetime(2026, 11, 5, 9, 31, tzinfo=NY).astimezone(UTC))
        assert at_open["status"] == "warning" and at_open["details"]["open_feed_grace"] is True
        assert at_open["value"]["failed_symbols"] == ["QQQ", "SPY"]  # still blocked per symbol
        later = _freshness_at(db, datetime(2026, 11, 5, 9, 41, tzinfo=NY).astimezone(UTC))
        assert later["status"] == "breach" and later["details"]["open_feed_grace"] is False
    assert _freshness_at(db, datetime(2026, 11, 5, 9, 31, tzinfo=NY).astimezone(UTC))["status"] == "breach"
