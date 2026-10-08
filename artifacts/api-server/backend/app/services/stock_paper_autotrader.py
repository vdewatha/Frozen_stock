"""Scheduled, paper-only execution through the governed stock-paper ledger.

This service deliberately does not use the legacy ``PaperTrade`` simulator.
It generates one signal per configured symbol and approved paper strategy, then
lets the durable stock-paper reservation and dispatch gates decide whether an
order can be submitted to Alpaca paper.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import RiskRule, Strategy
from app.models.stock_paper import StockPaperOrder, StockPaperPosition
from app.services.stock_paper_ledger import (
    NONTERMINAL_ORDER_STATUSES,
    StockPaperError,
    active_paper_account,
    create_stock_paper_signal,
    dispatch_reserved_order,
    reserve_stock_paper_order,
)
from app.services.intraday_data import session_bounds

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
SCHEDULED_SOURCE = "scheduled_paper_signal"
QUANTITY_INCREMENT = Decimal("0.000001")


def _is_regular_session(now: datetime) -> bool:
    local = now.astimezone(ET)
    bounds = session_bounds(local.date())
    return bool(bounds and bounds[0] <= now < bounds[1])


def _quantity_for_buy(account, reference_price: Decimal, rules: dict) -> Decimal:
    risk_fraction = Decimal(str(rules.get("max_risk_per_trade", "0.01")))
    notional = max(Decimal("0"), account.equity) * risk_fraction
    return (notional / reference_price).quantize(QUANTITY_INCREMENT, rounding=ROUND_DOWN)


def run_stock_paper_signal_cycle(db: Session) -> dict:
    """Run one bounded multi-symbol/multi-strategy paper execution cycle.

    A cycle outside market hours is a safe no-op.  During market hours every
    BUY/SELL still passes through the existing reservation and dispatch gates;
    a failed gate is recorded in the result and does not become an order.
    """
    now = datetime.now(UTC)
    account = active_paper_account(db)
    base = {
        "job": "paper_trading_signal_job",
        "paper_only": True,
        "live_trading": False,
        "symbols": list(settings.paper_execution_symbols),
    }
    if account is None:
        return base | {"status": "skipped", "reason": "stock paper account is not initialized"}
    if not _is_regular_session(now):
        return base | {"status": "skipped", "reason": "regular market session is closed"}
    if account.status != "reconciled" or account.reconciliation_required or account.unexplained_residual:
        return base | {"status": "skipped", "reason": "stock paper account is not reconciled"}

    all_strategies = (
        db.query(Strategy)
        .filter(Strategy.is_active.is_(True), Strategy.current_status == "paper_trading_active")
        .order_by(Strategy.id)
        .all()
    )
    strategies = []
    seen_strategy_types: set[str] = set()
    for strategy in all_strategies:
        if strategy.strategy_type not in seen_strategy_types:
            seen_strategy_types.add(strategy.strategy_type)
            strategies.append(strategy)
    if not strategies:
        return base | {"status": "skipped", "reason": "no strategies are approved for paper trading"}

    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    rules = rule.value if rule else {}
    session_key = now.astimezone(ET).date().isoformat()
    results: list[dict] = []
    for symbol in settings.paper_execution_symbols:
        symbol = symbol.strip().upper()
        for strategy in strategies:
            result = {"symbol": symbol, "strategy": strategy.strategy_type}
            try:
                signal = create_stock_paper_signal(db, symbol, strategy.strategy_type)
                result.update({
                    "signal_id": signal["signal_id"],
                    "signal_action": signal["signal_action"],
                    "reference_price": signal["reference_price"],
                })
                action = signal["signal_action"]
                reference_price = Decimal(signal["reference_price"])
                if action == "BUY":
                    quantity = _quantity_for_buy(account, reference_price, rules)
                    if quantity <= 0:
                        result.update({"status": "blocked", "reason": "risk-sized quantity rounded to zero"})
                        results.append(result)
                        continue
                    side = "buy"
                elif action == "SELL":
                    position = db.query(StockPaperPosition).filter_by(
                        account_id=account.id, symbol=symbol
                    ).one_or_none()
                    pending_sell = db.query(StockPaperOrder).filter(
                        StockPaperOrder.account_id == account.id,
                        StockPaperOrder.symbol == symbol,
                        StockPaperOrder.side == "sell",
                        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
                    ).first()
                    if not position or position.quantity <= 0:
                        result.update({"status": "skipped", "reason": "sell signal has no long position"})
                        results.append(result)
                        continue
                    if pending_sell:
                        result.update({"status": "skipped", "reason": "sell order is already pending", "order_id": pending_sell.id})
                        results.append(result)
                        continue
                    quantity = position.quantity
                    side = "sell"
                else:
                    result.update({"status": "observed", "reason": "signal is not actionable"})
                    results.append(result)
                    continue

                order = reserve_stock_paper_order(
                    db,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    reference_price=reference_price,
                    idempotency_key=f"scheduled-paper:{session_key}:{symbol}:{strategy.strategy_type}:{side}",
                    source=SCHEDULED_SOURCE,
                    signal_id=signal["signal_id"],
                )
                submitted = dispatch_reserved_order(db, order.id)
                order_status = str(submitted.status)
                result.update({
                    "status": "submitted" if order_status not in {"unknown", "cancelled"} else "blocked",
                    "order_id": submitted.id,
                    "order_status": order_status,
                })
                if order_status in {"unknown", "cancelled"}:
                    result["reason"] = f"broker order ended in {order_status} status"
            except StockPaperError as exc:
                result.update({"status": "blocked", "reason": str(exc)})
            except Exception as exc:  # one symbol/strategy must not stop the fan-out
                db.rollback()
                result.update({"status": "failed", "reason": f"{exc.__class__.__name__}: {exc}"})
            results.append(result)

    submitted = sum(item.get("status") == "submitted" for item in results)
    blocked = sum(item.get("status") in {"blocked", "failed"} for item in results)
    return base | {
        "status": "complete" if blocked == 0 else "partially_complete",
        "strategy_count": len(strategies),
        "signal_count": len(results),
        "submitted_count": submitted,
        "blocked_count": blocked,
        "results": results,
    }
