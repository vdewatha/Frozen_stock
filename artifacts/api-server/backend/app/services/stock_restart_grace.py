"""Bounded warm-up grace after a supervised (planned) process restart.

A redeploy stops every paper process at once. While the new generation boots,
the monitor and reconciliation heartbeats age, the minutely intraday import has
not run yet, and the entitlement proof (a recent authenticated ingestion) ages
out. Without a grace period the independent watchdog and the continuous
monitor read that as a safety breach and start the full pause -> cooldown ->
operator-revalidation cycle on every deploy.

This module narrows that one case and nothing else:

* Grace exists only for a boot the production supervisor marked
  (``PAPER_BOOT_ID`` + ``PAPER_WORKERS_STARTED_AT``, set once per gateway
  process), only when the previous process generation was demonstrably alive
  shortly before the restart (a watchdog heartbeat within
  ``stock_restart_grace_max_downtime_seconds``), only when recovery was not
  already paused, and at most ``stock_restart_grace_max_boots_per_hour`` times
  per rolling hour (restart-storm guard). The decision is recorded once per
  boot as an append-only ``restart_grace`` recovery event.
* Inside the window only *staleness that predates the worker start* is
  deferred: stale heartbeats last written before the restart, stale or
  incomplete execution bars, an entitlement proof that aged out across the
  restart, a reconciliation that is merely old on a reconciled account, and
  missing worker/beat evidence. Everything else (drawdown, risk, broker halt,
  reconciliation mismatch, residuals, future-dated evidence, configuration)
  is never deferred.
* While warming up the recovery status reports ``warming_up`` and launch
  admission fails, so new entries stay blocked (fail closed for new risk).
* When the window ends, findings are evaluated exactly as before: a condition
  that persists pauses the stock path through the unchanged code path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import uuid
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import StockPaperRecoveryEvent

UTC = timezone.utc
GRACE_ACTION = "restart_grace"
GRACE_ACTOR = "stock_restart_grace"
CLOCK_SKEW = timedelta(seconds=60)
# Monitoring checks whose breach can be explained by a restart. Every other
# key keeps today's behaviour even inside the grace window.
RESTART_EXPLAINABLE_KEYS = frozenset({
    "data.freshness_provenance",
    "health.reconciliation",
    "health.worker_scheduler",
})
# Feed failure classes produced by bars/entitlement proofs that simply aged
# while no importer was running. Configuration and availability failures are
# never restart-explained.
STALENESS_FEED_FAILURE_CLASSES = frozenset({"stale_data", "incomplete_data"})
ACTIVE_STATUSES = frozenset({"opened", "deferred"})


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).strip().replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def boot_identity(environ: Mapping[str, str] | None = None) -> dict | None:
    """Return the supervisor's boot marker, or ``None`` for an unmarked process.

    ``PAPER_WORKERS_STARTED_AT`` is fixed once per gateway process, so a worker
    that crashes and is respawned by the supervisor cannot reopen or extend the
    window; only a new gateway process (a deploy or VM restart) produces a new
    ``PAPER_BOOT_ID``.
    """
    env = os.environ if environ is None else environ
    boot_id = (env.get("PAPER_BOOT_ID") or "").strip()
    anchor = _parse(env.get("PAPER_WORKERS_STARTED_AT"))
    if not boot_id or anchor is None:
        return None
    return {
        "boot_id": boot_id,
        "build_id": (env.get("PAPER_BUILD_ID") or "").strip() or "unknown",
        "boot_at": _parse(env.get("PAPER_BOOT_AT")),
        "anchor": anchor,
    }


def _inactive(reason: str, **extra: Any) -> dict:
    return {"active": False, "status": "inactive", "reason": reason, **extra}


def _window_from_event(event: StockPaperRecoveryEvent, now: datetime) -> dict:
    payload = event.payload or {}
    anchor = _parse(payload.get("anchor"))
    grace_until = _parse(payload.get("grace_until"))
    active = bool(
        event.status in ACTIVE_STATUSES
        and anchor is not None
        and grace_until is not None
        and anchor - CLOCK_SKEW <= now < grace_until
    )
    status = event.status
    if event.status in ACTIVE_STATUSES and not active:
        status = "expired"
    return {
        "active": active,
        "status": status,
        "reason": event.reason,
        "boot_id": payload.get("boot_id"),
        "build_id": payload.get("build_id"),
        "anchor": anchor,
        "grace_until": grace_until,
        "event_id": event.id,
    }


def _recent_events(db: Session, limit: int = 200) -> list[StockPaperRecoveryEvent]:
    return (
        db.query(StockPaperRecoveryEvent)
        .filter(StockPaperRecoveryEvent.action == GRACE_ACTION)
        .order_by(StockPaperRecoveryEvent.id.desc())
        .limit(limit)
        .all()
    )


def _record(db: Session, *, status: str, reason: str, payload: dict, now: datetime) -> StockPaperRecoveryEvent:
    from app.services.audit import write_audit_log

    body = {**payload, "recorded_at": now.isoformat()}
    identity = {
        "action": GRACE_ACTION,
        "status": status,
        "actor": GRACE_ACTOR,
        "reason": reason,
        "payload": body,
        "nonce": uuid.uuid4().hex,
    }
    row = StockPaperRecoveryEvent(
        action=GRACE_ACTION,
        status=status,
        actor=GRACE_ACTOR,
        reason=reason,
        event_sha256=hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest(),
        payload=body,
    )
    db.add(row)
    write_audit_log(
        db,
        event_type="stock_recovery",
        action=GRACE_ACTION,
        status=status,
        message=reason,
        entity_type="stock_paper",
        payload=body,
    )
    db.flush()
    return row


def _window_payload(identity: dict, grace_until: datetime) -> dict:
    return {
        "boot_id": identity["boot_id"],
        "build_id": identity["build_id"],
        "boot_at": identity["boot_at"].isoformat() if identity["boot_at"] else None,
        "anchor": identity["anchor"].isoformat(),
        "grace_until": grace_until.isoformat(),
    }


def open_restart_grace(
    db: Session,
    *,
    now: datetime,
    recovery_status: str,
    prior_watchdog_heartbeat_at: datetime | None,
    source: str,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Return this boot's grace window, deciding it durably on first use.

    The first watchdog or monitor evaluation of a marked boot either opens or
    denies the window and records that decision; later evaluations of the same
    boot reuse it and can never widen it.
    """
    if not settings.stock_restart_grace_enabled or settings.stock_restart_grace_seconds <= 0:
        return _inactive("restart grace is disabled")
    identity = boot_identity(environ)
    if identity is None:
        return _inactive("no supervised boot marker")
    now = _utc(now)
    if now >= identity["anchor"] + timedelta(seconds=settings.stock_restart_grace_seconds):
        # Long-running processes skip the event lookup once the window is over.
        return _inactive("restart grace window has elapsed", boot_id=identity["boot_id"])
    events = _recent_events(db)
    for event in events:
        payload = event.payload or {}
        if payload.get("boot_id") == identity["boot_id"]:
            return _window_from_event(event, now)

    anchor = identity["anchor"]
    grace_until = anchor + timedelta(seconds=settings.stock_restart_grace_seconds)
    max_downtime = timedelta(seconds=settings.stock_restart_grace_max_downtime_seconds)
    prior = _utc(prior_watchdog_heartbeat_at) if prior_watchdog_heartbeat_at else None
    opened_last_hour = {
        (event.payload or {}).get("boot_id")
        for event in events
        if event.status == "opened"
        and (_parse((event.payload or {}).get("anchor")) or datetime.min.replace(tzinfo=UTC)) >= now - timedelta(hours=1)
    }
    denial = None
    if anchor > now + CLOCK_SKEW:
        denial = "Worker start marker is future-dated"
    elif recovery_status not in {"armed", "resumable"}:
        denial = f"Recovery was already {recovery_status} before the restart"
    elif prior is None:
        denial = "No watchdog heartbeat from the previous process generation; treating as an outage"
    elif prior > anchor + CLOCK_SKEW:
        denial = "Watchdog heartbeat already postdates the worker start; not a restart gap"
    elif anchor - prior > max_downtime:
        denial = "Previous process generation was silent longer than the planned-restart limit; treating as an outage"
    elif len(opened_last_hour) >= settings.stock_restart_grace_max_boots_per_hour:
        denial = "Restart-storm guard: too many restart grace windows in the last hour"
    payload = _window_payload(identity, grace_until) | {
        "source": source,
        "prior_watchdog_heartbeat_at": prior.isoformat() if prior else None,
        "recovery_status_at_boot": recovery_status,
        "grace_seconds": settings.stock_restart_grace_seconds,
    }
    if denial:
        event = _record(db, status="denied", reason=f"Restart grace denied: {denial}", payload=payload, now=now)
    else:
        event = _record(
            db,
            status="opened",
            reason=(
                "Supervised restart detected; stale heartbeat and data findings that predate "
                "the worker start are deferred and new entries are blocked until "
                f"{grace_until.isoformat()}"
            ),
            payload=payload,
            now=now,
        )
    return _window_from_event(event, now)


