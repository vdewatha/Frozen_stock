"""Governed re-admission of strategies suspended for edge decay.

Suspension stays the only automatic action on a strategy's own permission.
Re-admission never un-pauses the suspended strategy: after a quarantine it
schedules a brand-new governed learning cycle (fresh holdout, new challenger,
new forward paper trial).  Only a cycle that passes every normal gate and is
promoted counts as re-admission.  A cycle that trains a challenger but ends
without promotion is a failed attempt; attempts back off exponentially and
the strategy is retired after ``strategy_readmission_max_attempts``.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    StockLearningCycle,
    StockPaperTrial,
    StockStrategyReadmission,
    StockTrainingJob,
    Strategy,
)
from app.models.stock_paper import StockPaperStrategyEvidence
from app.services.audit import write_audit_log
from app.services.notifications import create_notification

ACTOR = "strategy_readmission"
NOT_EVALUATED_RETRY = timedelta(days=1)
# A not-evaluated retry lands this long after the next regular-session open,
# so a cycle blocked by an out-of-session preflight retries inside a session.
SESSION_RETRY_OFFSET = timedelta(minutes=15)
FAILED_CYCLE_STATUSES = {"demoted", "rolled_back", "failed", "complete"}
TERMINAL_STATES = {"readmitted", "retired"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _quarantine() -> timedelta:
    return timedelta(hours=settings.strategy_readmission_quarantine_hours)


def not_evaluated_retry_at(now: datetime) -> datetime:
    """Retry a never-evaluated cycle inside the next NYSE regular session.

    Retrying a fixed 24 hours later would keep missing the session when the
    first attempt happened outside market hours.  The retry is at least one
    hour away so an in-session block does not spin.
    """
    from app.services.intraday_data import next_regular_session_open

    return next_regular_session_open(now + timedelta(hours=1)) + SESSION_RETRY_OFFSET


def backoff_delay(attempts: int) -> timedelta:
    """Quarantine × factor^attempts (attempts = failed attempts so far)."""
    return _quarantine() * (settings.strategy_readmission_backoff_factor ** max(attempts, 0))


def _history(row: StockStrategyReadmission, event: str, now: datetime, **details) -> None:
    row.history = [*(row.history or []), {"event": event, "at": now.isoformat(), **details}][-50:]


def _suspended_strategies(db: Session) -> list[tuple[Strategy, StockPaperStrategyEvidence]]:
    rows = db.execute(
        select(Strategy, StockPaperStrategyEvidence)
        .join(StockPaperStrategyEvidence, StockPaperStrategyEvidence.strategy_id == Strategy.id)
        .where(StockPaperStrategyEvidence.status == "suspended", Strategy.current_status == "paused")
    ).all()
    return [(strategy, evidence) for strategy, evidence in rows
            if str((evidence.provenance or {}).get("suspension_policy", "")).startswith("edge-decay")]


def _strategy_symbols(db: Session, strategy_id: int) -> list[str]:
    trial = db.scalar(select(StockPaperTrial).where(StockPaperTrial.strategy_id == strategy_id)
                      .order_by(StockPaperTrial.created_at.desc()))
    if trial is not None:
        cycle = db.scalar(select(StockLearningCycle).where(StockLearningCycle.trial_id == trial.id))
        if cycle is not None and cycle.symbols:
            return list(cycle.symbols)
    return list(settings.stock_learning_default_symbols)


def _seed(strategy_id: int, attempt: int, today: date) -> int:
    digest = hashlib.sha256(f"readmission:{strategy_id}:{attempt}:{today.isoformat()}".encode()).hexdigest()
    return int(digest[:8], 16) % 2_147_483_647


def ensure_readmission_rows(db: Session, *, now: datetime) -> list[StockStrategyReadmission]:
    created = []
    for strategy, evidence in _suspended_strategies(db):
        if db.scalar(select(StockStrategyReadmission).where(StockStrategyReadmission.strategy_id == strategy.id)):
            continue
        suspended_raw = (evidence.provenance or {}).get("suspended_at")
        try:
            suspended_at = _utc(datetime.fromisoformat(suspended_raw)) if suspended_raw else now
        except ValueError:
            suspended_at = now
        row = StockStrategyReadmission(
            strategy_id=strategy.id, status="quarantined", suspended_at=suspended_at, attempts=0,
            next_attempt_at=suspended_at + _quarantine(), history=[],
            last_reason="Quarantined after edge-decay suspension",
        )
        _history(row, "quarantined", now, next_attempt_at=row.next_attempt_at.isoformat())
        db.add(row)
        created.append(row)
    if created:
        db.flush()
    return created


def _start_cycle(db: Session, row: StockStrategyReadmission, now: datetime) -> None:
    from app.services.stock_learning_cycle import StockTrainingError, create_learning_cycle
    from app.services.stock_training_jobs import enqueue_stock_training_job

    today = now.date()
    try:
        cycle, duplicate = create_learning_cycle(
            db, symbols=_strategy_symbols(db, row.strategy_id), cutoff_at=today, horizon_days=5,
            provider=settings.stock_learning_default_provider, actor=ACTOR, trigger="scheduled",
            seed=_seed(row.strategy_id, row.attempts, today),
        )
    except StockTrainingError as exc:
        row.status = "waiting"
        row.next_attempt_at = not_evaluated_retry_at(now)
        row.last_reason = f"Governed cycle could not be created: {exc}"
        _history(row, "create_blocked", now, reason=str(exc))
        return
    row.current_cycle_id = cycle.cycle_id
    row.status = "cycle_scheduled"
    row.next_attempt_at = None
    row.last_reason = "Governed re-admission cycle scheduled (fresh holdout, new challenger, new trial)"
    _history(row, "cycle_scheduled", now, cycle_id=cycle.cycle_id, deduplicated=duplicate)
    write_audit_log(db, event_type="strategy_readmission", action="schedule_cycle", status="complete",
                    message=row.last_reason, entity_type="strategy", entity_id=row.strategy_id,
                    payload={"cycle_id": cycle.cycle_id, "attempt": row.attempts + 1, "paper_only": True,
                             "live_authorized": False})
    db.flush()
    if cycle.training_job_id and not duplicate and cycle.status == "queued":
        job = db.get(StockTrainingJob, cycle.training_job_id)
        if job:
            enqueue_stock_training_job(db, job)


def _record_failure(db: Session, row: StockStrategyReadmission, cycle: StockLearningCycle, now: datetime) -> None:
    row.attempts = int(row.attempts or 0) + 1
    row.current_cycle_id = None
    reason = f"Re-admission cycle ended {cycle.status} without promotion: {cycle.last_reason or 'no reason'}"
    _history(row, "attempt_failed", now, cycle_id=cycle.cycle_id, cycle_status=cycle.status, attempt=row.attempts)
    if row.attempts >= settings.strategy_readmission_max_attempts:
        row.status = "retired"
        row.next_attempt_at = None
        row.last_reason = f"Retired after {row.attempts} failed re-admission attempt(s). {reason}"
        strategy = db.get(Strategy, row.strategy_id)
        old_status = strategy.current_status if strategy else None
        if strategy is not None:
            strategy.current_status = "retired"
        _history(row, "retired", now)
        write_audit_log(db, event_type="strategy_readmission", action="retire", status="complete",
                        message=row.last_reason, entity_type="strategy", entity_id=row.strategy_id,
                        payload={"old_status": old_status, "attempts": row.attempts, "paper_only": True})
        create_notification(db, category="strategy_governance", severity="warning", source=ACTOR,
                            title="Strategy retired after failed re-admission", message=row.last_reason,
                            entity_type="strategy", entity_id=row.strategy_id,
                            payload={"attempts": row.attempts, "dedupe_key": f"readmission:retired:{row.strategy_id}"})
        return
    row.status = "backoff"
    row.next_attempt_at = now + backoff_delay(row.attempts)
    row.last_reason = f"{reason}; next attempt after {row.next_attempt_at.isoformat()}"
    write_audit_log(db, event_type="strategy_readmission", action="attempt_failed", status="complete",
                    message=row.last_reason, entity_type="strategy", entity_id=row.strategy_id,
                    payload={"cycle_id": cycle.cycle_id, "attempts": row.attempts, "paper_only": True})


def _advance(db: Session, row: StockStrategyReadmission, now: datetime) -> None:
    from app.services.stock_learning_cycle import _blocked_only_on_approval

    if row.current_cycle_id:
        cycle = db.get(StockLearningCycle, row.current_cycle_id)
        if cycle is None:
            row.current_cycle_id = None
            row.status = "waiting"
            row.next_attempt_at = now
            return
        if cycle.status == "promoted":
            row.status = "readmitted"
            row.successor_cycle_id = cycle.cycle_id
            row.current_cycle_id = None
            row.last_reason = "Successor cycle passed every gate and was promoted"
            _history(row, "readmitted", now, cycle_id=cycle.cycle_id)
            write_audit_log(db, event_type="strategy_readmission", action="readmit", status="complete",
                            message=row.last_reason, entity_type="strategy", entity_id=row.strategy_id,
                            payload={"cycle_id": cycle.cycle_id, "paper_only": True, "live_authorized": False})
            return
        if cycle.status in {"blocked", "deferred"} and not cycle.model_run_id:
            # Never evaluated (prerequisites, serialization, data): not a strategy failure.
            row.current_cycle_id = None
            row.status = "waiting"
            row.next_attempt_at = not_evaluated_retry_at(now)
            row.last_reason = f"Cycle {cycle.status} before training: {cycle.last_reason or 'no reason'}"
            _history(row, "not_evaluated", now, cycle_id=cycle.cycle_id, cycle_status=cycle.status)
            return
        if cycle.status in FAILED_CYCLE_STATUSES or (
            cycle.status == "blocked" and not _blocked_only_on_approval(cycle)
        ):
            _record_failure(db, row, cycle, now)
            return
        row.status = "cycle_running"
        row.last_reason = f"Re-admission cycle is {cycle.status} at {cycle.stage}"
        return
    if row.next_attempt_at is not None and _utc(row.next_attempt_at) <= now:
        _start_cycle(db, row, now)


def run_strategy_readmission(db: Session, *, now: datetime | None = None) -> dict:
    from app.services.stock_learning_cycle import scheduled_learning_control_projection

    now = now or _now()
    ensure_readmission_rows(db, now=now)
    control = scheduled_learning_control_projection(db)
    if control.get("paused"):
        db.commit()
        return {"status": "paused", "reason": control.get("pause_reason")}
    rows = db.scalars(select(StockStrategyReadmission).where(
        StockStrategyReadmission.status.not_in(TERMINAL_STATES)).order_by(StockStrategyReadmission.id)).all()
    for row in rows:
        _advance(db, row, now)
    db.commit()
    return {"status": "complete", "job": "strategy_readmission_job",
            "readmissions": [readmission_projection(row) for row in rows],
            "paper_only": True, "live_authorized": False}


def readmission_projection(row: StockStrategyReadmission) -> dict:
    return {
        "strategy_id": row.strategy_id, "status": row.status, "attempts": row.attempts,
        "max_attempts": settings.strategy_readmission_max_attempts,
        "suspended_at": _utc(row.suspended_at).isoformat() if row.suspended_at else None,
        "next_attempt_at": _utc(row.next_attempt_at).isoformat() if row.next_attempt_at else None,
        "current_cycle_id": row.current_cycle_id, "successor_cycle_id": row.successor_cycle_id,
        "last_reason": row.last_reason, "history": row.history or [],
    }


def readmission_by_strategy(db: Session) -> dict[int, dict]:
    return {row.strategy_id: readmission_projection(row)
            for row in db.scalars(select(StockStrategyReadmission)).all()}


def readmission_policy() -> dict:
    return {
        "quarantine_hours": settings.strategy_readmission_quarantine_hours,
        "backoff_factor": settings.strategy_readmission_backoff_factor,
        "max_attempts": settings.strategy_readmission_max_attempts,
    }
