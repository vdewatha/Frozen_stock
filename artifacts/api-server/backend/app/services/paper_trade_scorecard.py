"""Daily scorecard of what the stock paper bot actually traded.

Closed round trips are rebuilt from the durable stock-paper ledger
(``StockPaperOrder`` + ``StockPaperFill``): every buy order opens one lot per
(strategy, symbol); sell fills close lots FIFO.  A broker bracket leg closes
its own parent entry first, a strategy-attributed sell closes that strategy's
lots, and an unattributed sell (app protective stop, manual close) closes the
symbol's lots FIFO.  Partial entry and exit fills are folded into the lot, and
a trade is "closed" only once its whole entry quantity has been sold.

Safety contract:
* Read-only analytics.  No broker call, no order, no strategy/risk mutation.
  The only write is :func:`record_scorecard_snapshot`, which appends one
  ``StockPaperLedgerEvent`` (``account_id=None``,
  ``event_type="paper_scorecard_snapshot"``) per New York trading date.
  Every ledger consumer filters by account and event type, so the snapshot
  can never be mistaken for reconciliation, recovery or probe evidence.
* ``paper_exploration`` trades are learning probes from candidate/research
  strategies; they are reported separately and are never promotion evidence.
* Mistake labels are hindsight evaluation (see ``app.services.trade_mistakes``)
  and must never feed decisions dated before the outcome was observed.
"""
from __future__ import annotations

import hashlib
from bisect import bisect_right
import math
import random
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MarketPrice, Strategy, StrategySignal
from app.models.stock_paper import (
    StockPaperAccount,
    StockPaperEquitySnapshot,
    StockPaperFill,
    StockPaperLedgerEvent,
    StockPaperOrder,
    StockPaperPosition,
)
from app.services.trade_mistakes import aggregate_mistakes, classify_round_trip

SCORECARD_VERSION = "paper-trade-scorecard-v1"
SNAPSHOT_EVENT_TYPE = "paper_scorecard_snapshot"
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
ZERO = Decimal("0")
QTY_EPSILON = Decimal("0.000000001")

GOAL_WIN_RATE = 0.80
GOAL_WINDOW = 10
ROLLING_WINDOWS = (20, 50, 100)
BOOTSTRAP_MIN_TRADES = 20
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261029
BENCHMARK_SYMBOL = "SPY"
RECENT_TRADES = 50
# Matches the autotrader's DEFAULT_POSITION_STOP_LOSS_PCT; used only when an
# entry carries no broker stop price.  Not imported to keep this module free
# of the execution path.
DEFAULT_STOP_FRACTION = 0.03

SOURCE_LABELS = {
    "paper_exploration": "exploration",
    "scheduled_paper_signal": "scheduled",
}
BOT_SOURCES = frozenset(SOURCE_LABELS.values())
EXIT_REASONS = (
    "take_profit_leg", "stop_loss_leg", "protective_stop", "sell_signal", "manual", "recovery", "other",
)
TIME_BUCKETS = (
    ("open_0930_1000", time(9, 30), time(10, 0)),
    ("morning_1000_1130", time(10, 0), time(11, 30)),
    ("midday_1130_1400", time(11, 30), time(14, 0)),
    ("afternoon_1400_1530", time(14, 0), time(15, 30)),
    ("close_1530_1600", time(15, 30), time(16, 0)),
)


# ----------------------------------------------------------------- helpers

