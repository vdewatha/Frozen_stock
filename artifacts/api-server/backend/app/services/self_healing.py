"""Self-healing: find the routine stalls the bot already knows and fix the safe ones.

Every 15 minutes (``self_healing_job`` on the ``learning_fast`` queue) this
checks the stalls that used to wait for a person, acts where a fix is safe
and records what it found and did as ``self_healing`` audit rows, so the
weekly report and ``GET /stock-paper/self-healing`` can say it in plain
language. No migration: the ledger is the audit log.

Cases with a safe fix (each with a cooldown and a daily cap; past the cap the
job raises a warning instead of acting):

* ``meta_label_artifact``: the meta-label model file is missing (a Replit
  publish ships without it; shadow mode fails open until the 20:15 retrain).
  Off hours, enqueue ``meta_label_retrain_job`` now.
* ``stale_intraday_symbols``: during the session some traded symbols have no
  fresh minute bar while SPY is current (the minutely import ran out of its
  repair budget). Re-run the backfill for those symbols only, inside the
  existing repair time budget.
* ``expired_learning_lease``: a learning scope is ``running`` but its worker
  lease expired without a result, and the minutely recovery job (on the
  ``learning`` queue, behind the long jobs) has not requeued it. Requeue it.

Cases that stay with the existing automation or a person are reported only:
a learning lane starved by long jobs, a heartbeat pause after a publish
(restart grace, the transient-pause restore and auto-revalidation own the
re-arm) and monitoring pauses.

Nothing here touches orders, exits, the kill switch, risk settings or the
recovery/resume logic. Paper only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    AuditLog,
    IntradayBar,
    Notification,
    StockLearningWorkerDispatch,
    StockPaperRecoveryEvent,
    StockPaperRecoveryState,
)
from app.services.audit import write_audit_log
from app.services.exchange_sessions import session_bounds
from app.services.notifications import create_notification

logger = logging.getLogger(__name__)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
EVENT = "self_healing"
ACTION_RUN = "run"
CATEGORY = "self_healing"
SOURCE = "self_healing"
SEVERITY = "warning"
SCHEDULE_TEXT = "every 15 minutes, 07:00-20:45 New York time on weekdays, on the learning_fast queue"
NEVER_TOUCHES = ("orders", "exits", "the kill switch", "risk settings", "recovery and resume logic")

# One case's outcome in one run.
CLEAR = "clear"            # nothing found
REMEDIATED = "remediated"  # found and fixed (or the fix was enqueued)
FAILED = "failed"          # found; the fix raised
COOLDOWN = "cooldown"      # found; the case acted recently, so it waits
CAPPED = "capped"          # found; today's cap is spent, so a warning was raised instead
WAITING = "waiting"        # found; the fix is not safe right now (for example during the session)
MANUAL = "manual"          # found; no safe automatic fix, reported only
ACTED = frozenset({REMEDIATED, FAILED})
RECORDED = frozenset({REMEDIATED, FAILED, CAPPED})

STALE_BAR_MINUTES = 15
OPEN_GRACE_MINUTES = 20
MAX_STALE_SYMBOLS_PER_RUN = 10
LEASE_GRACE = timedelta(minutes=5)
MAX_LEASE_ATTEMPTS = 3
MAX_LEASES_PER_RUN = 16
NEWS_EVENT = "news_symbol_discovery"
NEWS_LAG_MINUTES = 75
SYSTEM_PAUSE_ACTORS = ("stock_watchdog", "stock_monitor")


@dataclass
class Finding:
    key: str
    found: bool
    detail: str
    actionable: bool = False
    wait: bool = False  # found, fixable, but not right now
    targets: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Case:
    key: str
    title: str
    detect: Callable[[Session, datetime], Finding]
    remedy: str
    fix: Optional[Callable[[Session, Finding, datetime], dict]] = None
    cooldown: timedelta = timedelta(minutes=30)
    daily_cap: int = 0
    notify: bool = False  # keep one warning open while a reported-only finding persists


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _naive(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def _session(now: datetime) -> Optional[tuple[datetime, datetime]]:
    bounds = session_bounds(now.astimezone(ET).date())
    return None if bounds is None else (_utc(bounds[0]), _utc(bounds[1]))


def _in_session(now: datetime) -> bool:
    session = _session(now)
    return bool(session and session[0] <= now < session[1])


def _day_start(now: datetime) -> datetime:
    return now.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _minutes(seconds: Optional[float]) -> Optional[float]:
    return None if seconds is None else round(seconds / 60, 1)


# ------------------------------------------------------------ meta-label artifact

def detect_meta_label_artifact(db: Session, now: datetime) -> Finding:
    from app.services.meta_label_shadow import model_path

    key = "meta_label_artifact"
    mode = str(settings.paper_meta_label_mode or "off").strip().lower()
    if mode not in {"shadow", "enforce"}:
        return Finding(key, False, "the meta-label filter is off")
    path = model_path()
    if path.exists() and Path(f"{path}.json").exists():
        return Finding(key, False, f"the meta-label model is present ({path.name})")
    consequence = ("shadow mode fails open, so no BUY is scored" if mode == "shadow"
                   else "enforce mode skips every BUY")
    detail = f"the meta-label model is missing at {path} (a publish ships without it): {consequence}"
    evidence = {"model_path": str(path), "mode": mode}
    if not settings.meta_label_retrain_enabled:
        return Finding(key, True, detail + "; self-retraining is disabled, so it stays missing until someone "
                       "retrains or publishes the file", evidence=evidence)
    session = _session(now)
    if session and session[0] - timedelta(minutes=30) <= now < session[1]:
        return Finding(key, True, detail + "; the retrain is deferred until the close because model training "
                       "competes with the session's import and signal pass", wait=True,
                       evidence=evidence | {"retry_after": session[1].isoformat()})
    return Finding(key, True, detail, actionable=True, evidence=evidence)


def fix_meta_label_artifact(db: Session, finding: Finding, now: datetime) -> dict:
    from app.tasks.jobs import meta_label_retrain_job

    task = meta_label_retrain_job.apply_async(expires=6 * 60 * 60)
    return {"done": "enqueued the meta-label retrain on the learning queue instead of waiting for the 20:15 run",
            "task_id": task.id}


# ------------------------------------------------------------ stale intraday symbols

def _traded_symbols() -> list[str]:
    """The symbols the paper bot trades this session: the execution universe plus news-admitted names."""
    from app.services.trading_universe import discovered_symbols, execution_symbols

    return sorted(set(execution_symbols()) | set(discovered_symbols()))


def detect_stale_intraday_symbols(db: Session, now: datetime) -> Finding:
    from app.services.intraday_data import MARKET_DATA_PROVIDER
    from app.services.operations_health import FEED_SYMBOL, SPY_BAR_MAX_AGE_SECONDS

    key = "stale_intraday_symbols"
    session = _session(now)
    if not session or not (session[0] <= now < session[1]):
        return Finding(key, False, "outside the regular session; the post-close repair covers gaps")
    if now < session[0] + timedelta(minutes=OPEN_GRACE_MINUTES):
        return Finding(key, False, f"the first {OPEN_GRACE_MINUTES} minutes of the session; the feed is still filling")
    symbols = _traded_symbols()
    if not symbols:
        return Finding(key, False, "no traded symbols configured")
    latest = dict(
        db.query(IntradayBar.symbol, func.max(IntradayBar.opened_at))
        .filter(IntradayBar.symbol.in_(sorted(set(symbols) | {FEED_SYMBOL})), IntradayBar.timeframe == "1m",
                IntradayBar.provider == MARKET_DATA_PROVIDER)
        .group_by(IntradayBar.symbol).all()
    )

    def age(symbol: str) -> Optional[float]:
        stamp = latest.get(symbol)
        return None if stamp is None else (now - _utc(stamp)).total_seconds()

    spy_age = age(FEED_SYMBOL)
    if spy_age is None or spy_age > SPY_BAR_MAX_AGE_SECONDS + 60:
        return Finding(key, True, (f"{FEED_SYMBOL} itself has "
                       + ("no minute bar" if spy_age is None else f"no minute bar for {spy_age / 60:.0f} minutes")
                       + ": the feed or the provider is down, not single symbols; operations health alerts it "
                         "and a targeted backfill would not help"),
                       evidence={"spy_bar_age_minutes": _minutes(spy_age), "provider": MARKET_DATA_PROVIDER})
    stale = [(symbol, age(symbol)) for symbol in symbols
             if symbol != FEED_SYMBOL and (age(symbol) is None or age(symbol) > STALE_BAR_MINUTES * 60)]
    if not stale:
        return Finding(key, False, f"all {len(symbols)} traded symbols have a minute bar from the last "
                       f"{STALE_BAR_MINUTES} minutes")
    stale.sort(key=lambda item: (-(item[1] if item[1] is not None else float("inf")), item[0]))
    targets = [symbol for symbol, _ in stale[:MAX_STALE_SYMBOLS_PER_RUN]]
    more = len(stale) - len(targets)
    detail = (f"{len(stale)} traded symbols have no minute bar from the last {STALE_BAR_MINUTES} minutes while "
              f"{FEED_SYMBOL} is current (the minutely import ran out of its repair budget): {', '.join(targets)}"
              + (f" and {more} more" if more else ""))
    evidence = {"stale": [{"symbol": symbol, "minutes_since_bar": _minutes(seconds)} for symbol, seconds in stale],
                "spy_bar_age_minutes": _minutes(spy_age), "provider": MARKET_DATA_PROVIDER}
    return Finding(key, True, detail, actionable=True, targets=targets, evidence=evidence)


def fix_stale_intraday_symbols(db: Session, finding: Finding, now: datetime) -> dict:
    from app.services.intraday_data import ingest_intraday

    budget = float(settings.intraday_backfill_time_budget_seconds)
    result = ingest_intraday(db, list(finding.targets), now=now, backfill_time_budget_seconds=budget)
    db.commit()
    statuses = {row["symbol"]: row["status"] for row in result.get("results") or []}
    complete = sorted(symbol for symbol, status in statuses.items() if status == "complete")
    return {"done": (f"re-ran the intraday backfill for {', '.join(finding.targets)} within the {budget:.0f}-second "
                     f"repair budget; {len(complete)} of {len(finding.targets)} came back complete"),
            "statuses": statuses, "complete": complete,
            "requests_remaining": result.get("backfill_requests_remaining")}


# ------------------------------------------------------------ expired learning leases

def detect_expired_learning_leases(db: Session, now: datetime) -> Finding:
    key = "expired_learning_lease"
    rows = (db.query(StockLearningWorkerDispatch)
            .filter(StockLearningWorkerDispatch.status == "running",
                    StockLearningWorkerDispatch.lease_expires_at.is_not(None))
            .order_by(StockLearningWorkerDispatch.lease_expires_at, StockLearningWorkerDispatch.id).limit(64).all())
    expired = [row for row in rows if _utc(row.lease_expires_at) <= now - LEASE_GRACE]
    if not expired:
        return Finding(key, False, "no learning scope is running past its lease")
    exhausted = [row for row in expired if int(row.attempt_count or 0) >= MAX_LEASE_ATTEMPTS]
    retry = [row for row in expired if row not in exhausted][:MAX_LEASES_PER_RUN]

    def scope(row) -> dict:
        return {"dispatch_id": row.id, "scope": f"{row.symbol}/{row.strategy_slug}",
                "attempts": int(row.attempt_count or 0), "task_id": row.task_id,
                "lease_expired_at": _utc(row.lease_expires_at).isoformat()}

    names = ", ".join(f"{row.symbol}/{row.strategy_slug}" for row in expired[:8])
    detail = (f"{len(expired)} learning scopes are marked running but their worker lease expired more than "
              f"{int(LEASE_GRACE.total_seconds() // 60)} minutes ago without a result, and the minutely recovery "
              f"job has not requeued them (it waits on the learning queue behind the long jobs): {names}")
    evidence = {"expired": [scope(row) for row in expired], "exhausted": [row.id for row in exhausted]}
    if not retry:
        return Finding(key, True, detail + f"; each already ran {MAX_LEASE_ATTEMPTS} times, so they wait for the "
                       "next learning batch instead of another requeue", evidence=evidence)
    return Finding(key, True, detail, actionable=True, targets=[row.id for row in retry], evidence=evidence)


def fix_expired_learning_leases(db: Session, finding: Finding, now: datetime) -> dict:
    from app.tasks.jobs import strategy_learning_scope_job

    requeued, failed = [], []
    for dispatch_id in finding.targets:
        row = db.get(StockLearningWorkerDispatch, dispatch_id)
        if row is None or row.status != "running":
            continue  # the recovery job or a late worker got there first
        if row.lease_expires_at and _utc(row.lease_expires_at) > now:
            continue  # reclaimed by a live worker
        try:
            # Same steps as recover_unpublished_strategy_learning_scopes_job:
            # the scope's claim fences a duplicate delivery.
            row.status = "failed"
            row.error_type = "lease_expired"
            row.error_message = "Learning worker lease expired before completion; requeued by self-healing"
            row.lease_expires_at = None
            db.commit()
            task = strategy_learning_scope_job.apply_async(
                args=[row.symbol, row.strategy_slug, row.max_candidates, row.id], queue="learning",
            )
            db.refresh(row)
            if row.status in {"planned", "failed"}:
                row.task_id = task.id
                row.status = "published"
                row.claimed_at = None
                row.lease_expires_at = None
                row.error_type = None
                row.error_message = None
            elif not row.task_id:
                row.task_id = task.id
            db.commit()
            requeued.append({"dispatch_id": row.id, "scope": f"{row.symbol}/{row.strategy_slug}", "task_id": task.id})
        except Exception as exc:
            db.rollback()
            failed.append({"dispatch_id": dispatch_id, "error": f"{exc.__class__.__name__}: {exc}"})
    if failed and not requeued:
        raise RuntimeError(f"requeue failed for {len(failed)} scopes: {failed[0]['error']}")
    scopes = ", ".join(item["scope"] for item in requeued)
    return {"done": f"requeued {len(requeued)} learning scopes on the learning queue ({scopes})"
                    + (f"; {len(failed)} could not be requeued" if failed else ""),
            "requeued": requeued, "failed": failed}


# ------------------------------------------------------------ reported-only cases

def detect_learning_lane_starved(db: Session, now: datetime) -> Finding:
    key = "learning_lane_starved"
    if not settings.news_discovery_enabled:
        return Finding(key, False, "news discovery is off, so the fast lane has no heartbeat to check")
    if _session(now) is None:
        return Finding(key, False, "not a trading day")
    local = now.astimezone(ET)
    first_due = local.replace(hour=8, minute=10, second=0, microsecond=0) + timedelta(minutes=NEWS_LAG_MINUTES)
    last_due = local.replace(hour=15, minute=40, second=0, microsecond=0) + timedelta(minutes=NEWS_LAG_MINUTES)
    if not first_due <= local <= last_due:
        return Finding(key, False, "outside the news-discovery window")
    latest = db.query(AuditLog).filter(AuditLog.event_type == NEWS_EVENT).order_by(AuditLog.id.desc()).first()
    age = None if latest is None else (now - _utc(latest.created_at)).total_seconds() / 60
    if age is not None and age <= NEWS_LAG_MINUTES:
        return Finding(key, False, f"news discovery ran {age:.0f} minutes ago; the fast learning lane is served")
    detail = ("news discovery (every 30 minutes on the learning_fast lane) has not run "
              + ("at all" if latest is None else f"for {age:.0f} minutes")
              + ": the fast learning lane is not being served. Enqueueing more work would not help; the learning "
                "worker must be up and consuming learning,learning_fast")
    return Finding(key, True, detail, evidence={"minutes_since_news_run": None if age is None else round(age, 1)})


def detect_heartbeat_pause(db: Session, now: datetime) -> Finding:
    key = "heartbeat_pause"
    state = db.get(StockPaperRecoveryState, 1)
    if state is None or state.status in {"armed", "resumable"}:
        return Finding(key, False, "paper trading is tradable")
    reason = state.pause_reason or ""
    if "heartbeat" not in reason.lower():
        return Finding(key, False, f"recovery state is '{state.status}' for a reason other than heartbeats")
    detail = (f"paper trading is '{state.status}' after a heartbeat pause: {reason.strip()}. Restart grace, the "
              "transient-pause restore and auto-revalidation own the re-arm; self-healing never resumes trading")
    return Finding(key, True, detail, evidence={"status": state.status, "pause_reason": reason})


def detect_monitor_pauses(db: Session, now: datetime) -> Finding:
    key = "monitor_pauses"
    rows = (db.query(StockPaperRecoveryEvent)
            .filter(StockPaperRecoveryEvent.action == "pause",
                    StockPaperRecoveryEvent.actor.in_(SYSTEM_PAUSE_ACTORS),
                    StockPaperRecoveryEvent.created_at >= _day_start(now))
            .order_by(StockPaperRecoveryEvent.id).all())
    if not rows:
        return Finding(key, False, "no watchdog or monitor pause today")
    reasons = sorted({(row.reason or "no reason recorded").strip()[:120] for row in rows})
    detail = (f"{len(rows)} pause(s) today by the watchdog or monitor: {'; '.join(reasons[:3])}. Monitoring checks "
              "and the kill switch stay outside self-healing; the off-hours deferral and auto-revalidation handle "
              "the known false alarms")
    return Finding(key, True, detail, evidence={"pauses": len(rows), "reasons": reasons})


CASES: tuple[Case, ...] = (
    Case("meta_label_artifact", "Meta-label model present", detect_meta_label_artifact,
         "off hours, enqueue the meta-label retrain so the filter scores BUYs again", fix_meta_label_artifact,
         cooldown=timedelta(hours=6), daily_cap=2),
    Case("stale_intraday_symbols", "Traded symbols get minute bars", detect_stale_intraday_symbols,
         "re-run the intraday backfill for the stale symbols only, within the existing repair time budget",
         fix_stale_intraday_symbols, cooldown=timedelta(minutes=20), daily_cap=12),
    Case("expired_learning_lease", "Learning scopes finish or are requeued", detect_expired_learning_leases,
         "requeue the scope on the learning queue (same steps as the minutely recovery job)",
         fix_expired_learning_leases, cooldown=timedelta(minutes=30), daily_cap=20),
    Case("learning_lane_starved", "Fast learning lane is served", detect_learning_lane_starved,
         "none: the learning_fast lane is the fix; if it is not consumed the worker itself is down", notify=True),
    Case("heartbeat_pause", "No heartbeat pause after a publish", detect_heartbeat_pause,
         "none: restart grace, the transient-pause restore and auto-revalidation re-arm; never resumed here"),
    Case("monitor_pauses", "Monitoring did not pause trading", detect_monitor_pauses,
         "none: monitoring checks and the kill switch are never touched by self-healing"),
)
CASE_BY_KEY = {case.key: case for case in CASES}


# ------------------------------------------------------------ budget, ledger, notices

def _acted_rows(db: Session, key: str, since: datetime, limit: Optional[int] = None) -> list[AuditLog]:
    query = (db.query(AuditLog)
             .filter(AuditLog.event_type == EVENT, AuditLog.action == key, AuditLog.status.in_(ACTED),
                     AuditLog.created_at >= _naive(since))
             .order_by(AuditLog.id.desc()))
    return query.limit(limit).all() if limit else query.all()


def budget(db: Session, case: Case, now: datetime) -> dict:
    """How many times the case acted today, and when it may act again."""
    today = _acted_rows(db, case.key, _day_start(now))
    last = (db.query(AuditLog)
            .filter(AuditLog.event_type == EVENT, AuditLog.action == case.key, AuditLog.status.in_(ACTED))
            .order_by(AuditLog.id.desc()).first())
    cooldown_until = _utc(last.created_at) + case.cooldown if last else None
    return {
        "actions_today": len(today),
        "daily_cap": case.daily_cap,
        "cooldown_minutes": int(case.cooldown.total_seconds() // 60),
        "last_action_at": _utc(last.created_at).isoformat() if last else None,
        "cooldown_until": cooldown_until.isoformat() if cooldown_until and cooldown_until > now else None,
    }


def _sentence(case: Case, outcome: dict) -> str:
    found, done = outcome["found"], outcome.get("done")
    status = outcome["status"]
    if status == REMEDIATED:
        return f"Self-healing found {found}; it {done}."
    if status == FAILED:
        return f"Self-healing found {found}; its fix failed: {done}."
    if status == CAPPED:
        return (f"Self-healing found {found} but did not act: today's cap of {case.daily_cap} for "
                f"'{case.title}' is spent, so it raised a warning instead.")
    if status == COOLDOWN:
        return f"Self-healing found {found} but waits: {done}."
    if status == WAITING:
        return f"Self-healing found {found}; it will act when that is safe."
    if status == MANUAL:
        return f"Self-healing found {found}; there is no safe automatic fix, so it is reported only."
    return f"Self-healing checked '{case.title}': {found}."


def handle(db: Session, case: Case, finding: Finding, now: datetime) -> dict:
    """Decide and (when allowed) act on one finding; returns the outcome dict."""
    outcome = {"case": case.key, "title": case.title, "status": CLEAR, "found": finding.detail,
               "evidence": finding.evidence or None, "done": None, "result": None}
    if not finding.found:
        return outcome
    if not finding.actionable:
        outcome["status"] = WAITING if finding.wait else MANUAL
        return outcome
    limits = budget(db, case, now)
    outcome["budget"] = limits
    if limits["actions_today"] >= case.daily_cap:
        outcome["status"] = CAPPED
        outcome["done"] = f"not acted: the daily cap of {case.daily_cap} is spent; raised a warning instead"
        return outcome
    if limits["cooldown_until"]:
        outcome["status"] = COOLDOWN
        outcome["done"] = (f"it acted at {limits['last_action_at']} and the cooldown is "
                           f"{limits['cooldown_minutes']} minutes")
        return outcome
    try:
        result = case.fix(db, finding, now)
        outcome["status"] = REMEDIATED
        outcome["done"] = result.pop("done", case.remedy)
        outcome["result"] = result
    except Exception as exc:
        db.rollback()
        logger.warning("self-healing fix failed: case=%s error_type=%s", case.key, exc.__class__.__name__)
        outcome["status"] = FAILED
        outcome["done"] = f"{exc.__class__.__name__}: {exc}"
    return outcome


def _open_notices(db: Session) -> dict[str, Notification]:
    rows = (db.query(Notification)
            .filter(Notification.category == CATEGORY, Notification.source == SOURCE, Notification.status != "resolved")
            .order_by(Notification.id).all())
    found: dict[str, Notification] = {}
    for row in rows:
        key = (row.payload or {}).get("case")
        if key and key not in found:
            found[key] = row
    return found


def _sync_notice(db: Session, case: Case, outcome: dict, message: str, now: datetime,
                 existing: dict[str, Notification]) -> Optional[str]:
    """One warning per case: raised when the job cannot act, refreshed while that persists, resolved after."""
    wanted = outcome["status"] == CAPPED or (outcome["status"] in {MANUAL, WAITING} and case.notify)
    notice = existing.get(case.key)
    stamp = _naive(now)
    payload = {"case": case.key, "status": outcome["status"], "found": outcome["found"],
               "evidence": outcome.get("evidence"), "checked_at": now.isoformat(), "paper_only": True}
    if wanted and notice is None:
        create_notification(db, category=CATEGORY, severity=SEVERITY, source=SOURCE,
                            title=f"Self-healing needs attention: {case.title}", message=message,
                            entity_type="operations", payload=payload)
        return "raised"
    if wanted:
        notice.message = message
        notice.payload = payload | {"first_raised_at": (notice.payload or {}).get("first_raised_at")
                                    or (notice.created_at.isoformat() if notice.created_at else None)}
        notice.updated_at = stamp
        return "refreshed"
    if notice is not None and outcome["status"] in {CLEAR, REMEDIATED}:
        notice.status = "resolved"
        notice.acknowledged_at = notice.acknowledged_at or stamp
        notice.resolved_at = stamp
        notice.updated_at = stamp
        notice.payload = (notice.payload or {}) | {"resolved_detail": message}
        return "resolved"
    return None


def run_self_healing(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Detect every case, act where safe, and record the run (paper only)."""
    now = _utc(now or datetime.now(UTC))
    if not settings.self_healing_enabled:
        return {"status": "disabled", "checked_at": now.isoformat(), "paper_only": True}
    existing = _open_notices(db)
    outcomes, notices = [], {}
    for case in CASES:
        try:
            finding = case.detect(db, now)
        except Exception as exc:  # one broken detector must not stop the others
            db.rollback()
            logger.warning("self-healing detector failed: case=%s error_type=%s", case.key, exc.__class__.__name__)
            finding = Finding(case.key, True, f"the check itself failed ({exc.__class__.__name__}: {exc})")
        outcome = handle(db, case, finding, now)
        outcome["message"] = _sentence(case, outcome)
        if outcome["status"] in RECORDED:
            row = write_audit_log(db, event_type=EVENT, action=case.key, status=outcome["status"],
                                  message=outcome["message"], entity_type="operations",
                                  payload={**outcome, "remedy": case.remedy, "checked_at": now.isoformat(),
                                           "paper_only": True})
            row.created_at = _naive(now)
        action = _sync_notice(db, case, outcome, outcome["message"], now, existing)
        if action:
            notices[case.key] = action
        outcomes.append(outcome)
    statuses = [item["status"] for item in outcomes]
    status = ("acted" if REMEDIATED in statuses
              else "attention" if any(item != CLEAR for item in statuses) else CLEAR)
    summary = {"status": status, "checked_at": now.isoformat(), "in_session": _in_session(now),
               "cases": outcomes, "notifications": notices, "paper_only": True, "live_authorized": False}
    found = [item["title"] for item in outcomes if item["status"] != CLEAR]
    row = write_audit_log(db, event_type=EVENT, action=ACTION_RUN, status=status, entity_type="operations",
                          message=("Self-healing found nothing to fix." if not found
                                   else "Self-healing run: " + "; ".join(
                                       f"{item['title']}: {item['status']}" for item in outcomes
                                       if item["status"] != CLEAR) + "."),
                          payload=summary)
    row.created_at = _naive(now)
    db.commit()
    return {**summary, "status": "complete", "outcome": status}


