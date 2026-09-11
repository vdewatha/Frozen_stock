"""Observe external provider exits without creating fictitious model decisions.

Only exact owned trade identities are accepted. External exits engage the local
kill switch, preserve original model attribution, and never authorize re-entry.
"""
from decimal import Decimal
import hashlib
import json

from sqlalchemy import select
from app.models.execution import PaperOrderIntent, PaperExternalFill, PaperTrialApproval, PaperExecutionAccount
from app.services.freqtrade_dispatch import _amount, _trade
from app.services.paper_execution import execution_transaction, audit_account, PaperExecutionError
from app.services.audit import write_audit_log


def reconcile_provider_exits(session_factory, client):
    with session_factory() as db:
        entries = [(row.id, row.provider_context.get("trade_id")) for row in db.scalars(
            select(PaperOrderIntent).where(PaperOrderIntent.side == "buy", PaperOrderIntent.status == "reconciled"))
            if row.provider_context and row.filled_quantity > 0]
    imported = 0
    for entry_id, trade_id in entries:
        if type(trade_id) is not int:
            raise PaperExecutionError("Owned entry lacks provider trade identity")
        trade = _trade(client.trade(trade_id))
        with session_factory() as db, execution_transaction(db):
            account = db.get(PaperExecutionAccount, 1)
            entry = db.get(PaperOrderIntent, entry_id)
            audit_account(db)
            if (trade["trade_id"] != trade_id or trade.get("enter_tag") != entry.provider_context["tag"]
                    or entry.provider_context["tag"] != "paper:" + entry.client_order_id):
                raise PaperExecutionError("External exit trade ownership mismatch")
            identities = [order.get("order_id") for order in trade["orders"]]
            if any(not isinstance(ident, str) for ident in identities) or len(identities) != len(set(identities)):
                raise PaperExecutionError("Duplicate or invalid provider order identity")
            # Active model orders must reconcile first. Otherwise an intended
            # exit might be incorrectly relabelled as external activity.
            if db.scalar(select(PaperOrderIntent.id).where(PaperOrderIntent.status.in_(
                    ("submitting", "unknown", "submitted")))):
                raise PaperExecutionError("Reconcile pending submissions before external exits")
            known = {row.provider_order_id: row for row in db.scalars(select(PaperOrderIntent)) if row.provider_order_id}
            approval = db.get(PaperTrialApproval, entry.approval_id)
            owned_model_exits = sum((row.filled_quantity for row in known.values()
                if row.side == "sell" and row.provider_context and row.provider_context.get("trade_id") == trade_id), Decimal(0))
            owned_external_exits = sum((row.quantity for row in db.scalars(select(PaperExternalFill).where(
                PaperExternalFill.entry_intent_id == entry_id))), Decimal(0))
            for order in trade["orders"]:
                order_id = order.get("order_id")
                if not isinstance(order_id, str) or not order_id or len(order_id) > 100:
                    raise PaperExecutionError("Invalid external order identity")
                provider_id = f"{trade_id}:{order_id}"
                if provider_id in known:
                    recorded = known[provider_id]
                    if (order.get("pair") != "BTC/USD" or order.get("ft_order_side") != recorded.side
                            or _amount(order.get("filled")) != recorded.filled_quantity
                            or not recorded.provider_context
                            or _amount(order.get("cost")) != Decimal(recorded.provider_context["applied_cost"])
                            or order.get("ft_fee_base") not in (None, 0)):
                        raise PaperExecutionError("Previously reconciled provider order was revised")
                    continue
                if (order.get("pair") != "BTC/USD" or order.get("ft_order_side") not in ("sell", "stoploss")
                        or order.get("ft_fee_base") not in (None, 0)
                        or order.get("status") not in ("open", "closed", "canceled", "cancelled", "expired")
                        or (order.get("status") == "closed" and order["is_open"])):
                    raise PaperExecutionError("Unsupported external provider activity")
                quantity, cost = _amount(order.get("filled")), _amount(order.get("cost"))
                if quantity and (cost <= 0 or abs(_amount(order.get("safe_price")) * quantity - cost) > Decimal("0.00000001")):
                    raise PaperExecutionError("External fill cost mismatch")
                if (_amount(trade.get("fee_close")) > approval.fee_rate
                        or trade.get("fee_close_currency") not in (None, "USD")):
                    raise PaperExecutionError("Unsupported external exit fee")
                previous = db.scalars(select(PaperExternalFill).where(PaperExternalFill.provider_order_id == provider_id)).all()
                old_qty = sum((row.quantity for row in previous), Decimal(0))
                old_cost = sum((row.cost for row in previous), Decimal(0))
                if quantity < old_qty or cost < old_cost or (quantity == old_qty and cost != old_cost):
                    raise PaperExecutionError("External cumulative fill was revised")
                terminal = not order["is_open"] and order.get("status") in ("closed", "canceled", "cancelled", "expired")
                snapshot = hashlib.sha256(json.dumps({"provider": provider_id, "order": order}, sort_keys=True, allow_nan=False).encode()).hexdigest()
                if quantity == old_qty:
                    if order["is_open"]:
                        account.kill_switch = True
                    if quantity > 0 and terminal and not any(row.reason == "provider_exit_terminal" for row in previous):
                        # Record a terminal observation without inventing another
                        # execution when only order status changed after a fill.
                        db.add(PaperExternalFill(entry_intent_id=entry_id, provider_order_id=provider_id,
                            snapshot_sha256=snapshot, quantity=0, cost=0, fee=0, reason="provider_exit_terminal"))
                        db.flush()
                    continue
                delta, quote = quantity - old_qty, cost - old_cost
                if (delta > account.quantity or quote <= 0
                        or owned_model_exits + owned_external_exits + delta > entry.filled_quantity):
                    raise PaperExecutionError("External exit exceeds owned inventory")
                # Release only reservations that provably never reached the
                # provider. Unknown/submitted reservations were rejected above.
                for reserved in db.scalars(select(PaperOrderIntent).where(PaperOrderIntent.status == "approved")):
                    if reserved.provider_context or reserved.provider_order_id:
                        raise PaperExecutionError("Approved reservation has unexpected provider identity")
                    account.reserved_cash -= reserved.reserved_cash
                    account.reserved_quantity -= reserved.reserved_quantity
                    reserved.reserved_cash = reserved.reserved_quantity = Decimal(0)
                    reserved.status = "abandoned"
                fee = _amount(quote * approval.fee_rate)
                db.add(PaperExternalFill(entry_intent_id=entry_id, provider_order_id=provider_id,
                    snapshot_sha256=snapshot, quantity=delta, cost=quote, fee=fee,
                    reason="provider_exit_terminal" if terminal else "provider_external_exit"))
                account.cash += quote - fee
                account.quantity -= delta
                owned_external_exits += delta
                account.kill_switch = True
                imported += 1
                write_audit_log(db, event_type="paper_recovery", action="external_exit", status="halted",
                    message="Owned provider exit reconciled; explicit operator review required",
                    entity_type="paper_order_intent", entity_id=entry_id,
                    payload={"provider_order_id": provider_id, "snapshot_sha256": snapshot, "fees": "modeled_approved_rate"})
                db.flush()
            audit_account(db)
    return {"imported_external_fills": imported, "mode": "dry_run", "eligible_for_qualification": False}
