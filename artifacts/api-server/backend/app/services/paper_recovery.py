"""Read-only provider recovery. Never sends or retries orders; never clears a halt."""
from decimal import Decimal
from sqlalchemy import select

from app.models.execution import PaperOrderIntent, PaperExecutionAccount, PaperExternalFill
from app.services.audit import write_audit_log
from app.services.paper_execution import execution_transaction, audit_account, PaperExecutionError
from app.services.freqtrade_dispatch import reconcile_intent, _trade, _amount

PENDING = ("submitting", "unknown", "submitted")


def _audit(factory, status, reason, intent_id=None):
    with factory() as db, execution_transaction(db):
        account = db.get(PaperExecutionAccount, 1)
        if status == "blocked" and account:
            account.kill_switch = True
        write_audit_log(db, event_type="paper_recovery", action="reconcile", status=status,
            message=reason, entity_type="paper_order_intent", entity_id=intent_id,
            payload={"mode": "dry_run", "eligible_for_qualification": False})


def recover_intent(factory, client, intent_id):
    with factory() as db:
        intent = db.get(PaperOrderIntent, intent_id)
        if not intent or intent.status not in PENDING or not isinstance(intent.provider_context, dict):
            raise PaperExecutionError("No recoverable submission")
        context = dict(intent.provider_context)
        side, expected_tag = intent.side, "paper:" + intent.client_order_id
    if context.get("version") != 1 or context.get("venue") != "freqtrade-2026.8-kraken-dryrun":
        raise PaperExecutionError("Unknown provider context")
    if context.get("tag") != expected_tag:
        raise PaperExecutionError("Persisted entry tag does not match intent")
    trade_id = context.get("trade_id")
    if trade_id is None and side == "buy":
        matches = [t for t in client.history() if t.get("enter_tag") == expected_tag]
        if len(matches) != 1:
            raise PaperExecutionError("Ambiguous or missing provider entry evidence")
        trade_id = _trade(matches[0])["trade_id"]
    if type(trade_id) is not int or trade_id <= 0:
        raise PaperExecutionError("Missing provider trade identity")
    return reconcile_intent(factory, client, intent_id, trade_id=trade_id)


def run_recovery_cycle(factory, client):
    """Reconcile persisted submissions, audit cash/inventory, halt on uncertainty.

    Approved-but-unsent reservations remain unsent. Failures retain reservations;
    a later successful observation never automatically re-enables execution.
    """
    with factory() as db:
        if db.get(PaperExecutionAccount, 1) is None:
            return {"status": "inactive", "reason": "paper_account_not_initialized"}
        ids = list(db.scalars(select(PaperOrderIntent.id).where(PaperOrderIntent.status.in_(PENDING))))
    blocked, recovered = [], []
    for ident in ids:
        try:
            recover_intent(factory, client, ident)
            recovered.append(ident)
            _audit(factory, "success", "provider_evidence_reconciled", ident)
        except Exception:
            # Never persist provider errors, credentials, raw HTTP bodies, or SQL.
            blocked.append(ident)
            _audit(factory, "blocked", "submission_requires_provider_evidence", ident)
    try:
        from app.services.paper_protective_exits import reconcile_provider_exits
        reconcile_provider_exits(factory, client)
        trades = [_trade(t) for t in client.trades()]
        with factory() as db, execution_transaction(db):
            audit_account(db)
            account = db.get(PaperExecutionAccount, 1)
            intents = list(db.scalars(select(PaperOrderIntent)))
            owned = {i.provider_order_id for i in intents if i.provider_order_id}
            owned.update(db.scalars(select(PaperExternalFill.provider_order_id)))
            provider_qty = sum((_amount(t.get("amount")) for t in trades), Decimal(0))
            # Detect automatic strategy exits / foreign orders even if they net
            # to the same inventory. Such fills need explicit operator review.
            if len(trades) > 1 or provider_qty != account.quantity or any(
                str(t["trade_id"]) + ":" + str(o.get("order_id")) not in owned
                for t in trades for o in t["orders"]):
                raise PaperExecutionError("Provider inventory/order ownership drift")
            halted = account.kill_switch
        _audit(factory, "success", "ledger_and_provider_consistent")
    except Exception:
        _audit(factory, "blocked", "provider_or_ledger_consistency_unverified")
        return {"status": "blocked", "reconciled": recovered, "unresolved": blocked,
                "reason": "provider_or_ledger_consistency_unverified", "kill_switch": True}
    return {"status": "blocked" if blocked else "success", "reconciled": recovered,
            "unresolved": blocked, "kill_switch": halted, "mode": "dry_run", "eligible_for_qualification": False}
