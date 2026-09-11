"""Durable at-most-once paper submissions and cumulative-fill reconciliation."""
from decimal import Decimal, ROUND_HALF_EVEN, InvalidOperation
import hashlib
import json

from app.models.execution import PaperOrderIntent, PaperTrialApproval, PaperExecutionAccount
from app.services.paper_execution import (execution_transaction, mark_submitting, record_submission,
                                         apply_fill, reconcile, PaperExecutionError)


def _amount(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > 1000000000:
            raise ValueError()
        return number.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)
    except (ValueError, InvalidOperation):
        raise PaperExecutionError("Invalid provider amount") from None


def _result(intent):
    return {"intent_id": intent.id, "status": intent.status, "filled_quantity": str(intent.filled_quantity),
            "mode": "dry_run", "eligible_for_qualification": False, "fees": "modeled_approved_rate"}


def _trade(trade):
    if (not isinstance(trade, dict) or trade.get("pair") != "BTC/USD" or trade.get("is_short") is not False
            or trade.get("exchange") != "kraken" or type(trade.get("trade_id")) is not int
            or not isinstance(trade.get("orders"), list)
            or any(not isinstance(o, dict) or type(o.get("is_open")) is not bool for o in trade["orders"])):
        raise PaperExecutionError("Unexpected provider trade identity")
    return trade


def dispatch_intent(session_factory, client, intent_id):
    # Provider inspection happens before the durable submission transition; the
    # serialized transaction rechecks kill switch and intent approval afterward.
    trades = [_trade(t) for t in client.trades()]
    with session_factory() as db, execution_transaction(db):
        intent = db.get(PaperOrderIntent, intent_id)
        account = db.get(PaperExecutionAccount, 1)
        if account is None:
            raise PaperExecutionError("Paper account is not initialized")
        if not intent or intent.status != "approved":
            raise PaperExecutionError("Only never-submitted approved intents can dispatch")
        if len(trades) > 1 or any(any(o.get("is_open") for o in t["orders"]) for t in trades):
            raise PaperExecutionError("Provider has unsupported outstanding state")
        provider_qty = sum((_amount(t["amount"]) for t in trades), Decimal(0))
        if provider_qty != account.quantity:
            account.kill_switch = True
            return {"intent_id": intent_id, "status": "blocked", "reason": "provider_inventory_drift",
                    "kill_switch": True, "mode": "dry_run", "eligible_for_qualification": False}
        if intent.side == "buy" and trades:
            raise PaperExecutionError("Pyramiding is not supported")
        if intent.side == "sell" and len(trades) != 1:
            raise PaperExecutionError("Sell requires one reconciled provider position")
        context = {"version": 1, "venue": "freqtrade-2026.8-kraken-dryrun", "tag": "paper:" + intent.client_order_id,
                   "trade_id": trades[0]["trade_id"] if trades else None,
                   "before_order_ids": [o["order_id"] for t in trades for o in t["orders"]],
                   "applied_quantity": "0", "applied_cost": "0", "snapshots": []}
        mark_submitting(db, intent_id, provider_context=context)
        side, price, quantity = intent.side, intent.limit_price, intent.quantity
    try:
        response = client.enter(price=price, quantity=quantity, tag=context["tag"]) if side == "buy" else client.exit(
            trade_id=context["trade_id"], price=price, quantity=quantity)
        trade_id = response.get("trade_id") if side == "buy" else context["trade_id"]
        if type(trade_id) is not int or trade_id <= 0:
            raise PaperExecutionError("Submission lacks provider identity")
    except Exception:
        with session_factory() as db, execution_transaction(db):
            record_submission(db, intent_id, outcome="unknown")
        raise PaperExecutionError("Submission ambiguous; never retry; reconcile provider evidence") from None
    return reconcile_intent(session_factory, client, intent_id, trade_id=trade_id)


