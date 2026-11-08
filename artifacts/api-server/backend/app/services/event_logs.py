"""Zero-cost event logs: the earnings window and the pre-ex-dividend week, for point-in-time S&P 500 members.

Step 8 of the build plan (``reports/Retail bot edge feasibility.md``; the
design is in ``reports/event-logs-20261107.md``).  Neither log trades, gates
or sizes anything; they only collect evidence for one date-clustered test per
log, admitted (if ever) as a zero-turnover tie-breaker at t >= 2.5.

Earnings log (forward, daily)
-----------------------------
1. **Calendar snapshot** (``refresh_calendar``).  The free calendar the entry
   blackout already uses (``earnings_calendar.upcoming_earnings``: yfinance
   ``Ticker.calendar``) is fetched for today's S&P 500 members, at most
   ``event_logs_calendar_fetches_per_run`` a run: never-fetched symbols first,
   then those with a date inside the next ``CONFIRM_DAYS`` days (re-confirmed
   every run), then the stalest.  A single date is a confirmed report date; two
   dates are Yahoo's estimated range and create no event.  A future date that
   a later fetch no longer lists is dropped (rescheduled).  Each date keeps the
   day it was first and last seen.  The whole state is one ``event_logs`` /
   ``earnings_calendar`` audit row per run (latest row = current state).  A
   failed fetch keeps the symbol's previous dates and is retried next run.
2. **Score matured reports** (``score_earnings``).  For a confirmed date ``D``
   of a symbol that was a member on ``D``: the anchor is the first session on or
   after ``D``; the window runs from the close two sessions before the anchor
   to the close one session after it, so a report before the open or after the
   close both fall inside.  Market-adjusted = stock return minus SPY return
   over the same closes (yfinance adjusted closes).  ``known_ex_ante`` is true
   when the date was first seen on or before the window's start session, which
   is what a trade entered at that close could have known.  Missing prices are
   retried for ``GIVE_UP_DAYS`` days, then recorded as ``missing_data``.
   Scored events are ``event_logs`` / ``earnings_events`` rows.
3. **Progress** (``earnings_progress``).  The test waits for about 6,900
   effective events (``EARNINGS_TARGET_EFFECTIVE``: t = 2.5 for a 15 bps
   premium at a 5% event volatility).  Effective events discount same-date
   clustering: ``sum_d n_d / (1 + (n_d - 1) * rho)`` over event dates, with
   ``rho`` the intra-date correlation of the log's own market-adjusted returns
   once ``MIN_RHO_DATES`` dates hold two or more events, before that the
   ex-dividend backfill's estimate.  Only ``known_ex_ante`` events count.

Ex-dividend log (forward, weekly; backfilled 2015-2026 offline)
---------------------------------------------------------------
``score_ex_dividends`` downloads the last ``EXDIV_LOOKBACK_DAYS`` of adjusted
closes and dividends for today's members plus SPY in batches, and logs each new
ex-date ``e`` of a symbol that was a member on ``e``: the market-adjusted return
from the close six sessions before ``e`` to the close one session before it
(five sessions, all cum-dividend, so the price ratio is the same on adjusted
and raw closes).  Forward rows start the day after the backfill's last ex-date
(``backfill_end``), so no event is counted twice.  Rows are ``event_logs`` / ``ex_dividend_events``.  The
backfill (``scripts/run_ex_dividend_backfill.py``) ran the one date-clustered
test; its stored summary is ``event_log_backfill.EX_DIVIDEND_BACKFILL``.

Fail safe: no membership file, a provider error or missing bars write a
status, never raise into the caller and never touch orders.  Paper only.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
import math
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)
UTC = timezone.utc
ET = ZoneInfo("America/New_York")

EVENT = "event_logs"
ACTION_CALENDAR = "earnings_calendar"
ACTION_EARNINGS = "earnings_events"
ACTION_EXDIV = "ex_dividend_events"
MARKET = "SPY"

EARNINGS_BEFORE_SESSIONS = 2
EARNINGS_AFTER_SESSIONS = 1
EXDIV_BEFORE_SESSIONS = 6
EXDIV_END_SESSIONS = -1  # the close of the session before the ex-date
EARNINGS_TARGET_EFFECTIVE = 6900
ADMISSION_T = 2.5
CONFIRM_DAYS = 7
STALE_AFTER = timedelta(days=6)
GIVE_UP_DAYS = 10
KEEP_PAST_DAYS = 60
EXDIV_LOOKBACK_DAYS = 70
MIN_RHO_DATES = 50
SHADOW_MAX_AGE = timedelta(days=8)
DOWNLOAD_CHUNK = 100
RULE = ("Zero-cost logs only: one date-clustered test per log, admitted only as a zero-turnover tie-breaker "
        f"if t >= {ADMISSION_T}; the earnings test waits for {EARNINGS_TARGET_EFFECTIVE:,} effective events.")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# ------------------------------------------------------------ pure helpers

def window_return(stock: pd.Series, market: pd.Series, anchor, *, before: int, after: int) -> Optional[dict]:
    """Market-adjusted close-to-close return around ``anchor`` on the market's sessions.

    The anchor session is the first market session on or after ``anchor``; the
    window starts ``before`` sessions earlier and ends ``after`` sessions after
    it (negative = before it).  ``None`` when a close is missing.
    """
    if stock is None or market is None or len(market) == 0:
        return None
    sessions = pd.DatetimeIndex(market.index)
    k = int(sessions.searchsorted(pd.Timestamp(anchor)))
    if k >= len(sessions):
        return None
    i, j = k - before, k + after
    if i < 0 or j >= len(sessions) or j <= i:
        return None
    start, end = sessions[i], sessions[j]
    try:
        s0, s1, m0, m1 = (float(stock.loc[start]), float(stock.loc[end]), float(market.loc[start]),
                          float(market.loc[end]))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) and v > 0 for v in (s0, s1, m0, m1)):
        return None
    stock_return, market_return = s1 / s0 - 1.0, m1 / m0 - 1.0
    return {"anchor": sessions[k].date().isoformat(), "start": start.date().isoformat(),
            "end": end.date().isoformat(), "stock_return": stock_return, "market_return": market_return,
            "market_adjusted": stock_return - market_return}


def clustered_mean(values: Iterable[float], clusters: Iterable) -> dict:
    """Mean with a cluster-robust standard error (CR1: G/(G-1) correction) and its t."""
    x = np.asarray(list(values), dtype=float)
    groups = np.asarray(list(clusters))
    n = len(x)
    if n < 2:
        return {"n": n, "clusters": n, "mean": float(x.mean()) if n else None, "se": None, "t": None}
    mean = float(x.mean())
    sums = pd.Series(x - mean).groupby(groups).sum().to_numpy()
    g = len(sums)
    if g < 2:
        return {"n": n, "clusters": g, "mean": mean, "se": None, "t": None}
    se = math.sqrt((g / (g - 1)) * float((sums ** 2).sum())) / n
    iid_se = float(x.std(ddof=1)) / math.sqrt(n)
    return {"n": n, "clusters": g, "mean": mean, "se": se, "t": mean / se if se > 0 else None,
            "iid_t": mean / iid_se if iid_se > 0 else None}


def intra_date_correlation(values: Iterable[float], groups: Iterable) -> Optional[float]:
    """One-way ANOVA intra-class correlation of ``values`` within ``groups`` (None without two multi-event groups)."""
    frame = pd.DataFrame({"x": list(values), "g": list(groups)}).dropna()
    sizes = frame.groupby("g")["x"].size()
    if int((sizes >= 2).sum()) < 2:
        return None
    n, k = len(frame), len(sizes)
    grand = frame["x"].mean()
    means = frame.groupby("g")["x"].transform("mean")
    msb = float((frame.groupby("g")["x"].mean().sub(grand) ** 2 * sizes).sum()) / (k - 1)
    msw = float(((frame["x"] - means) ** 2).sum()) / (n - k) if n > k else 0.0
    n0 = (n - float((sizes ** 2).sum()) / n) / (k - 1)
    denominator = msb + (n0 - 1.0) * msw
    return float((msb - msw) / denominator) if denominator > 0 else None


def effective_count(groups: Iterable, rho: float) -> float:
    """``sum_d n_d / (1 + (n_d - 1) rho)``: the event count after same-date clustering."""
    rho = max(float(rho or 0.0), 0.0)
    sizes = pd.Series(list(groups)).value_counts()
    return float(sum(n / (1.0 + (n - 1.0) * rho) for n in sizes))


# --------------------------------------------------------------- sessions

def _is_session(day: date) -> bool:
    from app.services.exchange_sessions import is_nyse_session

    return is_nyse_session(day)


def session_on_or_after(day: date) -> date:
    for _ in range(14):
        if _is_session(day):
            return day
        day += timedelta(days=1)
    return day


def shift_sessions(day: date, count: int) -> date:
    step = 1 if count > 0 else -1
    moved = 0
    while moved < abs(count):
        day += timedelta(days=step)
        if _is_session(day):
            moved += 1
    return day


def last_completed_session(now: datetime) -> date:
    from app.services.gate_cost import last_completed_session as completed

    return completed(_utc(now))


# --------------------------------------------------------------- storage

def _rows(db: Session, action: str):
    from app.models import AuditLog

    return db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == action).order_by(AuditLog.id)


def _latest(db: Session, action: str) -> Optional[dict]:
    from app.models import AuditLog

    row = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == action).order_by(
        AuditLog.id.desc()).first()
    return dict(row.payload or {}) if row is not None else None


def logged_events(db: Session, action: str) -> list[dict]:
    out = []
    for row in _rows(db, action).all():
        out.extend((row.payload or {}).get("events") or [])
    return out


def _write(db: Session, action: str, status: str, message: str, payload: dict) -> None:
    from app.services.audit import write_audit_log

    write_audit_log(db, event_type=EVENT, entity_type="event_log", action=action, status=status, message=message,
                    payload={**payload, "paper_only": True})
    db.commit()


def _members(now: datetime, membership=None) -> tuple[Any, frozenset[str]]:
    from app.services import point_in_time_universe as pit

    membership = membership or pit.current_membership(download=True, now=_utc(now))
    return membership, membership.members_on(now.astimezone(ET).date())


# -------------------------------------------------------- the calendar state

def merge_fetch(entry: Optional[Mapping], fetched: Optional[list], fetched_on: date, stamp: str) -> dict:
    """One symbol's calendar state after a fetch (``None`` = the provider failed)."""
    entry = {"dates": {}, **dict(entry or {})}
    dates = {key: dict(value) for key, value in (entry.get("dates") or {}).items()}
    if fetched is None:
        entry.update({"dates": dates, "failed_at": stamp})
        return entry
    upcoming = sorted({_day(day) for day in fetched})
    confirmed = upcoming if len(upcoming) == 1 else []
    for key in list(dates):
        day = _day(key)
        if day > fetched_on and day not in confirmed:
            dates.pop(key)  # rescheduled, or back to an estimated range
        elif day < fetched_on - timedelta(days=KEEP_PAST_DAYS):
            dates.pop(key)
    for day in confirmed:
        key = day.isoformat()
        seen = dates.get(key) or {"first_seen": fetched_on.isoformat()}
        seen["last_seen"] = fetched_on.isoformat()
        dates[key] = seen
    entry.update({"dates": dates, "upcoming": [day.isoformat() for day in upcoming],
                  "range": [upcoming[0].isoformat(), upcoming[-1].isoformat()] if len(upcoming) > 1 else None,
                  "fetched_at": stamp})
    entry.pop("failed_at", None)
    return entry


