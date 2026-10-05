"""Audited repair of rounded quantities from an explicitly identified paper probe."""
import argparse
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from uuid import UUID

from sqlalchemy import select

from app.core.config import settings
from app.db.schema import assert_schema_current
from app.db.session import SessionLocal, engine
from app.models.stock_paper import StockPaperFill, StockPaperOrder
from app.services.stock_paper_ledger import AlpacaPaperClient, ALPACA_PAPER_URL, active_paper_account, _event


def quantity_repair(row, fresh, kind):
    raw = row.raw_payload
    if not isinstance(raw, dict) or raw != fresh:
        raise ValueError("Fresh broker evidence differs from preserved payload; manual review required")
    value = raw.get("qty") or (raw.get("filled_qty") if kind == "order" else None)
    exact = Decimal(str(value))
    if not exact.is_finite() or exact <= 0 or exact != exact.quantize(Decimal("0.000000001")):
        raise ValueError("Invalid nine-decimal broker quantity")
    if row.quantity == exact:
        return None
    if row.quantity != exact.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP):
        raise ValueError("Stored quantity is not explained by the previous eight-decimal schema")
    return {"kind": kind, "id": row.id, "before": str(row.quantity), "after": str(exact),
            "raw_sha256": hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if settings.allow_live_trading or settings.active_paper_broker != "alpaca_paper":
        raise RuntimeError("Only live-disabled Alpaca paper deployments are supported")
    assert_schema_current(engine)
    client = AlpacaPaperClient()
    if client.base_url != ALPACA_PAPER_URL:
        raise RuntimeError("Unexpected broker endpoint")
    broker = client.account()
    if client.positions() or any(r["status"] not in {"filled", "canceled", "expired", "rejected", "replaced"} for r in client.orders()):
        raise RuntimeError("Account must be flat with no pending orders")
    prefix = "qa-" + args.run_id.hex
    fresh_orders = {side: client.order_by_client_id(prefix + "-" + side) for side in ("buy", "sell")}
    activities = client.fills()
    if len({r["id"] for r in activities}) != len(activities):
        raise RuntimeError("Duplicate broker activity IDs require review")
    fresh_fills = {r["id"]: r for r in activities}
    changes = []
    with SessionLocal() as db:
        account = active_paper_account(db)
        if (not account or account.broker != "alpaca_paper" or account.status != "halted"
                or account.broker_account_id != broker["id"]):
            raise RuntimeError("An existing halted matching paper account is required")
        db.refresh(account, with_for_update=True)
        for side, fresh in fresh_orders.items():
            if not fresh or fresh.get("status") != "filled" or fresh.get("side") != side:
                raise RuntimeError("Both probe orders must be confirmed filled")
            order = db.execute(select(StockPaperOrder).where(
                StockPaperOrder.account_id == account.id,
                StockPaperOrder.client_order_id == prefix + "-" + side).with_for_update()).scalar_one()
            if order.source != "broker_import" or order.trial_id or order.strategy_id or order.signal_id:
                raise RuntimeError("Only unassigned imported probe orders may be repaired")
            rows = [(order, fresh, "order")]
            fills = db.scalars(select(StockPaperFill).where(
                StockPaperFill.account_id == account.id,
                StockPaperFill.broker_order_id == fresh["id"]).with_for_update()).all()
            if not fills:
                raise RuntimeError("Persisted probe fills are missing")
            rows.extend((fill, fresh_fills.get(fill.broker_activity_id), "fill") for fill in fills)
            for row, evidence, kind in rows:
                change = quantity_repair(row, evidence, kind)
                if change:
                    changes.append((row, change))
        report = {"run_id": str(args.run_id), "changes": [item for _, item in changes],
                  "apply": args.apply, "broker_writes": 0, "gates_changed": False}
        if args.apply and changes:
            for row, change in changes:
                row.quantity = Decimal(change["after"])
            db.info["stock_paper_actor"] = "paper-probe-precision-repair"
            _event(db, account, "probe_quantity_precision_repaired", "recorded",
                   "Restored nine-decimal quantities from identical preserved and fresh broker evidence", report)
            db.commit()
        print(json.dumps(report))


if __name__ == "__main__":
    main()
