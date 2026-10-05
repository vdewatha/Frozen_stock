"""Review restored paper reads without resuming execution or approving costs."""
from datetime import datetime, timezone

from app.models import StockPaperLedgerEvent
from app.services.paper_cash_policy import MONETARY_REVIEW_REASON
from app.services.paper_research_accounting import TRANSPORT_HALT, _assess
from app.services.stock_paper_ledger import PROBE_HALT, active_paper_account, _event, _utc


def review(db, *, actor, apply=False):
    if not actor.strip():
        raise ValueError("An identified reviewer is required")
    account = active_paper_account(db, for_update=True)
    report = _assess(db, account, transport_review=True)
    if not report["accounting_observation_ready"]:
        raise ValueError(report["reason"])
    events = db.query(StockPaperLedgerEvent).filter_by(account_id=account.id)
    prior = events.filter_by(event_type="monetary_residual_review", status="resolved").order_by(
        StockPaperLedgerEvent.id.desc()).first()
    if not prior or any((prior.payload or {}).get(k) != report[k] for k in (
            "journal_sha256", "baseline_journal_sha256")):
        raise ValueError("An unchanged, previously reviewed journal is required")
    failures = events.filter(StockPaperLedgerEvent.id > prior.id,
        StockPaperLedgerEvent.status.in_(["halted", "unavailable", "drift"])).order_by(
        StockPaperLedgerEvent.id.desc()).all()
    if not failures or any(e.event_type != "reconcile" or e.status != "unavailable"
                           or e.reason != TRANSPORT_HALT for e in failures):
        raise ValueError("Only isolated broker-read failures may be reviewed")
    observations = events.filter(StockPaperLedgerEvent.id > failures[0].id,
        StockPaperLedgerEvent.event_type == "activity_reconciliation").order_by(
        StockPaperLedgerEvent.id.desc()).limit(2).all()
    now = datetime.now(timezone.utc)
    if (len(observations) != 2 or observations[0].id == observations[1].id
            or _utc(observations[0].created_at) < _utc(observations[1].created_at)):
        raise ValueError("Two distinct fresh successful reconciliations are required")
    for observation in observations:
        payload = observation.payload or {}
        if (observation.status != "matched"
                or not 0 <= (now - _utc(observation.created_at)).total_seconds() <= 120
                or any(payload.get(k) != report[k] for k in (
                    "journal_sha256", "baseline_journal_sha256", "cash_policy"))):
            raise ValueError("Both fresh reconciliations must match the reviewed journal")
    from app.services.paper_probe_review import pending_probe_reservation
    pending = pending_probe_reservation(db, account)
    preserved_reason = PROBE_HALT if pending else MONETARY_REVIEW_REASON
    result = {"version": "paper-transport-recovery-v1", "scope": "restore_research_review_state_only",
        "apply": apply, "last_failure_event_id": failures[0].id,
        "reconciliation_event_ids": [e.id for e in observations],
        "accounting_observation": report, "costs_verified": False,
        "execution_authorized": False, "live_authorized": False, "halt_cleared": False,
        "preserved_halt_reason": preserved_reason, "preserved_probe_reservation_id": pending.id if pending else None}
    if apply:
        account.halt_reason = preserved_reason
        db.info["stock_paper_actor"] = actor.strip()
        _event(db, account, "paper_transport_recovery_review", "reviewed",
               "Broker reads restored; research qualification and recovery gates remain separate", result)
        db.flush()
    return result