def refresh_order(state: Mapping[str, Mapping], members: Iterable[str], now: datetime) -> list[str]:
    """Members to fetch, most urgent first: never fetched, reporting within CONFIRM_DAYS, then stalest."""
    today = now.astimezone(ET).date()
    soon = today + timedelta(days=CONFIRM_DAYS)
    ranked = []
    for symbol in sorted(set(members)):
        entry = state.get(symbol) or {}
        fetched = entry.get("fetched_at")
        if not fetched:
            ranked.append((0, "", symbol))
            continue
        fetched_at = _utc(datetime.fromisoformat(fetched))
        upcoming = [_day(day) for day in entry.get("upcoming") or []] + [_day(d) for d in entry.get("dates") or {}]
        if fetched_at.astimezone(ET).date() < today and any(today <= day <= soon for day in upcoming):
            ranked.append((1, fetched, symbol))
        elif _utc(now) - fetched_at >= STALE_AFTER:
            ranked.append((2, fetched, symbol))
    return [symbol for _, _, symbol in sorted(ranked)]


def _calendar_fetch(symbol: str, now: datetime) -> Optional[list]:
    from app.services.earnings_calendar import upcoming_earnings

    return upcoming_earnings(symbol, now)


def refresh_calendar(db: Session, *, now: Optional[datetime] = None, membership=None,
                     fetch: Optional[Callable[[str, datetime], Optional[list]]] = None,
                     budget: Optional[int] = None) -> dict:
    """Fetch up to ``budget`` member calendars and store the merged state (one audit row)."""
    now = _utc(now or datetime.now(UTC))
    budget = settings.event_logs_calendar_fetches_per_run if budget is None else int(budget)
    try:
        membership, members = _members(now, membership)
    except Exception as exc:  # no membership file: nothing to refresh
        return {"status": "unavailable", "reason": f"membership unavailable: {exc.__class__.__name__}"}
    previous = _latest(db, ACTION_CALENDAR) or {}
    state = {symbol: dict(entry) for symbol, entry in (previous.get("symbols") or {}).items()}
    order = refresh_order(state, members, now)
    fetched_on, stamp = now.astimezone(ET).date(), now.isoformat()
    fetched = failed = 0
    for symbol in order[:max(budget, 0)]:
        try:
            dates = (fetch or _calendar_fetch)(symbol, now)
        except Exception as exc:  # one symbol must not stop the run
            logger.info("event logs: calendar fetch failed for %s: %s", symbol, exc.__class__.__name__)
            dates = None
        state[symbol] = merge_fetch(state.get(symbol), dates, fetched_on, stamp)
        fetched += 1
        failed += dates is None
    for symbol in list(state):  # drop former members once their dates have passed
        if symbol not in members and not any(_day(day) >= fetched_on - timedelta(days=KEEP_PAST_DAYS)
                                             for day in state[symbol].get("dates") or {}):
            state.pop(symbol)
    covered = sum(1 for symbol in members if (state.get(symbol) or {}).get("fetched_at"))
    payload = {"updated_at": stamp, "members": len(members), "covered": covered, "fetched": fetched,
               "failed": failed, "pending": max(len(order) - fetched, 0), "symbols": state}
    status = "recorded" if fetched or not previous else "unchanged"
    if status == "recorded":  # a run that fetched nothing leaves the latest row as the state
        _write(db, ACTION_CALENDAR, status, f"earnings calendar: {fetched} fetched ({failed} failed), "
                                            f"{covered}/{len(members)} members covered", payload)
    return {key: value for key, value in payload.items() if key != "symbols"} | {"status": status}