def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def _r(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def source_label(source: Optional[str], trial_id: Optional[str] = None) -> str:
    if trial_id:
        # Forward-trial orders go through the manual reservation path but are
        # owned by a governed trial; report them apart from manual trades.
        return "forward_trial"
    if source in SOURCE_LABELS:
        return SOURCE_LABELS[source]
    if source in {"manual_control_room", "manual_close", "manual_reduce"}:
        return "manual"
    if source == "recovery_flatten":
        return "recovery"
    return "other"


def time_bucket(moment: datetime) -> str:
    local = _utc(moment).astimezone(ET).time()
    for name, start, end in TIME_BUCKETS:
        if start <= local < end:
            return name
    return "extended_hours"


def _client_order_id(idempotency_key: str) -> str:
    # Mirrors reserve_stock_paper_order's deterministic client id.
    return "sp-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:45]


def _session_dates(order: StockPaperOrder) -> list[str]:
    moments = [value for value in (order.created_at, order.submitted_at, order.submission_attempted_at) if value]
    days = set()
    for moment in moments:
        local = _utc(moment).astimezone(ET).date()
        days.update({local, local - timedelta(days=1), local + timedelta(days=1)})
    return sorted(day.isoformat() for day in days)


class _ExitAttribution:
    """Classify app-side autonomous sells from their deterministic client id.

    The autotrader reserves sells with idempotency keys
    ``{scheduled|explore}-paper:{ET date}:{symbol}:{strategy_type}:sell`` and
    ``scheduled-paper:{ET date}:{symbol}:protective_stop:sell``; sell orders do
    not store their strategy, so the key is recomputed to recover it.
    """

    def __init__(self, strategy_types: Iterable[str]):
        self.strategy_types = sorted({value for value in strategy_types if value})

    def classify(self, order: StockPaperOrder) -> tuple[str, Optional[str]]:
        if order.leg_role == "take_profit":
            return "take_profit_leg", None
        if order.leg_role == "stop_loss":
            return "stop_loss_leg", None
        label = source_label(order.source, order.trial_id)
        if label in {"manual", "recovery"}:
            return label, None
        if label not in BOT_SOURCES:
            return "other", None
        for day in _session_dates(order):
            if order.client_order_id == _client_order_id(f"scheduled-paper:{day}:{order.symbol}:protective_stop:sell"):
                return "protective_stop", None
            for strategy_type in self.strategy_types:
                for prefix in ("scheduled", "explore"):
                    key = f"{prefix}-paper:{day}:{order.symbol}:{strategy_type}:sell"
                    if order.client_order_id == _client_order_id(key):
                        return "sell_signal", strategy_type
        return "other", None


# ----------------------------------------------------------------- reconstruction

class _Lot:
    __slots__ = (
        "entry_order", "strategy_id", "symbol", "source", "quantity", "remaining", "cost", "entry_fees",
        "entry_fills", "entered_at", "exits", "costs_known",
    )

    def __init__(self, order: StockPaperOrder):
        self.entry_order = order
        self.strategy_id = order.strategy_id
        self.symbol = order.symbol
        self.source = source_label(order.source, order.trial_id)
        self.quantity = ZERO
        self.remaining = ZERO
        self.cost = ZERO
        self.entry_fees = ZERO
        self.entry_fills = 0
        self.entered_at: Optional[datetime] = None
        self.exits: list[dict] = []
        self.costs_known = True

    def add_entry(self, fill: StockPaperFill) -> None:
        self.quantity += fill.quantity
        self.remaining += fill.quantity
        self.cost += fill.quantity * fill.price
        self.entry_fees += _dec(fill.fee)
        self.entry_fills += 1
        self.costs_known = self.costs_known and bool(fill.cost_known) and fill.fee is not None
        moment = _utc(fill.filled_at)
        self.entered_at = moment if self.entered_at is None else min(self.entered_at, moment)

    @property
    def entry_price(self) -> Decimal:
        return self.cost / self.quantity if self.quantity > 0 else ZERO

    @property
    def closed(self) -> bool:
        return self.quantity > 0 and self.remaining <= QTY_EPSILON


def _fifo_targets(lots: list[_Lot], *, parent: Optional[int], strategy_ids: Optional[set],
                  prefer_simple: bool) -> list[_Lot]:
    open_lots = [lot for lot in lots if lot.remaining > QTY_EPSILON]
    ordered: list[_Lot] = []
    if parent is not None:
        ordered += [lot for lot in open_lots if lot.entry_order.id == parent]
    if strategy_ids:
        ordered += [lot for lot in open_lots if lot.strategy_id in strategy_ids and lot not in ordered]
    if prefer_simple:
        # An app protective stop sells only quantity not already covered by a
        # live broker stop leg; consume unbracketed lots first.
        ordered += [lot for lot in open_lots if lot.entry_order.order_class not in {"bracket", "oto"} and lot not in ordered]
    ordered += [lot for lot in open_lots if lot not in ordered]
    return ordered


def reconstruct_round_trips(db: Session, account_id: int) -> dict:
    """Rebuild every lot for the account from its orders and fills."""
    orders = {row.id: row for row in db.query(StockPaperOrder).filter_by(account_id=account_id).all()}
    by_broker_id = {row.broker_order_id: row for row in orders.values() if row.broker_order_id}
    fills = db.query(StockPaperFill).filter_by(account_id=account_id).all()
    fills.sort(key=lambda fill: (_utc(fill.filled_at), 0 if fill.side == "buy" else 1, fill.id))
    strategies = {row.id: row for row in db.query(Strategy).all()}
    ids_by_type: dict[str, set[int]] = defaultdict(set)
    for row in strategies.values():
        ids_by_type[row.strategy_type].add(row.id)
    attribution = _ExitAttribution(ids_by_type)

    lots_by_symbol: dict[str, list[_Lot]] = defaultdict(list)
    lot_by_order: dict[int, _Lot] = {}
    unmatched: list[dict] = []
    unattributed_fills = 0
    exit_cache: dict[int, tuple[str, Optional[str]]] = {}
    for fill in fills:
        order = orders.get(fill.order_id) if fill.order_id else None
        if order is None and fill.broker_order_id:
            order = by_broker_id.get(fill.broker_order_id)
        if order is None:
            unattributed_fills += 1
        if fill.side == "buy":
            if order is None:
                # Keep the inventory so later sells match FIFO correctly.
                order = StockPaperOrder(id=-fill.id, account_id=account_id, symbol=fill.symbol, side="buy",
                                        quantity=fill.quantity, source="broker_import", client_order_id=f"fill-{fill.id}",
                                        status="filled", order_class="simple")
            lot = lot_by_order.get(order.id)
            if lot is None or lot.closed:
                lot = _Lot(order)
                lot_by_order[order.id] = lot
                lots_by_symbol[fill.symbol].append(lot)
            lot.add_entry(fill)
            continue
        if order is not None:
            if order.id not in exit_cache:
                exit_cache[order.id] = attribution.classify(order)
            reason, recovered_type = exit_cache[order.id]
        else:
            reason, recovered_type = "other", None
        strategy_ids: Optional[set] = None
        if order is not None and order.strategy_id is not None:
            strategy_ids = {order.strategy_id}
        elif recovered_type:
            strategy_ids = ids_by_type.get(recovered_type)
        left = fill.quantity
        fee = _dec(fill.fee)
        fee_known = bool(fill.cost_known) and fill.fee is not None
        for lot in _fifo_targets(
            lots_by_symbol[fill.symbol],
            parent=order.parent_order_id if order is not None and order.leg_role else None,
            strategy_ids=strategy_ids,
            prefer_simple=reason == "protective_stop",
        ):
            if left <= QTY_EPSILON:
                break
            take = min(left, lot.remaining)
            share = take / fill.quantity
            lot.remaining -= take
            lot.costs_known = lot.costs_known and fee_known
            lot.exits.append({
                "fill_id": fill.id, "order_id": order.id if order is not None else None,
                "quantity": take, "price": fill.price, "fee": fee * share,
                "filled_at": _utc(fill.filled_at), "reason": reason,
            })
            left -= take
        if left > QTY_EPSILON:
            unmatched.append({"fill_id": fill.id, "symbol": fill.symbol, "quantity": str(left)})
    lots = [lot for symbol_lots in lots_by_symbol.values() for lot in symbol_lots]
    return {
        "lots": lots,
        "strategies": strategies,
        "unmatched_exits": unmatched,
        "unattributed_fills": unattributed_fills,
        "orders": orders,
    }


class _EquityCurve:
    def __init__(self, snapshots: list[StockPaperEquitySnapshot]):
        self.rows = snapshots
        self.times = [_utc(row.observed_at) for row in snapshots]


def _equity_before(curve: "_EquityCurve", moment: datetime) -> Optional[float]:
    """Equity of the last snapshot at or before ``moment`` (else the first one)."""
    snapshots, times = curve.rows, curve.times
    if not snapshots:
        return None
    index = bisect_right(times, moment) - 1
    row = snapshots[max(index, 0)]
    return float(row.equity) if row.equity else None


def _trade_row(lot: _Lot, strategies: dict, snapshots: "_EquityCurve", probability: Optional[float]) -> dict:
    exit_qty = sum((item["quantity"] for item in lot.exits), ZERO)
    proceeds = sum((item["quantity"] * item["price"] for item in lot.exits), ZERO)
    exit_fees = sum((item["fee"] for item in lot.exits), ZERO)
    exit_price = proceeds / exit_qty
    fees = lot.entry_fees + exit_fees
    gross = proceeds - lot.cost
    net = gross - fees
    reason_qty: Counter = Counter()
    for item in lot.exits:
        reason_qty[item["reason"]] += item["quantity"]
    reason = max(reason_qty.items(), key=lambda pair: (pair[1], -EXIT_REASONS.index(pair[0])))[0]
    exited_at = max(item["filled_at"] for item in lot.exits)
    entry = lot.entry_order
    entry_price = lot.entry_price
    stop_fraction = None
    if entry.stop_price and entry_price > 0:
        stop_fraction = float((entry_price - _dec(entry.stop_price)) / entry_price)
    equity = _equity_before(snapshots, lot.entered_at)
    planned_risk = None
    if equity and stop_fraction and stop_fraction > 0:
        planned_risk = float(lot.quantity * entry_price) * stop_fraction / equity
    strategy = strategies.get(lot.strategy_id)
    return {
        "entry_order_id": entry.id if entry.id and entry.id > 0 else None,
        "strategy_id": lot.strategy_id,
        "strategy_name": strategy.name if strategy else None,
        "strategy_type": strategy.strategy_type if strategy else None,
        "symbol": lot.symbol,
        "source": lot.source,
        "order_class": entry.order_class or "simple",
        "quantity": float(lot.quantity),
        "entry_price": float(entry_price),
        "exit_price": float(exit_price),
        "entered_at": lot.entered_at.isoformat(),
        "exited_at": exited_at.isoformat(),
        "exit_date": exited_at.astimezone(ET).date().isoformat(),
        "holding_minutes": round((exited_at - lot.entered_at).total_seconds() / 60, 2),
        "entry_time_bucket": time_bucket(lot.entered_at),
        "exit_reason": reason,
        "exit_reasons": {key: float(value) for key, value in sorted(reason_qty.items())},
        "entry_fills": lot.entry_fills,
        "exit_fills": len({item["fill_id"] for item in lot.exits}),
        "gross_pnl": float(gross),
        "fees": float(fees),
        "net_pnl": float(net),
        "net_return": float(net / lot.cost) if lot.cost > 0 else None,
        "costs_known": lot.costs_known,
        "win": net > 0,
        "stop_price": float(entry.stop_price) if entry.stop_price is not None else None,
        "take_profit_price": float(entry.take_profit_price) if entry.take_profit_price is not None else None,
        "stop_fraction": stop_fraction if stop_fraction else DEFAULT_STOP_FRACTION,
        "probability": probability,
        "equity_at_entry": equity,
        "planned_risk_fraction": planned_risk,
    }


def _open_row(lot: _Lot, strategies: dict, positions: dict) -> dict:
    remaining = lot.remaining
    entry_price = lot.entry_price
    entry_fee_share = lot.entry_fees * (remaining / lot.quantity) if lot.quantity > 0 else ZERO
    realized = sum((item["quantity"] * (item["price"] - entry_price) - item["fee"] for item in lot.exits), ZERO)
    realized -= lot.entry_fees - entry_fee_share if lot.exits else ZERO
    position = positions.get(lot.symbol)
    mark = position.current_price if position is not None and position.current_price else None
    unrealized = (remaining * (mark - entry_price) - entry_fee_share) if mark is not None else None
    strategy = strategies.get(lot.strategy_id)
    return {
        "entry_order_id": lot.entry_order.id if lot.entry_order.id and lot.entry_order.id > 0 else None,
        "strategy_id": lot.strategy_id,
        "strategy_name": strategy.name if strategy else None,
        "symbol": lot.symbol,
        "source": lot.source,
        "quantity_open": float(remaining),
        "quantity_entered": float(lot.quantity),
        "entry_price": float(entry_price),
        "entered_at": lot.entered_at.isoformat() if lot.entered_at else None,
        "mark_price": float(mark) if mark is not None else None,
        "marked_at": _utc(position.observed_at).isoformat() if position is not None and position.observed_at else None,
        "unrealized_pnl": float(unrealized) if unrealized is not None else None,
        "realized_partial_pnl": float(realized) if lot.exits else 0.0,
        "stop_price": float(lot.entry_order.stop_price) if lot.entry_order.stop_price is not None else None,
        "take_profit_price": float(lot.entry_order.take_profit_price) if lot.entry_order.take_profit_price is not None else None,
    }


# ----------------------------------------------------------------- statistics

def _bootstrap(values: list[float], *, seed: int = BOOTSTRAP_SEED) -> dict:
    rng = random.Random(seed)
    n = len(values)
    means, rates = [], []
    for _ in range(BOOTSTRAP_RESAMPLES):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
        rates.append(sum(1 for value in sample if value > 0) / n)
    means.sort()
    rates.sort()
    lo, hi = int(0.025 * BOOTSTRAP_RESAMPLES), int(0.975 * BOOTSTRAP_RESAMPLES) - 1
    return {
        "confidence": 0.95, "resamples": BOOTSTRAP_RESAMPLES, "seed": seed,
        "win_rate": [_r(rates[lo]), _r(rates[hi])],
        "expectancy": [_r(means[lo]), _r(means[hi])],
    }


def summarize(trades: list[dict]) -> dict:
    """Win rate, payoff, expectancy and profit factor over closed trades (net)."""
    pnls = [float(trade["net_pnl"]) for trade in trades]
    n = len(pnls)
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    gross_win = sum(wins)
    gross_loss = sum(losses)
    avg_win = gross_win / len(wins) if wins else None
    avg_loss = gross_loss / len(losses) if losses else None
    returns = [trade["net_return"] for trade in trades if trade.get("net_return") is not None]
    result = {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _r(len(wins) / n) if n else None,
        "avg_win": _r(avg_win),
        "avg_loss": _r(avg_loss),
        "payoff_ratio": _r(avg_win / abs(avg_loss)) if avg_win is not None and avg_loss else None,
        "expectancy": _r(sum(pnls) / n) if n else None,
        "expectancy_return": _r(sum(returns) / len(returns)) if returns else None,
        "profit_factor": _r(gross_win / abs(gross_loss)) if gross_loss < 0 else None,
        "total_pnl": _r(sum(pnls)) if n else 0.0,
        "fees": _r(sum(float(trade["fees"]) for trade in trades)) if n else 0.0,
        "avg_holding_minutes": _r(sum(trade["holding_minutes"] for trade in trades) / n, 2) if n else None,
        "costs_known": all(trade["costs_known"] for trade in trades) if n else None,
        "bootstrap_ci": _bootstrap(pnls) if n >= BOOTSTRAP_MIN_TRADES else None,
    }
    return result


def _group(trades: list[dict], key) -> list[dict]:
    groups: dict[Any, list[dict]] = defaultdict(list)
    for trade in trades:
        groups[key(trade)].append(trade)
    return [{"key": name, **summarize(rows)} for name, rows in sorted(groups.items(), key=lambda item: str(item[0]))]


def _rolling(trades: list[dict]) -> dict:
    ordered = sorted(trades, key=lambda trade: trade["exited_at"])
    out = {}
    for window in ROLLING_WINDOWS:
        rows = ordered[-window:]
        stats = summarize(rows)
        out[f"last_{window}"] = {
            "window": window, "trades": stats["trades"], "complete": len(rows) >= window,
            "win_rate": stats["win_rate"], "expectancy": stats["expectancy"], "total_pnl": stats["total_pnl"],
            "profit_factor": stats["profit_factor"],
        }
    return out


def goal_progress(trades: list[dict]) -> dict:
    """Owner goal: 8 of every 10 trades profitable while making money overall."""
    ordered = sorted(trades, key=lambda trade: trade["exited_at"])
    last = ordered[-GOAL_WINDOW:]
    wins = sum(1 for trade in last if trade["net_pnl"] > 0)
    total = sum(trade["net_pnl"] for trade in last)
    overall = summarize(ordered)
    return {
        "target_win_rate": GOAL_WIN_RATE,
        "window": GOAL_WINDOW,
        "last_window_trades": len(last),
        "last_window_wins": wins,
        "last_window_win_rate": _r(wins / len(last)) if last else None,
        "last_window_pnl": _r(total) if last else None,
        "met_last_window": bool(len(last) >= GOAL_WINDOW and wins >= GOAL_WIN_RATE * GOAL_WINDOW and total > 0),
        "overall_win_rate": overall["win_rate"],
        "overall_expectancy": overall["expectancy"],
        "win_rate_gap": _r(GOAL_WIN_RATE - overall["win_rate"]) if overall["win_rate"] is not None else None,
        "making_money": bool(overall["total_pnl"] and overall["total_pnl"] > 0),
        "rolling": _rolling(ordered),
        "scope": "bot trades (scheduled + exploration), net of recorded fees",
    }


# ----------------------------------------------------------------- benchmark

def _price_on(db: Session, symbol: str, day: date, *, on_or_after: bool) -> Optional[tuple[date, float]]:
    query = select(MarketPrice).where(MarketPrice.symbol == symbol, MarketPrice.close.is_not(None))
    if on_or_after:
        query = query.where(MarketPrice.price_date >= day).order_by(MarketPrice.price_date.asc(), MarketPrice.source)
    else:
        query = query.where(MarketPrice.price_date <= day).order_by(MarketPrice.price_date.desc(), MarketPrice.source)
    row = db.scalars(query.limit(1)).first()
    if row is None:
        return None
    price = row.adjusted_close if row.adjusted_close is not None else row.close
    return row.price_date, float(price)


def benchmark_comparison(db: Session, start: datetime, end: datetime, *, bot_pnl: float,
                         start_equity: Optional[float]) -> dict:
    """SPY buy-and-hold from stored daily closes over the same window."""
    begin = _price_on(db, BENCHMARK_SYMBOL, start.astimezone(ET).date(), on_or_after=True)
    finish = _price_on(db, BENCHMARK_SYMBOL, end.astimezone(ET).date(), on_or_after=False)
    bot_return = bot_pnl / start_equity if start_equity else None
    if begin is None or finish is None or finish[0] < begin[0] or begin[1] <= 0:
        return {"symbol": BENCHMARK_SYMBOL, "status": "unavailable",
                "reason": "No stored SPY daily closes cover the window", "bot_return_on_equity": _r(bot_return)}
    spy_return = finish[1] / begin[1] - 1
    spy_pnl = spy_return * start_equity if start_equity else None
    return {
        "symbol": BENCHMARK_SYMBOL, "status": "available",
        "start_date": begin[0].isoformat(), "end_date": finish[0].isoformat(),
        "start_close": _r(begin[1]), "end_close": _r(finish[1]),
        "benchmark_return": _r(spy_return),
        "start_equity": _r(start_equity, 2),
        "benchmark_pnl_on_start_equity": _r(spy_pnl, 2),
        "bot_realized_pnl": _r(bot_pnl, 2),
        "bot_return_on_equity": _r(bot_return),
        "excess_return": _r(bot_return - spy_return) if bot_return is not None else None,
        "note": "Bot figure is realized net P&L of closed trades only; open marks are reported separately.",
    }


# ----------------------------------------------------------------- report

def _account(db: Session, account_id: Optional[int]) -> Optional[StockPaperAccount]:
    if account_id is not None:
        return db.get(StockPaperAccount, account_id)
    from app.services.stock_paper_ledger import active_paper_account

    return active_paper_account(db)


def _empty(now: datetime, days: int, reason: str) -> dict:
    return {
        "version": SCORECARD_VERSION, "status": "unavailable", "reason": reason,
        "generated_at": now.isoformat(), "days": days, "headline": summarize([]),
        "goal": goal_progress([]), "trades": [], "open_positions": [],
        "paper_only": True, "live_authorized": False, "mutates_strategy_state": False,
    }


def build_scorecard(db: Session, *, days: int = 30, now: Optional[datetime] = None,
                    account_id: Optional[int] = None, classify: bool = True) -> dict:
    """Read-only scorecard of closed bot round trips whose exit is in the window."""
    now = _utc(now or datetime.now(UTC))
    days = max(1, int(days))
    account = _account(db, account_id)
    if account is None:
        return _empty(now, days, "No initialized stock paper account")
    rebuilt = reconstruct_round_trips(db, account.id)
    strategies = rebuilt["strategies"]
    snapshots = _EquityCurve(db.query(StockPaperEquitySnapshot).filter_by(account_id=account.id).order_by(
        StockPaperEquitySnapshot.observed_at).all())
    signal_ids = [lot.entry_order.signal_id for lot in rebuilt["lots"] if lot.entry_order.signal_id]
    probabilities = {
        row.id: float(row.probability_up) for row in db.query(StrategySignal).filter(StrategySignal.id.in_(signal_ids)).all()
        if row.probability_up is not None
    } if signal_ids else {}
    window_start = now - timedelta(days=days)
    closed, open_rows = [], []
    positions = {row.symbol: row for row in db.query(StockPaperPosition).filter_by(account_id=account.id).all()}
    for lot in rebuilt["lots"]:
        if lot.quantity <= 0:
            continue
        if lot.closed:
            exited_at = max(item["filled_at"] for item in lot.exits)
            if window_start <= exited_at <= now:
                closed.append(_trade_row(lot, strategies, snapshots, probabilities.get(lot.entry_order.signal_id)))
        else:
            open_rows.append(_open_row(lot, strategies, positions))
    closed.sort(key=lambda trade: (trade["exited_at"], trade["entry_order_id"] or 0))
    if classify:
        for trade in closed:
            if trade["net_pnl"] <= 0:
                trade["mistakes"] = classify_round_trip(db, trade)
    bot = [trade for trade in closed if trade["source"] in BOT_SOURCES]
    losing = [trade for trade in bot if trade.get("mistakes")]
    primary = Counter(trade["mistakes"]["primary_mistake"] for trade in losing if trade["mistakes"].get("primary_mistake"))
    every = Counter(name for trade in losing for name in trade["mistakes"].get("mistakes") or [])
    mistake_cost: Counter = Counter()
    for trade in losing:
        if trade["mistakes"].get("primary_mistake"):
            mistake_cost[trade["mistakes"]["primary_mistake"]] += trade["net_pnl"]
    strategy_mistakes = aggregate_mistakes(
        {"strategy_id": trade["strategy_id"], "classification": trade["mistakes"]} for trade in losing
    )["strategies"]
    start_equity = _equity_before(snapshots, window_start)
    bot_pnl = sum(trade["net_pnl"] for trade in bot)
    unrealized = [row["unrealized_pnl"] for row in open_rows if row["unrealized_pnl"] is not None]
    def strategy_key(trade: dict):
        return trade["strategy_id"] if trade["strategy_id"] is not None else "unattributed"

    by_strategy = _group(bot, strategy_key)
    for row in by_strategy:
        strategy = strategies.get(row["key"]) if isinstance(row["key"], int) else None
        row["strategy_name"] = strategy.name if strategy else None
        row["strategy_status"] = strategy.current_status if strategy else None
        row["sources"] = sorted({trade["source"] for trade in bot if strategy_key(trade) == row["key"]})
    return {
        "version": SCORECARD_VERSION,
        "status": "available",
        "generated_at": now.isoformat(),
        "window": {"days": days, "start": window_start.isoformat(), "end": now.isoformat()},
        "days": days,
        "account_id": account.id,
        "headline": summarize(bot),
        "goal": goal_progress(bot),
        "by_day": _group(bot, lambda trade: trade["exit_date"]),
        "by_strategy": by_strategy,
        "by_symbol": _group(bot, lambda trade: trade["symbol"]),
        "by_source": _group(closed, lambda trade: trade["source"]),
        "by_exit_reason": _group(bot, lambda trade: trade["exit_reason"]),
        "by_time_bucket": _group(bot, lambda trade: trade["entry_time_bucket"]),
        "mistakes": {
            "losing_trades": len(losing),
            "primary_counts": dict(primary.most_common()),
            "all_counts": dict(every.most_common()),
            "primary_net_pnl": {key: _r(value, 2) for key, value in mistake_cost.most_common()},
            "by_strategy": strategy_mistakes,
            "note": "Hindsight labels; proposals only (auto_apply is always false).",
        },
        "open_positions": open_rows,
        "open_mark_to_market": {
            "positions": len(open_rows),
            "unrealized_pnl": _r(sum(unrealized), 2) if unrealized else 0.0,
            "unmarked_positions": sum(1 for row in open_rows if row["unrealized_pnl"] is None),
            "note": "Marks use the last reconciled broker position price; not included in trade statistics.",
        },
        "benchmark": benchmark_comparison(db, window_start, now, bot_pnl=bot_pnl, start_equity=start_equity),
        "trades": closed[-RECENT_TRADES:][::-1],
        "trade_count_in_window": len(closed),
        "non_bot_trades": len(closed) - len(bot),
        "data_quality": {
            "unmatched_exit_fills": rebuilt["unmatched_exits"][:20],
            "unmatched_exit_count": len(rebuilt["unmatched_exits"]),
            "unattributed_fills": rebuilt["unattributed_fills"],
        },
        "exploration_note": "Exploration trades come from candidate/research strategies and never count as promotion evidence.",
        "paper_only": True,
        "live_authorized": False,
        "mutates_strategy_state": False,
    }


# ----------------------------------------------------------------- snapshots

def _snapshot_payload(scorecard: dict) -> dict:
    keep = {key: value for key, value in scorecard.items() if key not in {"trades", "open_positions"}}
    keep["trades"] = scorecard.get("trades", [])[:20]
    keep["open_positions"] = scorecard.get("open_positions", [])[:20]
    return keep


def record_scorecard_snapshot(db: Session, *, days: int = 30, now: Optional[datetime] = None) -> dict:
    """Append one scorecard snapshot per New York date (idempotent per date)."""
    now = _utc(now or datetime.now(UTC))
    as_of_date = now.astimezone(ET).date().isoformat()
    existing = [
        row for row in db.query(StockPaperLedgerEvent).filter(
            StockPaperLedgerEvent.account_id.is_(None),
            StockPaperLedgerEvent.event_type == SNAPSHOT_EVENT_TYPE,
        ).all()
        if (row.payload or {}).get("as_of_date") == as_of_date
    ]
    if existing:
        return {"status": "complete", "recorded": False, "reason": "snapshot already recorded for this date",
                "as_of_date": as_of_date, "event_id": existing[0].id, "paper_only": True, "live_authorized": False}
    scorecard = build_scorecard(db, days=days, now=now)
    payload = _snapshot_payload(scorecard) | {"as_of_date": as_of_date}
    row = StockPaperLedgerEvent(account_id=None, event_type=SNAPSHOT_EVENT_TYPE, status="recorded",
                                reason=None, actor="system", payload=payload)
    db.add(row)
    db.commit()
    headline = scorecard["headline"]
    return {"status": "complete", "recorded": True, "as_of_date": as_of_date, "event_id": row.id,
            "trades": headline["trades"], "win_rate": headline["win_rate"], "expectancy": headline["expectancy"],
            "paper_only": True, "live_authorized": False}


def scorecard_history(db: Session, *, limit: int = 30) -> list[dict]:
    rows = db.query(StockPaperLedgerEvent).filter(
        StockPaperLedgerEvent.account_id.is_(None),
        StockPaperLedgerEvent.event_type == SNAPSHOT_EVENT_TYPE,
    ).order_by(StockPaperLedgerEvent.id.desc()).limit(limit).all()
    history = []
    for row in rows:
        payload = row.payload or {}
        headline = payload.get("headline") or {}
        goal = payload.get("goal") or {}
        history.append({
            "as_of_date": payload.get("as_of_date"), "days": payload.get("days"),
            "trades": headline.get("trades"), "win_rate": headline.get("win_rate"),
            "expectancy": headline.get("expectancy"), "total_pnl": headline.get("total_pnl"),
            "last_window_wins": goal.get("last_window_wins"), "met_last_window": goal.get("met_last_window"),
        })
    return history


# ----------------------------------------------------------------- markdown

def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _usd(value: Optional[float]) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _num(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _table(title: str, rows: list[dict], label: str = "Key") -> list[str]:
    lines = [f"### {title}", "", f"| {label} | Trades | Win rate | Avg win | Avg loss | Payoff | Expectancy | PF | Total P&L |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    if not rows:
        return lines + ["| (none) | 0 | | | | | | | |", ""]
    for row in rows:
        name = row.get("strategy_name") or row["key"]
        lines.append(f"| {name} | {row['trades']} | {_pct(row['win_rate'])} | {_usd(row['avg_win'])} | "
                     f"{_usd(row['avg_loss'])} | {_num(row['payoff_ratio'])} | {_usd(row['expectancy'])} | "
                     f"{_num(row['profit_factor'])} | {_usd(row['total_pnl'])} |")
    return lines + [""]


def render_markdown(scorecard: dict) -> str:
    head = scorecard["headline"]
    goal = scorecard["goal"]
    lines = [f"# Paper trade scorecard ({scorecard.get('days')} days)", "",
             f"Generated {scorecard['generated_at']}. Paper only; read-only analytics.", ""]
    if scorecard.get("status") != "available":
        return "\n".join(lines + [f"Unavailable: {scorecard.get('reason')}", ""])
    lines += [
        "## Headline (bot trades, net of recorded fees)", "",
        f"- Closed trades: {head['trades']} ({head['wins']} wins / {head['losses']} losses)",
        f"- Win rate: {_pct(head['win_rate'])} (goal {_pct(goal['target_win_rate'])})",
        f"- Expectancy per trade: {_usd(head['expectancy'])}; payoff {_num(head['payoff_ratio'])}; "
        f"profit factor {_num(head['profit_factor'])}",
        f"- Total net P&L: {_usd(head['total_pnl'])} (fees {_usd(head['fees'])})",
        f"- Last {goal['window']} trades: {goal['last_window_wins']}/{goal['last_window_trades']} wins, "
        f"P&L {_usd(goal['last_window_pnl'])}; goal met: {'yes' if goal['met_last_window'] else 'no'}",
    ]
    ci = head.get("bootstrap_ci")
    if ci:
        lines.append(f"- 95% bootstrap CI: win rate {_pct(ci['win_rate'][0])} to {_pct(ci['win_rate'][1])}; "
                     f"expectancy {_usd(ci['expectancy'][0])} to {_usd(ci['expectancy'][1])}")
    else:
        lines.append(f"- Bootstrap CI shown once there are at least {BOOTSTRAP_MIN_TRADES} trades.")
    rolling = goal["rolling"]
    lines += ["", "| Rolling | Trades | Win rate | Expectancy | Total |", "|---|---:|---:|---:|---:|"]
    for key, row in rolling.items():
        lines.append(f"| {key.replace('_', ' ')} | {row['trades']} | {_pct(row['win_rate'])} | "
                     f"{_usd(row['expectancy'])} | {_usd(row['total_pnl'])} |")
    bench = scorecard["benchmark"]
    lines += ["", "## SPY buy-and-hold", ""]
    if bench["status"] == "available":
        lines.append(f"- SPY {bench['start_date']} to {bench['end_date']}: {_pct(bench['benchmark_return'])} "
                     f"({_usd(bench['benchmark_pnl_on_start_equity'])} on start equity); bot realized "
                     f"{_pct(bench['bot_return_on_equity'])}; excess {_pct(bench['excess_return'])}")
    else:
        lines.append(f"- {bench['reason']}")
    lines += [""]
    lines += _table("By strategy", scorecard["by_strategy"], "Strategy")
    lines += _table("By source", scorecard["by_source"], "Source")
    lines += _table("By exit reason", scorecard["by_exit_reason"], "Exit")
    lines += _table("By entry time (ET)", scorecard["by_time_bucket"], "Bucket")
    lines += _table("By symbol", scorecard["by_symbol"], "Symbol")
    lines += _table("By day", scorecard["by_day"], "Exit date")
    mistakes = scorecard["mistakes"]
    lines += ["## Mistakes on losing trades", ""]
    if mistakes["primary_counts"]:
        for name, count in mistakes["primary_counts"].items():
            lines.append(f"- {name}: {count} ({_usd(mistakes['primary_net_pnl'].get(name))})")
    else:
        lines.append("- none")
    mtm = scorecard["open_mark_to_market"]
    lines += ["", "## Open positions (mark-to-market, excluded from stats)", "",
              f"- {mtm['positions']} open lots; unrealized {_usd(mtm['unrealized_pnl'])}"
              f"{'; ' + str(mtm['unmarked_positions']) + ' without a mark' if mtm['unmarked_positions'] else ''}", ""]
    lines += ["## Recent trades", "", "| Exit | Strategy | Symbol | Source | Qty | Entry | Exit | Net P&L | Reason | Mistake |",
              "|---|---|---|---|---:|---:|---:|---:|---|---|"]
    for trade in scorecard["trades"][:20]:
        mistake = (trade.get("mistakes") or {}).get("primary_mistake") or ""
        lines.append(f"| {trade['exited_at'][:16]} | {trade['strategy_name'] or trade['strategy_id']} | {trade['symbol']} | "
                     f"{trade['source']} | {trade['quantity']:g} | {trade['entry_price']:.2f} | {trade['exit_price']:.2f} | "
                     f"{_usd(trade['net_pnl'])} | {trade['exit_reason']} | {mistake} |")
    quality = scorecard["data_quality"]
    if quality["unmatched_exit_count"] or quality["unattributed_fills"]:
        lines += ["", f"Data quality: {quality['unmatched_exit_count']} exit fills without an open lot, "
                  f"{quality['unattributed_fills']} fills without a ledger order."]
    lines += ["", scorecard["exploration_note"], ""]
    return "\n".join(lines)
