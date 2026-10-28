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
from app.models.stock_paper import StockPaperFill, StockPaperOrder, StockPaperPosition
from app.services.stock_paper_ledger import (
    NONTERMINAL_ORDER_STATUSES,
    StockPaperError,
    active_paper_account,
    create_stock_paper_signal,
    create_stock_paper_research_signal,
    EXPLORATION_SOURCE,
    EXPLORATION_STRATEGY_STATUSES,
    dispatch_reserved_order,
    reserve_stock_paper_order,
)
from app.services.intraday_data import session_bounds

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
SCHEDULED_SOURCE = "scheduled_paper_signal"
QUANTITY_INCREMENT = Decimal("0.000001")
# Matches the retired simulator's STOP_LOSS_PCT; override with the
# ``position_stop_loss_pct`` risk rule (0 disables).
DEFAULT_POSITION_STOP_LOSS_PCT = Decimal("0.03")


def _is_regular_session(now: datetime) -> bool:
    local = now.astimezone(ET)
    bounds = session_bounds(local.date())
    return bool(bounds and bounds[0] <= now < bounds[1])


def _quantity_for_buy(account, reference_price: Decimal, rules: dict, *, exploration: bool = False) -> Decimal:
    risk_fraction = (
        Decimal(str(settings.paper_exploration_risk_fraction)) if exploration
        else Decimal(str(rules.get("max_risk_per_trade", "0.01")))
    )
    notional = max(Decimal("0"), account.equity) * risk_fraction
    return (notional / reference_price).quantize(QUANTITY_INCREMENT, rounding=ROUND_DOWN)


def _pending_orders(db: Session, account_id: int, symbol: str) -> list:
    return db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account_id,
        StockPaperOrder.symbol == symbol,
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).all()


def _strategy_holdings(db: Session, account_id: int, symbol: str,
                       type_by_id: dict) -> tuple[dict[str, Decimal], bool]:
    """Net filled quantity per strategy type for ``symbol``.

    Returns ``(holdings, attributed)``; ``attributed`` is False when any fill
    for the symbol cannot be tied to a strategy-bound order.
    """
    fills = db.query(StockPaperFill).filter_by(account_id=account_id, symbol=symbol).all()
    order_ids = {row.order_id for row in fills if row.order_id is not None}
    orders = {
        row.id: row for row in db.query(StockPaperOrder).filter(StockPaperOrder.id.in_(order_ids)).all()
    } if order_ids else {}
    holdings: dict[str, Decimal] = {}
    attributed = True
    for fill in fills:
        order = orders.get(fill.order_id)
        strategy_type = type_by_id.get(order.strategy_id) if order and order.strategy_id else None
        if strategy_type is None:
            attributed = False
            continue
        sign = Decimal("1") if fill.side == "buy" else Decimal("-1")
        holdings[strategy_type] = holdings.get(strategy_type, Decimal("0")) + sign * fill.quantity
    return holdings, attributed