def stored_calendar(db: Session, *, now: Optional[datetime] = None, max_age: timedelta = SHADOW_MAX_AGE
                    ) -> dict[str, list[date]]:
    """``symbol -> upcoming dates`` from the latest snapshot, for calendars fetched within ``max_age``."""
    now = _utc(now or datetime.now(UTC))
    latest = _latest(db, ACTION_CALENDAR) or {}
    out = {}
    for symbol, entry in (latest.get("symbols") or {}).items():
        fetched = entry.get("fetched_at")
        if not fetched or now - _utc(datetime.fromisoformat(fetched)) > max_age:
            continue
        out[symbol] = sorted({_day(day) for day in entry.get("upcoming") or []} |
                             {_day(day) for day in entry.get("dates") or {}})
    return out


# ------------------------------------------------------------ prices

def yahoo_symbol(symbol: str) -> str:
    return str(symbol).strip().upper().replace(".", "-")


def download_prices(symbols: Iterable[str], start: date, end: date, *, actions: bool = False
                    ) -> dict[str, pd.DataFrame]:
    """Adjusted closes (and dividends with ``actions``) per symbol from yfinance, in batches; missing symbols absent."""
    import yfinance as yf

    symbols = sorted({str(symbol).upper() for symbol in symbols})
    out: dict[str, pd.DataFrame] = {}
    for offset in range(0, len(symbols), DOWNLOAD_CHUNK):
        chunk = symbols[offset:offset + DOWNLOAD_CHUNK]
        tickers = [yahoo_symbol(symbol) for symbol in chunk]
        try:
            frame = yf.download(tickers, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                auto_adjust=True, actions=actions, group_by="ticker", threads=2, progress=False)
        except Exception as exc:  # a failed batch leaves its symbols missing
            logger.info("event logs: price batch failed: %s", exc.__class__.__name__)
            continue
        if frame is None or frame.empty:
            continue
        for symbol, ticker in zip(chunk, tickers):
            try:
                sub = frame[ticker] if isinstance(frame.columns, pd.MultiIndex) else frame
            except KeyError:
                continue
            sub = sub.rename(columns=str.lower)
            if "close" not in sub:
                continue
            sub = sub.dropna(subset=["close"])
            if sub.empty:
                continue
            sub.index = pd.DatetimeIndex(pd.to_datetime(sub.index)).tz_localize(None).normalize()
            keep = ["close"] + (["dividends"] if actions and "dividends" in sub else [])
            out[symbol] = sub[keep].astype(float)
    return out


