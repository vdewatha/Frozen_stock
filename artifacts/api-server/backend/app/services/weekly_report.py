"""Weekly paper performance report: is each strategy earning what its backtest says?

Built every weekend from the broker-observed scorecard. For each strategy it
puts the week's and the all-time closed trades next to the backtest expectancy
the strategy was admitted on, lists why BUY signals did not trade (from the
``buy_signal_outcomes`` audit rows) and adds the short pilot's gross P/L.
One report is stored per ISO week; it never changes strategies or orders.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import re
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models import AuditLog, Strategy
from app.services.audit import write_audit_log
from app.services.paper_trade_scorecard import GOAL, GOAL_LABEL, MIN_TRADES_FOR_EVIDENCE, build_scorecard

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
REPORT_ACTION = "weekly_performance_report"
BUY_OUTCOME_ACTION = "buy_signal_outcomes"
ALL_TIME_DAYS = 3650

# Net expectancy (bps per trade) each strategy was admitted on, from the
# research reports. Post-2015 where available: older history overstates edges
# that have decayed.
BACKTEST_EXPECTANCY_BPS: dict[str, tuple[float, str]] = {
    "rsi2_pullback": (15.0, "reports/rsi2-scope-20261102.md, post-2015, index ETFs and stocks"),
    "ibs_mean_reversion": (21.3, "reports/regime-filter-20261029.md, post-2015, no trend filter"),
    "bollinger_mean_reversion": (39.4, "reports/strategy-scorecard-20261028.md, post-2015"),
    "rsi_mean_reversion": (31.2, "reports/strategy-scorecard-20261028.md, post-2015"),
    "model_predictive_long": (26.8, "reports/strategy-scorecard-20261028.md, post-2020, 4 symbols"),
    "macd_momentum": (-15.6, "reports/strategy-scorecard-20261028.md, all history; retired"),
    "moving_average_crossover": (-10.6, "reports/strategy-scorecard-20261028.md, all history; retired"),
}


def _week_key(now: datetime) -> str:
    year, week, _ = now.astimezone(ET).isocalendar()
    return f"{year}-W{week:02d}"


def _bps(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value * 10_000, 1)


def verdict(trades: int, paper_bps: Optional[float], backtest_bps: Optional[float]) -> str:
    """Plain-language status; no judgement before the evidence minimum."""
    if trades < MIN_TRADES_FOR_EVIDENCE:
        return f"needs {MIN_TRADES_FOR_EVIDENCE - trades} more closed trades"
    if paper_bps is None:
        return "no measured return"
    if paper_bps <= 0:
        return "losing money"
    if backtest_bps is not None and backtest_bps > 0 and paper_bps < backtest_bps / 2:
        return "profitable but below half its backtest"
    return "on track with its backtest"


def _normalize_reason(reason: Optional[str]) -> str:
    """Group reasons that differ only in dates, numbers or symbols."""
    if not reason:
        return "no reason recorded"
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", reason)
    text = re.sub(r"sector group \w+", "sector group <group>", text)
    return re.sub(r"\d+(\.\d+)?", "<n>", text)


def blocked_buy_reasons(db: Session, start: datetime, end: datetime, *, limit: int = 8) -> dict:
    """How often each reason stopped a BUY signal during the window.

    Outcome rows are written only when the picture changes, so this counts
    distinct (symbol, strategy, reason) blocks rather than minutes blocked.
    """
    rows = db.query(AuditLog).filter(
        AuditLog.event_type == "stock_paper", AuditLog.action == BUY_OUTCOME_ACTION,
        AuditLog.created_at >= start.replace(tzinfo=None), AuditLog.created_at <= end.replace(tzinfo=None),
    ).order_by(AuditLog.id).all()
    seen: set[tuple] = set()
    reasons: Counter = Counter()
    submitted = 0
    for row in rows:
        for outcome in (row.payload or {}).get("outcomes", []):
            key = (outcome.get("symbol"), outcome.get("strategy"), outcome.get("status"), outcome.get("reason"))
            if key in seen:
                continue
            seen.add(key)
            if outcome.get("status") == "submitted":
                submitted += 1
            else:
                reasons[_normalize_reason(outcome.get("reason"))] += 1
    return {
        "submitted_buys": submitted,
        "blocked_buys": sum(reasons.values()),
        "top_reasons": [{"reason": reason, "count": count} for reason, count in reasons.most_common(limit)],
        "snapshots": len(rows),
    }


def _short_pilot_summary(db: Session) -> Optional[dict]:
    try:
        from app.services.paper_short_pilot import pilot_status

        status = pilot_status(db)
    except Exception:  # the short lane is optional; the report must not fail
        db.rollback()
        return None
    latest = (status.get("performance") or {}).get("latest") or {}
    filled = [order for order in status.get("orders", []) if order.get("filled_quantity") not in (None, "0", "0E-9")]
    return {"enabled": bool(status.get("autonomous")), "gross_pl": latest.get("gross_pl"),
            "filled_orders": len(filled), "costs_verified": False}


def _cost_calibration_section(db: Session) -> Optional[dict]:
    """Realised vs modelled execution cost and the multiplier in force (``paper_cost_calibration``)."""
    try:
        from app.services.paper_cost_calibration import weekly_report_section

        return weekly_report_section(db)
    except Exception:  # the calibration is optional; the report must not fail
        db.rollback()
        return None


def build_weekly_report(db: Session, *, now: Optional[datetime] = None) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    week = build_scorecard(db, days=7, now=now, classify=False)
    all_time = build_scorecard(db, days=ALL_TIME_DAYS, now=now, classify=False)
    types = {row.id: row for row in db.query(Strategy).all()}
    weekly = {row["key"]: row for row in week.get("by_strategy", [])}
    strategies = []
    for row in all_time.get("by_strategy", []):
        strategy = types.get(row["key"]) if isinstance(row["key"], int) else None
        strategy_type = strategy.strategy_type if strategy else None
        backtest = BACKTEST_EXPECTANCY_BPS.get(strategy_type or "")
        paper_bps = _bps(row.get("expectancy_return"))
        this_week = weekly.get(row["key"], {})
        strategies.append({
            "strategy_id": row["key"] if isinstance(row["key"], int) else None,
            "strategy_name": row.get("strategy_name") or (strategy.name if strategy else "Unattributed"),
            "strategy_type": strategy_type,
            "status": strategy.current_status if strategy else None,
            "week_trades": this_week.get("trades", 0),
            "week_pnl": this_week.get("total_pnl", 0.0),
            "trades": row["trades"],
            "win_rate": row.get("win_rate"),
            "avg_pnl": row.get("expectancy"),
            "total_pnl": row.get("total_pnl"),
            "paper_bps": paper_bps,
            "backtest_bps": backtest[0] if backtest else None,
            "backtest_source": backtest[1] if backtest else None,
            "verdict": verdict(row["trades"], paper_bps, backtest[0] if backtest else None),
        })
    strategies.sort(key=lambda item: (-item["trades"], item["strategy_name"] or ""))
    headline = all_time.get("headline", {})
    week_headline = week.get("headline", {})
    return {
        "week": _week_key(now),
        "generated_at": now.isoformat(),
        "status": all_time.get("status"),
        "this_week": {"trades": week_headline.get("trades", 0), "win_rate": week_headline.get("win_rate"),
                      "total_pnl": week_headline.get("total_pnl", 0.0), "avg_pnl": week_headline.get("expectancy")},
        "all_time": {"trades": headline.get("trades", 0), "win_rate": headline.get("win_rate"),
                     "total_pnl": headline.get("total_pnl", 0.0), "avg_pnl": headline.get("expectancy"),
                     "paper_bps": _bps(headline.get("expectancy_return"))},
        "goal": {"target": GOAL, "label": GOAL_LABEL, "min_trades_for_evidence": MIN_TRADES_FOR_EVIDENCE},
        "strategies": strategies,
        "blocked_buys": blocked_buy_reasons(db, now - timedelta(days=7), now),
        "short_pilot": _short_pilot_summary(db),
        "cost_calibration": _cost_calibration_section(db),
        "open_unrealized_pnl": (all_time.get("open_mark_to_market") or {}).get("unrealized_pnl"),
        "paper_only": True,
        "mutates_strategy_state": False,
    }


def latest_weekly_report(db: Session) -> Optional[dict]:
    row = db.query(AuditLog).filter(
        AuditLog.event_type == "stock_paper", AuditLog.action == REPORT_ACTION,
    ).order_by(AuditLog.id.desc()).first()
    return (row.payload or {}).get("report") if row else None


def record_weekly_report(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Store this ISO week's report once and raise an info notification."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    week = _week_key(now)
    latest = latest_weekly_report(db)
    if latest and latest.get("week") == week:
        return {"status": "complete", "recorded": False, "week": week, "reason": "report already recorded this week"}
    report = build_weekly_report(db, now=now)
    write_audit_log(db, event_type="stock_paper", action=REPORT_ACTION, status="recorded",
                    message=f"Weekly paper performance report {week}", payload={"report": report})
    db.commit()
    try:
        from app.services.notifications import create_notification

        totals = report["all_time"]
        create_notification(
            db, category="weekly_performance_report", severity="info", source="weekly_report",
            title=f"Weekly paper report {week}",
            message=(f"This week: {report['this_week']['trades']} closed trades, "
                     f"${report['this_week']['total_pnl'] or 0:,.2f}. All time: {totals['trades']} trades, "
                     f"${totals['total_pnl'] or 0:,.2f}."),
            payload={"week": week}, commit=True,
        )
    except Exception:  # the stored report is the deliverable; the notice is a convenience
        db.rollback()
    return {"status": "complete", "recorded": True, "week": week,
            "trades": report["all_time"]["trades"], "strategies": len(report["strategies"])}