def reconcile_intent(session_factory, client, intent_id, trade_id=None):
    with session_factory() as db:
        intent = db.get(PaperOrderIntent, intent_id)
        if intent and intent.provider_context and intent.status in ("reconciled", "rejected"):
            client.verify()
            return _result(intent)
        if not intent or not intent.provider_context or intent.status not in ("submitting", "unknown", "submitted"):
            raise PaperExecutionError("No reconcilable submission")
        context = dict(intent.provider_context)
        trade_id = trade_id or context.get("trade_id")
    if type(trade_id) is not int:
        raise PaperExecutionError("Explicit provider trade identity required for ambiguous entry")
    trade = _trade(client.trade(trade_id))
    with session_factory() as db, execution_transaction(db):
        intent = db.get(PaperOrderIntent, intent_id)
        context = dict(intent.provider_context)
        if context.get("trade_id") not in (None, trade_id) or trade["trade_id"] != trade_id:
            raise PaperExecutionError("Provider trade identity changed")
        if intent.side == "buy" and trade.get("enter_tag") != context["tag"]:
            raise PaperExecutionError("Entry tag does not bind provider evidence")
        orders = [o for o in trade["orders"] if o.get("order_id") not in context["before_order_ids"]]
        if len(orders) != 1:
            raise PaperExecutionError("Replacement or external orders require manual reconciliation")
        order = orders[0]
        if (order.get("ft_order_side") != intent.side or order.get("pair") != "BTC/USD"
                or order.get("order_type") != "limit" or not isinstance(order.get("order_id"), str)
                or _amount(order.get("amount")) != intent.quantity or order.get("ft_fee_base") not in (None, 0)):
            raise PaperExecutionError("Provider order mismatch or unsupported base fee")
        provider_id = str(trade_id) + ":" + order["order_id"]
        if intent.status in ("submitting", "unknown"):
            record_submission(db, intent_id, provider_order_id=provider_id)
        elif intent.provider_order_id != provider_id:
            raise PaperExecutionError("Provider order identity changed")
        quantity, cost = _amount(order.get("filled")), _amount(order.get("cost"))
        approval = db.get(PaperTrialApproval, intent.approval_id)
        fee_side = "open" if intent.side == "buy" else "close"
        if (_amount(trade.get("fee_" + fee_side)) > approval.fee_rate
                or trade.get("fee_" + fee_side + "_currency") not in (None, "USD")):
            raise PaperExecutionError("Provider fee exceeds approved modeled fee policy")
        if quantity and abs(_amount(order.get("safe_price")) * quantity - cost) > Decimal("0.00000001"):
            raise PaperExecutionError("Provider cumulative cost and price disagree")
        previous_qty, previous_cost = Decimal(context["applied_quantity"]), Decimal(context["applied_cost"])
        if quantity < previous_qty or cost < previous_cost or (quantity == previous_qty and cost != previous_cost):
            raise PaperExecutionError("Provider cumulative fill was revised")
        digest = hashlib.sha256(json.dumps(order, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if quantity > previous_qty:
            delta = quantity - previous_qty
            price = _amount((cost - previous_cost) / delta)
            if _amount(delta * price) != cost - previous_cost:
                raise PaperExecutionError("Fill price precision cannot preserve authoritative quote cost")
            fee = _amount(delta * price * approval.fee_rate)
            apply_fill(db, intent_id, provider_fill_id=provider_id + ":" + digest[:32], quantity=delta, price=price, fee=fee)
        context.update(trade_id=trade_id, applied_quantity=str(quantity), applied_cost=str(cost),
                       snapshots=(context["snapshots"] + [digest])[-100:])
        intent.provider_context = context
        status = order.get("status")
        if status == "closed":
            reconcile(db, intent_id, final_status="filled")
        elif status in ("canceled", "cancelled", "expired"):
            reconcile(db, intent_id, final_status="cancelled")
        elif status == "rejected" and quantity == 0:
            reconcile(db, intent_id, final_status="rejected")
        elif status not in ("open", "pending"):
            raise PaperExecutionError("Unsupported provider order status")
        result = _result(intent)
    return result