def _close(frames: Mapping[str, pd.DataFrame], symbol: str) -> Optional[pd.Series]:
    frame = frames.get(symbol)
    if frame is None or "close" not in frame:
        return None
    return frame["close"]


# ----------------------------------------------------------- earnings log

def _event_key(symbol: str, day: str) -> str:
    return f"{symbol.upper()}|{day}"


def due_earnings(state: Mapping[str, Mapping], *, now: datetime, done: set[str], membership=None
                 ) -> list[dict]:
    """Confirmed report dates whose window has closed and that are not yet logged."""
    completed = last_completed_session(now)
    out = []
    for symbol, entry in sorted(state.items()):
        for key, seen in sorted((entry.get("dates") or {}).items()):
            if _event_key(symbol, key) in done:
                continue
            report = _day(key)
            anchor = session_on_or_after(report)
            end = shift_sessions(anchor, EARNINGS_AFTER_SESSIONS)
            if end > completed:
                continue
            if membership is not None and symbol not in membership.members_on(report):
                continue
            out.append({"symbol": symbol, "report_date": key, "first_seen": seen.get("first_seen"),
                        "last_seen": seen.get("last_seen"), "window_start": shift_sessions(
                            anchor, -EARNINGS_BEFORE_SESSIONS).isoformat(), "window_end": end.isoformat()})
    return out