def _protective_stop(db: Session, account, symbol: str, price: Decimal | None,
                     rules: dict, session_key: str) -> dict | None:
    """Exit a long whose loss from average entry breaches the stop budget.

    The governed stock path previously had no price-based exit at all: a
    losing position was held until a slow state signal (MA crossover / MACD)
    flipped.  This is a risk-reducing sell through the same reservation and
    dispatch gates; it never opens or adds exposure.
    """
    if price is None or price <= 0:
        return None
    stop_pct = Decimal(str(rules.get("position_stop_loss_pct", DEFAULT_POSITION_STOP_LOSS_PCT)))
    if stop_pct <= 0:
        return None
    position = db.query(StockPaperPosition).filter_by(account_id=account.id, symbol=symbol).one_or_none()
    entry = getattr(position, "average_entry_price", None) if position else None
    if not position or position.quantity <= 0 or not entry or entry <= 0:
        return None
    loss = (entry - price) / entry
    if loss < stop_pct:
        return None
    result = {"symbol": symbol, "strategy": "protective_stop", "signal_action": "SELL",
              "reference_price": str(price), "loss_from_entry": str(loss.quantize(Decimal("0.0001")))}
    if any(row.side == "sell" for row in _pending_orders(db, account.id, symbol)):
        return result | {"status": "skipped", "reason": "sell order is already pending"}
    try:
        order = reserve_stock_paper_order(
            db, symbol=symbol, side="sell", quantity=position.quantity, reference_price=price,
            idempotency_key=f"scheduled-paper:{session_key}:{symbol}:protective_stop:sell",
            source=SCHEDULED_SOURCE,
        )
        submitted = dispatch_reserved_order(db, order.id)
    except StockPaperError as exc:
        return result | {"status": "blocked", "reason": str(exc)}
    except Exception as exc:
        db.rollback()
        return result | {"status": "failed", "reason": f"{exc.__class__.__name__}: {exc}"}
    order_status = str(submitted.status)
    return result | {
        "status": "submitted" if order_status not in {"unknown", "cancelled"} else "blocked",
        "order_id": submitted.id, "order_status": order_status,
        "reason": f"loss from average entry reached the {stop_pct:.2%} stop",
    }


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
        .filter(
            Strategy.is_active.is_(True),
            Strategy.current_status.in_({"paper_trading_active", "paper_trading_candidate", "research"}),
        )
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
    type_by_id = {row.id: row.strategy_type for row in db.query(Strategy).all()}
    results: list[dict] = []
    for symbol in settings.paper_execution_symbols:
        symbol = symbol.strip().upper()
        # Phase 1: observe every strategy for this symbol before acting, so
        # opposing signals from different strategies are resolved
        # deterministically instead of by strategy id order.
        actionable: list[tuple[object, dict, dict]] = []
        observed_price: Decimal | None = None
        for strategy in strategies:
            result = {"symbol": symbol, "strategy": strategy.strategy_type}
            try:
                # Test doubles and older rows may omit the status field; the
                # existing cycle contract treated those rows as active.
                research_only = getattr(strategy, "current_status", "paper_trading_active") != "paper_trading_active"
                signal = (
                    create_stock_paper_research_signal(db, symbol, strategy.strategy_type)
                    if research_only
                    else create_stock_paper_signal(db, symbol, strategy.strategy_type)
                )
                result.update({
                    "signal_id": signal["signal_id"],
                    "signal_action": signal["signal_action"],
                    "reference_price": signal["reference_price"],
                })
                if observed_price is None:
                    observed_price = Decimal(signal["reference_price"])
                exploring = (
                    research_only
                    and settings.paper_exploration_enabled
                    and getattr(strategy, "current_status", None) in EXPLORATION_STRATEGY_STATUSES
                )
                if exploring and signal["signal_action"] in {"BUY", "SELL"}:
                    # Small tagged learning order; never promotion evidence.
                    result["mode"] = "explore"
                    actionable.append((strategy, signal, result))
                    continue
                if research_only:
                    result.update({"status": "observed", "reason": "research-only strategy; no order path"})
                elif signal["signal_action"] in {"BUY", "SELL"}:
                    actionable.append((strategy, signal, result))
                    continue
                else:
                    result.update({"status": "observed", "reason": "signal is not actionable"})
            except StockPaperError as exc:
                result.update({"status": "blocked", "reason": str(exc)})
            except Exception as exc:  # one symbol/strategy must not stop the fan-out
                db.rollback()
                result.update({"status": "failed", "reason": f"{exc.__class__.__name__}: {exc}"})
            results.append(result)
        stop_result = _protective_stop(db, account, symbol, observed_price, rules, session_key)
        if stop_result is not None:
            results.append(stop_result)
        if not actionable:
            continue

        # Phase 2: exits first, then entries.  An exit submitted (or already
        # pending) for a symbol suppresses same-cycle entries on that symbol,
        # which previously produced a buy and a sell of the same name in one
        # cycle and paid the spread twice.
        holdings, attributed = _strategy_holdings(db, account.id, symbol, type_by_id)
        pending = _pending_orders(db, account.id, symbol)
        exiting = any(row.side == "sell" for row in pending)
        if stop_result is not None and stop_result.get("status") == "submitted":
            exiting = True
            for _strategy, _signal, result in actionable:
                result.update({"status": "skipped", "reason": "protective stop is exiting this symbol"})
                results.append(result)
            continue
        ordered = sorted(actionable, key=lambda item: item[1]["signal_action"] != "SELL")
        for strategy, signal, result in ordered:
            try:
                action = signal["signal_action"]
                reference_price = Decimal(signal["reference_price"])
                owned = holdings.get(strategy.strategy_type, Decimal("0"))
                if action == "BUY":
                    if exiting:
                        result.update({"status": "skipped", "reason": "an exit for this symbol is pending; no same-cycle re-entry"})
                        results.append(result)
                        continue
                    if owned > 0 or any(
                        row.side == "buy" and type_by_id.get(row.strategy_id) == strategy.strategy_type
                        for row in pending
                    ):
                        # Crossover/MACD signals are states, not events: a
                        # persistent BUY must not add a new lot every session.
                        result.update({"status": "skipped", "reason": "strategy already holds or is buying this symbol; no pyramiding on a persistent signal"})
                        results.append(result)
                        continue
                    quantity = _quantity_for_buy(account, reference_price, rules,
                                                 exploration=result.get("mode") == "explore")
                    if quantity <= 0:
                        result.update({"status": "blocked", "reason": "risk-sized quantity rounded to zero"})
                        results.append(result)
                        continue
                    side = "buy"
                else:
                    position = db.query(StockPaperPosition).filter_by(
                        account_id=account.id, symbol=symbol
                    ).one_or_none()
                    pending_sell = next((row for row in pending if row.side == "sell"), None)
                    if not position or position.quantity <= 0:
                        result.update({"status": "skipped", "reason": "sell signal has no long position"})
                        results.append(result)
                        continue
                    if pending_sell:
                        result.update({"status": "skipped", "reason": "sell order is already pending", "order_id": pending_sell.id})
                        results.append(result)
                        continue
                    if attributed:
                        # Exit only the lots this strategy opened; another
                        # strategy's SELL state must not liquidate them.
                        quantity = min(owned, position.quantity)
                        if quantity <= 0:
                            result.update({"status": "skipped", "reason": "sell signal strategy holds no attributable quantity in this symbol"})
                            results.append(result)
                            continue
                    else:
                        # Unknown attribution: keep risk reduction available.
                        quantity = position.quantity
                    side = "sell"

                exploring = result.get("mode") == "explore"
                order = reserve_stock_paper_order(
                    db,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    reference_price=reference_price,
                    idempotency_key=(
                        f"{'explore' if exploring else 'scheduled'}-paper:{session_key}:{symbol}:"
                        f"{strategy.strategy_type}:{side}"
                    ),
                    source=EXPLORATION_SOURCE if exploring else SCHEDULED_SOURCE,
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
                if side == "sell" and order_status != "cancelled":
                    exiting = True
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
