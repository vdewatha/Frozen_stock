"""Zero-cost event logs (step 8), the blackout shadow's calendar fallback and the step 7 / backfill scripts."""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.core.config import settings
from app.models import AuditLog
from app.services import event_logs
from app.services import setup_shadow as shadow
from app.services.point_in_time_universe import Membership
from app.tasks.celery_app import celery_app
from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

UTC = timezone.utc
NOW = datetime(2026, 11, 7, 11, 10, tzinfo=UTC)  # Saturday 06:10 ET: Friday 11-06 is the last completed session


def _membership(*tickers, start="2010-01-01", end=None):
    return Membership.from_frame(pd.DataFrame({"ticker": list(tickers), "start": pd.Timestamp(start),
                                               "end": pd.Timestamp(end) if end else pd.NaT}))


def _series(values, start="2026-10-01"):
    return pd.Series(np.asarray(values, dtype=float), index=pd.bdate_range(start, periods=len(values)))


def _prices(closes: dict, dividends: dict | None = None, start="2026-10-01"):
    def load(symbols, first, last, actions=False):
        out = {}
        for symbol in symbols:
            if symbol not in closes:
                continue
            frame = pd.DataFrame({"close": _series(closes[symbol], start)})
            if actions:
                frame["dividends"] = 0.0
                for day, amount in (dividends or {}).get(symbol, {}).items():
                    frame.loc[pd.Timestamp(day), "dividends"] = amount
            out[symbol] = frame
        return out

    return load


# ------------------------------------------------------------ pure helpers

def test_window_return_spans_t_minus_2_to_t_plus_1_and_the_pre_ex_week():
    market = _series(100 + np.arange(20))           # 2026-10-01 .. 10-28 business days
    stock = _series(50 + np.arange(20) * 2.0)
    out = event_logs.window_return(stock, market, "2026-10-10", before=2, after=1)  # Saturday -> Monday 10-12
    assert (out["anchor"], out["start"], out["end"]) == ("2026-10-12", "2026-10-08", "2026-10-13")
    assert out["stock_return"] == pytest.approx(66 / 60 - 1)
    assert out["market_adjusted"] == pytest.approx((66 / 60 - 1) - (108 / 105 - 1))
    pre = event_logs.window_return(stock, market, "2026-10-15", before=6, after=-1)
    assert (pre["start"], pre["end"]) == ("2026-10-07", "2026-10-14")
    assert event_logs.window_return(stock.drop(pd.Timestamp("2026-10-13")), market, "2026-10-12",
                                    before=2, after=1) is None
    assert event_logs.window_return(stock, market, "2026-10-28", before=2, after=1) is None  # no t+1 yet


def test_clustered_mean_correlation_and_effective_count():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    out = event_logs.clustered_mean(values, ["a", "b", "c", "d", "e", "f"])
    iid = np.std(values, ddof=1) / np.sqrt(6)
    assert out["mean"] == pytest.approx(3.5)
    assert out["se"] == pytest.approx(np.sqrt(6 / 5 * np.sum((np.array(values) - 3.5) ** 2)) / 6)
    assert out["iid_t"] == pytest.approx(3.5 / iid)
    # Perfectly shared shocks within a date: correlation 1, one effective event per date.
    assert event_logs.intra_date_correlation([1, 1, 5, 5, 9, 9], list("aabbcc")) == pytest.approx(1.0)
    assert event_logs.intra_date_correlation([1, 2], ["a", "b"]) is None
    assert event_logs.effective_count(list("aaabbc"), 0.0) == 6
    assert event_logs.effective_count(list("aaabbc"), 1.0) == pytest.approx(3)
    assert event_logs.effective_count(list("aaabbc"), -0.3) == 6  # floored at zero


# ------------------------------------------------------- the calendar state