def score_earnings(db: Session, *, now: Optional[datetime] = None, membership=None,
                   prices: Optional[Callable[[list[str], date, date], dict]] = None) -> dict:
    """Log every matured confirmed report's market-adjusted t-2..t+1 return (one audit row per run with events)."""
    now = _utc(now or datetime.now(UTC))
    state = (_latest(db, ACTION_CALENDAR) or {}).get("symbols") or {}
    if not state:
        return {"status": "no_calendar", "logged": 0}
    if membership is None:
        try:
            membership, _ = _members(now)
        except Exception as exc:
            return {"status": "unavailable", "logged": 0, "reason": f"membership unavailable: {exc.__class__.__name__}"}
    done = {_event_key(e["symbol"], e["report_date"]) for e in logged_events(db, ACTION_EARNINGS)}
    due = due_earnings(state, now=now, done=done, membership=membership)
    if not due:
        return {"status": "nothing_due", "logged": 0}
    start = min(_day(event["window_start"]) for event in due) - timedelta(days=7)
    end = max(_day(event["window_end"]) for event in due)
    try:
        frames = (prices or download_prices)(sorted({event["symbol"] for event in due} | {MARKET}), start, end)
    except Exception as exc:
        return {"status": "unavailable", "logged": 0, "reason": f"prices unavailable: {exc.__class__.__name__}"}
    market = _close(frames, MARKET)
    today = now.astimezone(ET).date()
    events, waiting = [], 0
    for event in due:
        stock = _close(frames, event["symbol"])
        result = window_return(stock, market, event["report_date"], before=EARNINGS_BEFORE_SESSIONS,
                               after=EARNINGS_AFTER_SESSIONS) if market is not None else None
        if result is None:
            if (today - _day(event["report_date"])).days <= GIVE_UP_DAYS:
                waiting += 1
                continue
            events.append({**event, "status": "missing_data", "known_ex_ante": None})
            continue
        ex_ante = bool(event.get("first_seen")) and _day(event["first_seen"]) <= _day(result["start"])
        events.append({**event, **{k: v for k, v in result.items() if k in ("anchor", "start", "end")},
                       "stock_return": round(result["stock_return"], 6), "spy_return": round(result["market_return"], 6),
                       "market_adjusted": round(result["market_adjusted"], 6), "known_ex_ante": ex_ante,
                       "status": "scored"})
    if events:
        scored = sum(1 for event in events if event["status"] == "scored")
        _write(db, ACTION_EARNINGS, "recorded", f"earnings log: {scored} scored, {len(events) - scored} missing data",
               {"observed_at": now.isoformat(), "events": events})
    return {"status": "recorded" if events else "waiting_for_prices", "logged": len(events), "waiting": waiting}


