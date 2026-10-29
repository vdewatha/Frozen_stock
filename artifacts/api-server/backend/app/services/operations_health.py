"""Operations health alerts and the daily operations summary.

Read-mostly paper-only monitoring. The only writes are warning notifications
(raised once per condition, refreshed while it persists and resolved when it
clears) and a small Redis record of the newest intraday import duration.
Nothing here pauses trading, touches orders or changes live-trading settings,
and no notification is ``critical`` (unresolved critical notifications block
readiness).
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
import logging
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import IntradayBar, Notification, StockPaperRecoveryEvent, StockPaperRecoveryState
from app.services.exchange_sessions import session_bounds
from app.services.notifications import create_notification

logger = logging.getLogger(__name__)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
CATEGORY = "operations_health"
SOURCE = "operations_health"
SEVERITY = "warning"
FEED_SYMBOL = "SPY"

INTRADAY_IMPORT_MAX_SECONDS = 60
SPY_BAR_MAX_AGE_SECONDS = 5 * 60
RECONCILIATION_MAX_AGE_SECONDS = 7 * 60
RECOVERY_NON_TRADABLE_MAX_SECONDS = 15 * 60
TRADABLE_RECOVERY_STATUSES = frozenset({"armed", "resumable"})

INTRADAY_IMPORT_RUN_KEY = "operations:intraday_market_data_import:last_run"
INTRADAY_IMPORT_RUN_TTL_SECONDS = 24 * 60 * 60

# Recovery events that end a pause (the recovery state is tradable again).
_RESUME_ACTIONS = frozenset({"resume", "automatic_restore"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _naive_utc(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)


def _session(now: datetime) -> Optional[tuple[datetime, datetime]]:
    bounds = session_bounds(now.astimezone(ET).date())
    if bounds is None:
        return None
    return _utc(bounds[0]), _utc(bounds[1])


# ----------------------------------------------------------------- import timing

def record_intraday_import_run(*, started_at: datetime, finished_at: datetime, status: str) -> bool:
    """Remember the newest intraday import duration (advisory, never raises)."""
    payload = {
        "started_at": _utc(started_at).isoformat(),
        "finished_at": _utc(finished_at).isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        "status": status,
    }
    try:
        client = _redis()
        try:
            client.set(INTRADAY_IMPORT_RUN_KEY, json.dumps(payload), ex=INTRADAY_IMPORT_RUN_TTL_SECONDS)
        finally:
            client.close()
        return True
    except Exception as exc:
        logger.warning("Intraday import timing not recorded: error_type=%s", type(exc).__name__)
        return False


def latest_intraday_import_run() -> Optional[dict]:
    try:
        client = _redis()
        try:
            raw = client.get(INTRADAY_IMPORT_RUN_KEY)
        finally:
            client.close()
        value = json.loads(raw) if raw else None
        return value if isinstance(value, dict) and "duration_seconds" in value else None
    except Exception:
        return None


# ----------------------------------------------------------------- conditions

def _feed_provider() -> str:
    from app.services.intraday_data import MARKET_DATA_PROVIDER

    return MARKET_DATA_PROVIDER


def _latest_completed_bar_close(db: Session, symbol: str, now: datetime) -> Optional[datetime]:
    row = (
        db.query(IntradayBar.opened_at)
        .filter(
            IntradayBar.symbol == symbol,
            IntradayBar.provider == _feed_provider(),
            IntradayBar.timeframe == "1m",
            IntradayBar.opened_at <= now - timedelta(minutes=1),
        )
        .order_by(IntradayBar.opened_at.desc())
        .first()
    )
    return _utc(row[0]) + timedelta(minutes=1) if row else None


def _intraday_feed_condition(db: Session, now: datetime, in_session: bool) -> dict:
    run = latest_intraday_import_run()
    duration = run.get("duration_seconds") if run else None
    slow = isinstance(duration, (int, float)) and duration > INTRADAY_IMPORT_MAX_SECONDS
    bar_close = _latest_completed_bar_close(db, FEED_SYMBOL, now)
    bar_age = (now - bar_close).total_seconds() if bar_close else None
    stale = in_session and (bar_age is None or bar_age > SPY_BAR_MAX_AGE_SECONDS)
    problems = []
    if slow:
        problems.append(f"newest intraday import took {duration:.0f}s (limit {INTRADAY_IMPORT_MAX_SECONDS}s)")
    if stale:
        problems.append(
            f"newest completed {FEED_SYMBOL} bar is "
            + (f"{bar_age / 60:.1f} min old" if bar_age is not None else "missing")
            + f" (limit {SPY_BAR_MAX_AGE_SECONDS // 60} min)"
        )
    return {
        "key": "intraday_feed",
        "active": bool(problems),
        "title": "Intraday feed degraded",
        "message": "; ".join(problems) or "Intraday import and SPY bars are current.",
        "details": {
            "import_duration_seconds": duration,
            "import_finished_at": run.get("finished_at") if run else None,
            "import_timing_known": run is not None,
            "spy_bar_close": bar_close.isoformat() if bar_close else None,
            "spy_bar_age_seconds": round(bar_age, 1) if bar_age is not None else None,
            "provider": _feed_provider(),
        },
    }


def _reconciliation_condition(db: Session, now: datetime) -> dict:
    from app.services.stock_paper_ledger import active_paper_account

    account = active_paper_account(db)
    if account is None:
        return {"key": "broker_reconciliation", "active": None, "title": "Broker reconciliation stale",
                "message": "No initialized stock paper account.", "details": {}}
    last = _utc(account.last_reconciled_at) if account.last_reconciled_at else None
    age = (now - last).total_seconds() if last else None
    active = age is None or age > RECONCILIATION_MAX_AGE_SECONDS
    return {
        "key": "broker_reconciliation",
        "active": active,
        "title": "Broker reconciliation stale",
        "message": (
            "Stock paper account has never been reconciled." if last is None
            else f"Last broker reconciliation was {age / 60:.1f} min ago (limit {RECONCILIATION_MAX_AGE_SECONDS // 60} min)."
            if active else "Broker reconciliation is current."
        ),
        "details": {
            "account_id": account.id,
            "last_reconciled_at": last.isoformat() if last else None,
            "age_seconds": round(age, 1) if age is not None else None,
        },
    }


def _latest_pause_at(db: Session) -> Optional[datetime]:
    row = (
        db.query(StockPaperRecoveryEvent.created_at)
        .filter(StockPaperRecoveryEvent.action == "pause")
        .order_by(StockPaperRecoveryEvent.id.desc())
        .first()
    )
    return _utc(row[0]) if row and row[0] else None


def _recovery_condition(db: Session, now: datetime, session: Optional[tuple[datetime, datetime]]) -> dict:
    state = db.get(StockPaperRecoveryState, 1)
    status = state.status if state else "armed"
    tradable = status in TRADABLE_RECOVERY_STATUSES
    since = None
    session_seconds = 0.0
    if not tradable:
        since = _latest_pause_at(db) or (_utc(state.updated_at) if state and state.updated_at else now)
        if session is not None:
            start = max(since, session[0])
            end = min(now, session[1])
            session_seconds = max(0.0, (end - start).total_seconds())
    active = not tradable and session_seconds > RECOVERY_NON_TRADABLE_MAX_SECONDS
    return {
        "key": "recovery_not_tradable",
        "active": active,
        "title": "Paper bot paused during the session",
        "message": (
            f"Recovery state is '{status}' for {session_seconds / 60:.0f} session minutes"
            f" (limit {RECOVERY_NON_TRADABLE_MAX_SECONDS // 60} min): {(state.pause_reason if state else None) or 'no reason recorded'}"
            if active else f"Recovery state is '{status}'."
        ),
        "details": {
            "status": status,
            "non_tradable_since": since.isoformat() if since else None,
            "non_tradable_session_seconds": round(session_seconds, 1),
            "pause_reason": state.pause_reason if state else None,
        },
    }


def evaluate_operations_health(db: Session, *, now: Optional[datetime] = None) -> dict:
    now = _utc(now or datetime.now(UTC))
    session = _session(now)
    in_session = bool(session and session[0] <= now < session[1])
    conditions = [
        _intraday_feed_condition(db, now, in_session),
        _reconciliation_condition(db, now),
        _recovery_condition(db, now, session),
    ]
    return {"generated_at": now.isoformat(), "in_session": in_session, "conditions": conditions}


# ----------------------------------------------------------------- notifications

def _open_notifications(db: Session) -> dict[str, Notification]:
    rows = (
        db.query(Notification)
        .filter(
            Notification.category == CATEGORY,
            Notification.source == SOURCE,
            Notification.status != "resolved",
        )
        .order_by(Notification.id)
        .all()
    )
    found: dict[str, Notification] = {}
    for row in rows:
        key = (row.payload or {}).get("condition")
        if key and key not in found:
            found[key] = row
    return found


def run_operations_health_alerts(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Raise, refresh or resolve one warning notification per condition."""
    evaluation = evaluate_operations_health(db, now=now)
    now = datetime.fromisoformat(evaluation["generated_at"])
    stamp = _naive_utc(now)
    existing = _open_notifications(db)
    actions: dict[str, str] = {}
    for condition in evaluation["conditions"]:
        key = condition["key"]
        notification = existing.get(key)
        payload = {"condition": key, "details": condition["details"], "paper_only": True,
                   "checked_at": evaluation["generated_at"]}
        if condition["active"] is None:
            actions[key] = "unknown"
        elif condition["active"]:
            if notification is None:
                create_notification(
                    db, category=CATEGORY, severity=SEVERITY, source=SOURCE,
                    title=condition["title"], message=condition["message"],
                    entity_type="operations", payload=payload,
                )
                actions[key] = "raised"
            else:
                notification.message = condition["message"]
                notification.payload = payload | {
                    "first_raised_at": (notification.payload or {}).get("first_raised_at")
                    or (notification.created_at.isoformat() if notification.created_at else None),
                }
                notification.updated_at = stamp
                actions[key] = "refreshed"
        elif notification is not None:
            notification.status = "resolved"
            notification.acknowledged_at = notification.acknowledged_at or stamp
            notification.resolved_at = stamp
            notification.updated_at = stamp
            notification.payload = (notification.payload or {}) | {"resolved_detail": condition["message"]}
            actions[key] = "resolved"
        else:
            actions[key] = "clear"
    db.commit()
    active = [item["key"] for item in evaluation["conditions"] if item["active"]]
    return {
        "status": "complete",
        "in_session": evaluation["in_session"],
        "active_conditions": active,
        "actions": actions,
        "conditions": evaluation["conditions"],
        "paper_only": True,
    }


