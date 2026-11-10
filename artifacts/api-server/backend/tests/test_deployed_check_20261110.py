"""Findings from the 2026-11-10 deployed-app check.

* The Overview showed "Paused" while recovery was ``resumable``, which every
  order path treats as tradable.
* The deployment monitor raised a critical "blocked: none. Readiness
  blockers: none." alert when readiness held paper runs only on an
  unexpected warning (Strategy availability).

(The SGOV sparse-feed finding is fixed by #65; see test_book_sparse_feed.py.)
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.db.base import Base
from app.models import Notification, RiskRule, StockPaperAccount, StockPaperRecoveryState
from app.services import deployment_monitor
from app.services.paper_overview import paper_overview

NOW = datetime(2026, 11, 10, 15, 30, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _overview_state(db, status):
    account = StockPaperAccount(
        broker="alpaca_paper", broker_account_id="deployed-check",
        cash=Decimal("9000"), buying_power=Decimal("9000"), equity=Decimal("10000"),
        status="reconciled", costs_known=True, accounting_verified=True,
        reconciliation_required=False, last_reconciled_at=NOW, raw_payload={},
    )
    db.add_all([
        account,
        RiskRule(name="default", value={"kill_switch_enabled": False}, is_active=True),
        StockPaperRecoveryState(id=1, status=status, flatten_policy="none", updated_by="test",
                                pause_reason="stale_heartbeat" if status == "paused" else None),
    ])
    db.commit()
    with patch("app.services.paper_overview.active_paper_account", return_value=account):
        return paper_overview(db, now=NOW)["bot"]


@pytest.mark.parametrize("status", ["armed", "resumable"])
def test_overview_shows_a_tradable_recovery_state_as_running(db, status):
    bot = _overview_state(db, status)
    assert bot["state"] in {"Reconciled", "Mandate active"}


@pytest.mark.parametrize("status", ["paused", "cooldown", "revalidation_required"])
def test_overview_still_shows_real_pauses(db, status):
    bot = _overview_state(db, status)
    assert bot["state"] == "Paused"


def _snapshot(*, blockers=(), readiness_blockers=(), unexpected=()):
    return {
        "deployable": False,
        "blockers": list(blockers),
        "readiness_status": "blocked" if readiness_blockers else "warning",
        "readiness_blockers": list(readiness_blockers),
        "readiness": {"paper_trading_unexpected_warnings": list(unexpected)},
        "checks": [],
    }


def _run(db, snapshot):
    with patch.object(deployment_monitor, "deployment_monitor_snapshot", return_value=snapshot):
        deployment_monitor.run_deployment_monitor(db, source="test")
    return db.scalars(select(Notification)).all()


def test_warning_only_hold_is_a_named_warning_not_a_critical_incident(db):
    [alert] = _run(db, _snapshot(unexpected=["Strategy availability"]))
    assert alert.severity == "warning"
    assert "Strategy availability" in alert.message
    assert alert.title == "Deployment monitor: paper runs held by a warning"


def test_existing_critical_alert_is_downgraded_when_only_a_warning_remains(db):
    [alert] = _run(db, _snapshot(blockers=["Redis"]))
    assert alert.severity == "critical"
    [alert] = _run(db, _snapshot(unexpected=["Strategy availability"]))
    assert alert.severity == "warning"
    assert "Strategy availability" in alert.message


def test_a_real_blocker_stays_critical_and_still_names_the_warning(db):
    [alert] = _run(db, _snapshot(readiness_blockers=["Scheduler health"], unexpected=["Strategy availability"]))
    assert alert.severity == "critical"
    assert alert.title == "Deployment monitor blocked"
    assert "Scheduler health" in alert.message and "Strategy availability" in alert.message