def test_merge_fetch_confirms_drops_reschedules_and_ignores_ranges():
    day = date(2026, 11, 2)
    entry = event_logs.merge_fetch(None, [date(2026, 11, 20)], day, "2026-11-02T11:00:00+00:00")
    assert entry["dates"] == {"2026-11-20": {"first_seen": "2026-11-02", "last_seen": "2026-11-02"}}
    entry = event_logs.merge_fetch(entry, [date(2026, 11, 20)], date(2026, 11, 5), "s2")
    assert entry["dates"]["2026-11-20"] == {"first_seen": "2026-11-02", "last_seen": "2026-11-05"}
    failed = event_logs.merge_fetch(entry, None, date(2026, 11, 6), "s3")
    assert failed["dates"] == entry["dates"] and failed["failed_at"] == "s3" and failed["fetched_at"] == "s2"
    moved = event_logs.merge_fetch(entry, [date(2026, 11, 24)], date(2026, 11, 6), "s4")
    assert list(moved["dates"]) == ["2026-11-24"]                     # the rescheduled date is gone
    ranged = event_logs.merge_fetch(moved, [date(2026, 11, 23), date(2026, 11, 27)], date(2026, 11, 9), "s5")
    assert ranged["dates"] == {} and ranged["range"] == ["2026-11-23", "2026-11-27"]
    passed = event_logs.merge_fetch(moved, [date(2027, 2, 20)], date(2026, 11, 25), "s6")
    assert "2026-11-24" in passed["dates"] and "2027-02-20" in passed["dates"]  # a passed date is kept


def test_refresh_order_puts_new_then_reporting_soon_then_stale():
    now = datetime(2026, 11, 9, 11, 10, tzinfo=UTC)
    state = {
        "AAA": {"fetched_at": "2026-11-08T11:00:00+00:00", "upcoming": ["2026-11-12"], "dates": {}},
        "BBB": {"fetched_at": "2026-10-30T11:00:00+00:00", "upcoming": ["2027-01-20"], "dates": {}},
        "CCC": {"fetched_at": "2026-11-09T11:05:00+00:00", "upcoming": ["2026-11-10"], "dates": {}},
        "DDD": {"fetched_at": "2026-11-07T11:00:00+00:00", "upcoming": ["2027-01-20"], "dates": {}},
    }
    assert event_logs.refresh_order(state, ["AAA", "BBB", "CCC", "DDD", "NEW"], now) == ["NEW", "AAA", "BBB"]


def test_refresh_calendar_stores_one_state_row_within_its_budget(db):  # noqa: F811
    membership = _membership("AAA", "BBB", "CCC")
    calls = []

    def fetch(symbol, now):
        calls.append(symbol)
        return None if symbol == "CCC" else [date(2026, 11, 12)]

    out = event_logs.refresh_calendar(db, now=NOW, membership=membership, fetch=fetch, budget=2)
    assert calls == ["AAA", "BBB"] and out["covered"] == 2 and out["pending"] == 1
    out = event_logs.refresh_calendar(db, now=NOW + timedelta(minutes=5), membership=membership, fetch=fetch, budget=5)
    assert calls[-1] == "CCC" and out["failed"] == 1
    stored = event_logs.stored_calendar(db, now=NOW)
    assert stored == {"AAA": [date(2026, 11, 12)], "BBB": [date(2026, 11, 12)]}
    assert event_logs.stored_calendar(db, now=NOW + timedelta(days=9)) == {}
    rows = db.query(AuditLog).filter(AuditLog.event_type == "event_logs", AuditLog.action == "earnings_calendar").count()
    assert rows == 2


def test_refresh_calendar_fails_safe_without_membership(db, monkeypatch):  # noqa: F811
    def broken(*args, **kwargs):
        raise FileNotFoundError("no membership")

    monkeypatch.setattr("app.services.point_in_time_universe.current_membership", broken)
    assert event_logs.refresh_calendar(db, now=NOW, fetch=lambda s, n: [])["status"] == "unavailable"
    assert event_logs.score_ex_dividends(db, now=NOW)["status"] == "unavailable"


