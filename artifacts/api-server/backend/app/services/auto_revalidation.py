"""Automatic paper re-arm after a system pause clears.

The watchdog, monitor and recovery automation pause paper trading on stale
heartbeats, monitoring breaches and uncertain broker evidence. Each pause used
to wait for an operator click on "Resume paper recovery" even after every gate
had cleared, which left the bot idle for whole sessions. The owner asked for a
self-sufficient bot, so this job runs the same operator revalidation
(``resume_stock_paper_after_revalidation``, with all of its gates) once:

* the newest pause came from a system actor, not an operator;
* nobody enabled the kill switch by hand after that pause;
* the cooldown has passed and monitoring evidence is fresh and clear; and
* fewer than ``paper_auto_revalidation_max_per_day`` automatic re-arms ran in
  the last 24 hours (a pause loop then waits for a human and raises a warning).

It is paper-only and never runs while live trading is allowed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog, Notification, StockPaperRecoveryEvent
from app.services.notifications import create_notification
from app.services.stock_paper_ledger import StockPaperError
from app.services.stock_recovery import (
    AUTOMATIC_RECOVERY_ACTOR,
    _fresh_monitoring_is_clear,
    _state,
    _utc,
    resume_stock_paper_after_revalidation,
)

UTC = timezone.utc
AUTO_REVALIDATION_ACTOR = "stock_auto_revalidation"
SYSTEM_PAUSE_ACTORS = frozenset({"stock_watchdog", "stock_monitor", AUTOMATIC_RECOVERY_ACTOR})
WAITING_STATUSES = frozenset({"cooldown", "revalidation_required", "paused"})
BUDGET_CATEGORY = "paper_auto_revalidation"


def _budget_notice(db: Session, count: int) -> None:
    open_notice = db.query(Notification).filter(
        Notification.category == BUDGET_CATEGORY, Notification.status != "resolved",
    ).first()
    if open_notice is None:
        create_notification(
            db,
            category=BUDGET_CATEGORY,
            severity="warning",
            source="auto_revalidation",
            title="Paper bot paused repeatedly; automatic re-arm is waiting for review",
            message=(
                f"{count} automatic re-arms ran in the last 24 hours. The bot stays paused "
                "until someone resumes it from Risk & Safety, Recovery."
            ),
            commit=False,
        )


def run_auto_revalidation(db: Session, *, now: datetime | None = None) -> dict:
    now = _utc(now or datetime.now(UTC))
    if not settings.paper_auto_revalidation_enabled or settings.allow_live_trading:
        return {"status": "disabled"}
    state = _state(db, for_update=False)
    if state.status not in WAITING_STATUSES:
        return {"status": "not_paused", "recovery_status": state.status}
    if state.cooldown_until and _utc(state.cooldown_until) > now:
        return {"status": "cooldown", "until": _utc(state.cooldown_until).isoformat()}
    pause = (
        db.query(StockPaperRecoveryEvent)
        .filter(StockPaperRecoveryEvent.action == "pause")
        .order_by(StockPaperRecoveryEvent.id.desc())
        .first()
    )
    if pause is None or pause.actor not in SYSTEM_PAUSE_ACTORS:
        return {"status": "operator_hold", "reason": "The newest pause was not a system pause"}
    paused_at = _utc(pause.created_at) if pause.created_at else None
    manual_kill = db.query(AuditLog).filter(
        AuditLog.event_type == "safety_control",
        AuditLog.action == "set_kill_switch",
        AuditLog.status == "enabled",
        *([AuditLog.created_at > paused_at] if paused_at else []),
    ).first()
    if manual_kill is not None:
        return {"status": "operator_hold", "reason": "The kill switch was enabled by hand after the pause"}
    since = now - timedelta(hours=24)
    recent = db.query(StockPaperRecoveryEvent).filter(
        StockPaperRecoveryEvent.action == "resume",
        StockPaperRecoveryEvent.actor == AUTO_REVALIDATION_ACTOR,
        StockPaperRecoveryEvent.created_at >= since,
    ).count()
    if recent >= settings.paper_auto_revalidation_max_per_day:
        _budget_notice(db, recent)
        db.commit()
        return {"status": "budget_exhausted", "recent_auto_resumes": recent}
    # Check monitoring first: a refused resume records a preflight event, and
    # this job runs every two minutes.
    clear, reason = _fresh_monitoring_is_clear(db, now)
    if not clear:
        return {"status": "waiting", "reason": reason}
    try:
        status = resume_stock_paper_after_revalidation(
            db,
            actor=AUTO_REVALIDATION_ACTOR,
            reason=f"Automatic revalidation after system pause: {(pause.reason or '')[:200]}",
        )
    except StockPaperError as exc:
        db.rollback()
        return {"status": "blocked", "reason": str(exc)}
    return {"status": "resumed", "recovery_status": status.get("status")}
