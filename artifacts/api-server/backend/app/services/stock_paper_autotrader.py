"""Scheduled, paper-only execution through the governed stock-paper ledger.

This service deliberately does not use the legacy ``PaperTrade`` simulator.
It generates one signal per configured symbol and approved paper strategy (in
one batched pass, see ``stock_paper_signal_batch``), then lets the durable
stock-paper reservation and dispatch gates decide whether an order can be
submitted to Alpaca paper.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import logging
import time
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import RiskRule, Strategy
from app.models.stock_paper import StockPaperFill, StockPaperOrder, StockPaperPosition
from app.services.stock_paper_ledger import (
    NONTERMINAL_ORDER_STATUSES,
    StockPaperError,
    active_paper_account,
    EXPLORATION_SOURCE,
    EXPLORATION_STRATEGY_STATUSES,
    broker_stop_coverage,
    build_exit_plan,
    cancel_broker_exit_orders,
    cancel_stale_exit_entries,
    dispatch_reserved_order,
    live_exit_orders,
    release_unsubmitted_reservation,
    reserve_stock_paper_order,
)
from app.services.entry_protections import load_entry_protections, protective_stop_idempotency_key
from app.services.intraday_data import session_bounds
from app.services.meta_label_shadow import resolve_runtime as resolve_meta_label_runtime
from app.services.stock_paper_signal_batch import generate_cycle_signals
from app.services.strategy_execution_policy import (
    evaluate_policy_exit,
    exit_leg_percentages,
    exit_policy_for,
    symbol_scope_for,
)
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
    of each strategy's open lot) and ``buyers`` (strategy types that bought
    in the current episode).
    """
    fills = db.query(StockPaperFill).filter_by(account_id=account_id, symbol=symbol).all()
    order_ids = {row.order_id for row in fills if row.order_id is not None}
    orders = {
        row.id: row for row in db.query(StockPaperOrder).filter(StockPaperOrder.id.in_(order_ids)).all()
    } if order_ids else {}
    holdings: dict[str, Decimal] = {}
    opened_at: dict[str, datetime] = {}
    buyers: set[str] = set()
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
                if before <= 0:
                    opened_at[strategy_type] = getattr(fill, "filled_at", None)
        if total <= 0:
            holdings = {key: Decimal("0") for key in holdings}
            opened_at, buyers, attributed = {}, set(), True
    return {"holdings": holdings, "attributed": attributed, "opened_at": opened_at, "buyers": buyers}


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
    position = db.query(StockPaperPosition).filter_by(account_id=account.id, symbol=symbol).one_or_none()
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
        order = reserve_stock_paper_order(
            db, symbol=symbol, side="sell", quantity=quantity, reference_price=price,
            idempotency_key=protective_stop_idempotency_key(session_key, symbol),
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


def _max_positions_per_sector(rules: dict) -> int:
    value = rules.get("max_positions_per_sector", settings.paper_max_positions_per_sector)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return int(settings.paper_max_positions_per_sector)


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


def run_stock_paper_signal_cycle(db: Session) -> dict:
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
    type_by_id = {row.id: row.strategy_type for row in strategy_rows}
    # Per-strategy execution policy: symbol scope, broker exit legs and the
    # app-side indicator/time-stop exits.  Strategies without a declared
    # policy keep the risk-rule legs, SELL-signal exits and the full universe.
    scope_by_type = {row.strategy_type: symbol_scope_for(row) for row in strategies}
    policy_by_type = {row.strategy_type: exit_policy_for(row) for row in strategies}
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
    meta_label = resolve_meta_label_runtime()
    signal_outcomes, signal_stats = generate_cycle_signals(db, symbols, strategies, meta_label=meta_label, now=now)
    signals_done = time.perf_counter()
    sector_cap = _max_positions_per_sector(rules)
    sector_groups = _sector_exposure(db, account.id) if sector_cap else {}
    # Entry-only circuit breakers (stop-loss guard, exit cooldown, low-profit
    # lock, session drawdown, time of day); never consulted for sells.
    protections = load_entry_protections(db, account, now=now, rules=rules, type_by_id=type_by_id)
    base["entry_protections"] = protections.summary()
    results: list[dict] = []
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
                    protection_block = protections.check(symbol, strategy.strategy_type)
                    if protection_block is not None:
                        result.update(protection_block)
                        results.append(result)
                        continue
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
                    if any(_is_exit_leg(row) for row in pending):
                        # Another lot's resting exit legs are opposite-side
                        # orders; Alpaca may reject a new entry as a potential
                        # wash trade, and a rejected POST halts the ledger.
                        result.update({"status": "skipped", "reason": "broker-side exit legs are live for this symbol; no new entry until they close"})
                        results.append(result)
                        continue
                    meta_allowed, meta_reason = meta_label.allows_buy(signal.get("meta_label"))
                    if not meta_allowed:
                        # Only reachable with paper_meta_label_mode="enforce".
                        result.update({"status": "skipped", "reason": meta_reason})
                        results.append(result)
                        continue
                    quantity = _quantity_for_buy(account, reference_price, rules,
                                                 exploration=result.get("mode") == "explore")
                    if quantity <= 0:
                        result.update({"status": "blocked", "reason": "risk-sized quantity rounded to zero"})
                        results.append(result)
                        continue
                    exit_plan = _exit_plan(rules, reference_price, policy_by_type.get(strategy.strategy_type))
                    if exit_plan is not None:
                        # Alpaca only accepts bracket/OTO orders for whole
                        # shares; round down rather than enter without exits.
                        whole = quantity.quantize(WHOLE_SHARE, rounding=ROUND_DOWN)
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

                exploring = result.get("mode") == "explore"
                extra = {}
                if side == "buy" and exit_plan is not None:
                    extra["exit_plan"] = exit_plan
                if side == "sell" and exit_orders:
                    # Reserve first so every gate (kill switch, session,
                    # feed, reconciliation) passes before any exit leg is
                    # touched; dispatch still refuses while a leg is live.
                    extra["replacing_exit_order_ids"] = {row.id for row in exit_orders}
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
                    **extra,
                )
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
                if side == "buy" and order_status != "cancelled":
                    sector_groups.setdefault(sector_group(symbol), set()).add(symbol)
            except StockPaperError as exc:
                result.update({"status": "blocked", "reason": str(exc)})
            except Exception as exc:  # one symbol/strategy must not stop the fan-out
                db.rollback()
                result.update({"status": "failed", "reason": f"{exc.__class__.__name__}: {exc}"})
            results.append(result)

    submitted = sum(item.get("status") == "submitted" for item in results)
    blocked = sum(item.get("status") in {"blocked", "failed"} for item in results)
    finished = time.perf_counter()
    timing = {
        "signals_ms": round((signals_done - cycle_started) * 1000, 1),
        "orders_ms": round((finished - signals_done) * 1000, 1),
        "total_ms": round((finished - cycle_started) * 1000, 1),
    }
    logger.info(
        "stock paper cycle: symbols=%s strategies=%s signals_ms=%s orders_ms=%s total_ms=%s submitted=%s blocked=%s",
        len(symbols), len(strategies), timing["signals_ms"], timing["orders_ms"], timing["total_ms"],
        submitted, blocked,
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
