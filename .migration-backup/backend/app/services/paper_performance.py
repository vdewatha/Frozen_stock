"""Observed paper fill performance, separated from price labels and qualification."""
from decimal import Decimal, ROUND_HALF_EVEN
from sqlalchemy import select
from app.models.execution import PaperOrderIntent, PaperExecutionFill, PaperExternalFill


def paper_performance(db, approval_id):
    with db.no_autoflush:
        intents = list(db.scalars(select(PaperOrderIntent)))
        ids = [row.id for row in intents]
        fills = list(db.scalars(select(PaperExecutionFill).where(PaperExecutionFill.intent_id.in_(ids))))
        external = list(db.scalars(select(PaperExternalFill).where(PaperExternalFill.entry_intent_id.in_(ids))))
    quantum = Decimal("0.00000001")
    def cost(fill):
        return (fill.quantity * fill.price).quantize(quantum, rounding=ROUND_HALF_EVEN)
    trades = []
    for entry in intents:
        if entry.approval_id != approval_id or entry.side != "buy" or not entry.provider_context or not entry.provider_order_id:
            continue
        trade_id = entry.provider_context.get("trade_id")
        if type(trade_id) is not int:
            continue
        buys = [f for f in fills if f.intent_id == entry.id]
        if not buys:
            continue
        exit_ids = {row.id for row in intents if row.side == "sell" and row.provider_context
                    and row.provider_context.get("trade_id") == trade_id}
        sells = [f for f in fills if f.intent_id in exit_ids]
        exits = [f for f in external if f.entry_intent_id == entry.id]
        bought = sum((f.quantity for f in buys), Decimal(0))
        sold = sum((f.quantity for f in sells + exits), Decimal(0))
        entry_cost = sum((cost(f) + f.fee for f in buys), Decimal(0))
        exit_proceeds = sum((cost(f) - f.fee for f in sells), Decimal(0)) + sum((f.cost - f.fee for f in exits), Decimal(0))
        valid = 0 <= sold <= bought
        model_terminal = all(row.status in ("reconciled", "rejected") for row in intents if row.id in exit_ids)
        external_terminal = all(any(f.provider_order_id == order_id and f.reason == "provider_exit_terminal" for f in exits)
                                for order_id in {f.provider_order_id for f in exits})
        closed = valid and sold == bought and entry.status in ("reconciled", "rejected") and model_terminal and external_terminal
        profit = exit_proceeds - entry_cost if closed else None
        trades.append({"entry_intent_id": entry.id, "provider_trade_id": trade_id,
            "closed": closed, "consistent_quantities": valid, "bought_quantity": str(bought),
            "sold_quantity": str(sold), "entry_cost_including_modeled_fees": str(entry_cost),
            "exit_proceeds_after_modeled_fees": str(exit_proceeds),
            "realized_net_paper_pnl": str(profit) if profit is not None else None,
            "exit_approval_ids": sorted({row.approval_id for row in intents if row.id in exit_ids}),
            "provider_reconciliation_pending": not (model_terminal and external_terminal),
            "external_exit": bool(exits)})
    profits = [Decimal(t["realized_net_paper_pnl"]) for t in trades if t["closed"]]
    return {"approval_id": approval_id, "mode": "paper", "fees": "modeled_approved_rate",
        "completed_trades": len(profits), "positive_trades": sum(p > 0 for p in profits),
        "negative_trades": sum(p < 0 for p in profits), "breakeven_trades": sum(p == 0 for p in profits),
        "realized_net_paper_pnl": str(sum(profits, Decimal(0))), "trades": trades,
        "eligible_for_qualification": False, "live_authorized": False,
        "limitations": ["Observed paper fills with modeled fees, not a reconciled real-money wallet.",
                        "Positive trades do not establish a profitable strategy; losses remain included."]}