def earnings_progress(events: Iterable[Mapping], *, prior_rho: Optional[float] = None) -> dict:
    """Raw and effective (date-clustering discounted) ex-ante events against the 6,900 target."""
    scored = [e for e in events if e.get("status") == "scored"]
    usable = [e for e in scored if e.get("known_ex_ante")]
    dates = [e["anchor"] for e in usable]
    sizes = pd.Series(dates).value_counts() if dates else pd.Series(dtype=int)
    multi = int((sizes >= 2).sum())
    own = intra_date_correlation([e["market_adjusted"] for e in usable], dates) if usable else None
    if prior_rho is None:
        from app.services.event_log_backfill import EX_DIVIDEND_BACKFILL

        prior_rho = EX_DIVIDEND_BACKFILL.get("intra_date_correlation")
    rho_source = "earnings log" if own is not None and multi >= MIN_RHO_DATES else "ex-dividend backfill (prior)"
    rho = max(own if rho_source == "earnings log" else float(prior_rho or 0.0), 0.0)
    effective = effective_count(dates, rho) if dates else 0.0
    return {
        "scored": len(scored), "known_ex_ante": len(usable), "missing_data": sum(
            1 for e in events if e.get("status") == "missing_data"),
        "event_dates": int(len(sizes)), "multi_event_dates": multi, "rho": round(rho, 4), "rho_source": rho_source,
        "rho_own": None if own is None else round(own, 4), "effective_events": round(effective, 1),
        "target_effective_events": EARNINGS_TARGET_EFFECTIVE,
        "progress": round(min(effective / EARNINGS_TARGET_EFFECTIVE, 1.0), 4),
        "ready_for_test": effective >= EARNINGS_TARGET_EFFECTIVE,
        "first": min(dates) if dates else None, "last": max(dates) if dates else None,
        "test": f"one date-clustered t on market_adjusted once ready; admitted only as a tie-breaker if t >= {ADMISSION_T}",
    }


# ---------------------------------------------------------- ex-dividend log

def ex_dividend_events(symbol: str, frame: pd.DataFrame, market: pd.Series, *, membership=None,
                       since: Optional[date] = None, until: Optional[date] = None) -> list[dict]:
    """Every ex-date of ``symbol`` in ``frame`` (``dividends`` > 0) with its pre-ex five-session window."""
    if frame is None or "dividends" not in frame or market is None:
        return []
    out = []
    for stamp, amount in frame["dividends"].items():
        if not (amount and float(amount) > 0):
            continue
        ex = pd.Timestamp(stamp).date()
        if (since and ex < since) or (until and ex > until):
            continue
        if membership is not None and symbol not in membership.members_on(ex):
            continue
        result = window_return(frame["close"], market, ex, before=EXDIV_BEFORE_SESSIONS, after=EXDIV_END_SESSIONS)
        if result is None:
            continue
        out.append({"symbol": symbol, "ex_date": ex.isoformat(), "dividend": round(float(amount), 6),
                    "start": result["start"], "end": result["end"],
                    "stock_return": round(result["stock_return"], 6), "spy_return": round(result["market_return"], 6),
                    "market_adjusted": round(result["market_adjusted"], 6)})
    return out


def backfill_end() -> date:
    """The last ex-date the stored backfill covers (forward rows start the day after)."""
    from app.services.event_log_backfill import EX_DIVIDEND_BACKFILL

    try:
        return date.fromisoformat(EX_DIVIDEND_BACKFILL["sample"]["last"])
    except (KeyError, TypeError, ValueError):
        return date(2014, 12, 31)


