"""Scheduled, paper-only execution through the governed stock-paper ledger.

This service deliberately does not use the legacy ``PaperTrade`` simulator.
It generates one signal per configured symbol and approved paper strategy (in
one batched pass, see ``stock_paper_signal_batch``), then lets the durable
stock-paper reservation and dispatch gates decide whether an order can be
submitted to Alpaca paper.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import logging
import math
import time
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import RiskRule, Strategy, StrategySignal
from app.models.stock_paper import StockPaperFill, StockPaperOrder, StockPaperPosition
from app.services.stock_paper_ledger import (
    NONTERMINAL_ORDER_STATUSES,
    StockPaperError,
    active_paper_account,
    alert_repeated_order_rejects,
    EXPLORATION_SOURCE,
    EXPLORATION_STRATEGY_STATUSES,
    broker_stop_coverage,
    build_exit_plan,
    cancel_broker_exit_orders,
    cancel_aged_open_exits,
    cancel_stale_exit_entries,
    dispatch_reserved_order,
    live_exit_orders,
    release_unsubmitted_reservation,
    reserve_stock_paper_order,
)
from app.services.earnings_calendar import earnings_blackout, earnings_known, strategy_blackout_sessions
from app.services.exploration_budget import (
    SKIP_REASON as EXPLORATION_BUDGET_REASON,
    apply_lift,
    plan_exploration,
    record_plan,
)
from app.services.market_events import cached_events, event_blackout
from app.services.market_gauges import breadth_gate_min, entry_context, gauge_size_scale, latest_gauges
from app.services.entry_protections import (
    SELL_RETRY_LIMIT,
    load_entry_protections,
    protective_stop_idempotency_key,
    sell_retry_key,
)
from app.services.intraday_data import session_bounds
from app.services.meta_label_shadow import resolve_runtime as resolve_meta_label_runtime
from app.services.position_sizing import sizing_parameters
from app.services.stock_paper_signal_batch import generate_cycle_signals
from app.services.strategy_execution_policy import (
    evaluate_policy_exit,
    exit_leg_percentages,
    daily_bar_execution,
    exit_policy_for,
    symbol_scope_for,
)
from app.services.market_gate import gate_window, sector_gate_window
from app.services.trusted_data import trusted_history
from app.services.trading_universe import execution_symbols, sector_group

logger = logging.getLogger(__name__)
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
SCHEDULED_SOURCE = "scheduled_paper_signal"
QUANTITY_INCREMENT = Decimal("0.000001")
# Matches the retired simulator's STOP_LOSS_PCT; override with the
# ``position_stop_loss_pct`` risk rule (0 disables).
DEFAULT_POSITION_STOP_LOSS_PCT = Decimal("0.03")
# Broker-side take-profit leg distance; ``position_take_profit_pct`` risk rule
# (0 disables the take-profit leg).
DEFAULT_POSITION_TAKE_PROFIT_PCT = Decimal("0.03")
WHOLE_SHARE = Decimal("1")


def _is_exit_leg(row) -> bool:
    return bool(getattr(row, "leg_role", None))


def _exit_plan(rules: dict, reference_price: Decimal, policy=None) -> dict | None:
    """Broker-side stop-loss/take-profit plan for a new entry (None = disabled).

    A strategy's declared exit policy replaces the risk-rule percentages it
    sets (e.g. RSI(2): 10% catastrophe stop and no take-profit leg, sent as a
    stop-only OTO entry); strategies without a policy keep the rule defaults.
    """
    stop_pct, take_pct = exit_leg_percentages(
        policy,
        rules.get("position_stop_loss_pct", DEFAULT_POSITION_STOP_LOSS_PCT),
        rules.get("position_take_profit_pct", DEFAULT_POSITION_TAKE_PROFIT_PCT),
    )
    return build_exit_plan(reference_price, stop_loss_pct=stop_pct, take_profit_pct=take_pct)


def _is_regular_session(now: datetime) -> bool:
    local = now.astimezone(ET)
    bounds = session_bounds(local.date())
    return bool(bounds and bounds[0] <= now < bounds[1])


def _quantity_for_buy(account, reference_price: Decimal, rules: dict, *, exploration: bool = False,
                      size_scale: Decimal = Decimal("1"),
                      allocation_weight: Decimal = Decimal("1")) -> Decimal:
    """Risk-fraction-sized quantity, times ``size_scale`` clamped to [0, 1].

    The scale can only shrink a position; every other cap (exposure, sector,
    ledger gates) still applies to the resulting order.  ``allocation_weight``
    is the session's bounded performance weight of the strategy
    (``strategy_allocation``), clamped to the configured floor/ceiling; it
    never applies to exploration probes.
    """
    if exploration:
        risk_fraction = Decimal(str(settings.paper_exploration_risk_fraction))
    else:
        from app.services.strategy_allocation import clamp

        risk_fraction = Decimal(str(rules.get("max_risk_per_trade", "0.01"))) * clamp(Decimal(str(allocation_weight)))
    scale = min(max(Decimal(str(size_scale)), Decimal("0")), Decimal("1"))
    notional = max(Decimal("0"), account.equity) * risk_fraction * scale
    return (notional / reference_price).quantize(QUANTITY_INCREMENT, rounding=ROUND_DOWN)


def _sized_quantity(db: Session, account, reference_price: Decimal, rules: dict, *, strategy_type: str,
                    sizing: dict, policy, signal: dict, feedback: dict, payoff_by_type: dict,
                    size_scale: Decimal, allocation_weight: Decimal) -> tuple[Decimal, dict]:
    """Quantity under the strategy's ``sizing_mode`` (``position_sizing``), and the record for its signal.

    The fixed fraction (``_quantity_for_buy``) is always the cap: a mode can
    only shrink the entry, so every later check (whole-share rounding, the
    one-share floor, the ledger's exposure gate) applies unchanged.  Kelly
    needs the strategy in meta-label enforce mode (``feedback["enforced_types"]``),
    the signal's calibrated probability and the strategy's closed paper
    trades (looked up once per cycle per strategy); anything missing falls
    back inside ``position_sizing``.
    """
    from app.services import position_sizing

    fixed = _quantity_for_buy(account, reference_price, rules, size_scale=size_scale,
                              allocation_weight=allocation_weight)
    mode = sizing["sizing_mode"]
    cap = fixed * reference_price
    if mode == position_sizing.FIXED_FRACTION:
        sized = {"mode": mode, "requested_mode": mode, "status": "sized", "notional": cap,
                 "fixed_fraction_notional": cap, "size_scale": str(size_scale),
                 "allocation_weight": str(allocation_weight)}
        return fixed, position_sizing.sizing_features(sized, quantity=fixed, reference_price=reference_price)
    stop_distance, _take = exit_leg_percentages(
        policy, rules.get("position_stop_loss_pct", DEFAULT_POSITION_STOP_LOSS_PCT), Decimal("0"))
    probability = (signal.get("meta_label") or {}).get("probability") if isinstance(signal.get("meta_label"), dict) \
        else None
    enforced = strategy_type in feedback.get("enforced_types", set())
    payoff = None
    if mode == position_sizing.FRACTIONAL_KELLY and enforced:
        if strategy_type not in payoff_by_type:
            try:
                payoff_by_type[strategy_type] = position_sizing.strategy_payoff(db, strategy_type)
            except Exception as exc:  # evidence only; the mode falls back
                logger.info("payoff ratio unavailable for %s: %s", strategy_type, exc.__class__.__name__)
                payoff_by_type[strategy_type] = {"trades": 0, "payoff_ratio": None, "status": "unavailable"}
        payoff = payoff_by_type[strategy_type]
    sized = position_sizing.size_notional(
        mode=mode, equity=account.equity, risk_fraction=Decimal("1"), size_scale=Decimal("1"),
        stop_distance=stop_distance, risk_per_trade=sizing["risk_per_trade"], probability=probability,
        payoff=payoff, enforced=enforced, cap_notional=cap)
    sized["size_scale"] = str(size_scale)
    sized["allocation_weight"] = str(allocation_weight)
    quantity = (sized["notional"] / reference_price).quantize(QUANTITY_INCREMENT, rounding=ROUND_DOWN)
    return quantity, position_sizing.sizing_features(sized, quantity=quantity, reference_price=reference_price)


def _loss_feedback_context(db: Session, strategies: list, now: datetime) -> dict:
    """Load paper loss-feedback state for one cycle.

    Each piece degrades explicitly (recorded in the summary) to the neutral
    behaviour: no overlay, shadow-only meta-label, weight 1.0.
    """
    out = {"overrides": {}, "enforced_types": set(), "weights": {}, "allocation_features": {}, "summary": {}}
    try:
        from app.services.parameter_application import parameter_overrides

        with db.begin_nested():
            out["overrides"] = parameter_overrides(db)
        out["summary"]["parameter_sets"] = {key: value["version"] for key, value in out["overrides"].items()}
    except Exception as exc:
        out["summary"]["parameter_sets_error"] = f"{exc.__class__.__name__}: {exc}"
    try:
        from app.services.meta_label_modes import enforced_strategy_types

        with db.begin_nested():
            out["enforced_types"] = enforced_strategy_types(db)
        out["summary"]["meta_label_enforced"] = sorted(out["enforced_types"])
    except Exception as exc:
        out["summary"]["meta_label_modes_error"] = f"{exc.__class__.__name__}: {exc}"
    try:
        from app.services.strategy_allocation import allocation_features, clamp, session_weight_rows

        active = [row.strategy_type for row in strategies if row.current_status == "paper_trading_active"]
        with db.begin_nested():
            rows = session_weight_rows(db, active, session_date=now.astimezone(ET).date(), now=now)
        out["weights"] = {key: clamp(Decimal(str(row.multiplier))) for key, row in rows.items()}
        # The weight (and the regime cell behind it) is recorded on each BUY's
        # signal, so attribution can see how the entry was sized.
        out["allocation_features"] = {key: allocation_features(row) for key, row in rows.items()}
        out["summary"]["allocation_weights"] = {key: str(value) for key, value in out["weights"].items()}
        cells = {value["regime_cell"] for value in out["allocation_features"].values() if value.get("regime_cell")}
        if cells:
            out["summary"]["allocation_regime_cells"] = sorted(cells)
    except Exception as exc:
        out["weights"] = {}
        out["allocation_features"] = {}
        out["summary"]["allocation_weights_error"] = f"{exc.__class__.__name__}: {exc}"
    return out


def realized_volatility(closes, lookback: int) -> float | None:
    """Annualized close-to-close volatility of the last ``lookback`` daily returns.

    None when there are fewer than ``lookback + 1`` positive finite closes or
    the result is not a positive finite number.
    """
    values = [float(value) for value in list(closes)[-(lookback + 1):]]
    if len(values) < lookback + 1 or any(not math.isfinite(v) or v <= 0 for v in values):
        return None
    returns = [math.log(values[i] / values[i - 1]) for i in range(1, len(values))]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    vol = math.sqrt(variance) * math.sqrt(252)
    return vol if math.isfinite(vol) and vol > 0 else None


def volatility_size_scale(closes) -> dict:
    """Position-size multiplier min(1, target / realized vol), floored.

    Fails closed to the floor when volatility cannot be computed.
    """
    target = float(settings.paper_vol_target_annual)
    floor = float(settings.paper_vol_scale_floor)
    lookback = int(settings.paper_vol_lookback_days)
    if target <= 0:
        return {"scale": 1.0, "status": "disabled"}
    vol = None
    try:
        vol = realized_volatility(closes, lookback) if closes is not None else None
    except Exception:  # bad data must not size up
        vol = None
    if vol is None:
        return {"scale": floor, "status": "volatility_unavailable", "target_vol": target,
                "lookback_days": lookback}
    scale = min(1.0, max(floor, target / vol))
    return {"scale": round(scale, 6), "status": "ok", "realized_vol": round(vol, 6),
            "target_vol": target, "lookback_days": lookback}


def _volatility_scale_for(db: Session, symbol: str) -> dict:
    try:
        frame = _daily_frame(db, symbol)
        closes = frame["close"].tolist()
    except Exception:
        closes = None
    return volatility_size_scale(closes)


# Per-cycle read cache (enabled by run_stock_paper_signal_cycle). The order
# phase looked up each symbol's position twice and re-read its whole fill
# history for every holdings check: ~2-12 queries per symbol, ~30 s of every
# cycle at production's ~200 ms per query. Positions and fills only change
# through reconciliation (another session), so one bulk read per cycle is the
# same evidence. Pending orders change as the cycle trades and stay live.
CYCLE_READS = "stock_paper_cycle_reads"


def _cycle_reads(db: Session, account_id: int) -> dict | None:
    info = getattr(db, "info", None)
    reads = info.get(CYCLE_READS) if isinstance(info, dict) else None
    if not isinstance(reads, dict):
        return None
    if reads.get("account_id") != account_id:
        positions = db.query(StockPaperPosition).filter_by(account_id=account_id).all()
        fills = db.query(StockPaperFill).filter_by(account_id=account_id).all()
        order_ids = {row.order_id for row in fills if row.order_id is not None}
        orders = {row.id: row for row in db.query(StockPaperOrder).filter(StockPaperOrder.id.in_(order_ids)).all()} \
            if order_ids else {}
        fills_by_symbol: dict[str, list] = {}
        for fill in fills:
            fills_by_symbol.setdefault(fill.symbol, []).append(fill)
        reads.update(account_id=account_id, positions={row.symbol: row for row in positions},
                     fills=fills_by_symbol, orders=orders, lots={})
    return reads


def _position(db: Session, account_id: int, symbol: str):
    reads = _cycle_reads(db, account_id)
    if reads is not None:
        return reads["positions"].get(symbol)
    return db.query(StockPaperPosition).filter_by(account_id=account_id, symbol=symbol).one_or_none()


def _pending_orders(db: Session, account_id: int, symbol: str) -> list:
    return db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account_id,
        StockPaperOrder.symbol == symbol,
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).all()


def _fill_sort_key(item: tuple[int, object]) -> tuple[datetime, int]:
    index, fill = item
    filled_at = getattr(fill, "filled_at", None)
    if not isinstance(filled_at, datetime):
        return datetime.min.replace(tzinfo=UTC), index
    if filled_at.tzinfo is None:
        filled_at = filled_at.replace(tzinfo=UTC)
    return filled_at, index


def _strategy_lots(db: Session, account_id: int, symbol: str, type_by_id: dict) -> dict:
    """Per-strategy view of the *current* holding episode in ``symbol``.

    Fills are walked in time order.  Whenever the symbol's net quantity
    returns to zero no lot survives, so attribution restarts: an old
    unattributed fill (an app-side sell, a broker import) no longer marks
    every later lot as unattributed.  Returns ``holdings`` (net quantity per
    strategy type), ``attributed`` (False when a fill in the current episode
    cannot be tied to a strategy-bound order), ``opened_at`` (first buy fill
    of each strategy's open lot), ``buyers`` (strategy types that bought
    in the current episode) and ``owner_ids`` (the strategy id of each
    type's latest buy in the episode, used to attribute its exits).
    """
    reads = _cycle_reads(db, account_id)
    if reads is not None:
        cached = reads["lots"].get((symbol, id(type_by_id)))
        if cached is not None:
            return cached
        fills = reads["fills"].get(symbol, [])
        orders = reads["orders"]
    else:
        fills = db.query(StockPaperFill).filter_by(account_id=account_id, symbol=symbol).all()
        order_ids = {row.order_id for row in fills if row.order_id is not None}
        orders = {
            row.id: row for row in db.query(StockPaperOrder).filter(StockPaperOrder.id.in_(order_ids)).all()
        } if order_ids else {}
    holdings: dict[str, Decimal] = {}
    opened_at: dict[str, datetime] = {}
    buyers: set[str] = set()
    owner_ids: dict[str, int] = {}
    attributed = True
    total = Decimal("0")
    for _index, fill in sorted(enumerate(fills), key=_fill_sort_key):
        order = orders.get(fill.order_id)
        strategy_type = type_by_id.get(order.strategy_id) if order and order.strategy_id else None
        sign = Decimal("1") if fill.side == "buy" else Decimal("-1")
        total += sign * fill.quantity
        if strategy_type is None:
            attributed = False
        else:
            before = holdings.get(strategy_type, Decimal("0"))
            holdings[strategy_type] = before + sign * fill.quantity
            if fill.side == "buy":
                buyers.add(strategy_type)
                owner_ids[strategy_type] = order.strategy_id
                if before <= 0:
                    opened_at[strategy_type] = getattr(fill, "filled_at", None)
        if total <= 0:
            holdings = {key: Decimal("0") for key in holdings}
            opened_at, buyers, owner_ids, attributed = {}, set(), {}, True
    lots = {"holdings": holdings, "attributed": attributed, "opened_at": opened_at, "buyers": buyers,
            "owner_ids": owner_ids}
    if reads is not None:
        reads["lots"][(symbol, id(type_by_id))] = lots
    return lots


def _sell_owner_id(db: Session, account_id: int, symbol: str, type_by_id: dict,
                   quantity: Decimal, strategy_type: str | None = None) -> int | None:
    """Strategy id an app-side sell of ``quantity`` should be attributed to.

    Only an unambiguous owner is returned: the current episode is fully
    attributed and the strategy (``strategy_type``, or the only holder when
    None) owns at least ``quantity``.  Otherwise the sell stays unattributed,
    exactly as before, and downstream FIFO accounting handles it.
    """
    lots = _strategy_lots(db, account_id, symbol, type_by_id)
    if not lots["attributed"]:
        return None
    held = {key: value for key, value in lots["holdings"].items() if value > 0}
    if strategy_type is None:
        if len(held) != 1:
            return None
        strategy_type = next(iter(held))
    if held.get(strategy_type, Decimal("0")) < quantity:
        return None
    return lots["owner_ids"].get(strategy_type)


def _strategy_holdings(db: Session, account_id: int, symbol: str,
                       type_by_id: dict) -> tuple[dict[str, Decimal], bool]:
    """Net filled quantity per strategy type for ``symbol`` (current episode).

    Returns ``(holdings, attributed)``; ``attributed`` is False when any fill
    of the current holding episode cannot be tied to a strategy-bound order.
    """
    lots = _strategy_lots(db, account_id, symbol, type_by_id)
    return lots["holdings"], lots["attributed"]


def _lot_entry_times(db: Session, account_id: int, symbol: str, type_by_id: dict) -> dict:
    lots = _strategy_lots(db, account_id, symbol, type_by_id)
    return {"opened_at": lots["opened_at"], "buyers": lots["buyers"]}


def _daily_frame(db: Session, symbol: str):
    """Trusted completed daily bars (today's bar is withheld by the importer)."""
    return trusted_history(db, symbol, require_active=False)[0]


def _policy_exits(db: Session, account, symbol: str, now: datetime, policy_rows: list,
                  type_by_id: dict) -> list[tuple[object, dict]]:
    """Indicator / time-stop exits due for lots of strategies with an exit policy.

    Evaluated for every held lot of a policy strategy, whether or not that
    strategy is still in the cycle's tradeable set (an exit only reduces risk).
    A lot qualifies when the current episode is attributed and the strategy
    owns quantity, or when attribution is unknown but this strategy is the
    only buyer of the open episode (so the exit cannot liquidate another
    strategy's lot).
    """
    if not policy_rows:
        return []
    position = _position(db, account.id, symbol)
    if not position or not position.quantity or position.quantity <= 0:
        return []
    holdings, attributed = _strategy_holdings(db, account.id, symbol, type_by_id)
    lots = _lot_entry_times(db, account.id, symbol, type_by_id)
    exits: list[tuple[object, dict]] = []
    frame = None
    frame_loaded = False
    for strategy in policy_rows:
        strategy_type = strategy.strategy_type
        owns = attributed and holdings.get(strategy_type, Decimal("0")) > 0
        sole_buyer = not attributed and lots["buyers"] == {strategy_type}
        opened_at = lots["opened_at"].get(strategy_type)
        if not (owns or sole_buyer) or opened_at is None:
            continue
        if not frame_loaded:
            frame_loaded = True
            try:
                frame = _daily_frame(db, symbol)
            except Exception as exc:  # time stop still works without bars
                logger.info("exit policy: daily bars unavailable for %s: %s", symbol, exc)
                frame = None
        decision = evaluate_policy_exit(strategy, symbol, frame, opened_at, now)
        if decision is not None:
            exits.append((strategy, decision))
    return exits


CANCELLED_ORDER_STATUSES = frozenset({"cancelled", "canceled", "expired"})


ENTRY_IN_FLIGHT_STATUSES = NONTERMINAL_ORDER_STATUSES | {"uncertain_submission"}


class EntryRetryBlocked(Exception):
    """A same-day re-entry the session limits do not allow yet."""


def _buy_key(db: Session, account_id: int, symbol: str, base: str, strategy_ids: set[int],
             now: datetime) -> str:
    """The idempotency key for this strategy's next entry in ``symbol`` today.

    The base key is one per strategy, symbol and session. A second entry the
    same day (a re-entry after a stop, or a retry after a rejected or
    cancelled order) reused it with a new price and quantity, so the ledger
    refused it as "Idempotency key was already used for a different order"
    on every cycle for the rest of the day (DIA, GOOGL and TSLA ~230 times
    each on 2026-11-05). Each ended entry now advances the key; one still in
    flight keeps it, so a crash-and-retry still finds its own reservation.
    """
    day_start = datetime.combine(now.astimezone(ET).date(), datetime.min.time(), tzinfo=ET).astimezone(UTC)
    rows = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account_id,
        StockPaperOrder.symbol == symbol,
        StockPaperOrder.side == "buy",
        StockPaperOrder.leg_role.is_(None),
        StockPaperOrder.strategy_id.in_(strategy_ids),
        StockPaperOrder.source.in_({SCHEDULED_SOURCE, EXPLORATION_SOURCE}),
        StockPaperOrder.created_at >= day_start,
    ).all()
    ended = [row for row in rows if row.status not in ENTRY_IN_FLIGHT_STATUSES]
    if len(rows) > len(ended):
        return sell_retry_key(base, len(ended))
    if len(ended) >= settings.paper_max_entries_per_symbol_per_day:
        raise EntryRetryBlocked(
            f"{len(ended)} entries in {symbol} already ended this session for this strategy "
            f"(limit {settings.paper_max_entries_per_symbol_per_day})")
    wait = timedelta(minutes=settings.paper_entry_reject_retry_minutes)
    rejected = [_aware(row.updated_at or row.created_at) for row in ended if row.status == "rejected"]
    if rejected and wait and now - max(rejected) < wait:
        raise EntryRetryBlocked(
            f"the broker rejected the last entry in {symbol} at "
            f"{max(rejected).astimezone(ET):%H:%M} ET; retry after {settings.paper_entry_reject_retry_minutes} minutes")
    return sell_retry_key(base, len(ended))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _sell_key(db: Session, account_id: int, symbol: str, base: str, now: datetime) -> str:
    """The idempotency key for this session's next app-side exit sell.

    ``cancel_aged_open_exits`` cancels an exit that rested unfilled; the next
    one needs a fresh client order id, numbered by how many autonomous exit
    sells for the symbol were cancelled this session. While one is pending the
    count, and so the key, is unchanged (and the pending-sell guards skip).
    """
    day_start = datetime.combine(now.astimezone(ET).date(), datetime.min.time(), tzinfo=ET).astimezone(UTC)
    attempt = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account_id,
        StockPaperOrder.symbol == symbol,
        StockPaperOrder.side == "sell",
        StockPaperOrder.leg_role.is_(None),
        StockPaperOrder.source.in_({SCHEDULED_SOURCE, EXPLORATION_SOURCE}),
        StockPaperOrder.status.in_(CANCELLED_ORDER_STATUSES),
        StockPaperOrder.created_at >= day_start,
    ).count()
    if attempt > SELL_RETRY_LIMIT:
        raise StockPaperError(
            f"exit sell for {symbol} was re-issued {SELL_RETRY_LIMIT} times this session without filling; "
            "the broker-side exit re-arm keeps protecting it"
        )
    return sell_retry_key(base, attempt)


def _protective_stop(db: Session, account, symbol: str, price: Decimal | None,
                     rules: dict, session_key: str, type_by_id: dict | None = None) -> dict | None:
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
    position = _position(db, account.id, symbol)
    entry = getattr(position, "average_entry_price", None) if position else None
    if not position or position.quantity <= 0 or not entry or entry <= 0:
        return None
    loss = (entry - price) / entry
    if loss < stop_pct:
        return None
    result = {"symbol": symbol, "strategy": "protective_stop", "signal_action": "SELL",
              "reference_price": str(price), "loss_from_entry": str(loss.quantize(Decimal("0.0001")))}
    if any(row.side == "sell" and not _is_exit_leg(row) for row in _pending_orders(db, account.id, symbol)):
        return result | {"status": "skipped", "reason": "sell order is already pending"}
    # A live broker stop-loss leg already protects its lot at the broker and
    # fires even when this app is down; selling those shares here as well
    # would double-sell.  Only quantity without a live stop leg (fractional
    # legacy lots, partially filled entries) is exited app-side.
    covered = broker_stop_coverage(db, account.id, symbol)
    quantity = position.quantity - covered
    if quantity <= 0:
        return result | {"status": "skipped", "reason": "broker-side stop-loss leg protects this position",
                         "broker_stop_quantity": str(covered)}
    if covered > 0:
        result["broker_stop_quantity"] = str(covered)
    try:
        owner_id = _sell_owner_id(db, account.id, symbol, type_by_id, quantity) if type_by_id else None
        order = reserve_stock_paper_order(
            db, symbol=symbol, side="sell", quantity=quantity, reference_price=price,
            idempotency_key=_sell_key(db, account.id, symbol, protective_stop_idempotency_key(session_key, symbol),
                                      datetime.now(UTC)),
            source=SCHEDULED_SOURCE, strategy_id=owner_id,
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


class _EntryLimits:
    """Open-position, exploration-lot and exploration-confidence limits for one cycle.

    Mirrors the refusals in ``reserve_stock_paper_order`` /
    ``_validate_exploration_buy`` with the same messages, so a BUY that would
    certainly be refused skips the reservation. ``None`` from ``check`` only
    means "not refused here"; the reservation still decides.
    """

    def __init__(self, held: set[str], max_open: int, exploration_lots: int | None,
                 max_lots: int, min_confidence: float | None):
        self.held, self.max_open = held, max_open
        self.exploration_lots, self.max_lots = exploration_lots, max_lots
        self.min_confidence = min_confidence

    def check(self, symbol: str, signal: dict, *, exploring: bool) -> str | None:
        if symbol not in self.held and len(self.held) >= self.max_open:
            return "Order exceeds the configured open-position risk limit"
        if exploring:
            try:
                confidence = float(signal.get("confidence"))
            except (TypeError, ValueError):
                confidence = None
            if self.min_confidence is not None and confidence is not None and (
                not math.isfinite(confidence) or confidence < self.min_confidence
            ):
                return "Exploration signal confidence is below the minimum threshold"
            if self.exploration_lots is not None and self.exploration_lots >= self.max_lots:
                return "Exploration lot limit reached"
        return None

    def record_buy(self, symbol: str, *, exploring: bool) -> None:
        self.held.add(symbol)
        if exploring and self.exploration_lots is not None:
            self.exploration_lots += 1


def _entry_limit_state(db: Session, account, rules: dict) -> _EntryLimits:
    from app.services.stock_paper_ledger import _open_exploration_lots

    try:
        held = {row.symbol for row in db.query(StockPaperPosition).filter(
            StockPaperPosition.account_id == account.id, StockPaperPosition.quantity > 0).all()}
        lots = _open_exploration_lots(db, account) if settings.paper_exploration_enabled else None
    except Exception as exc:  # fall back to the reservation's own checks
        db.rollback()
        logger.info("entry limit pre-check unavailable: %s", exc.__class__.__name__)
        return _EntryLimits(set(), 10**9, None, 10**9, None)
    try:
        min_confidence = float(rules["min_confidence"]) if "min_confidence" in rules else None
    except (TypeError, ValueError):
        min_confidence = None
    try:
        # Only an explicit limit is pre-checked; otherwise the reservation decides.
        max_open = int(rules["max_open_positions"]) if "max_open_positions" in rules else 10**9
    except (TypeError, ValueError):
        max_open = 10**9
    return _EntryLimits(held, max_open, lots, int(settings.paper_exploration_max_positions), min_confidence)


def _max_positions_per_sector(rules: dict) -> int:
    value = rules.get("max_positions_per_sector", settings.paper_max_positions_per_sector)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return int(settings.paper_max_positions_per_sector)


def _news_universe(db: Session, now: datetime, core: set[str]) -> tuple[dict[str, dict], set[str]]:
    """News-discovered symbols admitted for entries, and ones kept for exits only."""
    if not settings.news_discovery_enabled:
        return {}, set()
    from app.services.news_discovery import news_universe

    try:
        admitted, exits_only = news_universe(db, now)
    except Exception as exc:  # discovery state must never stop the cycle
        db.rollback()
        logger.info("news discovery state unavailable: %s", exc.__class__.__name__)
        return {}, set()
    return ({symbol: row for symbol, row in admitted.items() if symbol not in core},
            exits_only - core - set(admitted))


def _news_context(news: dict) -> dict:
    headlines = news.get("headlines") or []
    return {
        "source": "alpaca_news",
        "discovered": True,
        "sentiment_score": news.get("avg_sentiment"),
        "articles": news.get("articles"),
        "admitted_at": news.get("admitted_at"),
        "top_headline": headlines[0].get("headline") if headlines else None,
    }


def _market_backdrop(db: Session, now: datetime) -> tuple[list[dict] | None, dict]:
    """Scheduled announcements and market gauges; unavailable is never a block."""
    events, gauges = None, {}
    try:
        events = cached_events(db)
    except Exception as exc:
        db.rollback()
        logger.info("market event calendar unavailable: %s", exc.__class__.__name__)
    try:
        gauges = latest_gauges(db, now)
    except Exception as exc:
        db.rollback()
        logger.info("market gauges unavailable: %s", exc.__class__.__name__)
    return events, gauges


def _tag_signal(db: Session, signal_id, key: str, value: dict) -> None:
    """Evidence for loss diagnosis on the entry's signal row; never blocks the entry."""
    try:
        row = db.get(StrategySignal, signal_id)
        if row is not None:
            row.features = {**(row.features or {}), key: value}
            db.flush()
    except Exception as exc:
        logger.info("%s not recorded on signal %s: %s", key, signal_id, exc.__class__.__name__)


def _tag_signal_news(db: Session, signal_id, news: dict) -> None:
    """Record the news that admitted the symbol on the entry's signal row.

    Loss diagnosis reads ``features.news_context``: a loss on a stock bought
    on positive news that fell on its own is ``news_wrong``.
    """
    try:
        row = db.get(StrategySignal, signal_id)
        if row is not None:
            row.features = {**(row.features or {}), "news_context": _news_context(news)}
            db.flush()
    except Exception as exc:  # evidence only; never block the entry
        logger.info("news context not recorded on signal %s: %s", signal_id, exc.__class__.__name__)


def _other_holders(symbol: str, strategy_type: str, holdings: dict, pending: list, type_by_id: dict,
                   cycle_buyers: dict[str, set[str]], held_symbols: set[str]) -> list[str]:
    """Other strategies holding, buying or (this cycle) bought ``symbol``."""
    holders = {kind for kind, quantity in holdings.items() if kind != strategy_type and quantity > 0}
    holders |= {type_by_id.get(row.strategy_id) or "an unattributed order" for row in pending
                if row.side == "buy" and not _is_exit_leg(row)} - {strategy_type}
    holders |= cycle_buyers.get(symbol, set()) - {strategy_type}
    if not holders and symbol in held_symbols:
        holders.add("an unattributed position")
    return sorted(str(holder) for holder in holders)


def _sector_exposure(db: Session, account_id: int) -> dict[str, set[str]]:
    """Symbols held or being bought, grouped by sector ETF group."""
    held = db.query(StockPaperPosition).filter(
        StockPaperPosition.account_id == account_id, StockPaperPosition.quantity > 0
    ).all()
    buying = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account_id,
        StockPaperOrder.side == "buy",
        StockPaperOrder.status.in_(NONTERMINAL_ORDER_STATUSES),
    ).all()
    groups: dict[str, set[str]] = {}
    for row in [*held, *buying]:
        symbol = getattr(row, "symbol", None)
        if symbol:
            symbol = str(symbol).upper()
            groups.setdefault(sector_group(symbol), set()).add(symbol)
    return groups


BUY_OUTCOME_ACTION = "buy_signal_outcomes"


def _record_buy_outcomes(db: Session, results: list[dict], now: datetime) -> None:
    """Audit what happened to every BUY signal, only when that picture changes.

    State signals repeat every minute; recording each repeat would flood the
    audit log, so a row is written only when the set of outcomes differs from
    the previous one. This is the evidence for why signals did not trade.
    A BUY a strategy's own gate turned into a HOLD (``gated_action``) counts
    as a withheld BUY, so ``gate_cost`` can score what the gate stopped.
    """
    from app.models import AuditLog
    from app.services.audit import write_audit_log

    outcomes = sorted(
        (
            {"symbol": item.get("symbol"), "strategy": item.get("strategy"),
             "mode": item.get("mode", "governed"), "status": item.get("status"),
             "reason": item.get("reason")}
            for item in results
            if item.get("signal_action") == "BUY" or item.get("gated_action") == "BUY"
        ),
        key=lambda row: (str(row["symbol"]), str(row["strategy"])),
    )
    if not outcomes:
        return
    previous = db.query(AuditLog).filter(
        AuditLog.event_type == "stock_paper", AuditLog.action == BUY_OUTCOME_ACTION,
    ).order_by(AuditLog.id.desc()).first()
    if previous is not None and (previous.payload or {}).get("outcomes") == outcomes:
        return
    counts: dict[str, int] = {}
    for row in outcomes:
        counts[row["status"] or "unknown"] = counts.get(row["status"] or "unknown", 0) + 1
    write_audit_log(
        db, event_type="stock_paper", action=BUY_OUTCOME_ACTION, status="recorded",
        message="BUY signal outcomes changed",
        payload={"observed_at": now.isoformat(), "counts": counts, "outcomes": outcomes, "paper_only": True},
    )
    db.commit()


def run_stock_paper_signal_cycle(db: Session) -> dict:
    """Run one cycle, reusing launch-admission gates across its orders."""
    from app.services.stock_paper_ledger import LAUNCH_GATES_CACHE, LAUNCH_GATES_CACHE_ENABLED

    info = getattr(db, "info", None)
    if not isinstance(info, dict):  # test doubles without a session info dict
        return _run_signal_cycle(db)
    info[LAUNCH_GATES_CACHE_ENABLED] = True
    info[CYCLE_READS] = {}
    try:
        return _run_signal_cycle(db)
    finally:
        info.pop(LAUNCH_GATES_CACHE_ENABLED, None)
        info.pop(LAUNCH_GATES_CACHE, None)
        info.pop(CYCLE_READS, None)


def _run_signal_cycle(db: Session) -> dict:
    """Run one bounded multi-symbol/multi-strategy paper execution cycle.

    A cycle outside market hours is a safe no-op.  During market hours every
    BUY/SELL still passes through the existing reservation and dispatch gates;
    a failed gate is recorded in the result and does not become an order.
    """
    cycle_started = time.perf_counter()
    now = datetime.now(UTC)
    account = active_paper_account(db)
    symbols = execution_symbols()
    base = {
        "job": "paper_trading_signal_job",
        "paper_only": True,
        "live_trading": False,
        "symbols": list(symbols),
    }
    if account is None:
        return base | {"status": "skipped", "reason": "stock paper account is not initialized"}
    if not _is_regular_session(now):
        return base | {"status": "skipped", "reason": "regular market session is closed"}
    if account.status != "reconciled" or account.reconciliation_required or account.unexplained_residual:
        return base | {"status": "skipped", "reason": "stock paper account is not reconciled"}
    try:
        stale_entries = cancel_stale_exit_entries(db, account, now)
    except StockPaperError as exc:
        db.rollback()
        stale_entries = {"status": "blocked", "reason": str(exc)}
    if stale_entries:
        base["stale_entry_cancellations"] = stale_entries
    try:
        stale_exits = cancel_aged_open_exits(db, account, now)
    except StockPaperError as exc:
        db.rollback()
        stale_exits = {"status": "blocked", "reason": str(exc)}
    if stale_exits:
        base["stale_exit_cancellations"] = stale_exits
    try:
        reject_alert = alert_repeated_order_rejects(db, account, now)
    except Exception as exc:  # alerting must never stop the cycle
        db.rollback()
        logger.warning("order reject alert unavailable: %s", exc)
        reject_alert = None
    if reject_alert:
        base["order_reject_alert"] = reject_alert
    # News-discovered symbols: admitted ones are traded learning-sized; an
    # expired one stays in the cycle while held so its exits still run.
    news_admitted, news_exits_only = _news_universe(db, now, set(symbols))
    if news_admitted or news_exits_only:
        symbols = [*symbols, *sorted(set(news_admitted) | news_exits_only)]
        base["symbols"] = list(symbols)
        base["news_discovered"] = {"admitted": sorted(news_admitted), "exits_only": sorted(news_exits_only)}
    market_events, gauges = _market_backdrop(db, now)
    market_context = entry_context(gauges, market_events, now)
    base["market_context"] = market_context
    gauge_scale = gauge_size_scale(gauges) if settings.paper_market_gauge_sizing_enabled else None

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
    strategy_rows = db.query(Strategy).order_by(Strategy.id).all()
    # Paper loss feedback loop: governed parameter overlays (never in-place
    # edits), per-strategy meta-label enforce and session allocation weights.
    feedback = _loss_feedback_context(db, strategies, now)
    base["loss_feedback"] = feedback["summary"]
    if feedback["overrides"]:
        from app.services.parameter_application import overlay

        strategies = [overlay(row, feedback["overrides"]) for row in strategies]
        strategy_rows = [overlay(row, feedback["overrides"]) for row in strategy_rows]
    type_by_id = {row.id: row.strategy_type for row in strategy_rows}
    # Per-strategy execution policy: symbol scope, broker exit legs and the
    # app-side indicator/time-stop exits.  Strategies without a declared
    # policy keep the risk-rule legs, SELL-signal exits and the full universe.
    scope_by_type = {row.strategy_type: symbol_scope_for(row) for row in strategies}
    policy_by_type = {row.strategy_type: exit_policy_for(row) for row in strategies}
    # Per-strategy sizing mode (``position_sizing``); the payoff ratio Kelly
    # needs is looked up at most once per strategy per cycle.
    sizing_by_type = {row.strategy_type: sizing_parameters(getattr(row, "parameters", None)) for row in strategies}
    payoff_by_type: dict[str, dict] = {}
    daily_bar_types = {row.strategy_type for row in strategies if daily_bar_execution(row)}
    cycle_rows = {row.strategy_type: row for row in strategies}
    policy_rows: list = []
    for row in [*strategies, *strategy_rows]:
        if row.strategy_type in {item.strategy_type for item in policy_rows}:
            continue
        policy = policy_by_type.get(row.strategy_type) if row.strategy_type in cycle_rows else exit_policy_for(row)
        if policy is not None and (policy.indicator_exit or policy.max_holding_sessions):
            policy_rows.append(row)
    # Phase 0: every strategy signal for every symbol in one batched pass
    # (one data load per symbol, one commit).  Outcomes are the same signal
    # dicts / exceptions the per-pair create_* calls produced.
    # Meta-label filter: "shadow" (default) only records what it would do;
    # only "enforce" (off by default) can skip a BUY.
    meta_label = resolve_meta_label_runtime(enforced_types=feedback["enforced_types"])
    signal_outcomes, signal_stats = generate_cycle_signals(
        db, symbols, strategies, meta_label=meta_label, now=now,
        parameter_overrides=feedback["overrides"] or None,
    )
    signals_done = time.perf_counter()
    sector_cap = _max_positions_per_sector(rules)
    sector_groups = _sector_exposure(db, account.id) if sector_cap else {}
    # Entry-only circuit breakers (stop-loss guard, exit cooldown, low-profit
    # lock, session drawdown, time of day); never consulted for sells.
    protections = load_entry_protections(
        db, account, now=now, rules=rules, type_by_id=type_by_id,
        market_gates={row.strategy_type: gate_window(getattr(row, "parameters", None)) for row in strategies},
        # Sector gate (per-symbol sector ETF trend) and breadth gate (latest
        # gauge, fail open) per strategy row; the session entry cap reads the
        # risk rule through ``protection_config``.
        sector_gates={row.strategy_type: sector_gate_window(getattr(row, "parameters", None)) for row in strategies},
        symbols=symbols,
        breadth_gates={row.strategy_type: breadth_gate_min(getattr(row, "parameters", None)) for row in strategies},
        gauges=gauges,
    )
    # Cycle-level entry limits. Each refused BUY used to go through a full
    # reservation (~10 queries under the account lock) only to hit one of
    # these; they are decided here from one lookup and the reservation still
    # re-checks every order that passes.
    entry_limits = _entry_limit_state(db, account, rules)
    # Exploration budget: this session's learning-sized lots go to the
    # learning BUYs whose evidence is scarcest (exploration_budget.py), in
    # priority order until the budget is spent.  Scheduled entries, exits
    # and every cap are untouched; the plan never raises into the cycle.
    exploration_plan = plan_exploration(db, account, signal_outcomes, strategies, news_admitted, now=now)
    base["exploration_budget"] = exploration_plan.summary()
    # Time spent reserving (account row lock, gates, risk checks) and
    # dispatching (broker POST), so a slow orders phase shows its cause.
    order_ms = {"reserve": 0.0, "dispatch": 0.0}
    base["entry_protections"] = protections.summary()
    results: list[dict] = []
    cycle_buyers: dict[str, set[str]] = {}
    for symbol in symbols:
        # Phase 1: observe every strategy for this symbol before acting, so
        # opposing signals from different strategies are resolved
        # deterministically instead of by strategy id order.
        actionable: list[tuple[object, dict, dict]] = []
        observed_price: Decimal | None = None
        for strategy in strategies:
            scope = scope_by_type.get(strategy.strategy_type)
            if scope is not None and symbol not in scope:
                continue  # outside this strategy's symbol scope; no signal was generated
            result = {"symbol": symbol, "strategy": strategy.strategy_type}
            try:
                # Test doubles and older rows may omit the status field; the
                # existing cycle contract treated those rows as active.
                research_only = getattr(strategy, "current_status", "paper_trading_active") != "paper_trading_active"
                signal = signal_outcomes.get((symbol, strategy.strategy_type))
                if signal is None:
                    raise StockPaperError("signal was not generated for this symbol and strategy")
                if isinstance(signal, BaseException):
                    raise signal
                result.update({
                    "signal_id": signal["signal_id"],
                    "signal_action": signal["signal_action"],
                    "reference_price": signal["reference_price"],
                })
                if signal.get("meta_label") is not None:
                    result["meta_label"] = signal["meta_label"]
                if observed_price is None:
                    observed_price = Decimal(signal["reference_price"])
                policy = policy_by_type.get(strategy.strategy_type)
                if signal["signal_action"] == "SELL" and policy is not None and not policy.sell_signal_exit:
                    # Its backtest never exited on a SELL; the stop, target and
                    # time stop are the tested exits.
                    result.update({"status": "observed",
                                   "reason": "tested exit plan has no SELL-signal exit (stop, target, time stop only)"})
                    results.append(result)
                    continue
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
                elif signal.get("gated_action") == "BUY":
                    # The strategy's own gate (VIX, regime) withheld a BUY:
                    # recorded like any other blocked BUY (gate_cost.py scores
                    # it); no order path.
                    result.update({"status": "skipped", "gated_action": "BUY",
                                   "reason": signal.get("gate_reason") or "strategy gate withheld the BUY"})
                else:
                    result.update({"status": "observed", "reason": "signal is not actionable"})
            except StockPaperError as exc:
                result.update({"status": "blocked", "reason": str(exc)})
            except Exception as exc:  # one symbol/strategy must not stop the fan-out
                db.rollback()
                result.update({"status": "failed", "reason": f"{exc.__class__.__name__}: {exc}"})
            results.append(result)
        stop_result = _protective_stop(db, account, symbol, observed_price, rules, session_key, type_by_id)
        if stop_result is not None:
            results.append(stop_result)
        try:
            due_exits = _policy_exits(db, account, symbol, now, policy_rows, type_by_id)
        except Exception as exc:  # never let exit evaluation stop the cycle
            db.rollback()
            due_exits = []
            results.append({"symbol": symbol, "strategy": "exit_policy", "status": "failed",
                            "reason": f"{exc.__class__.__name__}: {exc}"})
        for strategy, decision in due_exits:
            strategy_type = strategy.strategy_type
            same = [item for item in actionable if item[0].strategy_type == strategy_type]
            if any(item[1]["signal_action"] == "SELL" for item in same):
                # The strategy's own SELL already exits through the same path
                # and idempotency key; just record why the policy agrees.
                for item in same:
                    item[2]["exit_policy"] = decision
                continue
            # A persistent BUY from the holding strategy is superseded by its
            # exit (it could only be skipped as pyramiding anyway).
            actionable = [item for item in actionable if item[0].strategy_type != strategy_type]
            outcome = signal_outcomes.get((symbol, strategy_type))
            signal_id = outcome.get("signal_id") if isinstance(outcome, dict) else None
            result = {"symbol": symbol, "strategy": strategy_type, "signal_id": signal_id,
                      "signal_action": "SELL", "exit_policy": decision}
            if same:
                result["superseded_signal"] = same[0][1]["signal_action"]
            if observed_price is None:
                result.update({"status": "blocked",
                               "reason": "no trusted reference price this cycle; policy exit retries next cycle"})
                results.append(result)
                continue
            result["reference_price"] = str(observed_price)
            status = getattr(strategy, "current_status", "paper_trading_active")
            if (
                status != "paper_trading_active"
                and settings.paper_exploration_enabled
                and status in EXPLORATION_STRATEGY_STATUSES
            ):
                result["mode"] = "explore"
            actionable.append((strategy, {"signal_id": signal_id, "signal_action": "SELL",
                                          "reference_price": str(observed_price)}, result))
        if not actionable:
            continue

        # Phase 2: exits first, then entries.  An exit submitted (or already
        # pending) for a symbol suppresses same-cycle entries on that symbol,
        # which previously produced a buy and a sell of the same name in one
        # cycle and paid the spread twice.
        holdings, attributed = _strategy_holdings(db, account.id, symbol, type_by_id)
        pending = _pending_orders(db, account.id, symbol)
        # Broker exit legs are resting protection, not an in-flight exit.
        exiting = any(row.side == "sell" and not _is_exit_leg(row) for row in pending)
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
                    if symbol in news_exits_only:
                        result.update({"status": "skipped",
                                       "reason": "news admission expired; the symbol stays in the cycle for exits only"})
                        results.append(result)
                        continue
                    holders = _other_holders(symbol, strategy.strategy_type, holdings, pending, type_by_id,
                                             cycle_buyers, entry_limits.held) \
                        if settings.paper_one_position_per_symbol else []
                    if holders:
                        # Two strategies in one name doubled every loss on it
                        # (GOOGL x3, META, AMD and AAPL x2 in the first 20 trades).
                        result.update({"status": "skipped",
                                       "reason": f"one position per symbol: {', '.join(holders)} already holds "
                                                 f"or is buying {symbol}"})
                        results.append(result)
                        continue
                    protection_block = protections.check(symbol, strategy.strategy_type)
                    if protection_block is not None:
                        result.update(protection_block)
                        results.append(result)
                        continue
                    earnings_block = earnings_blackout(
                        symbol, now, sessions=strategy_blackout_sessions(getattr(strategy, "parameters", None)))
                    if earnings_block is not None:
                        result.update(earnings_block)
                        results.append(result)
                        continue
                    if not earnings_known(symbol):
                        result["earnings_unknown"] = True
                    # Every lot in this cycle may be held overnight (no
                    # end-of-day flatten), so the session-before rule applies.
                    event_block = event_blackout(market_events, now, holds_overnight=True)
                    if event_block is not None:
                        result.update(event_block)
                        results.append(result)
                        continue
                    if market_events is None:
                        result["event_calendar_unknown"] = True
                    group = sector_group(symbol)
                    group_symbols = sector_groups.get(group, set())
                    if sector_cap and symbol not in group_symbols and len(group_symbols) >= sector_cap:
                        # Correlation guard: a new name in a sector group that
                        # already has ``sector_cap`` open/pending names.
                        result.update({
                            "status": "skipped",
                            "reason": (
                                f"sector group {group} already has {len(group_symbols)} open or pending "
                                f"positions (cap {sector_cap})"
                            ),
                        })
                        results.append(result)
                        continue
                    limit_block = entry_limits.check(symbol, signal, exploring=result.get("mode") == "explore")
                    if limit_block is not None:
                        result.update({"status": "blocked", "reason": limit_block})
                        results.append(result)
                        continue
                    if any(_is_exit_leg(row) for row in pending):
                        # Another lot's resting exit legs are opposite-side
                        # orders; Alpaca may reject a new entry as a potential
                        # wash trade, and a rejected POST halts the ledger.
                        result.update({"status": "skipped", "reason": "broker-side exit legs are live for this symbol; no new entry until they close"})
                        results.append(result)
                        continue
                    meta_allowed, meta_reason = meta_label.allows_buy(signal.get("meta_label"), strategy.strategy_type)
                    if not meta_allowed:
                        # Global enforce, or this strategy was auto-promoted
                        # to enforce on measured shadow precision/recall.
                        result.update({"status": "skipped", "reason": meta_reason})
                        results.append(result)
                        continue
                    size_scale = Decimal("1")
                    if strategy.strategy_type in daily_bar_types:
                        # Volatility-scaled sizing (never above 1x); recorded
                        # on the cycle result next to the order it sized.
                        vol_scale = _volatility_scale_for(db, symbol)
                        result["volatility_scale"] = vol_scale
                        size_scale = Decimal(str(vol_scale["scale"]))
                    if gauge_scale is not None and gauge_scale["reasons"]:
                        result["market_gauge_scale"] = {"scale": str(gauge_scale["scale"]),
                                                        "reasons": gauge_scale["reasons"]}
                        size_scale *= gauge_scale["scale"]
                    exploring = result.get("mode") == "explore"
                    news = news_admitted.get(symbol)
                    if news is not None:
                        # Admitted on news, not on the strategy's track record:
                        # learning-sized whatever the strategy's status.
                        result["news_discovery"] = _news_context(news)
                    allocation_weight = feedback["weights"].get(strategy.strategy_type, Decimal("1"))
                    if not exploring and news is None:
                        result["allocation_weight"] = str(allocation_weight)
                    if exploring or news is not None:
                        # Learning-sized probes keep the exploration fraction;
                        # sizing modes apply to scheduled entries only.  The
                        # exploration budget decides whether this lot is
                        # taken this cycle and whether its evidence is scarce
                        # enough to lift it (never above the scheduled size).
                        lot = exploration_plan.decide(symbol, strategy.strategy_type)
                        result["exploration"] = lot
                        if lot["status"] == "skipped":
                            result.update({"status": "skipped", "reason": EXPLORATION_BUDGET_REASON})
                            results.append(result)
                            continue
                        quantity = _quantity_for_buy(account, reference_price, rules, exploration=True,
                                                     size_scale=size_scale, allocation_weight=allocation_weight)
                        if lot.get("multiplier", 1.0) > 1.0:
                            quantity = apply_lift(
                                quantity, lot["multiplier"],
                                _quantity_for_buy(account, reference_price, rules, size_scale=size_scale))
                            lot["lifted_quantity"] = str(quantity)
                    else:
                        quantity, result["sizing"] = _sized_quantity(
                            db, account, reference_price, rules, strategy_type=strategy.strategy_type,
                            sizing=sizing_by_type.get(strategy.strategy_type) or sizing_parameters(None),
                            policy=policy_by_type.get(strategy.strategy_type), signal=signal, feedback=feedback,
                            payoff_by_type=payoff_by_type, size_scale=size_scale,
                            allocation_weight=allocation_weight)
                    if quantity <= 0:
                        result.update({"status": "blocked", "reason": "risk-sized quantity rounded to zero"})
                        results.append(result)
                        continue
                    exit_plan = _exit_plan(rules, reference_price, policy_by_type.get(strategy.strategy_type))
                    if exit_plan is not None:
                        # Alpaca only accepts bracket/OTO orders for whole
                        # shares; round down rather than enter without exits.
                        whole = quantity.quantize(WHOLE_SHARE, rounding=ROUND_DOWN)
                        round_up = Decimal(str(settings.paper_min_share_round_up))
                        one_share_cap = max(Decimal("0"), account.equity) * Decimal(
                            str(settings.paper_min_one_share_equity_fraction))
                        if whole <= 0 and round_up > 0 and quantity >= round_up:
                            # QQQ, COST and LLY sized to 0.5-0.99 shares were
                            # skipped every cycle; one share is the smallest
                            # bracketable position and stays under 2x the budget.
                            whole = WHOLE_SHARE
                            result["rounded_up_from"] = str(quantity)
                        elif whole <= 0 and one_share_cap > 0 and reference_price <= one_share_cap:
                            # Exploration probes are 0.25% of equity: below one
                            # share of SPY, META or MSFT.  One share capped at a
                            # small fraction of equity lets them gather evidence.
                            whole = WHOLE_SHARE
                            result["rounded_up_from"] = str(quantity)
                            result["one_share_floor"] = str(one_share_cap)
                        if whole <= 0:
                            result.update({
                                "status": "skipped",
                                "reason": (
                                    f"risk-sized quantity {quantity} is below one whole share; broker-side "
                                    "bracket exits require whole shares (Alpaca rejects fractional brackets)"
                                ),
                            })
                            results.append(result)
                            continue
                        quantity = whole
                        result["exit_plan"] = {
                            "order_class": exit_plan["order_class"],
                            "stop_price": str(exit_plan["stop_price"]) if exit_plan["stop_price"] is not None else None,
                            "take_profit_price": (
                                str(exit_plan["take_profit_price"]) if exit_plan["take_profit_price"] is not None else None
                            ),
                        }
                    side = "buy"
                else:
                    position = db.query(StockPaperPosition).filter_by(
                        account_id=account.id, symbol=symbol
                    ).one_or_none()
                    pending_sell = next((row for row in pending if row.side == "sell" and not _is_exit_leg(row)), None)
                    if not position or position.quantity <= 0:
                        result.update({"status": "skipped", "reason": "sell signal has no long position"})
                        results.append(result)
                        continue
                    strategy_ids = {sid for sid, kind in type_by_id.items() if kind == strategy.strategy_type}
                    exit_orders = live_exit_orders(
                        db, account.id, symbol, strategy_ids=strategy_ids if attributed else None,
                    ) if any(_is_exit_leg(row) for row in pending) else []
                    if pending_sell and not (exit_orders and pending_sell.status == "reserved"):
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
                    # Attributed exits sell at most the strategy's own lot,
                    # so they are booked against the strategy that opened it.
                    sell_owner = (
                        _strategy_lots(db, account.id, symbol, type_by_id)["owner_ids"].get(strategy.strategy_type)
                        if attributed else None
                    )

                exploring = result.get("mode") == "explore"
                if side == "buy" and symbol in news_admitted:
                    _tag_signal_news(db, signal["signal_id"], news_admitted[symbol])
                if side == "buy":
                    _tag_signal(db, signal["signal_id"], "market_context", market_context)
                if side == "buy" and "allocation_weight" in result:
                    _tag_signal(db, signal["signal_id"], "allocation",
                                feedback["allocation_features"].get(strategy.strategy_type)
                                or {"multiplier": str(allocation_weight), "status": "unweighted"})
                if side == "buy" and "sizing" in result:
                    _tag_signal(db, signal["signal_id"], "sizing", result["sizing"])
                if side == "buy" and "exploration" in result:
                    # The multiplier and its reason; the ledger admits a
                    # lifted learning lot only from this record.
                    _tag_signal(db, signal["signal_id"], "exploration", result["exploration"])
                extra = {}
                if side == "buy" and exit_plan is not None:
                    extra["exit_plan"] = exit_plan
                if side == "sell" and sell_owner is not None:
                    # Book the exit against the strategy whose lot it closes.
                    extra["strategy_id"] = sell_owner
                if side == "sell" and exit_orders:
                    # Reserve first so every gate (kill switch, session,
                    # feed, reconciliation) passes before any exit leg is
                    # touched; dispatch still refuses while a leg is live.
                    extra["replacing_exit_order_ids"] = {row.id for row in exit_orders}
                idempotency_key = (
                    f"{'explore' if exploring else 'scheduled'}-paper:{session_key}:{symbol}:"
                    f"{strategy.strategy_type}:{side}"
                )
                if side == "sell":
                    idempotency_key = _sell_key(db, account.id, symbol, idempotency_key, now)
                else:
                    try:
                        idempotency_key = _buy_key(
                            db, account.id, symbol, idempotency_key,
                            {sid for sid, kind in type_by_id.items() if kind == strategy.strategy_type}, now)
                    except EntryRetryBlocked as blocked:
                        result.update({"status": "skipped", "reason": str(blocked)})
                        results.append(result)
                        continue
                reserve_started = time.perf_counter()
                try:
                    order = reserve_stock_paper_order(
                        db,
                        symbol=symbol,
                        side=side,
                        quantity=quantity,
                        reference_price=reference_price,
                        idempotency_key=idempotency_key,
                        source=EXPLORATION_SOURCE if exploring else SCHEDULED_SOURCE,
                        signal_id=signal["signal_id"],
                        **extra,
                    )
                finally:
                    order_ms["reserve"] += (time.perf_counter() - reserve_started) * 1000
                if side == "sell" and exit_orders:
                    cancel = cancel_broker_exit_orders(db, exit_orders)
                    result["cancelled_exit_order_ids"] = cancel["cancelled_order_ids"]
                    if cancel["leg_filled"]:
                        release_unsubmitted_reservation(
                            db, order.id, "Broker-side exit leg already filled; app-side sell not needed",
                        )
                        result.update({"status": "skipped", "order_id": order.id,
                                       "reason": "broker-side exit leg already filled; reconciliation will settle the lot"})
                        exiting = True
                        results.append(result)
                        continue
                    if not cancel["clear"]:
                        result.update({"status": "deferred", "order_id": order.id,
                                       "reason": "waiting for broker to confirm exit-leg cancellation; sell stays reserved"})
                        exiting = True
                        results.append(result)
                        continue
                dispatch_started = time.perf_counter()
                try:
                    submitted = dispatch_reserved_order(db, order.id)
                finally:
                    order_ms["dispatch"] += (time.perf_counter() - dispatch_started) * 1000
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
                if side == "buy" and order_status != "cancelled":
                    sector_groups.setdefault(sector_group(symbol), set()).add(symbol)
                    entry_limits.record_buy(symbol, exploring=result.get("mode") == "explore")
                    cycle_buyers.setdefault(symbol, set()).add(strategy.strategy_type)
                    if hasattr(protections, "record_entry"):  # session entry cap counts this cycle's BUYs too
                        protections.record_entry(symbol)
            except StockPaperError as exc:
                result.update({"status": "blocked", "reason": str(exc)})
            except Exception as exc:  # one symbol/strategy must not stop the fan-out
                db.rollback()
                result.update({"status": "failed", "reason": f"{exc.__class__.__name__}: {exc}"})
            results.append(result)

    submitted = sum(item.get("status") == "submitted" for item in results)
    blocked = sum(item.get("status") in {"blocked", "failed"} for item in results)
    try:
        _record_buy_outcomes(db, results, now)
    except Exception as exc:  # evidence only; never fail the cycle
        db.rollback()
        logger.info("buy outcome record unavailable: %s", exc.__class__.__name__)
    try:
        # What the exploration budget took and skipped this cycle, with the
        # evidence map it ranked against (exploration_budget.py); evidence only.
        record_plan(db, exploration_plan, results, now)
    except Exception as exc:  # evidence only; never fail the cycle
        db.rollback()
        logger.info("exploration budget record unavailable: %s", exc.__class__.__name__)
    try:
        # Every candidate value of every gate template decided for this
        # cycle's BUYs from the states already loaded (setup_shadow.py);
        # evidence only, after the orders phase.
        from app.services.setup_shadow import record_cycle_decisions

        record_cycle_decisions(db, results, cycle_rows, now=now, protections=protections)
    except Exception as exc:  # evidence only; never fail the cycle
        db.rollback()
        logger.info("setup shadow record unavailable: %s", exc.__class__.__name__)
    finished = time.perf_counter()
    timing = {
        "signals_ms": round((signals_done - cycle_started) * 1000, 1),
        "orders_ms": round((finished - signals_done) * 1000, 1),
        "total_ms": round((finished - cycle_started) * 1000, 1),
        "reserve_ms": round(order_ms["reserve"], 1),
        "dispatch_ms": round(order_ms["dispatch"], 1),
    }
    logger.info(
        "stock paper cycle: symbols=%s strategies=%s signals_ms=%s orders_ms=%s total_ms=%s "
        "reserve_ms=%s dispatch_ms=%s submitted=%s blocked=%s",
        len(symbols), len(strategies), timing["signals_ms"], timing["orders_ms"], timing["total_ms"],
        timing["reserve_ms"], timing["dispatch_ms"], submitted, blocked,
    )
    return base | {
        "status": "complete" if blocked == 0 else "partially_complete",
        "strategy_count": len(strategies),
        "signal_count": len(results),
        "submitted_count": submitted,
        "blocked_count": blocked,
        "timing": timing,
        "signal_batch": {key: value for key, value in signal_stats.items()},
        "meta_label": meta_label.summary(),
        "results": results,
    }