# ----------------------------------------------------------- earnings log

def _seed_calendar(db, symbols_dates, first_seen):  # noqa: F811
    state = {symbol: {"fetched_at": "2026-11-06T11:00:00+00:00", "upcoming": [],
                      "dates": {day: {"first_seen": first_seen.get(symbol, "2026-10-20"),
                                      "last_seen": "2026-11-02"} for day in days}}
             for symbol, days in symbols_dates.items()}
    event_logs._write(db, event_logs.ACTION_CALENDAR, "recorded", "seed", {"symbols": state})


def test_score_earnings_logs_market_adjusted_window_once(db):  # noqa: F811
    # Report Tue 11-03: window close 10-30 -> close 11-04.  Bars 2026-10-01.. (22 business days).
    _seed_calendar(db, {"AAA": ["2026-11-03"], "BBB": ["2026-11-03"], "LATE": ["2026-11-05"],
                        "GONE": ["2026-11-03"], "NEW": ["2026-11-03"]}, {"NEW": "2026-11-02"})
    membership = _membership("AAA", "BBB", "LATE", "NEW")
    spy = 100 + np.arange(30) * 0.1
    closes = {"SPY": spy, "AAA": 50 + np.arange(30) * 0.5, "NEW": np.full(30, 20.0)}
    out = event_logs.score_earnings(db, now=NOW, membership=membership, prices=_prices(closes))
    # AAA scored; NEW scored but first seen after the window opened; BBB waits for prices;
    # LATE (11-05 -> window ends 11-06) is due too but has no bars; GONE was not a member.
    assert out == {"status": "recorded", "logged": 2, "waiting": 2}
    events = {e["symbol"]: e for e in event_logs.logged_events(db, event_logs.ACTION_EARNINGS)}
    aaa = events["AAA"]
    assert (aaa["start"], aaa["end"], aaa["known_ex_ante"]) == ("2026-10-30", "2026-11-04", True)
    i, j = 21, 24  # positions of 10-30 and 11-04
    assert aaa["market_adjusted"] == pytest.approx((closes["AAA"][j] / closes["AAA"][i] - 1)
                                                   - (spy[j] / spy[i] - 1), abs=1e-6)
    assert events["NEW"]["known_ex_ante"] is False
    again = event_logs.score_earnings(db, now=NOW, membership=membership, prices=_prices(closes))
    assert again["logged"] == 0
    later = event_logs.score_earnings(db, now=NOW + timedelta(days=12), membership=membership, prices=_prices(closes))
    assert later["logged"] == 2
    missing = [e for e in event_logs.logged_events(db, event_logs.ACTION_EARNINGS) if e["status"] == "missing_data"]
    assert sorted(e["symbol"] for e in missing) == ["BBB", "LATE"]


def test_earnings_progress_counts_effective_ex_ante_events():
    events = [{"status": "scored", "known_ex_ante": True, "anchor": f"2026-11-{d:02d}", "market_adjusted": 0.01 * k}
              for d in range(2, 7) for k in range(3)]
    events += [{"status": "scored", "known_ex_ante": False, "anchor": "2026-11-02", "market_adjusted": 0.0},
               {"status": "missing_data"}]
    out = event_logs.earnings_progress(events, prior_rho=0.5)
    assert out["scored"] == 16 and out["known_ex_ante"] == 15 and out["missing_data"] == 1
    assert out["rho_source"].startswith("ex-dividend") and out["rho"] == 0.5
    assert out["effective_events"] == pytest.approx(5 * 3 / (1 + 2 * 0.5))
    assert out["target_effective_events"] == 6900 and out["ready_for_test"] is False
    assert out["progress"] == pytest.approx(round(7.5 / 6900, 4))


# ---------------------------------------------------------- ex-dividend log