def score_ex_dividends(db: Session, *, now: Optional[datetime] = None, membership=None,
                       prices: Optional[Callable[..., dict]] = None) -> dict:
    """Log the new ex-dates of today's members from the last EXDIV_LOOKBACK_DAYS (one audit row per run)."""
    now = _utc(now or datetime.now(UTC))
    try:
        membership, members = _members(now, membership)
    except Exception as exc:
        return {"status": "unavailable", "logged": 0, "reason": f"membership unavailable: {exc.__class__.__name__}"}
    today = now.astimezone(ET).date()
    start = today - timedelta(days=EXDIV_LOOKBACK_DAYS)
    try:
        frames = (prices or download_prices)(sorted(set(members) | {MARKET}), start, today, actions=True)
    except Exception as exc:
        return {"status": "unavailable", "logged": 0, "reason": f"prices unavailable: {exc.__class__.__name__}"}
    market = _close(frames, MARKET)
    if market is None or market.empty:
        return {"status": "unavailable", "logged": 0, "reason": "no SPY closes"}
    done = {_event_key(e["symbol"], e["ex_date"]) for e in logged_events(db, ACTION_EXDIV)}
    completed = last_completed_session(now)
    since = backfill_end() + timedelta(days=1)  # the backfill already holds the earlier ex-dates
    events, without = [], []
    for symbol in sorted(members):
        frame = frames.get(symbol)
        if frame is None:
            without.append(symbol)
            continue
        for event in ex_dividend_events(symbol, frame, market, membership=membership, since=since,
                                        until=completed):
            if _event_key(symbol, event["ex_date"]) not in done:
                events.append({**event, "source": "forward"})
    if events:
        _write(db, ACTION_EXDIV, "recorded", f"ex-dividend log: {len(events)} new ex-dates",
               {"observed_at": now.isoformat(), "events": events, "members": len(members),
                "members_without_prices": without})
    return {"status": "recorded" if events else "nothing_new", "logged": len(events), "members": len(members),
            "members_without_prices": len(without)}


# --------------------------------------------------------------- the job

def run_daily(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Weekdays: refresh the calendar, then log the matured reports."""
    if not settings.event_logs_enabled:
        return {"status": "disabled"}
    out = {}
    for name, step in (("calendar", refresh_calendar), ("earnings", score_earnings)):
        try:
            out[name] = step(db, now=now)
        except Exception as exc:  # evidence only; one step must not stop the other
            db.rollback()
            logger.warning("event logs: %s failed: %s", name, exc)
            out[name] = {"status": "failed", "reason": f"{exc.__class__.__name__}: {exc}"[:200]}
    return {"status": "ran", **out}


def run_weekly(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Weekly: log the week's new ex-dividend dates."""
    if not settings.event_logs_enabled:
        return {"status": "disabled"}
    try:
        return {"status": "ran", "ex_dividend": score_ex_dividends(db, now=now)}
    except Exception as exc:
        db.rollback()
        logger.warning("event logs: ex-dividend failed: %s", exc)
        return {"status": "failed", "reason": f"{exc.__class__.__name__}: {exc}"[:200]}


# --------------------------------------------------------------- the view

def blackout_shadow_candidates() -> list[int]:
    from app.services.experiment_engine import TEMPLATE_BY_NAME

    return [int(c["earnings_blackout_sessions"]) for c in TEMPLATE_BY_NAME["earnings_blackout"].candidates]


def event_log_status(db: Session) -> dict:
    """Progress of both logs, the backfill verdict and the blackout shadow's candidates (never raises)."""
    from app.services.event_log_backfill import EX_DIVIDEND_BACKFILL

    out: dict = {"rule": RULE, "enabled": bool(settings.event_logs_enabled), "paper_only": True}
    try:
        calendar = _latest(db, ACTION_CALENDAR) or {}
        out["calendar"] = {key: calendar.get(key) for key in ("updated_at", "members", "covered", "fetched",
                                                               "failed", "pending")} if calendar else {"status": "no_data"}
        out["earnings"] = earnings_progress(logged_events(db, ACTION_EARNINGS))
        forward = [e for e in logged_events(db, ACTION_EXDIV)]
        out["ex_dividend"] = {"forward_events": len(forward),
                              "forward_first": min((e["ex_date"] for e in forward), default=None),
                              "forward_last": max((e["ex_date"] for e in forward), default=None),
                              "backfill": {key: value for key, value in EX_DIVIDEND_BACKFILL.items()
                                           if key not in ("by_year",)}}
    except Exception as exc:
        db.rollback()
        out["error"] = f"{exc.__class__.__name__}: {exc}"[:200]
    try:
        out["earnings_blackout_shadow"] = {"candidates": blackout_shadow_candidates(),
                                           "calendar_fallback": "latest earnings_calendar snapshot (<= 8 days old)"}
    except Exception as exc:
        out["earnings_blackout_shadow"] = {"status": "unavailable", "error": exc.__class__.__name__}
    return out
