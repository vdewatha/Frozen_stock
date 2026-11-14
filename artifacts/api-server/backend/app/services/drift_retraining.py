"""Drift-triggered challenger retraining (paper only).

The calendar retrain (``scheduled_stock_challenger_retraining_job``, daily at
18:30 New York) bounds how stale the paper model can get.  This module makes a
persistent ``model.*`` monitoring breach (prediction-distribution drift,
input-feature drift, calibration or realised-performance degradation) start a
governed learning cycle at once, with trigger ``drift``.  The cycle itself is
unchanged: fresh holdout, challenger training, validation gates, forward paper
trial, promotion gates.  Nothing here touches the active binding; demotion
stays with ``stock_monitoring``.

Guard rails (derived from the cycle rows and ``drift_retrain`` audit rows; no
migration, no new table):

- one drift cycle at a time: no new drift cycle while one is open;
- a cooldown (``drift_retrain_cooldown_hours``) after a drift cycle ends,
  multiplied by ``drift_retrain_backoff_factor`` ** (consecutive failures - 1)
  after repeated failures (a promotion resets the count);
- a breach episode a promoted drift cycle already answered never re-triggers;
- a daily cap (``drift_retrain_max_per_day``, New York calendar day);
- a cycle is created only inside a regular session, at least 15 minutes after
  the open and more than 30 minutes before the close.  The cycle preflight
  passes only on a live verified feed (``verified_feed`` is ``unknown`` out of
  session, and nothing retries a preflight-deferred cycle) and the dataset
  must be captured on its cutoff day to stay binding-eligible, so a cycle
  created after the close would never train.  Outside the window the trigger
  is evaluated and recorded, and the cycle waits for the next window;
- the operator pause of scheduled learning pauses drift retraining too.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog, StockLearningCycle, StockMonitoringBreach, StockMonitoringSnapshot, StockTrainingJob
from app.services.audit import write_audit_log
from app.services.intraday_data import NY, next_regular_session_open, session_bounds
from app.services.notifications import create_notification
from app.services.stock_monitoring import MONITOR_KEY

UTC = timezone.utc
EVENT = "drift_retrain"
ACTOR = "drift_retraining"
TRIGGER = "drift"
MODEL_BREACH_PREFIX = "model."
# A cycle needs the live verified feed (preflight) and a same-day dataset
# capture (binding eligibility); give the feed time to settle after the open
# and leave the close alone.
OPEN_OFFSET = timedelta(minutes=15)
CLOSE_MARGIN = timedelta(minutes=30)
# Monitoring evidence older than this is not a reason to retrain: the monitor
# runs every five minutes while the stack is up.
EVIDENCE_MAX_AGE = timedelta(hours=6)
# A breach row and the snapshot it belongs to are written in the same
# evaluation; anything further apart describes a different run.
SNAPSHOT_BREACH_TOLERANCE = timedelta(minutes=5)
MAX_BACKOFF_EXPONENT = 6
# An open cycle older than this is treated as abandoned so one stuck cycle
# cannot block drift retraining forever (a trial runs at most 20 sessions).
OPEN_CYCLE_MAX_AGE = timedelta(days=45)
FAILED_CYCLE_STATUSES = {"demoted", "rolled_back", "failed", "complete"}
TERMINAL_CYCLE_STATUSES = {"complete", "promoted", "demoted", "rolled_back", "failed"}
ENDED_OUTCOMES = {"promoted", "failed", "not_evaluated", "abandoned"}
RECENT_CYCLES = 50


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    value = _utc(value)
    return value.isoformat() if value else None


def _day_start(now: datetime) -> datetime:
    return now.astimezone(NY).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


# ------------------------------------------------------------------ session window

def trigger_window(now: datetime) -> dict:
    """Whether a drift cycle may be created now, and when the next window opens."""
    now = _utc(now)
    bounds = session_bounds(now.astimezone(NY).date())
    if bounds is not None:
        start, end = _utc(bounds[0]) + OPEN_OFFSET, _utc(bounds[1]) - CLOSE_MARGIN
        if start <= now < end:
            return {"open": True, "next_at": None, "closes_at": end.isoformat(),
                    "reason": "inside the regular session creation window"}
        if now < start:
            return {"open": False, "next_at": start.isoformat(), "closes_at": None,
                    "reason": "before the regular session creation window"}
    next_open = next_regular_session_open(now) + OPEN_OFFSET
    return {"open": False, "next_at": next_open.isoformat(), "closes_at": None,
            "reason": "outside the regular session; cycle creation needs the live verified feed"}


# ------------------------------------------------------------------ trigger evidence

def _breach_projection(row: StockMonitoringBreach) -> dict:
    return {
        "key": row.breach_key, "status": row.status, "consecutive_count": int(row.consecutive_count or 0),
        "severity": row.severity, "first_observed_at": _iso(row.first_observed_at),
        "last_observed_at": _iso(row.last_observed_at),
        "observed_value": row.observed_value or {}, "threshold": row.threshold or {},
    }


def episode_key(row: StockMonitoringBreach) -> str:
    """One breach episode: the key plus the start of its consecutive run."""
    started = _utc(row.first_observed_at) or _utc(row.last_observed_at)
    return f"{row.breach_key}@{started.isoformat() if started else 'unknown'}"


def evaluate_drift_trigger(db: Session, *, now: datetime | None = None) -> dict:
    """Read the latest monitoring snapshot and the ``model.*`` breach states.

    The trigger fires when a model breach is ``persistent`` (the monitor marks
    the second consecutive observation persistent, so "observed twice in a
    row" is the same condition) and belongs to the latest snapshot.
    """
    now = _utc(now) or _now()
    snapshot = db.scalar(
        select(StockMonitoringSnapshot).where(StockMonitoringSnapshot.monitor_key == MONITOR_KEY)
        .order_by(StockMonitoringSnapshot.generated_at.desc(), StockMonitoringSnapshot.id.desc())
        .limit(1)  # one row, not every snapshot ever recorded (live_safety._latest_monitoring_snapshot)
    )
    result = {
        "triggered": False, "breach_keys": [], "episodes": [], "breaches": [],
        "snapshot_id": snapshot.id if snapshot else None,
        "snapshot_generated_at": _iso(snapshot.generated_at) if snapshot else None,
        "reason": None,
    }
    if snapshot is None:
        result["reason"] = "no monitoring snapshot has been recorded"
        return result
    generated_at = _utc(snapshot.generated_at)
    rows = db.scalars(
        select(StockMonitoringBreach).where(StockMonitoringBreach.breach_key.like(f"{MODEL_BREACH_PREFIX}%"))
        .order_by(StockMonitoringBreach.breach_key)
    ).all()
    result["breaches"] = [_breach_projection(row) for row in rows]
    if now - generated_at > EVIDENCE_MAX_AGE:
        result["reason"] = f"monitoring evidence is stale (snapshot {generated_at.isoformat()})"
        return result
    persistent = [
        row for row in rows
        if (row.status == "persistent" or (row.status == "observed" and int(row.consecutive_count or 0) >= 2))
        and _utc(row.last_observed_at) is not None
        and abs(_utc(row.last_observed_at) - generated_at) <= SNAPSHOT_BREACH_TOLERANCE
    ]
    if not persistent:
        result["reason"] = "no persistent model monitoring breach in the latest snapshot"
        return result
    result.update({
        "triggered": True,
        "breach_keys": sorted(row.breach_key for row in persistent),
        "episodes": sorted(episode_key(row) for row in persistent),
        "reason": "persistent model monitoring breach: " + ", ".join(sorted(row.breach_key for row in persistent)),
    })
    return result


# ------------------------------------------------------------------ cycle outcomes

def _lineage(cycle: StockLearningCycle) -> dict:
    return ((cycle.evidence or {}).get("request") or {}).get("lineage") or {}


def cycle_outcome(cycle: StockLearningCycle, *, now: datetime) -> str | None:
    """``None`` while the cycle is still open; otherwise how it ended."""
    from app.services.stock_learning_cycle import _blocked_only_on_approval

    if cycle.status == "promoted":
        return "promoted"
    if cycle.status in FAILED_CYCLE_STATUSES:
        return "failed"
    if cycle.status == "blocked" and not _blocked_only_on_approval(cycle):
        return "failed" if cycle.model_run_id else "not_evaluated"
    if cycle.status == "deferred" and cycle.stage in {"preflight", "training"} and not cycle.model_run_id:
        # Nothing retries a preflight or training deferral; the cycle is dead.
        return "not_evaluated"
    created_at = _utc(cycle.created_at)
    if created_at is not None and now - created_at > OPEN_CYCLE_MAX_AGE:
        return "abandoned"
    return None


def _cycle_projection(cycle: StockLearningCycle, outcome: str | None) -> dict:
    return {
        "cycle_id": cycle.cycle_id, "status": cycle.status, "stage": cycle.stage, "outcome": outcome,
        "created_at": _iso(cycle.created_at), "updated_at": _iso(cycle.updated_at),
        "last_reason": cycle.last_reason, "breach_keys": list(_lineage(cycle).get("breach_keys") or []),
        "monitoring_snapshot_id": _lineage(cycle).get("monitoring_snapshot_id"),
    }


def _drift_cycles(db: Session) -> list[StockLearningCycle]:
    return db.scalars(
        select(StockLearningCycle).where(StockLearningCycle.trigger == TRIGGER)
        .order_by(StockLearningCycle.created_at.desc(), StockLearningCycle.cycle_id.desc()).limit(RECENT_CYCLES)
    ).all()


def _last_schedule_block(db: Session) -> AuditLog | None:
    return db.scalar(
        select(AuditLog).where(AuditLog.event_type == EVENT, AuditLog.action == "schedule_blocked")
        .order_by(AuditLog.id.desc())
    )


def backoff_delay(consecutive_failures: int) -> timedelta:
    """Cooldown × factor^(failures - 1): the first failure waits one cooldown."""
    base = timedelta(hours=settings.drift_retrain_cooldown_hours)
    exponent = min(max(int(consecutive_failures) - 1, 0), MAX_BACKOFF_EXPONENT)
    return base * (settings.drift_retrain_backoff_factor ** exponent)


def guard_state(db: Session, *, now: datetime, trigger: dict | None = None) -> dict:
    """Everything the guard rails need, from the drift cycles and audit rows."""
    now = _utc(now)
    cycles = _drift_cycles(db)
    outcomes = [(cycle, cycle_outcome(cycle, now=now)) for cycle in cycles]
    open_cycles = [cycle for cycle, outcome in outcomes if outcome is None]
    ended = [(cycle, outcome) for cycle, outcome in outcomes if outcome is not None]
    consecutive_failures = 0
    for _cycle, outcome in ended:  # newest first
        if outcome == "failed":
            consecutive_failures += 1
        elif outcome == "promoted":
            break
    cooldown_until = None
    cooldown_reason = None
    if ended:
        last_cycle, last_outcome = ended[0]
        ended_at = _utc(last_cycle.updated_at) or _utc(last_cycle.created_at)
        delay = backoff_delay(consecutive_failures) if last_outcome == "failed" else timedelta(
            hours=settings.drift_retrain_cooldown_hours)
        cooldown_until = ended_at + delay
        cooldown_reason = (
            f"drift cycle {last_cycle.cycle_id[:12]} ended {last_outcome} ({last_cycle.status}); "
            f"{'back-off after ' + str(consecutive_failures) + ' consecutive failure(s)' if last_outcome == 'failed' else 'cooldown'}"
            f" until {cooldown_until.isoformat()}"
        )
    blocked = _last_schedule_block(db)
    if blocked is not None:
        blocked_until = _utc(blocked.created_at) + timedelta(hours=settings.drift_retrain_cooldown_hours)
        if cooldown_until is None or blocked_until > cooldown_until:
            cooldown_until = blocked_until
            cooldown_reason = (f"cycle creation was blocked at {_iso(blocked.created_at)} "
                               f"({(blocked.payload or {}).get('reason')}); retry after {blocked_until.isoformat()}")
    day_start = _day_start(now)
    cycles_today = [cycle for cycle in cycles if (_utc(cycle.created_at) or now) >= day_start]
    episodes = set((trigger or {}).get("episodes") or [])
    answered = set()
    for cycle, outcome in ended:
        if outcome == "promoted":
            answered |= episodes & set(_lineage(cycle).get("episodes") or [])
    return {
        "open_cycle": _cycle_projection(open_cycles[0], None) if open_cycles else None,
        "last_cycle": _cycle_projection(ended[0][0], ended[0][1]) if ended else None,
        "consecutive_failures": consecutive_failures,
        "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
        "cooldown_active": bool(cooldown_until and now < cooldown_until),
        "cooldown_reason": cooldown_reason,
        "cycles_today": len(cycles_today),
        "max_per_day": settings.drift_retrain_max_per_day,
        "answered_episodes": sorted(answered),
        "unanswered_episodes": sorted(episodes - answered),
    }


# ------------------------------------------------------------------ decisions

def _last_decision(db: Session) -> AuditLog | None:
    return db.scalar(
        select(AuditLog).where(AuditLog.event_type == EVENT, AuditLog.action.in_(("evaluate", "schedule_cycle")))
        .order_by(AuditLog.id.desc())
    )


def _decision_projection(row: AuditLog | None) -> dict | None:
    if row is None:
        return None
    payload = row.payload or {}
    return {
        "action": row.action, "status": row.status, "reason": payload.get("reason"), "at": _iso(row.created_at),
        "cycle_id": payload.get("cycle_id"), "breach_keys": payload.get("breach_keys") or [],
        "monitoring_snapshot_id": payload.get("monitoring_snapshot_id"),
        "next_window_at": payload.get("next_window_at"), "cooldown_until": payload.get("cooldown_until"),
    }


def _record_decision(db: Session, *, now: datetime, status: str, reason: str, trigger: dict, **extra) -> None:
    """One ``evaluate`` row per distinct decision while a breach stands.

    The job runs every 15 minutes; repeating the same skip reason every tick
    would bury the ledger, so a decision is written only when the status,
    reason or breach set differs from the latest recorded decision.
    """
    last = _last_decision(db)
    if last is not None and last.action == "evaluate" and last.status == status:
        payload = last.payload or {}
        if payload.get("reason") == reason and payload.get("breach_keys") == trigger["breach_keys"]:
            return
    write_audit_log(
        db, event_type=EVENT, action="evaluate", status=status,
        message=f"Drift retraining {status}: {reason}", entity_type="stock_monitoring",
        entity_id=trigger.get("snapshot_id"),
        payload={"reason": reason, "breach_keys": trigger["breach_keys"], "episodes": trigger["episodes"],
                 "monitoring_snapshot_id": trigger.get("snapshot_id"), "at": now.isoformat(),
                 "paper_only": True, "live_authorized": False, **extra},
    )


def _seed(snapshot_id: int | None, breach_keys: list[str], today: date) -> int:
    digest = hashlib.sha256(f"drift:{snapshot_id}:{','.join(breach_keys)}:{today.isoformat()}".encode()).hexdigest()
    return int(digest[:8], 16) % 2_147_483_647


def _start_cycle(db: Session, *, now: datetime, trigger: dict, state: dict) -> dict:
    from app.services.stock_learning_cycle import StockTrainingError, create_learning_cycle
    from app.services.stock_training_jobs import enqueue_stock_training_job

    today = now.astimezone(NY).date()
    symbols = list(settings.stock_learning_default_symbols)
    lineage = {
        "source": ACTOR, "trigger": TRIGGER, "monitoring_snapshot_id": trigger["snapshot_id"],
        "monitoring_snapshot_generated_at": trigger["snapshot_generated_at"],
        "breach_keys": trigger["breach_keys"], "episodes": trigger["episodes"],
        "breaches": [breach for breach in trigger["breaches"] if breach["key"] in trigger["breach_keys"]],
        "triggered_at": now.isoformat(), "attempt": state["consecutive_failures"] + 1,
    }
    try:
        cycle, duplicate = create_learning_cycle(
            db, symbols=symbols, cutoff_at=today, horizon_days=5,
            provider=settings.stock_learning_default_provider, actor=ACTOR, trigger=TRIGGER,
            seed=_seed(trigger["snapshot_id"], trigger["breach_keys"], today), lineage=lineage,
        )
    except StockTrainingError as exc:
        reason = f"Governed drift cycle could not be created: {exc}"
        write_audit_log(
            db, event_type=EVENT, action="schedule_blocked", status="blocked", message=reason,
            entity_type="stock_monitoring", entity_id=trigger.get("snapshot_id"),
            payload={"reason": str(exc), "breach_keys": trigger["breach_keys"], "episodes": trigger["episodes"],
                     "monitoring_snapshot_id": trigger["snapshot_id"], "at": now.isoformat(),
                     "paper_only": True, "live_authorized": False},
        )
        return {"status": "blocked", "reason": reason, "cycle_id": None}
    reason = (f"Governed drift cycle {cycle.status} at {cycle.stage} after persistent "
              f"{', '.join(trigger['breach_keys'])} (fresh holdout, new challenger, forward trial, promotion gates)")
    write_audit_log(
        db, event_type=EVENT, action="schedule_cycle", status="scheduled", message=reason,
        entity_type="stock_learning_cycle", entity_id=trigger.get("snapshot_id"),
        payload={"reason": reason, "cycle_id": cycle.cycle_id, "cycle_status": cycle.status, "cycle_stage": cycle.stage,
                 "deduplicated": duplicate, "breach_keys": trigger["breach_keys"], "episodes": trigger["episodes"],
                 "monitoring_snapshot_id": trigger["snapshot_id"], "attempt": lineage["attempt"],
                 "symbols": symbols, "cutoff_date": today.isoformat(), "at": now.isoformat(),
                 "paper_only": True, "live_authorized": False},
    )
    create_notification(
        db, category="stock_learning_cycle", severity="info", source=ACTOR,
        title="Drift-triggered challenger cycle scheduled",
        message=reason, entity_type="stock_learning_cycle",
        payload={"cycle_id": cycle.cycle_id, "breach_keys": trigger["breach_keys"],
                 "monitoring_snapshot_id": trigger["snapshot_id"], "paper_only": True,
                 "dedupe_key": f"drift_retrain:scheduled:{cycle.cycle_id}"},
    )
    db.flush()
    if cycle.training_job_id and not duplicate and cycle.status == "queued":
        job = db.get(StockTrainingJob, cycle.training_job_id)
        if job:
            enqueue_stock_training_job(db, job)
    return {"status": "scheduled", "reason": reason, "cycle_id": cycle.cycle_id,
            "cycle_status": cycle.status, "cycle_stage": cycle.stage, "deduplicated": duplicate}


def run_drift_retraining(db: Session, *, now: datetime | None = None) -> dict:
    """Evaluate the drift trigger and its guard rails; create at most one cycle."""
    from app.services.stock_learning_cycle import scheduled_learning_control_projection

    now = _utc(now) or _now()
    base = {"paper_only": True, "live_authorized": False, "evaluated_at": now.isoformat(), "cycle_id": None}
    if not settings.drift_retrain_enabled:
        db.commit()
        return {**base, "status": "disabled", "reason": "drift retraining is disabled (drift_retrain_enabled)"}
    control = scheduled_learning_control_projection(db)
    if control.get("paused"):
        db.commit()
        return {**base, "status": "paused", "reason": control.get("pause_reason") or "scheduled learning is paused"}
    trigger = evaluate_drift_trigger(db, now=now)
    if not trigger["triggered"]:
        db.commit()
        return {**base, "status": "clear", "reason": trigger["reason"], "trigger": trigger}
    state = guard_state(db, now=now, trigger=trigger)
    window = trigger_window(now)
    result: dict
    if state["open_cycle"]:
        open_cycle = state["open_cycle"]
        reason = f"drift cycle {open_cycle['cycle_id'][:12]} is still open ({open_cycle['status']} at {open_cycle['stage']})"
        _record_decision(db, now=now, status="waiting", reason=reason, trigger=trigger, cycle_id=open_cycle["cycle_id"])
        result = {"status": "waiting", "reason": reason, "cycle_id": open_cycle["cycle_id"]}
    elif not state["unanswered_episodes"]:
        reason = "every current breach episode was already answered by a promoted drift cycle"
        _record_decision(db, now=now, status="answered", reason=reason, trigger=trigger)
        result = {"status": "answered", "reason": reason}
    elif state["cooldown_active"]:
        reason = state["cooldown_reason"]
        _record_decision(db, now=now, status="cooldown", reason=reason, trigger=trigger,
                         cooldown_until=state["cooldown_until"])
        result = {"status": "cooldown", "reason": reason, "cooldown_until": state["cooldown_until"]}
    elif state["cycles_today"] >= settings.drift_retrain_max_per_day:
        reason = (f"daily cap reached: {state['cycles_today']} drift cycle(s) today, "
                  f"drift_retrain_max_per_day={settings.drift_retrain_max_per_day}")
        _record_decision(db, now=now, status="capped", reason=reason, trigger=trigger)
        result = {"status": "capped", "reason": reason}
    elif not window["open"]:
        reason = f"{window['reason']}; next creation window opens {window['next_at']}"
        _record_decision(db, now=now, status="deferred", reason=reason, trigger=trigger, next_window_at=window["next_at"])
        result = {"status": "deferred", "reason": reason, "next_window_at": window["next_at"]}
    else:
        result = _start_cycle(db, now=now, trigger=trigger, state=state)
    db.commit()
    return {**base, **result, "trigger": {key: trigger[key] for key in ("breach_keys", "episodes", "snapshot_id",
                                                                         "snapshot_generated_at")},
            "guards": {key: state[key] for key in ("consecutive_failures", "cooldown_until", "cycles_today",
                                                   "max_per_day")}}


def drift_retraining_projection(db: Session, *, now: datetime | None = None) -> dict:
    """Read-only state for the learning-cycle status route."""
    now = _utc(now) or _now()
    trigger = evaluate_drift_trigger(db, now=now)
    state = guard_state(db, now=now, trigger=trigger)
    return {
        "enabled": bool(settings.drift_retrain_enabled),
        "policy": {
            "cooldown_hours": settings.drift_retrain_cooldown_hours,
            "max_per_day": settings.drift_retrain_max_per_day,
            "backoff_factor": settings.drift_retrain_backoff_factor,
            "window": {"open_offset_minutes": int(OPEN_OFFSET.total_seconds() // 60),
                       "close_margin_minutes": int(CLOSE_MARGIN.total_seconds() // 60)},
        },
        "trigger": {key: trigger[key] for key in ("triggered", "breach_keys", "episodes", "snapshot_id",
                                                   "snapshot_generated_at", "reason", "breaches")},
        "window": trigger_window(now),
        **{key: state[key] for key in ("open_cycle", "last_cycle", "consecutive_failures", "cooldown_until",
                                       "cooldown_active", "cycles_today", "max_per_day", "answered_episodes")},
        "last_decision": _decision_projection(_last_decision(db)),
        "paper_only": True,
        "live_authorized": False,
    }