def test_score_ex_dividends_logs_new_pre_ex_windows_for_members(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(event_logs, "backfill_end", lambda: date(2026, 10, 1))
    membership = _membership("AAA", "BBB")
    closes = {"SPY": 100 + np.arange(26) * 0.2, "AAA": 40 + np.arange(26) * 0.4, "BBB": np.full(26, 10.0),
              "OUT": np.full(26, 5.0)}
    dividends = {"AAA": {"2026-10-20": 0.25}, "BBB": {"2026-10-05": 0.1}, "OUT": {"2026-10-20": 0.1}}
    out = event_logs.score_ex_dividends(db, now=NOW, membership=membership, prices=_prices(closes, dividends))
    assert out["logged"] == 1  # BBB's ex-date has no six sessions before it in the window; OUT is not a member
    event = event_logs.logged_events(db, event_logs.ACTION_EXDIV)[0]
    assert (event["symbol"], event["ex_date"], event["start"], event["end"]) == (
        "AAA", "2026-10-20", "2026-10-12", "2026-10-19")
    assert event["market_adjusted"] == pytest.approx((closes["AAA"][12] / closes["AAA"][7] - 1)
                                                     - (closes["SPY"][12] / closes["SPY"][7] - 1), abs=1e-6)
    assert event_logs.score_ex_dividends(db, now=NOW, membership=membership,
                                         prices=_prices(closes, dividends))["logged"] == 0
    monkeypatch.setattr(event_logs, "backfill_end", lambda: date(2026, 10, 20))  # already in the backfill
    db.query(AuditLog).filter(AuditLog.action == event_logs.ACTION_EXDIV).delete()
    assert event_logs.score_ex_dividends(db, now=NOW, membership=membership,
                                         prices=_prices(closes, dividends))["logged"] == 0


def test_disabled_logs_do_nothing(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "event_logs_enabled", False)
    assert event_logs.run_daily(db, now=NOW) == {"status": "disabled"}
    assert event_logs.run_weekly(db, now=NOW) == {"status": "disabled"}


# --------------------------------------------- the blackout shadow fallback

def test_blackout_shadow_keeps_0_10_20_and_decides_uncached_symbols_from_the_snapshot(db):  # noqa: F811
    from app.services import earnings_calendar

    assert event_logs.blackout_shadow_candidates() == [0, 10, 20]
    earnings_calendar.clear_cache()
    now = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
    state = {"AAPL": {"fetched_at": "2026-11-06T11:10:00+00:00", "upcoming": ["2026-11-25"], "dates": {}},
             "MSFT": {"fetched_at": "2026-10-01T11:10:00+00:00", "upcoming": ["2026-11-10"], "dates": {}}}
    event_logs._write(db, event_logs.ACTION_CALENDAR, "recorded", "seed", {"symbols": state})
    context = shadow.CycleContext(db, now=now, spy=None, vix={"vix": 15.0, "vix_change_pct": 0.0})
    # 11-25 is 13 sessions after 11-06: outside 10, inside 20.
    assert context.earnings_block("AAPL", 10) is False and context.earnings_block("AAPL", 20) is True
    assert context.earnings_block("SPY", 20) is False      # index ETF: no earnings
    assert context.earnings_block("MSFT", 10) is None      # snapshot too old: unknown, as before
    strategies = {"rsi2_pullback": SimpleNamespace(id=1, strategy_type="rsi2_pullback",
                                                   parameters={"earnings_blackout_sessions": 0})}
    decisions = shadow.cycle_decisions([{"symbol": "AAPL", "strategy": "rsi2_pullback", "signal_action": "BUY",
                                         "status": "skipped", "reference_price": "200"}], strategies, context=context)
    assert decisions[0]["templates"]["earnings_blackout"]["candidates"] == {
        "off": True, "blackout10": True, "blackout20": False}
    assert context.summary()["earnings_sources"]["snapshot"] >= 2


# ------------------------------------------------------------- wiring

def test_status_jobs_and_views_are_wired(db):  # noqa: F811
    status = event_logs.event_log_status(db)
    assert status["calendar"] == {"status": "no_data"}
    assert status["earnings"]["target_effective_events"] == 6900 and status["earnings"]["effective_events"] == 0
    assert status["ex_dividend"]["backfill"]["passes"] is False
    assert status["earnings_blackout_shadow"]["candidates"] == [0, 10, 20]
    routes = celery_app.conf.task_routes
    for job in ("event_log_daily_job", "event_log_weekly_job"):
        assert routes[f"app.tasks.jobs.{job}"]["queue"] == "learning_fast"
    schedule = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert {"app.tasks.jobs.event_log_daily_job", "app.tasks.jobs.event_log_weekly_job"} <= schedule
    from app.services.learning_scoreboard import build_learning_scoreboard
    from app.services.yardstick import yardstick_view

    assert yardstick_view(db)["event_logs"]["earnings"]["progress"] == 0
    assert build_learning_scoreboard(db)["yardstick"]["event_logs"]["earnings"]["ready_for_test"] is False


# ------------------------------------------------------------- the scripts

def test_month_end_signal_and_hac_slope_recover_a_planted_effect():
    import run_month_end_flow as flow

    rng = np.random.default_rng(7)
    days = pd.bdate_range("2010-01-01", "2016-12-31")
    spread = rng.normal(0, 0.01, len(days))
    spy = pd.Series(100 * np.cumprod(1 + spread + 0.0002), index=days)
    ief = pd.Series(100 * np.cumprod(1 + np.full(len(days), 0.0002)), index=days)
    frame = flow.signal_frame(spy, ief)
    assert set(frame["from_end"]) == {0, 1, 2}
    row = frame.iloc[-1]
    month_start = spy[spy.index.to_period("M") < pd.Period(row["period"])].iloc[-1]
    assert row["mtd"] == pytest.approx(spy.loc[frame.index[-1]] / month_start - ief.loc[frame.index[-1]]
                                       / ief[ief.index.to_period("M") < pd.Period(row["period"])].iloc[-1])
    # Plant next-day spread = -20 bps per SD of the signal and recover it.
    planted = frame.assign(next_spread_bps=-20 * frame["z"] + rng.normal(0, 5, len(frame)))
    fit = flow.fit(planted)
    assert fit["slope_bps_per_sd"] == pytest.approx(-20, abs=1.5) and fit["t_hac"] < -10
    half = dict(fit, slope_bps_per_sd=-9.0)
    assert flow.verdict(fit, fit)["passes"] is True and flow.verdict(fit, half)["passes"] is False


def test_ex_dividend_backfill_flags_missing_members_and_tests_once():
    import run_ex_dividend_backfill as backfill

    membership = _membership("AAA", "BBB", "GONE", start="2014-01-01")
    days = pd.bdate_range("2014-09-01", "2016-06-30")
    rng = np.random.default_rng(3)
    spy = pd.DataFrame({"close": 100 * np.cumprod(1 + rng.normal(0, 0.01, len(days))), "dividends": 0.0}, index=days)
    frames = {"SPY": spy}
    for symbol in ("AAA", "BBB"):
        frame = pd.DataFrame({"close": 50 * np.cumprod(1 + rng.normal(0, 0.01, len(days))), "dividends": 0.0},
                             index=days)
        frame.loc[frame.index[::63][2:], "dividends"] = 0.3
        frames[symbol] = frame
    out = backfill.run(frames, ["GONE"], ["AAA", "BBB", "GONE"], membership, until=date(2016, 6, 30))
    assert out["events"] > 10 and out["members_without_history"] == 1
    assert out["flags_by_year"]["2015"] == {"members": 3, "without_history": 1, "share_without_history": 0.3333}
    assert out["label"].startswith("upper bound") and out["admission_t"] == 2.5
    assert out["passes"] == (out["mean_bps"] > 0 and out["t_date_clustered"] >= 2.5)
