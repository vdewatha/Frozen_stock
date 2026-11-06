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
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models import AuditLog, Strategy
from app.services.audit import write_audit_log
from app.services.paper_trade_scorecard import GOAL, GOAL_LABEL, MIN_TRADES_FOR_EVIDENCE, build_scorecard

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
REPORT_ACTION = "weekly_performance_report"
BUY_OUTCOME_ACTION = "buy_signal_outcomes"
ALL_TIME_DAYS = 3650
CHANGE_WINDOW_DAYS = 7
GATE_COST_ACTION = "gate_cost"
# Audit events the ``changes`` section narrates (gate-cost rows live under ``stock_paper``).
NARRATED_EVENTS = ("strategy_governance", "experiment_engine", "self_healing")
# Payload keys of an experiment-engine governance row that are not parameter changes.
_GOVERNANCE_META = frozenset({"source", "change_id", "reverted_change", "paper_only"})

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


# ------------------------------------------------------------ change narrative

def _field(row: Any, name: str, default=None):
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _row(row: Any) -> dict:
    """One audit row (ORM object or dict) as the plain fields the narrative needs."""
    stamp = _field(row, "created_at")
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp)
    if stamp is not None and stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return {"event_type": _field(row, "event_type"), "action": _field(row, "action") or "",
            "status": _field(row, "status") or "", "message": _field(row, "message") or "",
            "payload": _field(row, "payload") or {}, "created_at": stamp, "entity_id": _field(row, "entity_id")}


def _when(stamp: Optional[datetime], *, time: bool = False) -> str:
    if stamp is None:
        return "an unknown date"
    local = stamp.astimezone(ET)
    text = f"{local:%b} {local.day}"
    return f"{text} at {local:%H:%M} ET" if time else text


def _words(value: Any) -> str:
    return str(value).replace("_", " ")


def _bps_text(value: Any) -> str:
    return "no measured return" if value is None else f"{float(value):+.1f} bps per trade"


def _clause(text: str) -> str:
    text = (text or "").strip().rstrip(".")
    return text[:1].lower() + text[1:] if text else ""


def _full_stop(text: str) -> str:
    text = (text or "").strip()
    return text if not text or text.endswith((".", "!", "?")) else text + "."


def governance_sentence(row: Any, strategy_names: Mapping[int, str]) -> Optional[str]:
    """A ``strategy_governance`` row as one sentence; ``None`` when nothing changed."""
    r = _row(row)
    payload, when = r["payload"], _when(r["created_at"])
    name = strategy_names.get(r["entity_id"]) or (f"strategy #{r['entity_id']}" if r["entity_id"] else "a strategy")
    if payload.get("source") == "experiment_engine":
        if payload.get("reverted_change"):
            return (f"On {when}, the experiment engine reverted change {payload['reverted_change']} on {name}: "
                    f"{_full_stop(_clause(r['message']))}")
        knobs = ", ".join(f"{key} = {value}" for key, value in payload.items() if key not in _GOVERNANCE_META)
        tag = f" (change {payload['change_id']})" if payload.get("change_id") else ""
        return f"On {when}, the experiment engine set {knobs or 'its gate'} on {name}{tag}: {_full_stop(_clause(r['message']))}"
    old, new = payload.get("old_status"), payload.get("new_status")
    if not (old and new and old != new):
        return None  # a daily "hold" or review note, not a change
    verb = {"pause": "paused", "retire": "retired", "admit": "admitted"}.get(r["action"])
    moved = f"{verb} {name}" if verb else f"moved {name} from {_words(old)} to {_words(new)}"
    reason = _full_stop(_clause(r["message"])) if r["message"] else f"it is now {_words(new)}."
    return f"On {when}, the bot {moved}: {reason}"


def _verdict_so_far(change_id: str, verdicts: Mapping[str, dict], watch: Mapping) -> str:
    latest = verdicts.get(change_id)
    if latest is None:
        needed = (watch or {}).get("min_trades")
        return f"watching; a verdict needs {needed} trades" if needed else "watching"
    after = latest.get("after") or {}
    return f"judged {_words(latest.get('verdict'))} after {after.get('trades', 0)} trades"


def experiment_sentence(row: Any, verdicts: Mapping[str, dict]) -> Optional[str]:
    """An ``experiment_engine`` row (run, experiment, adoption or verdict) as one sentence."""
    r = _row(row)
    payload, when, action = r["payload"], _when(r["created_at"]), r["action"]
    if action == "run":
        experiments = payload.get("experiments") or []
        adopted = payload.get("adopted") or []
        untemplated = payload.get("untemplated") or []
        text = (f"On {when}, the experiment engine reviewed {payload.get('lessons', 0)} repeating lessons, ran "
                f"{len(experiments)} experiments and adopted {len(adopted)} changes")
        if untemplated:
            text += f"; {len(untemplated)} lessons have no experiment template yet"
        return text + "."
    if action == "experiment":
        base = (f"On {when}, the engine tested the {_words(payload.get('template'))} on {payload.get('strategy_type')} "
                f"because {_words(payload.get('trigger'))} repeated {payload.get('lesson_count', 0)} times")
        if r["status"] == "adopted":
            return f"{base} and adopted '{payload.get('adopted_description')}' under the pre-registered rule."
        if r["status"] == "eligible":
            return (f"{base}; '{payload.get('adopted_description')}' passed the rule but adoption is switched off, "
                    "so nothing changed.")
        return f"{base}; no candidate beat the current setup, so nothing changed."
    if action == "change_adopted":
        before = payload.get("recent_before") or {}
        so_far = _verdict_so_far(payload.get("change_id"), verdicts, payload.get("watch") or {})
        return (f"On {when}, change {payload.get('change_id')} on {payload.get('strategy_type')} went live: "
                f"{payload.get('description')}. Before it, the last {before.get('trades', 0)} trades averaged "
                f"{_bps_text(before.get('expectancy_bps'))}. So far: {so_far}.")
    if action == "change_verdict":
        after, before = payload.get("after") or {}, payload.get("before") or {}
        ending = ("the previous setup was restored" if payload.get("reverted")
                  else "the change stays" if payload.get("state") == "confirmed" else "the watch is closed")
        return (f"On {when}, after {after.get('trades', 0)} trades, change {payload.get('change_id')} on "
                f"{payload.get('strategy_type')} ({_words(payload.get('template'))} for "
                f"{_words(payload.get('trigger'))}) was judged {_words(payload.get('verdict'))}: "
                f"{_bps_text(after.get('expectancy_bps'))} against {_bps_text(before.get('expectancy_bps'))} "
                f"before; {ending}.")
    return None


