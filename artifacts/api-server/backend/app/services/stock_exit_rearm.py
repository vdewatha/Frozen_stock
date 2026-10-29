"""Re-arm broker-side exits for stock-paper long positions (paper only).

Bracket entries carry their own broker stop/take-profit legs, but some shares
can still have no live broker stop:

* positions opened before bracket exits existed,
* partially filled bracket entries (Alpaca activates the legs only when the
  entry fills completely),
* a lot whose legs were cancelled for an app-side SELL that then did not go out,
  for example because the kill switch was turned on in between.

This module places an Alpaca **OCO** exit (take-profit limit root plus a
stop-loss child) for the uncovered **whole** shares of each strategy-owned
lot. It places a plain GTC stop order when no take-profit applies. Alpaca
accepts OCO only as an exit on an existing position, only for whole shares,
and with ``type: limit``. Fractional quantities can only be simple DAY orders,
so a fractional remainder is reported and left to the app-side protective
stop.

Safety contract:

* It only submits SELL exit orders, never an entry. The quantity is limited by
  the ledger position less every live exit group and in-flight sell, and by
  the broker's ``qty_available``, read from a fresh positions call. So it can
  never double-sell or open a short.
* Recovery cancel/flatten (any recovery state other than armed/resumable)
  always blocks it, because recovery wants no resting orders. Any uncertain
  submission, external (broker_import) order, pending cancel, open entry or
  in-flight app-side sell on a symbol also blocks that symbol.
* While halted or with the kill switch on it still runs by default
  (``paper_exit_rearm_while_halted``). A protective exit reduces risk, and the
  kill switch's job is to stop new exposure. Turn the setting off to make the
  kill switch block re-arm too.
* Submission follows the exactly-once contract of ``dispatch_reserved_order``.
  The row is committed as ``submitting`` before the POST. A broker 4xx is a
  definite rejection: the row becomes ``rejected`` and the symbol backs off. Any
  other failure marks the row ``unknown`` and halts the account for
  reconciliation, which recovers the order by client-id lookup.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import RiskRule, StockPaperRecoveryState, Strategy
from app.models.stock_paper import StockPaperFill, StockPaperOrder, StockPaperPosition
from app.services.stock_paper_ledger import (
    ENTRY_EXIT_ORDER_CLASSES,
    MAX_BROKER_SNAPSHOT_AGE,
    NONTERMINAL_ORDER_STATUSES,
    StockPaperError,
    StockPaperUnavailable,
    _committed_sell_quantity,
    _event,
    _exit_group_exposure,
    _halt,
    _price_tick,
    _record_exit_legs,
    _utc,
    active_paper_account,
    active_paper_broker_name,
    active_paper_gateway,
    build_exit_plan,
    live_exit_orders,
)

UTC = timezone.utc
WHOLE_SHARE = Decimal("1")
# Mirrors the autotrader defaults (``position_stop_loss_pct`` /
# ``position_take_profit_pct`` risk rules override them).
DEFAULT_STOP_LOSS_PCT = Decimal("0.03")
DEFAULT_TAKE_PROFIT_PCT = Decimal("0.03")
REARM_CLIENT_PREFIX = "rearm-"
REARM_SOURCE = "exit_rearm"
# Recovery states in which resting protective orders are welcome.
REARM_RECOVERY_STATES = frozenset({"armed", "resumable"})
# A definite broker rejection is not retried for this long per symbol.
REJECTION_BACKOFF = timedelta(minutes=60)
# A fresh app-side sell reservation is about to replace exits; give it this long.
RESERVED_SELL_GRACE = timedelta(minutes=10)
UNCERTAIN_STATUSES = frozenset({"unknown", "submitting"})


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


def is_rearm_order(order: StockPaperOrder) -> bool:
    return str(order.client_order_id or "").startswith(REARM_CLIENT_PREFIX)


def _strategy_pct(params: dict, key: str) -> Any:
    for container in (params.get("exit_plan"), params):
        if isinstance(container, dict):
            for name in (key, f"position_{key}"):
                if container.get(name) is not None:
                    return container[name]
    return None


def exit_percentages(rules: dict, strategy: Strategy | None) -> tuple[Decimal, Decimal]:
    """Stop-loss / take-profit distances for a lot.

    The owning strategy's own exit settings win, if it declares
    ``stop_loss_pct`` / ``take_profit_pct`` (flat or under ``exit_plan``).
    Otherwise the active risk rules apply.
    """
    params = strategy.parameters if strategy is not None and isinstance(strategy.parameters, dict) else {}
    stop = _strategy_pct(params, "stop_loss_pct")
    take = _strategy_pct(params, "take_profit_pct")
    if stop is None:
        stop = rules.get("position_stop_loss_pct", DEFAULT_STOP_LOSS_PCT)
    if take is None:
        take = rules.get("position_take_profit_pct", DEFAULT_TAKE_PROFIT_PCT)
    return Decimal(str(stop or 0)), Decimal(str(take or 0))


def _owner_buckets(db: Session, account_id: int, symbol: str, position_qty: Decimal) -> tuple[dict, bool]:
    """Split the position into strategy-owned lots (``None`` = unattributed).

    Returns ``(buckets, ambiguous)``. App-side sells carry no strategy, so when
    several strategies hold the symbol and unattributed sells have reduced it,
    the owner of the remaining shares is unknown. Those shares are then
    protected as one unattributed lot (``ambiguous``), the same fallback the
    autotrader uses for unattributed SELLs.
    """
    fills = db.query(StockPaperFill).filter_by(account_id=account_id, symbol=symbol).all()
    order_ids = {row.order_id for row in fills if row.order_id is not None}
    orders = {
        row.id: row for row in db.query(StockPaperOrder).filter(StockPaperOrder.id.in_(order_ids)).all()
    } if order_ids else {}
    net: dict[int | None, Decimal] = {}
    for fill in fills:
        order = orders.get(fill.order_id)
        owner = order.strategy_id if order is not None else None
        sign = Decimal("1") if fill.side == "buy" else Decimal("-1")
        net[owner] = net.get(owner, Decimal("0")) + sign * fill.quantity
    positive = sorted(sid for sid, qty in net.items() if sid is not None and qty > 0)
    if len(positive) > 1 and net.get(None, Decimal("0")) < 0:
        return {None: position_qty}, True
    buckets: dict[int | None, Decimal] = {}
    remaining = position_qty
    for sid in positive:
        take = min(net[sid], remaining)
        if take > 0:
            buckets[sid] = take
            remaining -= take
    if remaining > 0:
        buckets[None] = remaining
    return buckets, False


def _anchor_order(db: Session, account_id: int, symbol: str, strategy_id: int | None) -> StockPaperOrder | None:
    """The newest filled entry of a lot; its source/trial/evidence tag the exit."""
    if strategy_id is None:
        return None
    rows = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account_id,
        StockPaperOrder.symbol == symbol,
        StockPaperOrder.side == "buy",
        StockPaperOrder.strategy_id == strategy_id,
        StockPaperOrder.status.in_({"filled", "partially_filled", "canceled", "cancelled", "expired"}),
    ).order_by(StockPaperOrder.id.desc()).all()
    filled_ids = {
        row.order_id for row in db.query(StockPaperFill.order_id).filter(
            StockPaperFill.order_id.in_([row.id for row in rows])
        ).all()
    } if rows else set()
    return next((row for row in rows if row.id in filled_ids), rows[0] if rows else None)


def _stop_coverage_by_owner(db: Session, account_id: int, symbol: str) -> dict[int | None, Decimal]:
    groups: dict[int, list[StockPaperOrder]] = {}
    for row in live_exit_orders(db, account_id, symbol):
        if row.leg_role == "stop_loss":
            groups.setdefault(row.parent_order_id or row.id, []).append(row)
    coverage: dict[int | None, Decimal] = {}
    for key, legs in groups.items():
        owner = legs[0].strategy_id
        coverage[owner] = coverage.get(owner, Decimal("0")) + _exit_group_exposure(db, key, legs)
    return coverage


def _kill_switch(db: Session) -> tuple[bool, dict]:
    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    rules = dict(rule.value or {}) if rule else {}
    return bool(rules.get("kill_switch_enabled", False)), rules


def _cancel_stuck_partial_entries(db: Session, account, gateway, now: datetime) -> list[int]:
    """Cancel bracket entries that stay partially filled past the grace period.

    The legs of a partially filled entry are held until it fills completely,
    so its filled shares have no broker stop. Cancelling the rest of the entry
    reduces exposure; Alpaca cancels the held legs with it, and the next re-arm
    covers the filled shares with an OCO once reconciliation confirms the
    cancel.
    """
    grace = timedelta(minutes=int(settings.paper_exit_rearm_partial_entry_grace_minutes))
    rows = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id,
        StockPaperOrder.side == "buy",
        StockPaperOrder.status == "partially_filled",
        StockPaperOrder.order_class.in_(ENTRY_EXIT_ORDER_CLASSES),
    ).all()
    cancelled = []
    for row in rows:
        if not row.broker_order_id:
            continue
        first_fill = db.query(StockPaperFill.filled_at).filter(
            StockPaperFill.order_id == row.id
        ).order_by(StockPaperFill.filled_at).first()
        started = first_fill[0] if first_fill else (row.submitted_at or row.created_at)
        if started is None or now - _utc(started) < grace:
            continue
        gateway.cancel_order(row.broker_order_id)
        row.status = "pending_cancel"
        cancelled.append(row.id)
    if cancelled:
        _event(db, account, "exit_rearm", "entry_cancel_requested",
               "Partially filled bracket entry cancelled so its filled shares can get a broker stop",
               {"order_ids": cancelled})
        db.commit()
    return cancelled


def _symbol_block(db: Session, account_id: int, symbol: str, now: datetime, *, trading_enabled: bool) -> str | None:
    rows = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account_id,
        StockPaperOrder.symbol == symbol,
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).all()
    if any(row.source == "broker_import" for row in rows):
        return "externally submitted order is open for this symbol; manual review first"
    if any(row.status == "pending_cancel" for row in rows):
        return "a cancellation is pending for this symbol; re-arm after it is confirmed"
    if any(row.side == "buy" for row in rows):
        return "an entry order is open for this symbol; its bracket legs or the next re-arm will cover it"
    for row in rows:
        if row.side != "sell" or row.leg_role:
            continue
        if row.status != "reserved":
            return "an app-side sell is in flight for this symbol"
        created = _utc(row.created_at) if row.created_at else now
        if trading_enabled and now - created < RESERVED_SELL_GRACE:
            return "an app-side sell reservation is about to replace the exits"
    recent = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account_id,
        StockPaperOrder.symbol == symbol,
        StockPaperOrder.status == "rejected",
        StockPaperOrder.client_order_id.like(f"{REARM_CLIENT_PREFIX}%"),
    ).all()
    if any(row.created_at and now - _utc(row.created_at) < REJECTION_BACKOFF for row in recent):
        return "the broker rejected a re-arm for this symbol recently; backing off"
    return None


def _payload(order: StockPaperOrder) -> dict:
    qty = str(order.quantity.to_integral_value())
    payload = {"symbol": order.symbol, "qty": qty, "side": "sell", "time_in_force": "gtc",
               "client_order_id": order.client_order_id}
    stop = str(order.stop_price.quantize(_price_tick(order.stop_price)))
    if order.order_class == "oco":
        take = str(order.take_profit_price.quantize(_price_tick(order.take_profit_price)))
        payload |= {"type": "limit", "order_class": "oco",
                    "take_profit": {"limit_price": take}, "stop_loss": {"stop_price": stop}}
    else:
        payload |= {"type": "stop", "stop_price": stop}
    return payload


def _submit(db: Session, account, gateway, order: StockPaperOrder) -> str:
    """Exactly-once POST. Returns submitted / rejected / unknown."""
    order_id, account_id = order.id, account.id
    order.status, order.submission_attempted_at = "submitting", datetime.now(UTC)
    db.commit()
    try:
        raw = gateway.submit_order(_payload(order))
        broker_id = str(raw.get("id") or "").strip()
        if not broker_id:
            raise StockPaperUnavailable("Broker accepted an order without an identifier")
    except StockPaperUnavailable as exc:
        db.rollback()
        if exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code != 408:
            order = db.get(StockPaperOrder, order_id)
            order.status = "rejected"
            _event(db, db.get(type(account), account_id), "exit_rearm", "rejected",
                   f"Broker rejected the protective exit (HTTP {exc.status_code})",
                   {"order_id": order_id, "client_order_id": order.client_order_id, "symbol": order.symbol})
            db.commit()
            return "rejected"
        return _uncertain(db, account, order_id)
    except Exception:
        db.rollback()
        return _uncertain(db, account, order_id)
    order = db.get(StockPaperOrder, order_id)
    order.broker_order_id = broker_id
    order.status = str(raw.get("status") or "accepted")
    order.submitted_at = datetime.now(UTC)
    order.raw_payload = raw
    legs = []
    if order.order_class == "oco":
        try:
            legs = _record_exit_legs(db, order, raw.get("legs"))
        except StockPaperError:
            legs = []  # reconciliation adopts the child by client-id lookup
    _event(db, db.get(type(account), account_id), "exit_rearm", "submitted",
           "Broker-side protective exit re-armed",
           {"order_id": order_id, "client_order_id": order.client_order_id, "broker_order_id": broker_id,
            "symbol": order.symbol, "quantity": str(order.quantity), "order_class": order.order_class,
            "stop_price": str(order.stop_price), "take_profit_price":
                str(order.take_profit_price) if order.take_profit_price is not None else None,
            "strategy_id": order.strategy_id,
            "exit_leg_broker_order_ids": [leg.broker_order_id for leg in legs]})
    db.commit()
    return "submitted"


def _uncertain(db: Session, account, order_id: int) -> str:
    order = db.get(StockPaperOrder, order_id)
    account = db.get(type(account), account.id)
    order.status, order.uncertain_submission = "unknown", True
    _halt(account, "Order submission outcome is uncertain; read-only broker lookup/reconciliation required")
    _event(db, account, "order_submission", "unknown", account.halt_reason,
           {"client_order_id": order.client_order_id, "exit_rearm": True})
    db.commit()
    return "unknown"


def rearm_broker_exits(db: Session, gateway=None, *, now: datetime | None = None) -> dict:
    """Place broker exits for uncovered whole shares of every long position."""
    now = _utc(now or datetime.now(UTC))
    base = {"job": "stock_exit_rearm", "paper_only": True}
    if not settings.paper_exit_rearm_enabled:
        return base | {"status": "skipped", "reason": "exit re-arm is disabled"}
    account = active_paper_account(db, for_update=True)
    if account is None:
        return base | {"status": "skipped", "reason": "stock paper account is not initialized"}
    if active_paper_broker_name() != "alpaca_paper":
        return base | {"status": "skipped", "reason": "broker-side OCO exits are only supported on Alpaca paper"}
    gateway = gateway or active_paper_gateway()
    if not getattr(gateway, "supports_bracket_orders", True):
        return base | {"status": "skipped", "reason": "active paper broker cannot carry broker-side exits"}
    recovery = db.get(StockPaperRecoveryState, 1)
    if recovery is not None and recovery.status not in REARM_RECOVERY_STATES:
        return base | {"status": "skipped",
                       "reason": f"recovery is {recovery.status}; resting orders would fight cancel/flatten"}
    kill_switch, rules = _kill_switch(db)
    halted = account.status != "reconciled" or account.reconciliation_required or account.unexplained_residual
    if (halted or kill_switch) and not settings.paper_exit_rearm_while_halted:
        return base | {"status": "skipped", "reason": "account is halted or the kill switch is on"}
    if not account.source_timestamp or _utc(account.source_timestamp) < now - MAX_BROKER_SNAPSHOT_AGE:
        return base | {"status": "skipped", "reason": "broker snapshot is stale; reconcile first"}
    uncertain = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id,
        StockPaperOrder.status.in_(UNCERTAIN_STATUSES),
    ).count()
    if uncertain:
        return base | {"status": "skipped", "reason": "an order submission outcome is uncertain; reconcile first"}
    try:
        broker_rows = gateway.positions()
    except (StockPaperError, StockPaperUnavailable) as exc:
        db.rollback()
        return base | {"status": "skipped", "reason": f"broker positions unavailable: {exc}"}
    broker = {str(row.get("symbol") or "").upper(): row for row in broker_rows if isinstance(row, dict)}
    cancelled_entries = _cancel_stuck_partial_entries(db, account, gateway, now)
    trading_enabled = not halted and not kill_switch
    strategies = {row.id: row for row in db.query(Strategy).all()}
    results: list[dict] = []
    halted_now = False
    positions = db.query(StockPaperPosition).filter(
        StockPaperPosition.account_id == account.id, StockPaperPosition.quantity > 0,
    ).order_by(StockPaperPosition.symbol).all()
    for position in positions:
        if halted_now:
            break
        symbol = position.symbol
        entry = {"symbol": symbol, "position_quantity": _fmt(position.quantity)}
        raw = broker.get(symbol)
        broker_qty = _dec(raw.get("qty")) if raw else None
        available = _dec(raw.get("qty_available")) if raw else None
        price = _dec(raw.get("current_price")) if raw else None
        average = _dec(raw.get("avg_entry_price")) if raw else None
        average = average or position.average_entry_price
        if broker_qty is None or broker_qty != position.quantity:
            results.append(entry | {"status": "skipped", "reason": "ledger and broker position differ; reconcile first"})
            continue
        if available is None or price is None or price <= 0 or not average or average <= 0:
            results.append(entry | {"status": "skipped",
                                    "reason": "broker did not report available quantity, price and entry price"})
            continue
        block = _symbol_block(db, account.id, symbol, now, trading_enabled=trading_enabled)
        if block:
            results.append(entry | {"status": "skipped", "reason": block})
            continue
        reserved_ids = {
            row.id for row in db.query(StockPaperOrder).filter(
                StockPaperOrder.account_id == account.id, StockPaperOrder.symbol == symbol,
                StockPaperOrder.side == "sell", StockPaperOrder.status == "reserved",
            ).all()
        }
        # Shares the broker already holds for a live exit group or sell.
        committed = _committed_sell_quantity(db, account.id, symbol, exclude_ids=reserved_ids)
        capacity = min(position.quantity - committed, available).quantize(WHOLE_SHARE, rounding=ROUND_DOWN)
        buckets, ambiguous = _owner_buckets(db, account.id, symbol, position.quantity)
        coverage = _stop_coverage_by_owner(db, account.id, symbol)
        uncovered: dict[int | None, Decimal] = {}
        if ambiguous:
            uncovered[None] = max(Decimal("0"), position.quantity - sum(coverage.values(), Decimal("0")))
        else:
            spill = Decimal("0")
            for owner, cover in coverage.items():
                held = buckets.get(owner, Decimal("0"))
                spill += max(Decimal("0"), cover - held)
            for owner, held in buckets.items():
                left = max(Decimal("0"), held - coverage.get(owner, Decimal("0")))
                if owner is None:
                    left = max(Decimal("0"), left - spill)
                if left > 0:
                    uncovered[owner] = left
        total_uncovered = sum(uncovered.values(), Decimal("0"))
        entry["uncovered_quantity"] = _fmt(total_uncovered)
        fractional = total_uncovered - sum(
            (qty.quantize(WHOLE_SHARE, rounding=ROUND_DOWN) for qty in uncovered.values()), Decimal("0"))
        if fractional > 0:
            entry["fractional_uncovered"] = _fmt(fractional)
        if total_uncovered <= 0:
            results.append(entry | {"status": "covered", "reason": "live broker stop legs cover the position"})
            continue
        lots = []
        for owner in sorted(uncovered, key=lambda sid: (sid is None, sid or 0)):
            whole = uncovered[owner].quantize(WHOLE_SHARE, rounding=ROUND_DOWN)
            whole = min(whole, capacity)
            lot = {"strategy_id": owner, "uncovered_quantity": _fmt(uncovered[owner])}
            if whole <= 0:
                lot |= {"status": "skipped", "reason": (
                    "only a fractional remainder is uncovered; Alpaca accepts OCO/GTC stops for whole shares "
                    "only, so the app-side protective stop covers it"
                    if uncovered[owner] < WHOLE_SHARE else "no broker-available shares left to cover")}
                lots.append(lot)
                continue
            anchor = _anchor_order(db, account.id, symbol, owner)
            if anchor is not None and anchor.trial_id:
                lots.append(lot | {"status": "skipped",
                                   "reason": "forward-trial lot; exits are governed by its approval"})
                continue
            stop_pct, take_pct = exit_percentages(rules, strategies.get(owner))
            if stop_pct <= 0:
                lots.append(lot | {"status": "skipped", "reason": "stop-loss distance is 0 (disabled)"})
                continue
            try:
                plan = build_exit_plan(average, stop_loss_pct=stop_pct, take_profit_pct=take_pct)
            except StockPaperError as exc:
                lots.append(lot | {"status": "skipped", "reason": str(exc)})
                continue
            stop_price, take_price = plan["stop_price"], plan["take_profit_price"]
            if price <= stop_price:
                lots.append(lot | {"status": "skipped", "stop_price": str(stop_price), "reason": (
                    "price is already at or beyond the stop; the app-side protective stop exits it")})
                continue
            if take_price is not None and price >= take_price:
                take_price = None  # a take-profit below the market would sell at once
            capacity -= whole
            key = f"exit-rearm:{account.id}:{symbol}:{owner}:{whole}:{now.isoformat()}"
            client_order_id = REARM_CLIENT_PREFIX + hashlib.sha256(key.encode()).hexdigest()[:45]
            oco = take_price is not None
            order = StockPaperOrder(
                account_id=account.id, client_order_id=client_order_id, symbol=symbol, side="sell",
                quantity=whole, order_type="limit" if oco else "stop", time_in_force="gtc",
                limit_price=take_price if oco else None, reserved_cash=Decimal("0"), status="reserved",
                source=anchor.source if anchor is not None else REARM_SOURCE,
                strategy_id=owner, trial_id=None,
                evidence_id=anchor.evidence_id if anchor is not None else None,
                order_class="oco" if oco else "simple", leg_role="take_profit" if oco else "stop_loss",
                stop_price=stop_price, take_profit_price=take_price,
            )
            db.add(order)
            db.flush()
            outcome = _submit(db, account, gateway, order)
            lots.append(lot | {"status": outcome, "order_id": order.id, "quantity": _fmt(whole),
                               "order_class": "oco" if oco else "stop",
                               "stop_price": str(stop_price),
                               "take_profit_price": str(take_price) if take_price is not None else None})
            if outcome == "unknown":
                halted_now = True
                break
            if outcome == "rejected":
                break
            account = active_paper_account(db, for_update=True)
        results.append(entry | {"status": "evaluated", "lots": lots})
    submitted = sum(1 for item in results for lot in item.get("lots", []) if lot.get("status") == "submitted")
    return base | {
        "status": "halted" if halted_now else "complete",
        "kill_switch": kill_switch,
        "account_halted": halted,
        "submitted_count": submitted,
        "cancelled_partial_entries": cancelled_entries,
        "results": results,
    }
