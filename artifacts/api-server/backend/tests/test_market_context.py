"""Real economic data, the announcement blackout and the market gauges. No network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models import EconomicIndicator
from app.services import economic_data, loss_diagnosis, market_events, market_gauges
from app.tasks.celery_app import LEARNING_FAST_QUEUE, celery_app

UTC = timezone.utc
ET = market_events.ET


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture()
def fred_key(monkeypatch):
    monkeypatch.setattr(settings, "fred_api_key", SecretStr("test-key"))


def et(year, month, day, hour=10, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ET).astimezone(UTC)


# --- FRED ------------------------------------------------------------------------------------

def _fred(series: dict, fail: set = frozenset()):
    calls = []

    def fetch(path, params):
        calls.append((path, params))
        series_id = params["series_id"]
        if series_id in fail:
            raise RuntimeError("down")
        return {"observations": [{"date": day, "value": value} for day, value in series.get(series_id, [])]}

    fetch.calls = calls
    return fetch


SERIES = {
    "DGS10": [("2026-11-03", "4.12"), ("2026-11-04", "."), ("2026-11-05", "4.18")],
    "DFF": [("2026-11-05", "3.88")],
    "UNRATE": [("2026-10-01", "4.4")],
    "VIXCLS": [("2026-11-05", "28.18")],
    "CPIAUCSL": [("2025-09-01", "320.0"), ("2026-09-01", "329.6")],
}


def test_fred_import_stores_real_series_and_derives_inflation(db, fred_key):
    economic_data.import_fallback_economic_indicators(db)  # yesterday's placeholders
    result = economic_data.import_fred_economic_indicators(db, today=date(2026, 11, 6), fetch=_fred(SERIES))
    assert result["status"] == "complete" and result["rows_imported"] == 8  # "." is skipped
    inflation = db.query(EconomicIndicator).filter_by(indicator_name="inflation_yoy", source="fred").one()
    assert inflation.observation_date == date(2026, 9, 1) and float(inflation.value) == pytest.approx(3.0)
    summary = economic_data.summarize_macro_context(db)
    # Real rows win over the placeholder rows dated today.
    assert summary["source"] == "fred"
    assert summary["latest"]["treasury_10y"] == {"value": 4.18, "unit": "percent",
                                                 "observation_date": "2026-11-05", "source": "fred"}
    assert summary["volatility_regime"] == "elevated"
    again = economic_data.import_fred_economic_indicators(db, today=date(2026, 11, 6), fetch=_fred(SERIES))
    assert again["rows_imported"] == 0 and again["rows_updated"] == 0


def test_one_failing_series_keeps_the_others(db, fred_key):
    result = economic_data.import_fred_economic_indicators(db, today=date(2026, 11, 6),
                                                           fetch=_fred(SERIES, fail={"UNRATE"}))
    assert result["status"] == "partial" and result["errors"] == {"unemployment_rate": "RuntimeError"}
    assert db.query(EconomicIndicator).filter_by(indicator_name="treasury_10y").count() == 2


def test_without_a_key_the_placeholders_stay_labelled(db, monkeypatch):
    monkeypatch.setattr(settings, "fred_api_key", SecretStr(""))
    assert economic_data.import_economic_indicators(db)["source"] == "mock_macro"
    assert economic_data.summarize_macro_context(db)["source"] == "mock_macro"


def test_with_a_key_nothing_seeds_placeholders(db, fred_key):
    assert economic_data.latest_indicator_map(db) == {}


def test_the_key_goes_only_to_fred():
    import httpx

    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json={"observations": []})

    with patch.object(settings, "fred_api_key", SecretStr("abc")):
        economic_data.fred_get("series/observations", {"series_id": "DGS10"},
                               client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen[0].host == "api.stlouisfed.org" and seen[0].params["api_key"] == "abc"


# --- Announcement calendar -------------------------------------------------------------------

FED_PAGE = """
<h4><a>2027 FOMC Meetings</a></h4>
<div class="fomc-meeting__month col"><strong>January</strong></div><div class="fomc-meeting__date col">26-27</div>
<div class="fomc-meeting__month col"><strong>Apr/May</strong></div><div class="fomc-meeting__date col">30-1*</div>
<div class="fomc-meeting__month col"><strong>June</strong></div><div class="fomc-meeting__date col">8-9*</div>
<h4><a>2026 FOMC Meetings</a></h4>
<div class="fomc-meeting__month col"><strong>December</strong></div><div class="fomc-meeting__date col">8-9</div>
<div class="fomc-meeting__month col"><strong>October</strong></div><div class="fomc-meeting__date col">5 (unscheduled)</div>
"""


def test_the_fed_page_gives_statement_days():
    assert market_events.parse_fomc_calendar(FED_PAGE) == [
        date(2026, 12, 9), date(2027, 1, 27), date(2027, 5, 1), date(2027, 6, 9)]


def test_an_unreadable_fed_page_falls_back_to_the_built_in_schedule(monkeypatch):
    monkeypatch.setattr(settings, "fred_api_key", SecretStr(""))

    def broken():
        raise RuntimeError("page moved")

    calendar = market_events.build_calendar(date(2026, 11, 6), fomc=broken)
    assert calendar["sources"] == {"fomc": "built-in schedule"}
    assert calendar["errors"] == {"fomc": "RuntimeError", "cpi": "FRED_API_KEY not set", "jobs": "FRED_API_KEY not set"}
    assert {"kind": "fomc", "date": "2026-12-09", "time_et": "14:00"} in calendar["events"]


def test_refresh_stores_cpi_and_jobs_dates_from_fred(db):
    def fred(path, params):
        assert path == "release/dates" and params["include_release_dates_with_no_data"] == "true"
        return {"release_dates": [{"date": "2026-11-13"}] if params["release_id"] == 10 else [{"date": "2026-12-04"}]}

    state = market_events.refresh_market_events(db, now=et(2026, 11, 6), fomc=lambda: [date(2026, 12, 9)], fred=fred)
    assert state["status"] == "complete" and state["missing"] == []
    assert [(e["kind"], e["date"], e["time_et"]) for e in state["events"]] == [
        ("cpi", "2026-11-13", "08:30"), ("jobs", "2026-12-04", "08:30"), ("fomc", "2026-12-09", "14:00")]
    assert market_events.cached_events(db) == state["events"]


@pytest.fixture()
def blackout_on(monkeypatch):
    monkeypatch.setattr(settings, "market_event_blackout_enabled", True)


EVENTS = [
    {"kind": "jobs", "date": "2026-11-06", "time_et": "08:30"},   # Friday
    {"kind": "cpi", "date": "2026-11-16", "time_et": "08:30"},    # Monday
    {"kind": "fomc", "date": "2026-12-09", "time_et": "14:00"},   # Wednesday
]


@pytest.mark.parametrize("now, blocked", [
    (et(2026, 11, 5, 15, 30), "jobs report on 2026-11-06"),       # the session before
    (et(2026, 11, 4, 15, 30), None),                               # two sessions before
    (et(2026, 11, 6, 9, 45), None),                                # released at 08:30, before the open
    (et(2026, 11, 13, 11), "CPI inflation report on 2026-11-16"),  # Friday before a Monday release
    (et(2026, 12, 8, 10), "Fed rate decision on 2026-12-09"),
    (et(2026, 12, 9, 13, 59), "Fed rate decision at 14:00 ET today"),
    (et(2026, 12, 9, 14, 29), "no new BUYs until 14:30 ET"),
    (et(2026, 12, 9, 14, 30), None),
])
def test_buys_skip_the_session_before_and_the_fed_morning(blackout_on, now, blocked):
    result = market_events.event_blackout(EVENTS, now, holds_overnight=True)
    if blocked is None:
        assert result is None
    else:
        assert result["status"] == "skipped" and blocked in result["reason"]
        assert market_events.is_blackout_reason(result["reason"])


def test_intraday_holds_only_skip_the_fed_morning(blackout_on):
    assert market_events.event_blackout(EVENTS, et(2026, 11, 5, 15), holds_overnight=False) is None
    assert market_events.event_blackout(EVENTS, et(2026, 12, 9, 11), holds_overnight=False) is not None


def test_the_blackout_can_be_turned_off_and_never_blocks_on_no_calendar(monkeypatch):
    monkeypatch.setattr(settings, "market_event_blackout_enabled", False)
    assert market_events.event_blackout(EVENTS, et(2026, 11, 5, 15), holds_overnight=True) is None
    monkeypatch.setattr(settings, "market_event_blackout_enabled", True)
    assert market_events.event_blackout(None, et(2026, 11, 5, 15), holds_overnight=True) is None


def test_events_inside_a_hold():
    assert market_events.events_between(EVENTS, et(2026, 11, 5, 15), et(2026, 11, 17, 10)) == ["jobs", "cpi"]
    assert market_events.events_between(EVENTS, et(2026, 11, 6, 9), et(2026, 11, 6, 15)) == []
    assert market_events.events_between(None, et(2026, 11, 5), et(2026, 11, 6)) is None


def test_the_cycle_skips_a_buy_on_the_fed_morning(monkeypatch, blackout_on):
    from test_stock_paper_autotrader import _Db, _account, signal_source
    from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle

    # The cycle reads the real clock: a decision late today is always ahead.
    today = datetime.now(ET).date().isoformat()
    events = [{"kind": "fomc", "date": today, "time_et": "23:59"}]
    db = _Db([SimpleNamespace(id=1, strategy_type="moving_average_crossover")])
    with patch.object(settings, "paper_execution_symbols", ["AAPL"]), \
         patch("app.services.stock_paper_autotrader.cached_events", return_value=events), \
         patch("app.services.stock_paper_autotrader.latest_gauges", return_value={}), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=_account()), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         signal_source(False, return_value={"signal_id": 7, "signal_action": "BUY", "reference_price": "100"}), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order") as reserve:
        result = run_stock_paper_signal_cycle(db)
    reserve.assert_not_called()
    [row] = [item for item in result["results"] if item.get("signal_action") == "BUY"]
    assert row["status"] == "skipped" and "Fed rate decision" in row["reason"]
    assert result["market_context"]["calendar_known"] is True


# --- Market gauges ---------------------------------------------------------------------------

def _closes(above: int, below: int, days: int = 260) -> pd.DataFrame:
    index = pd.bdate_range(end="2026-11-06", periods=days)
    rising = np.linspace(50, 150, days)
    data = {f"UP{i}": rising for i in range(above)} | {f"DN{i}": rising[::-1] for i in range(below)}
    data["NEW"] = [np.nan] * (days - 50) + [100.0] * 50  # under 200 days: not counted
    return pd.DataFrame(data, index=index)


def test_breadth_counts_names_above_their_200_day_average():
    breadth = market_gauges.breadth_from_closes(_closes(above=3, below=1))
    assert breadth == {"priced": 4, "above": 3, "pct": 75.0, "as_of": "2026-11-06"}


def _download(frames):
    def download(symbols, period):
        if symbols == ["^VIX"]:
            index = pd.bdate_range(end="2026-11-06", periods=8)
            return pd.DataFrame({("Close", "^VIX"): [16, 16, 17, 16.6, 16.6, 16.61, 28.18, 24.66]}, index=index)
        return pd.concat({"Close": frames}, axis=1)
    return download


def test_refresh_stores_both_gauges(db):
    members = [f"S{i}" for i in range(500)]
    closes = _closes(above=300, below=200)
    result = market_gauges.refresh_market_gauges(db, now=et(2026, 11, 6, 16, 50), members=lambda: members,
                                                 download=_download(closes))
    assert result == {"status": "complete", "stored": {"vix": 24.66, "breadth_above_200d": 60.0}, "errors": {}}
    gauges = market_gauges.latest_gauges(db, et(2026, 11, 9))
    assert gauges["vix"] == {"value": 24.66, "as_of": "2026-11-06", "fresh": True}
    assert market_gauges.latest_gauges(db, et(2026, 11, 20))["vix"]["fresh"] is False


def test_a_short_member_list_publishes_no_breadth(db):
    result = market_gauges.refresh_market_gauges(db, now=et(2026, 11, 6), members=lambda: ["AAPL"],
                                                 download=_download(_closes(1, 0)))
    assert result["status"] == "partial" and "only 1 S&P 500 members" in result["errors"]["breadth_above_200d"]
    assert result["stored"] == {"vix": 24.66}


def test_member_symbols_use_yfinance_spelling():
    text = "{{NyseSymbol|MMM}} {{NasdaqSymbol|AAPL}} {{NyseSymbol|BRK.B}} {{NyseSymbol|MMM}}"
    assert market_gauges.parse_members(text) == ["AAPL", "BRK-B", "MMM"]


def test_gauge_sizing_halves_only_on_fresh_readings():
    calm = {"vix": {"value": 18, "fresh": True}, "breadth_above_200d": {"value": 60, "fresh": True}}
    assert market_gauges.gauge_size_scale(calm) == {"scale": Decimal("1"), "reasons": []}
    fear = {"vix": {"value": 34, "fresh": True}, "breadth_above_200d": {"value": 30, "fresh": True}}
    scaled = market_gauges.gauge_size_scale(fear)
    assert scaled["scale"] == Decimal("0.5") and len(scaled["reasons"]) == 2
    stale = {"vix": {"value": 34, "fresh": False}}
    assert market_gauges.gauge_size_scale(stale)["reasons"] == []


def test_entry_context_records_gauges_and_the_next_event():
    gauges = {"vix": {"value": 24.66, "as_of": "2026-11-06", "fresh": True}}
    context = market_gauges.entry_context(gauges, EVENTS, et(2026, 11, 9))
    assert context["vix"] == 24.66 and context["breadth_above_200d"] is None
    assert context["next_event"]["kind"] == "cpi" and context["calendar_known"] is True


def test_gauge_sizing_is_opt_in_until_backtested():
    assert settings.paper_market_gauge_sizing_enabled is False


# --- Loss diagnosis ---------------------------------------------------------------------------

def test_losses_flag_macro_events_high_vix_and_weak_breadth():
    result = loss_diagnosis.diagnose(gross_return=-0.03, is_loss=True, benchmark_return=0.0,
                                     macro_events_in_hold=["cpi"], entry_vix=26.0, entry_breadth=35.0)
    assert result["cause"] == "signal_wrong"
    assert result["contributing"] == ["macro_event_in_hold", "high_vix_entry", "weak_breadth_entry"]
    calm = loss_diagnosis.diagnose(gross_return=-0.03, is_loss=True, benchmark_return=0.0,
                                   macro_events_in_hold=[], entry_vix=15.0, entry_breadth=65.0)
    assert calm["contributing"] == []


def test_repeated_flags_propose_an_experiment():
    for flag in ("macro_event_in_hold", "high_vix_entry", "weak_breadth_entry"):
        assert "test" in loss_diagnosis.CONTRIBUTING_PROPOSALS[flag].lower()


# --- Scheduling --------------------------------------------------------------------------------

def test_the_new_jobs_run_on_the_fast_lane_and_are_scheduled():
    routes = celery_app.conf.task_routes
    for task in ("daily_economic_data_import", "market_events_job", "market_gauges_job"):
        assert routes[f"app.tasks.jobs.{task}"] == {"queue": LEARNING_FAST_QUEUE}
    beat = celery_app.conf.beat_schedule
    assert beat["market-events"]["task"] == "app.tasks.jobs.market_events_job"
    assert beat["market-gauges"]["task"] == "app.tasks.jobs.market_gauges_job"
