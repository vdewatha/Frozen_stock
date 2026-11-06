"""A refused monitoring preflight names the checks that block it."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  register all tables
import app.services.stock_recovery as recovery
from app.db.base import Base
from app.models import StockMonitoringSnapshot
from app.models.stock_paper import StockPaperAccount
from app.services.stock_paper_ledger import active_paper_broker_name

NOW = datetime(2026, 11, 6, 15, 50, tzinfo=timezone.utc)


def _db(checks, status):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    db.add(StockPaperAccount(
        broker=active_paper_broker_name(), broker_account_id="paper-test", activity_contract="legacy-v1",
        cash=Decimal("10000"), buying_power=Decimal("10000"), equity=Decimal("10000"), status="reconciled", reconciliation_required=False,
        last_reconciled_at=NOW - timedelta(minutes=1), raw_payload={}))
    db.add(StockMonitoringSnapshot(monitor_key="stock_continuous_monitor", status=status,
                                   generated_at=NOW - timedelta(seconds=30), checks=checks, actions=[], source="test"))
    db.commit()
    return db


def test_the_refusal_names_each_blocking_check_but_not_the_tolerated_ones():
    db = _db([
        {"key": "model.realized_performance", "status": "breach", "details": {"recent_count": 40, "baseline_count": 40}},
        {"key": "execution.fill_divergence", "status": "warning"},
        {"key": "data.freshness_provenance", "status": "warning"},  # tolerated partial feed gap
        {"key": "risk.exposure", "status": "clear"},
    ], "breach")
    clear, reason = recovery._fresh_monitoring_is_clear(db, NOW)
    assert not clear
    # Model breaches pause only the model's strategies (see test_kill_switch_triggers),
    # so they are tolerated here and not named.
    assert reason == "Monitoring status is breach, not clear: execution.fill_divergence=warning"


def test_tolerated_checks_alone_still_pass():
    db = _db([{"key": "data.freshness_provenance", "status": "warning"},
              {"key": "health.broker", "status": "breach"}], "breach")
    assert recovery._fresh_monitoring_is_clear(db, NOW) == (True, "Monitoring evidence is fresh and clear")