def gate_cost_sentence(row: Any) -> Optional[str]:
    """The latest gate-cost report as one sentence: what the gates cost, and the verdicts so far."""
    r = _row(row)
    report = r["payload"].get("report") or {}
    totals = report.get("totals") or {}
    window = report.get("window") or {}
    pairs = report.get("by_strategy") or []
    costly = [item for item in pairs if item.get("verdict") == "costly"]
    earning = [item for item in pairs if item.get("verdict") == "earning its keep"]

    def pair(item: Mapping) -> str:
        mean = item.get("mean_net_bps")
        return (f"{_words(item.get('gate'))} on {item.get('strategy_type')}"
                + (f" ({float(mean):+.0f} bps per blocked trade)" if mean is not None else ""))

    text = (f"Through {report.get('as_of') or _when(r['created_at'])}, the gate-cost audit scored "
            f"{totals.get('scored', 0)} of the {totals.get('blocked', 0)} BUYs the entry gates withheld over the "
            f"last {window.get('sessions', '?')} sessions")
    text += ("; costly so far: " + ", ".join(pair(item) for item in costly) if costly
             else "; no gate is costing more than it saves yet")
    if earning:
        text += "; earning their keep: " + ", ".join(pair(item) for item in earning)
    return text + "."


def self_healing_sentence(row: Any) -> Optional[str]:
    """A ``self_healing`` action row (not the run rows) as one sentence."""
    r = _row(row)
    if r["action"] == "run" or not r["message"]:
        return None
    return f"On {_when(r['created_at'], time=True)}, {_full_stop(_clause(r['message']))}"


def narrate_changes(rows: Iterable[Any], *, strategy_names: Optional[Mapping[int, str]] = None) -> list[dict]:
    """Audit rows -> plain-language change sentences, oldest first (pure; no database)."""
    names = dict(strategy_names or {})
    ordered = sorted((_row(row) for row in rows),
                     key=lambda r: (r["created_at"] or datetime.min.replace(tzinfo=UTC)))
    verdicts: dict[str, dict] = {}
    for r in ordered:
        if r["event_type"] == "experiment_engine" and r["action"] == "change_verdict":
            verdicts[r["payload"].get("change_id")] = r["payload"]
    gate_cost_rows = [r for r in ordered if r["event_type"] == "stock_paper" and r["action"] == GATE_COST_ACTION]
    latest_gate_cost = gate_cost_rows[-1] if gate_cost_rows else None
    items = []
    for r in ordered:
        source, sentence = None, None
        if r["event_type"] == "strategy_governance":
            source, sentence = "strategy_governance", governance_sentence(r, names)
        elif r["event_type"] == "experiment_engine":
            source, sentence = "experiment_engine", experiment_sentence(r, verdicts)
        elif r is latest_gate_cost:
            source, sentence = "gate_cost", gate_cost_sentence(r)
        elif r["event_type"] == "self_healing":
            source, sentence = "self_healing", self_healing_sentence(r)
        if sentence:
            items.append({"at": r["created_at"].isoformat() if r["created_at"] else None, "source": source,
                          "action": r["action"], "sentence": sentence})
    return items


def change_narrative(db: Session, start: datetime, end: datetime) -> dict:
    """The ``changes`` section: what the bot changed in the window, why, and the result so far."""
    rows = db.query(AuditLog).filter(
        AuditLog.created_at >= start.astimezone(UTC).replace(tzinfo=None),
        AuditLog.created_at <= end.astimezone(UTC).replace(tzinfo=None),
        or_(AuditLog.event_type.in_(NARRATED_EVENTS),
            and_(AuditLog.event_type == "stock_paper", AuditLog.action == GATE_COST_ACTION)),
    ).order_by(AuditLog.id).all()
    names = {row.id: row.name for row in db.query(Strategy.id, Strategy.name).all()}
    items = narrate_changes(rows, strategy_names=names)
    return {"window": {"start": start.isoformat(), "end": end.isoformat()},
            "counts": dict(Counter(item["source"] for item in items)), "items": items,
            "method": "One sentence per governance decision, experiment-engine step, the latest gate-cost report "
                      "and each self-healing action, built from the audit log without a language model."}


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
        "changes": change_narrative(db, now - timedelta(days=CHANGE_WINDOW_DAYS), now),
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