def record_restart_deferral(db: Session, grace: dict, *, source: str, deferred: list[str], now: datetime) -> None:
    """Record that an otherwise-pausing finding was deferred by the grace window."""
    _record(
        db,
        status="deferred",
        reason=f"{source} deferred restart-explained findings while warming up: " + "; ".join(deferred),
        payload={
            "boot_id": grace.get("boot_id"),
            "build_id": grace.get("build_id"),
            "anchor": grace["anchor"].isoformat() if grace.get("anchor") else None,
            "grace_until": grace["grace_until"].isoformat() if grace.get("grace_until") else None,
            "source": source,
            "deferred": deferred,
        },
        now=now,
    )


def record_restart_warmup_complete(db: Session, grace: dict, *, source: str, now: datetime) -> None:
    _record(
        db,
        status="complete",
        reason="Restart warm-up complete: post-restart monitoring found no stale heartbeat or data findings",
        payload={
            "boot_id": grace.get("boot_id"),
            "build_id": grace.get("build_id"),
            "anchor": grace["anchor"].isoformat() if grace.get("anchor") else None,
            "grace_until": grace["grace_until"].isoformat() if grace.get("grace_until") else None,
            "source": source,
        },
        now=now,
    )


def restart_warmup_status(db: Session, now: datetime | None = None) -> dict:
    """Read-only view used by entry gates: is the latest boot still warming up?"""
    now = _utc(now or _now())
    latest = (
        db.query(StockPaperRecoveryEvent)
        .filter(StockPaperRecoveryEvent.action == GRACE_ACTION)
        .order_by(StockPaperRecoveryEvent.id.desc())
        .first()
    )
    if latest is None:
        return _inactive("no restart grace has been recorded")
    window = _window_from_event(latest, now)
    return {
        **window,
        "anchor": window["anchor"].isoformat() if window["anchor"] else None,
        "grace_until": window["grace_until"].isoformat() if window["grace_until"] else None,
    }