# ------------------------------------------------------------ projections

def latest_run(db: Session) -> Optional[dict]:
    row = (db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION_RUN)
           .order_by(AuditLog.id.desc()).first())
    if row is None:
        return None
    return {"at": _utc(row.created_at).isoformat(), "status": row.status, "message": row.message,
            **{key: value for key, value in (row.payload or {}).items() if key in {"cases", "in_session", "checked_at"}}}


def recent_actions(db: Session, *, limit: int = 20) -> list[dict]:
    rows = (db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action != ACTION_RUN)
            .order_by(AuditLog.id.desc()).limit(limit).all())
    return [{"at": _utc(row.created_at).isoformat(), "case": row.action, "status": row.status,
             "message": row.message, "done": (row.payload or {}).get("done")} for row in rows]


def self_healing_summary(db: Session, *, now: Optional[datetime] = None) -> dict:
    """For ``GET /stock-paper/self-healing``: what the bot fixes on its own, and what it did lately."""
    now = _utc(now or datetime.now(UTC))
    run = latest_run(db)
    latest_by_case = {item["case"]: item for item in (run or {}).get("cases") or []}
    cases = []
    for case in CASES:
        latest = latest_by_case.get(case.key)
        cases.append({
            "key": case.key, "title": case.title, "automatic": case.fix is not None, "remedy": case.remedy,
            **(budget(db, case, now) if case.fix else {"actions_today": 0, "daily_cap": 0,
                                                       "cooldown_minutes": None, "last_action_at": None,
                                                       "cooldown_until": None}),
            "latest": {"status": latest["status"], "found": latest["found"], "done": latest.get("done"),
                       "message": latest.get("message")} if latest else None,
        })
    warnings = [{"id": row.id, "case": (row.payload or {}).get("case"), "title": row.title, "message": row.message,
                 "since": _utc(row.created_at).isoformat() if row.created_at else None}
                for row in _open_notices(db).values()]
    return {"enabled": bool(settings.self_healing_enabled), "schedule": SCHEDULE_TEXT,
            "never_touches": list(NEVER_TOUCHES), "last_run": run, "cases": cases,
            "recent": recent_actions(db), "open_warnings": warnings, "paper_only": True}