# ----------------------------------------------------------------- daily summary

def _pause_intervals(db: Session, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Intervals inside [start, end) where recovery was paused."""
    relevant = (StockPaperRecoveryEvent.action == "pause") | StockPaperRecoveryEvent.action.in_(_RESUME_ACTIONS)
    prior = (
        db.query(StockPaperRecoveryEvent)
        .filter(relevant, StockPaperRecoveryEvent.created_at < start)
        .order_by(StockPaperRecoveryEvent.created_at.desc(), StockPaperRecoveryEvent.id.desc())
        .first()
    )
    events = (
        db.query(StockPaperRecoveryEvent)
        .filter(relevant, StockPaperRecoveryEvent.created_at >= start,
                StockPaperRecoveryEvent.created_at < end)
        .order_by(StockPaperRecoveryEvent.created_at, StockPaperRecoveryEvent.id)
        .all()
    )
    paused_since = start if prior is not None and not _is_resume(prior) else None
    intervals = []
    for event in events:
        moment = _utc(event.created_at)
        if event.action == "pause":
            paused_since = paused_since or moment
        elif _is_resume(event) and paused_since is not None:
            intervals.append((paused_since, moment))
            paused_since = None
    if paused_since is not None:
        intervals.append((paused_since, end))
    return intervals


def _is_resume(event: StockPaperRecoveryEvent) -> bool:
    if event.action == "resume":
        return event.status == "revalidated"
    return event.action == "automatic_restore" and event.status in TRADABLE_RECOVERY_STATUSES


def _feed_coverage(db: Session, start: datetime, end: datetime) -> dict:
    minutes = int((end - start).total_seconds() // 60)
    if minutes <= 0:
        return {"symbol": FEED_SYMBOL, "session_minutes": 0, "minutes_with_bar": 0, "ready_share": None}
    rows = (
        db.query(IntradayBar.opened_at)
        .filter(
            IntradayBar.symbol == FEED_SYMBOL,
            IntradayBar.provider == _feed_provider(),
            IntradayBar.timeframe == "1m",
            IntradayBar.opened_at >= start,
            IntradayBar.opened_at < end,
        )
        .all()
    )
    covered = {_utc(row[0]).replace(second=0, microsecond=0) for row in rows}
    return {
        "symbol": FEED_SYMBOL,
        "provider": _feed_provider(),
        "session_minutes": minutes,
        "minutes_with_bar": len(covered),
        "ready_share": round(min(1.0, len(covered) / minutes), 4),
        "method": "Share of elapsed regular-session minutes with a stored completed 1m bar.",
    }


def daily_operations_summary(db: Session, *, now: Optional[datetime] = None,
                             day: Optional[date] = None) -> dict:
    """Pause minutes/reasons, watchdog pauses and SPY feed coverage for one ET day."""
    now = _utc(now or datetime.now(UTC))
    day = day or now.astimezone(ET).date()
    day_start = datetime(day.year, day.month, day.day, tzinfo=ET).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    pauses = (
        db.query(StockPaperRecoveryEvent)
        .filter(
            StockPaperRecoveryEvent.action == "pause",
            StockPaperRecoveryEvent.created_at >= day_start,
            StockPaperRecoveryEvent.created_at < min(day_end, now + timedelta(seconds=1)),
        )
        .order_by(StockPaperRecoveryEvent.id)
        .all()
    )
    reasons = Counter((row.reason or "").strip()[:240] for row in pauses)
    actors = Counter(row.actor for row in pauses)
    bounds = session_bounds(day)
    session: dict[str, Any] = {"is_session": bounds is not None}
    paused_minutes = 0.0
    coverage = None
    if bounds is not None:
        start, close = _utc(bounds[0]), _utc(bounds[1])
        end = min(close, now)
        session |= {"open": start.isoformat(), "close": close.isoformat(), "elapsed_through": end.isoformat()}
        if end > start:
            for begin, finish in _pause_intervals(db, start, end):
                paused_minutes += max(0.0, (min(finish, end) - max(begin, start)).total_seconds()) / 60
            coverage = _feed_coverage(db, start, end)
        else:
            coverage = _feed_coverage(db, start, start)
    return {
        "date": day.isoformat(),
        "session": session,
        "paused_session_minutes": round(paused_minutes, 1),
        "pause_count": len(pauses),
        "pause_reasons": [{"reason": reason, "count": count} for reason, count in reasons.most_common()],
        "pauses_by_actor": dict(actors.most_common()),
        "watchdog_pauses": actors.get("stock_watchdog", 0),
        "intraday_feed": coverage,
        "paper_only": True,
    }
