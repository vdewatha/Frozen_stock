"""Findings from the 2026-11-10 deployed-app check.

* The Overview showed "Paused" while recovery was ``resumable``, which every
  order path treats as tradable.
* The deployment monitor raised a critical "blocked: none. Readiness
  blockers: none." alert when readiness held paper runs only on an
  unexpected warning (Strategy availability).
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


# --------------------------------------------------------------------------
# SGOV, the slow book's T-bill ETF, prints on IEX a few times an hour. On
# 2026-11-10 its first print came at 09:51 ET, so its feed stayed
# "incomplete" all session and the tranche was refused.

from datetime import timedelta  # noqa: E402

from app.models import IntradayBar  # noqa: E402
from app.services import stock_paper_ledger, target_weight_book  # noqa: E402
from app.services.intraday_data import MARKET_DATA_PROVIDER  # noqa: E402

SPARSE = {"status": "incomplete", "execution_status": "stale", "entitlement_state": "verified"}


def _bar(db, symbol, minutes_ago, price="100.50"):
    at = NOW - timedelta(minutes=minutes_ago)
    db.add(IntradayBar(symbol=symbol, timeframe="1m", provider=MARKET_DATA_PROVIDER, feed_class="iex",
                       opened_at=at, open=Decimal(price), high=Decimal(price), low=Decimal(price),
                       close=Decimal(price), volume=10, exchange_timestamp=at, ingested_at=NOW,
                       last_verified_at=NOW))
    db.commit()


def test_the_two_sparse_limits_agree():
    assert target_weight_book.SPARSE_TBILL_PRICE_AGE == stock_paper_ledger.SPARSE_TBILL_MAX_MINUTE_BAR_AGE


def test_slow_book_prices_sgov_from_an_older_bar_but_not_voo(db):
    _bar(db, "SGOV", 39)
    _bar(db, "VOO", 39, "620.00")
    assert target_weight_book.latest_price(db, "SGOV", now=NOW) == Decimal("100.50")
    assert target_weight_book.latest_price(db, "VOO", now=NOW) is None


def test_slow_book_still_refuses_a_sgov_bar_older_than_two_hours(db):
    _bar(db, "SGOV", 125)
    assert target_weight_book.latest_price(db, "SGOV", now=NOW) is None


def _accepts(market, symbol="SGOV", source="target_weight_book", minutes_ago=39):
    market = market | {"exchange_timestamp": NOW - timedelta(minutes=minutes_ago)}
    return stock_paper_ledger._sparse_tbill_feed_acceptable(market, symbol, source, NOW)


def test_ledger_accepts_a_sparse_verified_sgov_feed_for_slow_book_orders():
    assert _accepts(SPARSE)
    assert _accepts(SPARSE | {"status": "stale"})


@pytest.mark.parametrize("kwargs", [
    {"symbol": "VOO"},
    {"symbol": "SPY"},
    {"source": "scheduled_paper_signal"},
    {"source": "cost_probe"},
    {"source": None},
    {"minutes_ago": 125},
    {"minutes_ago": -5},
])
def test_ledger_keeps_the_strict_gate_for_everything_else(kwargs):
    assert not _accepts(SPARSE, **kwargs)


@pytest.mark.parametrize("market", [
    SPARSE | {"entitlement_state": "unverified"},
    SPARSE | {"execution_status": "unavailable"},
    SPARSE | {"status": "unavailable"},
    {k: v for k, v in SPARSE.items() if k != "entitlement_state"},
])
def test_ledger_never_accepts_an_unverified_or_unavailable_sgov_feed(market):
    assert not _accepts(market)