def heartbeat_explained_by_restart(observed: datetime | None, grace: dict) -> bool:
    """A stale heartbeat is restart-explained only if it was last written before the restart."""
    if not grace.get("active") or observed is None or grace.get("anchor") is None:
        return False
    return _utc(observed) <= grace["anchor"] + CLOCK_SKEW


def monitoring_check_explained_by_restart(check: dict, grace: dict, account: Any) -> bool:
    if not grace.get("active") or check.get("status") != "breach":
        return False
    key = check.get("key")
    if key not in RESTART_EXPLAINABLE_KEYS:
        return False
    details = check.get("details") or {}
    anchor = grace["anchor"]
    if key == "data.freshness_provenance":
        failures = details.get("failures") or {}
        symbols = details.get("symbols") or {}
        if not failures:
            return False
        for symbol in failures:
            row = symbols.get(symbol)
            if not row:
                return False  # evaluation raised; never restart-explained
            failure_class = row.get("failure_class")
            if failure_class in STALENESS_FEED_FAILURE_CLASSES:
                continue
            if failure_class == "entitlement":
                verified = _parse(row.get("ingestion_timestamp"))
                if verified is not None and verified <= anchor + CLOCK_SKEW:
                    continue
            return False
        return True
    if key == "health.reconciliation":
        if account is None:
            return False
        last = getattr(account, "last_reconciled_at", None)
        return bool(
            account.status == "reconciled"
            and not account.reconciliation_required
            and not account.unexplained_residual
            and last is not None
            and _utc(last) <= anchor + CLOCK_SKEW
        )
    if key == "health.worker_scheduler":
        return not details.get("recent_unresolved_failures")
    return False
