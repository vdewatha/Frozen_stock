"""Read-only projection of the persisted broker book; no evaluations or broker calls.

The broker position is the source of quantity/mark truth. Strategy ownership must
explain that exact quantity from this account's fills, never legacy PaperTrades.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    RiskRule, Strategy, StrategySignal, StockPaperEquitySnapshot, StockPaperFill, StockPaperOrder,
    StockPaperPosition, StockPaperRecoveryState, StockPaperRunApproval,
    StockPaperTrial, StockPaperTrialLot,
)
from app.services.exchange_sessions import session_bounds
from app.services.paper_mandate import current_mandate, mandate_state
from app.services.stock_paper_ledger import active_paper_account
from app.services.strategy_execution_policy import daily_bar_entry_window, sessions_held

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
ZERO = Decimal("0")
STALE_SECONDS = 180


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value else None


def _money(value) -> str | None:
    return str(value) if value is not None else None


def _position(row, fills, orders, strategies, lots, now, verified):
    # Same fail-closed attribution rule as execution's position-count gate:
    # any unowned fill or mismatched aggregate leaves the symbol unattributed.
    net = defaultdict(lambda: ZERO)
    unknown = False
    for fill in fills:
        order = orders.get(fill.order_id)
        if order is None or order.strategy_id not in strategies or order.symbol != row.symbol:
            unknown = True
            continue
        net[order.strategy_id] += fill.quantity * (1 if fill.side == "buy" else -1)
    owners = [sid for sid, qty in net.items() if qty > 0]
    known = (
        not unknown and bool(owners) and all(qty >= 0 for qty in net.values())
        and sum(net.values(), ZERO) == row.quantity
    )
    owner_lots = [
        lot for lot in lots
        if lot.symbol == row.symbol
        and lot.entry_order_id in orders
        and orders[lot.entry_order_id].strategy_id in owners
        and lot.quantity - (lot.exited_quantity or ZERO) > 0
    ] if known else []
    # A single compatible durable lot can describe this holding. Never use an
    # unrelated historical lot just because it has the same ticker.
    lot = owner_lots[0] if len(owner_lots) == 1 else None
    if lot and lot.quantity - (lot.exited_quantity or ZERO) != row.quantity:
        lot = None
    entry_order = orders.get(lot.entry_order_id) if lot else None
    entry_fills = [f for f in fills if entry_order and f.order_id == entry_order.id and f.side == "buy"]
    entry_at = min((f.filled_at for f in entry_fills), default=None)
    held = None
    if lot:
        try:
            entry_day = datetime.fromisoformat(lot.entry_session).date()
            held = sessions_held(entry_day, now.astimezone(ET).date())
        except ValueError:
            pass  # Historical malformed session evidence remains unknown.
    pending_exits = [
        o for o in orders.values()
        if o.symbol == row.symbol and o.side == "sell"
        and o.status not in {"filled", "cancelled", "canceled", "rejected", "expired", "failed"}
    ]
    intent = None
    if pending_exits:
        protective = all(
            o.order_type in {"stop", "stop_limit", "trailing_stop"}
            or o.leg_role in {"stop_loss", "take_profit"}
            for o in pending_exits
        )
        intent = "Protective exit working" if protective else "Exit order pending"
    elif lot and lot.exit_status:
        intent = f"{lot.exit_reason or 'Exit'} · {lot.exit_status}"
    elif lot:
        intent = f"Horizon exit · session {lot.planned_horizon_sessions}"
    stop = entry_order.stop_price if entry_order else None
    pnl = row.unrealized_pl if verified else None
    cost = row.cost_basis
    return {
        "symbol": row.symbol, "quantity": str(row.quantity),
        "entry_price": _money(row.average_entry_price),
        "current_price": _money(row.current_price), "market_value": _money(row.market_value),
        "unrealized_pl": _money(pnl),
        "unrealized_pl_pct": float(pnl / cost * 100) if pnl is not None and cost and cost > 0 else None,
        "observed_at": _iso(row.observed_at),
        "attribution": ("attributed" if len(owners) == 1 else "multiple") if known else "unknown",
        "strategy": " / ".join(strategies[sid].name for sid in sorted(owners)) if known else None,
        "strategy_ids": sorted(owners) if known else [],
        "entry_at": _iso(entry_at), "sessions_held": held,
        "horizon_sessions": lot.planned_horizon_sessions if lot else None,
        "exit_intent": intent, "stop_price": _money(stop),
    }


def paper_overview(db: Session, *, now: datetime | None = None) -> dict:
    now = _utc(now or datetime.now(UTC))
    today = now.astimezone(ET).date()
    session = session_bounds(today)
    window = daily_bar_entry_window(now)
    account = active_paper_account(db)
    strategies = {s.id: s for s in db.scalars(select(Strategy).order_by(Strategy.name))}
    rows = db.scalars(select(StockPaperPosition).where(
        StockPaperPosition.account_id == account.id, StockPaperPosition.quantity > 0,
    ).order_by(StockPaperPosition.symbol)).all() if account else []
    symbols = [r.symbol for r in rows]
    fills = db.scalars(select(StockPaperFill).where(
        StockPaperFill.account_id == account.id, StockPaperFill.symbol.in_(symbols),
    ).order_by(StockPaperFill.filled_at, StockPaperFill.id)).all() if symbols else []
    # All fill history for open symbols is required, not a display-limited tail.
    orders = {o.id: o for o in db.scalars(select(StockPaperOrder).where(
        StockPaperOrder.account_id == account.id,
        StockPaperOrder.symbol.in_(symbols),
    ))} if symbols else {}
    lots = db.scalars(select(StockPaperTrialLot).where(
        StockPaperTrialLot.entry_order_id.in_(list(orders)),
    )).all() if orders else []
    verified = bool(account and account.accounting_verified and account.costs_known)
    positions = [_position(r, [f for f in fills if f.symbol == r.symbol], orders, strategies, lots, now, verified) for r in rows]

    # Latest persisted signal per strategy without reading the whole signal table.
    ranked = select(
        StrategySignal.id,
        func.row_number().over(
            partition_by=StrategySignal.strategy_id,
            order_by=(StrategySignal.signal_time.desc(), StrategySignal.id.desc()),
        ).label("rank"),
    ).subquery()
    latest_ids = select(ranked.c.id).where(ranked.c.rank == 1)
    signals = {s.strategy_id: s for s in db.scalars(select(StrategySignal).where(StrategySignal.id.in_(latest_ids)))}
    risk = db.query(RiskRule).first()
    limits = risk.value or {} if risk else {}
    kill = limits.get("kill_switch_enabled")
    kill = kill if isinstance(kill, bool) else None
    last = _utc(account.last_reconciled_at) if account and account.last_reconciled_at else None
    stale = not last or (now - last).total_seconds() > STALE_SECONDS or last > now
    recovery = db.get(StockPaperRecoveryState, 1)
    mandate = current_mandate(db)
    mandate_status = mandate_state(db, mandate, now=now)[0] if mandate else None
    state = "Unknown"
    reason = "No persisted operating evidence"
    if not account:
        state, reason = "Not initialized", "No broker paper account has been imported"
    elif kill is True:
        state, reason = "Stopped", "Kill switch enabled"
    elif account.status != "reconciled" or account.reconciliation_required:
        state, reason = "Blocked", account.halt_reason or f"Broker account {account.status}"
    elif recovery and recovery.status != "armed":
        state, reason = "Paused", recovery.pause_reason or recovery.status
    elif stale:
        state, reason = "Unknown", "Broker snapshot is stale"
    elif kill is None:
        state, reason = "Unknown", "Kill-switch evidence is missing"
    else:
        # Authorization is not proof a worker is alive or that entries are ready.
        state = "Mandate active" if mandate_status == "active" else "Reconciled"
        reason = "Worker execution readiness not established by this snapshot"

    trials = db.scalars(select(StockPaperTrial).where(
        StockPaperTrial.status.in_(["approved", "running"]),
        StockPaperTrial.source_cycle_id.is_not(None),
    ).order_by(StockPaperTrial.created_at.desc()).limit(10)).all()
    approval = None
    for trial in trials:
        candidate = db.scalar(select(StockPaperRunApproval).where(
            StockPaperRunApproval.cycle_id == trial.source_cycle_id,
        ).order_by(StockPaperRunApproval.id.desc()))
        if candidate:
            schedule = candidate.schedule or {}
            try:
                start = _utc(datetime.fromisoformat(schedule["start_at"]))
                end = _utc(datetime.fromisoformat(schedule["end_at"]))
                if start <= now < end:
                    approval = candidate
                    break
            except (KeyError, ValueError, TypeError):
                continue
    exposure_limits = approval.exposure_limits if approval else {}
    approved_schedule = approval.schedule if approval else {}

    admitted = []
    owned_ids = {sid for p in positions for sid in p["strategy_ids"]}
    for sid, strategy in strategies.items():
        if strategy.current_status not in {"paper_trading_active", "paper_trading_candidate", "paused"} and sid not in owned_ids:
            continue
        signal = signals.get(sid)
        timing = (signal.features or {}).get("entry_timing") if signal else None
        admitted.append({
            "id": sid, "name": strategy.name, "status": strategy.current_status,
            "open_positions": sum(sid in p["strategy_ids"] for p in positions),
            "position_limit": limits.get("max_open_positions_per_strategy"),
            "last_signal": ({
                "action": signal.action, "symbol": signal.symbol,
                "at": _iso(signal.signal_time),
                "withheld": timing.get("withheld_action") == "BUY" if isinstance(timing, dict) else None,
                "reason": signal.reason,
            } if signal else None),
            # Cadence is not a durable per-strategy next-run timestamp.
            "next_evaluation_at": None,
        })
    activity = []
    recent_fills = db.scalars(select(StockPaperFill).where(
        StockPaperFill.account_id == account.id,
    ).order_by(StockPaperFill.filled_at.desc(), StockPaperFill.id.desc()).limit(12)).all() if account else []
    activity_order_ids = [f.order_id for f in recent_fills if f.order_id]
    activity_orders = {o.id: o for o in db.scalars(select(StockPaperOrder).where(
        StockPaperOrder.account_id == account.id, StockPaperOrder.id.in_(activity_order_ids),
    ))} if activity_order_ids else {}
    for fill in recent_fills:
        order = activity_orders.get(fill.order_id)
        strategy = strategies.get(order.strategy_id) if order else None
        activity.append({
            "id": f"fill-{fill.id}", "kind": fill.side, "symbol": fill.symbol,
            "strategy": strategy.name if strategy else None, "at": _iso(fill.filled_at),
            "detail": f"{fill.quantity} @ ${fill.price}",
        })
    withheld = db.scalars(select(StrategySignal).where(
        StrategySignal.features["entry_timing"]["withheld_action"].as_string() == "BUY",
    ).order_by(StrategySignal.id.desc()).limit(8)).all()
    for signal in withheld:
        strategy = strategies.get(signal.strategy_id)
        activity.append({
            "id": f"signal-{signal.id}", "kind": "withheld", "symbol": signal.symbol,
            "strategy": strategy.name if strategy else None, "at": _iso(signal.signal_time),
            "detail": "BUY withheld · entry window",
        })
    activity.sort(key=lambda a: a["at"] or "", reverse=True)
    snapshots = db.scalars(select(StockPaperEquitySnapshot).where(
        StockPaperEquitySnapshot.account_id == account.id,
    ).order_by(StockPaperEquitySnapshot.observed_at.desc(), StockPaperEquitySnapshot.id.desc()).limit(365)).all() if account else []
    equity_curve = [
        {"observed_at": _iso(s.observed_at), "equity": _money(s.equity)}
        for s in reversed(snapshots) if s.equity is not None
    ]
    exposure = sum((r.market_value for r in rows), ZERO) if account and all(r.market_value is not None for r in rows) else None
    equity = account.equity if account else None
    return {
        "generated_at": now.isoformat(), "mode": "paper",
        "session": {
            "state": "open" if session and session[0] <= now < session[1] else "closed",
            "opens_at": _iso(session[0]) if session else None,
            "closes_at": _iso(session[1]) if session else None,
            "entry_start": _iso(window[0]) if window else None,
            "entry_end": _iso(window[1]) if window else None,
        },
        "bot": {"state": state, "reason": reason, "kill_switch": kill, "mandate_status": mandate_status},
        "bounds": {
            "starts_at": approved_schedule.get("start_at"), "ends_at": approved_schedule.get("end_at"),
            "max_positions": exposure_limits.get("max_positions"),
            "max_gross": _money(exposure_limits.get("max_aggregate_notional")),
            "per_symbol": _money(exposure_limits.get("max_symbol_notional")),
            "stop_fraction": None,
        },
        "account": {
            "status": account.status if account else "uninitialized",
            "last_reconciled_at": _iso(last), "stale": bool(stale),
            "equity": _money(equity), "cash": _money(account.cash) if account else None,
            "exposure": _money(exposure),
            "exposure_pct": float(exposure / equity * 100) if exposure is not None and equity and equity > 0 else None,
            "pnl_verified": verified, "realized_pl": None,
        },
        "equity_curve": equity_curve,
        "positions": positions, "strategies": admitted,
        "research_count": len(strategies) - len(admitted), "activity": activity[:12],
    }
