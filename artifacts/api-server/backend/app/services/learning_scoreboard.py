"""Learning scoreboard: is the bot learning from its paper trades, and is it improving?

One read-only snapshot for the Learning > Scoreboard page:

* ``health``: after a deploy, did each learning piece actually run during
  the latest session? (losses explained, news discovery, news symbols fed
  and learning-sized, tested exits on new entries, no stacking);
* ``progress``: win rate and expectancy (average net return per trade) by
  week, overall and per strategy, against the 80% goal;
* ``causes`` / ``repeating_failures``: why trades lost and which lessons
  keep coming back (from ``loss_diagnosis``);
* ``news_vs_core``: news-found symbols against the fixed universe;
* ``changes``: what shipped when, with results before and after.

Nothing here writes; every number comes from attributions, orders, bars
and the audit log.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import AuditLog, IntradayBar, Strategy
from app.models.stock_paper import StockPaperOrder
from app.services import trade_attribution
from app.services.exchange_sessions import session_bounds
from app.services.stock_paper_autotrader import SCHEDULED_SOURCE
from app.services.stock_paper_ledger import EXPLORATION_SOURCE
from app.services.paper_trade_scorecard import GOAL, GOAL_LABEL, MIN_TRADES_FOR_EVIDENCE

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
NEWS_EVENT = "news_symbol_discovery"
BUY_OUTCOMES = "buy_signal_outcomes"
WEEKS = 8

# What shipped, and what to watch for. ``effective`` is the first session the
# change traded (it is live after the Replit publish that follows the merge).
CHANGES = (
    {"key": "loss_learning", "effective": "2026-11-06", "title": "Every loss explained; tested exits; one-share floor",
     "watch": "Every closed trade gets a cause. Technical strategies trade a 3% stop / 6% target / 20-session hold "
              "and ignore SELL signals. Learning trades in names under ~$1,500 fill."},
    {"key": "news_symbols", "effective": "2026-11-06", "title": "Symbols from the news; one position per symbol",
     "watch": "Up to 8 news-found stocks admitted each day and traded learning-sized. crowded_symbol disappears "
              "from loss flags."},
    {"key": "market_context", "effective": "2026-11-09", "title": "Real economic data; announcement blackout; VIX and breadth",
     "watch": "No BUYs the session before a Fed decision, CPI or jobs report. Losses get macro_event_in_hold, "
              "high_vix_entry and weak_breadth_entry flags; a repeat proposes a gate to backtest."},
)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _check(key: str, label: str, status: str, detail: str, **evidence) -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail, "evidence": evidence or None}


def latest_session(now: datetime) -> tuple[date, datetime, datetime, bool] | None:
    """The most recent NYSE session that has opened: (day, open, close, still open)."""
    day = now.astimezone(ET).date()
    for _ in range(10):
        bounds = session_bounds(day)
        if bounds is not None:
            opened, closed = (_utc(value) for value in bounds)
            if opened <= now:
                return day, opened, closed, now < closed
        day -= timedelta(days=1)
    return None


def _ret(row) -> float:
    return float(row.net_return if row.net_return is not None else row.gross_return)


def totals(rows: Iterable) -> dict:
    rows = list(rows)
    wins = sum(not row.is_loss for row in rows)
    pnl = [float(row.net_pnl) for row in rows if row.net_pnl is not None]
    return {
        "trades": len(rows),
        "wins": wins,
        "win_rate": round(wins / len(rows), 4) if rows else None,
        # Expectancy: average net return per trade (fraction of the position).
        "avg_return": round(sum(_ret(row) for row in rows) / len(rows), 6) if rows else None,
        "net_pnl": round(sum(pnl), 2) if pnl else None,
    }


def _week(moment: datetime) -> str:
    year, week, _ = _utc(moment).astimezone(ET).isocalendar()
    return f"{year}-W{week:02d}"


def _progress(rows: list, now: datetime) -> dict:
    by_week: dict[str, list] = defaultdict(list)
    for row in rows:
        by_week[_week(row.closed_at)].append(row)
    weeks = []
    for offset in range(WEEKS - 1, -1, -1):
        key = _week(now - timedelta(weeks=offset))
        weeks.append({"week": key, **totals(by_week.get(key, []))})
    by_strategy: dict[str, list] = defaultdict(list)
    for row in rows:
        by_strategy[row.strategy_type].append(row)
    strategies = []
    for strategy_type, members in by_strategy.items():
        members.sort(key=lambda row: _utc(row.closed_at))
        recent, earlier = members[-10:], members[:-10]
        strategies.append({
            "strategy_type": strategy_type, **totals(members),
            "recent": totals(recent), "earlier": totals(earlier),
            "evidence": "enough trades to judge" if len(members) >= MIN_TRADES_FOR_EVIDENCE
                        else f"{MIN_TRADES_FOR_EVIDENCE - len(members)} more trades before judging",
        })
    strategies.sort(key=lambda item: (-item["trades"], item["strategy_type"]))
    return {"overall": totals(rows), "weeks": weeks, "strategies": strategies,
            "goal": {"target": GOAL, "label": GOAL_LABEL, "min_trades_for_evidence": MIN_TRADES_FOR_EVIDENCE}}


def _news_runs(db: Session, limit: int = 200) -> list[AuditLog]:
    return (db.query(AuditLog).filter(AuditLog.event_type == NEWS_EVENT)
            .order_by(AuditLog.id.desc()).limit(limit).all())


def _ever_admitted(runs: list[AuditLog]) -> set[str]:
    return {row["symbol"] for run in runs for row in (run.payload or {}).get("admitted") or []}


def _news_summary(runs: list[AuditLog]) -> dict | None:
    if not runs:
        return None
    latest = runs[0].payload or {}
    return {
        "observed_at": latest.get("observed_at"),
        "status": latest.get("status"),
        "error": latest.get("error"),
        "articles_fetched": latest.get("articles_fetched"),
        "admitted": [
            {"symbol": row["symbol"], "name": row.get("name"), "articles": row.get("articles"),
             "avg_sentiment": row.get("avg_sentiment"), "price": row.get("price"),
             "admitted_at": row.get("admitted_at"), "expires_at": row.get("expires_at"),
             "top_headline": ((row.get("headlines") or [{}])[0]).get("headline")}
            for row in latest.get("admitted") or []
        ],
        "holding": latest.get("holding") or [],
        "rejected": (latest.get("rejected") or [])[:8],
        "runs_recorded": len(runs),
    }


def _buy_outcomes(db: Session, start: datetime) -> list[dict]:
    rows = db.query(AuditLog).filter(
        AuditLog.event_type == "stock_paper", AuditLog.action == BUY_OUTCOMES,
        AuditLog.created_at >= start.astimezone(UTC).replace(tzinfo=None),
    ).order_by(AuditLog.id).all()
    seen, out = set(), []
    for row in rows:
        for outcome in (row.payload or {}).get("outcomes") or []:
            key = (outcome.get("symbol"), outcome.get("strategy"), outcome.get("status"), outcome.get("reason"))
            if key not in seen:
                seen.add(key)
                out.append(outcome)
    return out


def _entries(db: Session, start: datetime) -> list[StockPaperOrder]:
    """Entry orders (not exit legs) created since ``start``."""
    return [row for row in db.query(StockPaperOrder).filter(
        StockPaperOrder.side == "buy", StockPaperOrder.created_at >= start,
    ).all() if not row.leg_role and row.source in {SCHEDULED_SOURCE, EXPLORATION_SOURCE}]


def _health(db: Session, *, now: datetime, rows: list, runs: list[AuditLog], equity: float) -> dict:
    from app.services.loss_diagnosis import exit_tested
    from app.services.strategy_execution_policy import exit_policy_for

    session = latest_session(now)
    checks = []
    if session is None:
        return {"session": None, "checks": checks}
    day, opened, closed, is_open = session
    session_out = {"date": day.isoformat(), "open": opened.isoformat(), "close": closed.isoformat(),
                   "in_progress": is_open}

    # 1. Closed trades are explained (attributed) and every explanation has a cause.
    coverage = trade_attribution.attribution_coverage(db, since=now - timedelta(days=10))
    missing = len(coverage["missing_entry_order_ids"])
    if not coverage["closed_round_trips"]:
        checks.append(_check("explained", "Closed trades are explained", "waiting",
                             "No closed trades in the last 10 days yet.", **coverage))
    elif missing:
        checks.append(_check("explained", "Closed trades are explained", "warning",
                             f"{missing} of {coverage['closed_round_trips']} closed trades are not explained yet. "
                             "Attribution runs at :05 and :35 during the session; check again after the next run.",
                             **coverage))
    else:
        checks.append(_check("explained", "Closed trades are explained", "ok",
                             f"All {coverage['closed_round_trips']} closed trades of the last 10 days are explained.",
                             **coverage))
    recent = [row for row in rows if _utc(row.closed_at) >= now - timedelta(days=10)]
    diagnosed = [row for row in recent if (row.detail or {}).get("diagnosis")]
    if not recent:
        checks.append(_check("diagnosed", "Each explained trade has a cause", "waiting", "No explained trades yet."))
    else:
        status = "ok" if len(diagnosed) == len(recent) else ("failing" if not diagnosed else "warning")
        checks.append(_check("diagnosed", "Each explained trade has a cause", status,
                             f"{len(diagnosed)} of {len(recent)} explained trades have a loss/win cause."))

    # 2. News discovery ran this session.
    session_runs = [run for run in runs if _utc(run.created_at) >= opened - timedelta(hours=2)]
    if not runs:
        checks.append(_check("news_ran", "News discovery ran", "waiting",
                             "No discovery run recorded yet. The first run is 8:10 New York time on a weekday "
                             "after the publish."))
    elif not session_runs:
        checks.append(_check("news_ran", "News discovery ran", "failing" if is_open else "warning",
                             f"No run since before the {day.isoformat()} session. Runs are every 30 minutes, "
                             "8:10-15:40 New York time; check that the learning worker is running."))
    else:
        latest = runs[0].payload or {}
        age = (now - _utc(runs[0].created_at)).total_seconds() / 60
        unavailable = sum((run.payload or {}).get("status") == "news_unavailable" for run in session_runs)
        if latest.get("status") == "news_unavailable":
            status, detail = "failing", f"The latest run could not read Alpaca news: {latest.get('error')}"
        elif is_open and age > 75:
            status, detail = "warning", f"The latest run was {age:.0f} minutes ago (expected every 30)."
        else:
            status = "warning" if unavailable else "ok"
            detail = (f"{len(session_runs)} runs this session; the latest read {latest.get('articles_fetched')} "
                      f"articles and admits {len(latest.get('admitted') or [])} symbols.")
            if unavailable:
                detail += f" {unavailable} runs could not read the news."
        checks.append(_check("news_ran", "News discovery ran", status, detail))

    # 3. Admitted symbols get intraday prices.
    admitted = [row["symbol"] for row in ((runs[0].payload or {}).get("admitted") or [])] if runs else []
    if not admitted:
        checks.append(_check("news_fed", "News symbols get live prices", "waiting", "No symbols admitted yet."))
    else:
        latest_bar = dict(db.query(IntradayBar.symbol, func.max(IntradayBar.opened_at)).filter(
            IntradayBar.symbol.in_(admitted)).group_by(IntradayBar.symbol).all())
        fed = [symbol for symbol in admitted
               if latest_bar.get(symbol) is not None and _utc(latest_bar[symbol]) >= opened]
        unfed = sorted(set(admitted) - set(fed))
        if not unfed:
            checks.append(_check("news_fed", "News symbols get live prices", "ok",
                                 f"All {len(admitted)} admitted symbols have minute bars this session."))
        else:
            status = "warning" if fed or now - opened < timedelta(minutes=30) else "failing"
            checks.append(_check("news_fed", "News symbols get live prices", status,
                                 f"No minute bars this session for {', '.join(unfed)}. A symbol admitted mid-session "
                                 "gets bars from the next ingestion; without bars its entries are blocked.",
                                 unfed=unfed))

    entries = _entries(db, opened)
    ever = _ever_admitted(runs)
    strategy_rows = {row.id: row for row in db.query(Strategy).all()}

    # 4. News-found entries are learning-sized.
    news_entries = [row for row in entries if row.symbol in ever]
    cap = equity * 0.015 * 1.05 if equity else None
    if not news_entries:
        checks.append(_check("news_sized", "News-found buys are learning-sized", "waiting",
                             "No buys in news-found symbols this session yet (a strategy has to signal BUY first)."))
    else:
        oversized = [f"{row.symbol} {row.quantity}x{row.limit_price}" for row in news_entries
                     if cap and row.limit_price and float(row.quantity) * float(row.limit_price) > cap]
        checks.append(_check(
            "news_sized", "News-found buys are learning-sized", "failing" if oversized else "ok",
            f"{len(news_entries)} news-found buys this session"
            + (f"; over the learning size: {', '.join(oversized)}" if oversized else ", all learning-sized."),
            symbols=sorted({row.symbol for row in news_entries})))

    # 5. New technical entries carry their tested exits.
    tested, untested = 0, []
    for row in entries:
        strategy = strategy_rows.get(row.strategy_id)
        policy = exit_policy_for(strategy) if strategy is not None else None
        if policy is None or not row.limit_price:
            continue
        verdict = exit_tested(policy, entry_price=float(row.limit_price),
                              stop_price=float(row.stop_price) if row.stop_price is not None else None,
                              take_profit_price=float(row.take_profit_price) if row.take_profit_price is not None else None)
        if verdict is True:
            tested += 1
        elif verdict is False:
            untested.append(f"{row.symbol} ({strategy.strategy_type})")
    if not tested and not untested:
        checks.append(_check("tested_exits", "New entries use their tested exits", "waiting",
                             "No new strategy entries with exit legs this session yet."))
    else:
        checks.append(_check("tested_exits", "New entries use their tested exits", "failing" if untested else "ok",
                             f"{tested} of {tested + len(untested)} new entries match their strategy's tested "
                             "stop and target" + (f"; not matching: {', '.join(untested[:6])}" if untested else "."),
                             untested=untested[:20]))

    # 6. One position per symbol.
    session_entries: dict[str, set] = defaultdict(set)
    from app.services.stock_paper_ledger import NONTERMINAL_ORDER_STATUSES

    for row in entries:
        # Only buys that filled or are still working hold the symbol.  The
        # broker spells it "canceled": an unfilled, canceled ABBV buy on
        # 2026-11-06 made this check report a second buyer that never held it.
        if row.status in {"filled", "partially_filled"} or row.status in NONTERMINAL_ORDER_STATUSES:
            session_entries[row.symbol].add(row.strategy_id)
    stacked = sorted(symbol for symbol, owners in session_entries.items() if len(owners) > 1)
    outcomes = _buy_outcomes(db, opened)
    blocked = sum("one position per symbol" in str(item.get("reason") or "") for item in outcomes)
    if stacked:
        checks.append(_check("one_per_symbol", "One position per symbol", "failing",
                             f"More than one strategy bought {', '.join(stacked)} this session.", stacked=stacked))
    else:
        checks.append(_check("one_per_symbol", "One position per symbol", "ok" if entries or blocked else "waiting",
                             f"No symbol bought by two strategies this session; the rule skipped {blocked} buy "
                             "signals that would have stacked." if entries or blocked else
                             "No entries this session yet."))
    checks += _market_data_checks(db, now=now, outcomes=outcomes)
    return {"session": session_out, "checks": checks}


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + "."


def _market_data_checks(db: Session, *, now: datetime, outcomes: list[dict]) -> list[dict]:
    """7-9. The economic data is real; the announcement calendar and the gauges are current."""
    from app.services.economic_data import MOCK_SOURCE, fred_configured, latest_indicator_map
    from app.services.market_events import EVENT_NAMES, is_blackout_reason, latest_calendar, next_events
    from app.services.market_gauges import BREADTH, VIX, latest_gauges

    checks = []
    latest = latest_indicator_map(db, auto_seed=False)
    rates = latest.get("treasury_10y")
    if rates is None or rates.source == MOCK_SOURCE:
        detail = ("FRED_API_KEY is set; waiting for the 07:00 ET import." if fred_configured()
                  else "Placeholder numbers. Add a free FRED_API_KEY in Replit Secrets and publish.")
        checks.append(_check("real_macro", "Economic data is real", "warning", detail))
    else:
        age = (now.astimezone(ET).date() - rates.observation_date).days
        checks.append(_check(
            "real_macro", "Economic data is real", "ok" if age <= 7 else "warning",
            f"FRED: 10-year Treasury {float(rates.value):.2f}% on {rates.observation_date.isoformat()}"
            + ("" if age <= 7 else f", {age} days old; check the 07:00 ET import.")))

    calendar = latest_calendar(db)
    skipped = sum(is_blackout_reason(item.get("reason")) for item in outcomes)
    if not calendar:
        checks.append(_check("event_calendar", "Announcement dates are loaded", "waiting",
                             "The calendar refreshes at 06:45 ET; until then only the built-in Fed dates apply."))
    else:
        observed = datetime.fromisoformat(calendar["observed_at"])
        upcoming = next_events(calendar.get("events"), now, limit=1)
        fomc_ahead = next_events([e for e in calendar.get("events") or [] if e["kind"] == "fomc"], now, limit=1)
        problems = []
        if now - observed > timedelta(hours=36):
            problems.append(f"last refreshed {observed.astimezone(ET):%b %d %H:%M} ET")
        if calendar.get("missing"):
            problems.append("no dates for " + ", ".join(EVENT_NAMES.get(k, k) for k in calendar["missing"])
                            + (" (needs FRED_API_KEY)" if not fred_configured() else ""))
        if not fomc_ahead or datetime.fromisoformat(fomc_ahead[0]["date"]).date() - now.date() > timedelta(days=75):
            problems.append("no Fed meeting known in the next 75 days")
        nxt = (f"Next: {EVENT_NAMES.get(upcoming[0]['kind'], upcoming[0]['kind'])} on {upcoming[0]['date']}."
               if upcoming else "No upcoming event known.")
        checks.append(_check("event_calendar", "Announcement dates are loaded", "warning" if problems else "ok",
                             f"{nxt} Skipped {skipped} BUYs before an announcement this session."
                             + (" " + _sentence("; ".join(problems)) if problems else ""),
                             skipped=skipped))

    gauges = latest_gauges(db, now)
    parts = []
    if VIX in gauges:
        parts.append(f"VIX {gauges[VIX]['value']:.1f} ({gauges[VIX]['as_of']})")
    if BREADTH in gauges:
        parts.append(f"{gauges[BREADTH]['value']:.0f}% of S&P 500 above 200-day ({gauges[BREADTH]['as_of']})")
    stale = [name for name in (VIX, BREADTH) if not (gauges.get(name) or {}).get("fresh")]
    if not gauges:
        checks.append(_check("market_gauges", "VIX and breadth are current", "waiting",
                             "First reading at 09:50 ET on the next session."))
    else:
        checks.append(_check("market_gauges", "VIX and breadth are current", "warning" if stale else "ok",
                             "; ".join(parts) + "." + (f" Stale or missing: {', '.join(stale)}." if stale else "")))
    return checks


def _news_vs_core(rows: list, ever: set[str]) -> dict:
    news = [row for row in rows if row.symbol in ever]
    core = [row for row in rows if row.symbol not in ever]
    news_wrong = sum(((row.detail or {}).get("diagnosis") or {}).get("cause") == "news_wrong" for row in news)
    return {"news": totals(news), "core": totals(core), "news_wrong": news_wrong}


def _changes(rows: list, extra: Iterable[dict] = ()) -> list[dict]:
    """Shipped changes plus the experiment engine's adopted ones, each with results before and after.

    An engine change is judged on its own strategy's trades only.
    """
    out = []
    for change in (*CHANGES, *extra):
        start = datetime.fromisoformat(change["effective"]).replace(tzinfo=ET)
        scope = [row for row in rows if not change.get("strategy_type") or row.strategy_type == change["strategy_type"]]
        before = [row for row in scope if _utc(row.opened_at) < start]
        after = [row for row in scope if _utc(row.opened_at) >= start]
        out.append({**change, "before": totals(before), "after": totals(after)})
    return out


def _engine_changes(db: Session) -> list[dict]:
    from app.services.experiment_engine import scoreboard_changes

    try:
        return scoreboard_changes(db)
    except Exception:  # the scoreboard must render without the ledger
        db.rollback()
        return []


def _equity(db: Session) -> float:
    from app.services.stock_paper_ledger import active_paper_account

    try:
        account = active_paper_account(db)
        return float(getattr(account, "equity", None) or 0)
    except Exception:
        db.rollback()
        return 0.0


def build_learning_scoreboard(db: Session, *, now: Optional[datetime] = None) -> dict:
    from app.services.loss_diagnosis import cause_breakdown, repeating_failures

    now = _utc(now or datetime.now(UTC))
    rows = [row for row in trade_attribution.current_attributions(db) if row.source_key.startswith("round_trip:")]
    runs = _news_runs(db)
    ever = _ever_admitted(runs)
    equity = _equity(db)
    recent = [row for row in rows if _utc(row.closed_at) >= now - timedelta(days=30)]
    return {
        "generated_at": now.isoformat(),
        "health": _health(db, now=now, rows=rows, runs=runs, equity=equity),
        "progress": _progress(rows, now),
        "causes": cause_breakdown(recent),
        "repeating_failures": repeating_failures(rows),
        "news": _news_summary(runs),
        "news_vs_core": _news_vs_core(rows, ever),
        "changes": _changes(rows, _engine_changes(db)),
        "paper_only": True,
    }

