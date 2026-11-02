"""Earnings blackout: no network; fetches are injected."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.services import earnings_calendar as earnings
from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle

# Monday 2026-11-02, 10:00 ET.
NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_cache():
    earnings.clear_cache()
    yield
    earnings.clear_cache()


def reports_on(*days):
    return lambda symbol: list(days)


def test_report_inside_the_hold_window_blocks_the_entry():
    block = earnings.earnings_blackout("AAPL", NOW, sessions=5, fetch=reports_on(date(2026, 11, 6)))
    assert block["status"] == "skipped" and block["earnings_date"] == "2026-11-06"


def test_report_today_blocks_and_report_after_the_window_does_not():
    assert earnings.earnings_blackout("AAPL", NOW, sessions=5, fetch=reports_on(date(2026, 11, 2)))
    earnings.clear_cache()
    assert earnings.earnings_blackout("AAPL", NOW, sessions=5, fetch=reports_on(date(2026, 11, 20))) is None


def test_window_counts_exchange_sessions_not_calendar_days():
    # Friday 2026-11-20 plus 1 session is Monday 2026-11-23, not Saturday.
    friday = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
    assert earnings.earnings_blackout("AAPL", friday, sessions=1, fetch=reports_on(date(2026, 11, 23)))


def test_past_reports_and_funds_never_block():
    assert earnings.earnings_blackout("AAPL", NOW, sessions=10, fetch=reports_on(date(2026, 10, 30))) is None
    assert earnings.earnings_blackout("SPY", NOW, sessions=10, fetch=reports_on()) is None
    assert earnings.earnings_known("SPY")


def test_unavailable_calendar_does_not_block_and_is_retried_later():
    calls = []

    def broken(symbol):
        calls.append(symbol)
        raise RuntimeError("provider down")

    assert earnings.earnings_blackout("AAPL", NOW, sessions=10, fetch=broken) is None
    assert not earnings.earnings_known("AAPL")
    earnings.earnings_blackout("AAPL", NOW, sessions=10, fetch=broken)
    assert len(calls) == 1  # cached briefly, no retry storm
    later = NOW.replace(hour=16)
    assert earnings.earnings_blackout("AAPL", later, sessions=10, fetch=reports_on(date(2026, 11, 5)))


def test_zero_sessions_disables_without_fetching():
    def never(symbol):
        raise AssertionError("must not fetch")

    assert earnings.earnings_blackout("AAPL", NOW, sessions=0, fetch=never) is None


def test_cycle_skips_buy_inside_blackout_without_reserving(monkeypatch):
    from test_stock_paper_autotrader import _Db, _account, signal_source

    monkeypatch.setattr(settings, "paper_earnings_blackout_sessions", 10)
    # The cycle reads the real clock, so the report is dated from today.
    today = datetime.now(earnings.ET).date()
    monkeypatch.setattr(earnings, "_fetch", reports_on(today))
    db = _Db([SimpleNamespace(id=1, strategy_type="moving_average_crossover")])
    with patch.object(settings, "paper_execution_symbols", ["AAPL"]), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         signal_source(False, return_value={"signal_id": 7, "signal_action": "BUY", "reference_price": "100"}), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order") as reserve:
        result = run_stock_paper_signal_cycle(db)
    reserve.assert_not_called()
    [row] = [item for item in result["results"] if item.get("signal_action") == "BUY"]
    assert row["status"] == "skipped" and "earnings on" in row["reason"]


def test_buy_outcomes_are_recorded_only_when_they_change():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.base import Base
    from app.models import AuditLog
    from app.services.stock_paper_autotrader import BUY_OUTCOME_ACTION, _record_buy_outcomes

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    skipped = {"symbol": "AAPL", "strategy": "macd_momentum", "signal_action": "BUY",
               "mode": "explore", "status": "skipped", "reason": "earnings on 2026-11-04"}
    with Session(engine) as db:
        _record_buy_outcomes(db, [skipped, {"symbol": "SPY", "signal_action": "HOLD"}], NOW)
        _record_buy_outcomes(db, [skipped], NOW)
        _record_buy_outcomes(db, [skipped | {"status": "submitted", "reason": None}], NOW)
        _record_buy_outcomes(db, [], NOW)
        rows = db.query(AuditLog).filter_by(action=BUY_OUTCOME_ACTION).order_by(AuditLog.id).all()
        assert [row.payload["counts"] for row in rows] == [{"skipped": 1}, {"submitted": 1}]
        assert rows[0].payload["outcomes"][0]["reason"] == "earnings on 2026-11-04"
