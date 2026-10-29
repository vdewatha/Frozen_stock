"""Entry-only circuit breakers for autonomous stock paper BUYs.

The ideas follow freqtrade's "protections" (StoplossGuard, CooldownPeriod,
LowProfitPairs, MaxDrawdown) plus a time-of-day filter.  freqtrade is GPL, so
nothing here is copied; it is an independent implementation on top of the
governed stock-paper ledger.

Every protection here can only *withhold a new entry*.  None of them is
consulted for sells, protective stops, broker exit legs, recovery or manual
paths, and none relaxes an existing gate (kill switch, readiness, launch
admission, reconciliation, risk caps, the daily-drawdown check in
``_validate_signal_buy``).  The autotrader calls :meth:`EntryProtections.check`
only on the BUY branch, after the existing "already exiting / already holding"
skips and before quantity sizing and ``reserve_stock_paper_order``.

Protections (each disabled by a value of 0):

1. **Stop-loss guard** — ``stoploss_guard_count`` stop-outs for one strategy
   within ``stoploss_guard_lookback_minutes`` pause that strategy's entries for
   ``stoploss_guard_pause_minutes`` after the latest stop-out.  The optional
   ``stoploss_guard_global_count`` applies the same window to stop-outs across
   all strategies and pauses every entry.  A *stop-out* is one sell order
   (partial fills of the same order count once) whose fills are either
     * a broker exit leg with ``leg_role == "stop_loss"`` (bracket/OTO stop), or
     * the app-side protective stop sell from the autotrader, recognised by its
       deterministic idempotency key (:func:`protective_stop_idempotency_key`)
       hashed into ``client_order_id``.
   Strategy attribution: stop legs inherit the entry's ``strategy_id``; an
   app-side protective stop carries the strategy of the lot it closes when a
   single strategy holds the symbol, and otherwise none, in which case it is
   attributed FIFO to the strategy lots it closed (see
   :func:`reconstruct_trades`).
2. **Exit cooldown** — any sell fill on a symbol (any source) blocks new
   entries on that symbol for ``exit_cooldown_minutes``.
3. **Low-profit symbol lock** — when the last ``low_profit_trades`` closed
   round trips of a strategy on a symbol sum to a negative net P&L (fees
   included when reported), that strategy is locked out of that symbol for
   ``low_profit_lock_minutes`` after the latest of those closes.
4. **Session drawdown lock** — if paper equity (realized + unrealized, i.e.
   broker-reported account equity) has fallen more than
   ``session_drawdown_lock_pct`` from its peak since the regular-session open,
   all new entries are blocked for the rest of the session.  This is an
   earlier, entry-only stop that complements (does not replace) the existing
   prior-close ``max_daily_drawdown`` gate.
5. **Time of day** — no entries in the first ``entry_open_buffer_minutes``
   after the regular-session open or the last ``entry_close_buffer_minutes``
   before the close; bounds come from the exchange calendar, so half-days
   use their early close.

Configuration: each key is read from the active ``RiskRule.value`` first and
falls back to ``settings.paper_<key>``.  Unparseable or negative risk-rule
values fall back to the setting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import logging
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services.intraday_data import session_bounds

logger = logging.getLogger(__name__)
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
# Fills older than this are not loaded.  Open lots whose buys are older fall
# out of the reconstruction (their sells are ignored for P&L), which can only
# under-count closed trades; it never fabricates a lock-free state for a
# recent stop-out or exit, because those use the sell fills themselves.
HISTORY_DAYS = 30
QUANTITY_EPSILON = Decimal("0.000000001")

INT_KEYS = (
    "stoploss_guard_count",
    "stoploss_guard_global_count",
    "stoploss_guard_lookback_minutes",
    "stoploss_guard_pause_minutes",
    "exit_cooldown_minutes",
    "low_profit_trades",
    "low_profit_lock_minutes",
    "entry_open_buffer_minutes",
    "entry_close_buffer_minutes",
)
DECIMAL_KEYS = ("session_drawdown_lock_pct",)


def protective_stop_idempotency_key(session_key: str, symbol: str) -> str:
    """Idempotency key of the autotrader's app-side protective stop sell."""
    return f"scheduled-paper:{session_key}:{symbol}:protective_stop:sell"


def _client_order_id(idempotency_key: str) -> str:
    # Mirrors reserve_stock_paper_order's client_order_id derivation.
    return "sp-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:45]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _is_protective_stop(client_order_id: str | None, symbol: str, *times: datetime | None) -> bool:
    if not client_order_id:
        return False
    for moment in times:
        moment = _utc(moment)
        if moment is None:
            continue
        local_day = moment.astimezone(ET).date()
        for day in (local_day, local_day - timedelta(days=1)):
            if _client_order_id(protective_stop_idempotency_key(day.isoformat(), symbol)) == client_order_id:
                return True
    return False


# --------------------------------------------------------------------------
# configuration


@dataclass(frozen=True)
class ProtectionConfig:
    stoploss_guard_count: int = 3
    stoploss_guard_global_count: int = 0
    stoploss_guard_lookback_minutes: int = 360
    stoploss_guard_pause_minutes: int = 120
    exit_cooldown_minutes: int = 30
    low_profit_trades: int = 4
    low_profit_lock_minutes: int = 1440
    session_drawdown_lock_pct: Decimal = Decimal("0.02")
    entry_open_buffer_minutes: int = 15
    entry_close_buffer_minutes: int = 10

    def as_dict(self) -> dict:
        return {key: (str(value) if isinstance(value, Decimal) else value) for key, value in self.__dict__.items()}


def protection_config(rules: dict | None = None) -> ProtectionConfig:
    """Resolve thresholds: risk-rule key, else ``settings.paper_<key>``."""
    rules = rules or {}
    values: dict = {}
    for key in INT_KEYS:
        fallback = int(getattr(settings, f"paper_{key}", getattr(ProtectionConfig, key)))
        raw = rules.get(key)
        try:
            value = int(raw) if raw is not None and not isinstance(raw, bool) else fallback
        except (TypeError, ValueError):
            value = fallback
        values[key] = value if value >= 0 else fallback
    for key in DECIMAL_KEYS:
        fallback = Decimal(str(getattr(settings, f"paper_{key}", getattr(ProtectionConfig, key))))
        raw = rules.get(key)
        try:
            value = Decimal(str(raw)) if raw is not None and not isinstance(raw, bool) else fallback
        except (InvalidOperation, ValueError):
            value = fallback
        values[key] = value if value.is_finite() and value >= 0 else fallback
    return ProtectionConfig(**values)


# --------------------------------------------------------------------------
# pure trade reconstruction


@dataclass(frozen=True)
class FillRecord:
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    at: datetime
    fee: Decimal = Decimal("0")
    order_id: int | None = None
    strategy: str | None = None
    stop_out: bool = False


@dataclass(frozen=True)
class ExitEvent:
    symbol: str
    at: datetime
    order_key: str
    stop_out: bool
    strategies: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    strategy: str
    opened_at: datetime
    closed_at: datetime
    net_pnl: Decimal


@dataclass
class _Lot:
    strategy: str | None
    quantity: Decimal
    unit_cost: Decimal
    opened_at: datetime


def reconstruct_trades(fills: Iterable[FillRecord]) -> tuple[list[ExitEvent], list[ClosedTrade]]:
    """Replay fills into exit events and per-strategy closed round trips.

    Buy fills open lots tagged with their strategy.  A sell attributed to a
    strategy closes that strategy's lots first (FIFO); an unattributed sell
    (app-side protective stop, manual close) or any remainder closes the
    symbol's lots FIFO across strategies.  A round trip closes when the
    strategy's open quantity in the symbol returns to zero.  Sells that find
    no open lot (history before the load window) still produce exit events
    but no P&L.
    """
    ordered = sorted(fills, key=lambda row: (_utc(row.at), row.side != "buy"))
    lots: dict[str, list[_Lot]] = {}
    cycle: dict[tuple[str, str], dict] = {}
    exits: dict[tuple[str, str], dict] = {}
    trades: list[ClosedTrade] = []
    for index, fill in enumerate(ordered):
        at = _utc(fill.at)
        symbol = fill.symbol.upper()
        quantity = Decimal(fill.quantity)
        if quantity <= 0:
            continue
        fee = Decimal(fill.fee or 0)
        if fill.side == "buy":
            unit_cost = Decimal(fill.price) + fee / quantity
            lots.setdefault(symbol, []).append(_Lot(fill.strategy, quantity, unit_cost, at))
            if fill.strategy:
                state = cycle.setdefault((symbol, fill.strategy), {"pnl": Decimal("0"), "opened_at": at})
                state.setdefault("opened_at", at)
            continue
        if fill.side != "sell":
            continue
        order_key = f"order:{fill.order_id}" if fill.order_id is not None else f"fill:{index}"
        event = exits.setdefault((symbol, order_key), {
            "symbol": symbol, "at": at, "order_key": order_key, "stop_out": False, "strategies": [],
        })
        event["at"] = max(event["at"], at)
        event["stop_out"] = event["stop_out"] or fill.stop_out
        unit_proceeds = Decimal(fill.price) - fee / quantity
        open_lots = lots.get(symbol, [])
        remaining = quantity
        preferred = [lot for lot in open_lots if fill.strategy and lot.strategy == fill.strategy]
        preferred_ids = {id(lot) for lot in preferred}
        others = [lot for lot in open_lots if id(lot) not in preferred_ids]
        touched: list[str] = []
        for lot in [*preferred, *others]:
            if remaining <= QUANTITY_EPSILON:
                break
            take = min(lot.quantity, remaining)
            lot.quantity -= take
            remaining -= take
            if lot.strategy:
                state = cycle.setdefault((symbol, lot.strategy), {"pnl": Decimal("0"), "opened_at": lot.opened_at})
                state["pnl"] += take * (unit_proceeds - lot.unit_cost)
                if lot.strategy not in touched:
                    touched.append(lot.strategy)
        lots[symbol] = [lot for lot in open_lots if lot.quantity > QUANTITY_EPSILON]
        if fill.strategy and fill.strategy not in touched:
            touched.insert(0, fill.strategy)
        for strategy in touched:
            if strategy not in event["strategies"]:
                event["strategies"].append(strategy)
            still_open = sum((lot.quantity for lot in lots[symbol] if lot.strategy == strategy), Decimal("0"))
            state = cycle.get((symbol, strategy))
            if state is not None and still_open <= QUANTITY_EPSILON:
                trades.append(ClosedTrade(symbol, strategy, state["opened_at"], at, state["pnl"]))
                del cycle[(symbol, strategy)]
    events = [
        ExitEvent(row["symbol"], row["at"], row["order_key"], row["stop_out"], tuple(row["strategies"]))
        for row in exits.values()
    ]
    events.sort(key=lambda row: row.at)
    trades.sort(key=lambda row: row.closed_at)
    return events, trades


# --------------------------------------------------------------------------
# pure checks.  Each returns None (allow) or a block dict.


def _block(name: str, reason: str, until: datetime | None = None) -> dict:
    out = {"status": "skipped", "protection": name, "reason": f"entry protection {name}: {reason}"}
    if until is not None:
        out["protection_until"] = _utc(until).isoformat()
    return out


def time_of_day_block(now: datetime, bounds: tuple[datetime, datetime] | None,
                      config: ProtectionConfig) -> dict | None:
    if bounds is None:
        return None  # closed session is the session gate's job, not ours
    opened, closes = _utc(bounds[0]), _utc(bounds[1])
    now = _utc(now)
    if not opened <= now < closes:
        return None
    if config.entry_open_buffer_minutes:
        until = opened + timedelta(minutes=config.entry_open_buffer_minutes)
        if now < until:
            return _block("time_of_day", (
                f"no new entries in the first {config.entry_open_buffer_minutes} minutes after the "
                f"regular-session open ({opened.astimezone(ET):%H:%M} ET)"
            ), until)
    if config.entry_close_buffer_minutes:
        start = closes - timedelta(minutes=config.entry_close_buffer_minutes)
        if now >= start:
            return _block("time_of_day", (
                f"no new entries in the last {config.entry_close_buffer_minutes} minutes before the "
                f"regular-session close ({closes.astimezone(ET):%H:%M} ET)"
            ), closes)
    return None


def session_drawdown(current_equity: Decimal | None, session_equities: Iterable[Decimal | None],
                     opening_fallback: Decimal | None = None) -> Decimal | None:
    """Peak-to-current equity drawdown since the session open (None = unknown)."""
    if current_equity is None:
        return None
    series = [Decimal(value) for value in session_equities if value is not None and Decimal(value) > 0]
    if not series and opening_fallback is not None and Decimal(opening_fallback) > 0:
        series = [Decimal(opening_fallback)]
    peak = max([*series, Decimal(current_equity)])
    if peak <= 0:
        return None
    return max(Decimal("0"), (peak - Decimal(current_equity)) / peak)


def drawdown_block(drawdown: Decimal | None, config: ProtectionConfig,
                   session_close: datetime | None = None) -> dict | None:
    limit = config.session_drawdown_lock_pct
    if not limit or drawdown is None:
        return None
    if drawdown >= limit:
        return _block("session_drawdown", (
            f"paper equity is {drawdown:.2%} below its peak since the session open "
            f"(lock at {limit:.2%}); no new entries for the rest of the session"
        ), session_close)
    return None


def stoploss_guard_block(events: Iterable[ExitEvent], strategy: str | None, now: datetime,
                         config: ProtectionConfig) -> dict | None:
    if not config.stoploss_guard_lookback_minutes or not config.stoploss_guard_pause_minutes:
        return None
    now = _utc(now)
    window_start = now - timedelta(minutes=config.stoploss_guard_lookback_minutes)
    pause = timedelta(minutes=config.stoploss_guard_pause_minutes)
    stops = [row for row in events if row.stop_out and window_start <= _utc(row.at) <= now]
    scopes = []
    if config.stoploss_guard_global_count:
        scopes.append(("all strategies", config.stoploss_guard_global_count, stops))
    if config.stoploss_guard_count and strategy:
        scopes.append((strategy, config.stoploss_guard_count, [row for row in stops if strategy in row.strategies]))
    for label, limit, rows in scopes:
        if len(rows) >= limit:
            until = max(_utc(row.at) for row in rows) + pause
            if now < until:
                return _block("stoploss_guard", (
                    f"{len(rows)} stop-loss exits for {label} in the last "
                    f"{config.stoploss_guard_lookback_minutes} minutes (limit {limit}); entries paused for "
                    f"{config.stoploss_guard_pause_minutes} minutes"
                ), until)
    return None


def cooldown_block(events: Iterable[ExitEvent], symbol: str, now: datetime,
                   config: ProtectionConfig) -> dict | None:
    if not config.exit_cooldown_minutes:
        return None
    now = _utc(now)
    symbol = symbol.upper()
    times = [_utc(row.at) for row in events if row.symbol == symbol and _utc(row.at) <= now]
    if not times:
        return None
    until = max(times) + timedelta(minutes=config.exit_cooldown_minutes)
    if now < until:
        return _block("exit_cooldown", (
            f"{symbol} was exited at {max(times).astimezone(ET):%H:%M} ET; no re-entry for "
            f"{config.exit_cooldown_minutes} minutes"
        ), until)
    return None


def low_profit_block(trades: Iterable[ClosedTrade], symbol: str, strategy: str | None, now: datetime,
                     config: ProtectionConfig) -> dict | None:
    if not config.low_profit_trades or not config.low_profit_lock_minutes or not strategy:
        return None
    now = _utc(now)
    symbol = symbol.upper()
    rows = [row for row in trades if row.symbol == symbol and row.strategy == strategy and _utc(row.closed_at) <= now]
    rows.sort(key=lambda row: _utc(row.closed_at))
    recent = rows[-config.low_profit_trades:]
    if len(recent) < config.low_profit_trades:
        return None
    net = sum((row.net_pnl for row in recent), Decimal("0"))
    if net >= 0:
        return None
    until = _utc(recent[-1].closed_at) + timedelta(minutes=config.low_profit_lock_minutes)
    if now < until:
        return _block("low_profit_symbol", (
            f"last {len(recent)} closed {strategy} trades on {symbol} netted {net.quantize(Decimal('0.01'))}; "
            f"symbol locked for this strategy for {config.low_profit_lock_minutes} minutes"
        ), until)
    return None


# --------------------------------------------------------------------------
# per-cycle evaluator


@dataclass
class EntryProtections:
    now: datetime
    config: ProtectionConfig
    events: list[ExitEvent] = field(default_factory=list)
    trades: list[ClosedTrade] = field(default_factory=list)
    bounds: tuple[datetime, datetime] | None = None
    drawdown: Decimal | None = None
    load_error: str | None = None

    def check(self, symbol: str, strategy: str | None) -> dict | None:
        """Block dict for a new BUY entry, or None to let existing gates decide."""
        if self.load_error:
            return _block("unavailable", f"protection state could not be loaded ({self.load_error}); entry withheld")
        for outcome in (
            time_of_day_block(self.now, self.bounds, self.config),
            drawdown_block(self.drawdown, self.config, self.bounds[1] if self.bounds else None),
            stoploss_guard_block(self.events, strategy, self.now, self.config),
            cooldown_block(self.events, symbol, self.now, self.config),
            low_profit_block(self.trades, symbol, strategy, self.now, self.config),
        ):
            if outcome is not None:
                return outcome
        return None

    def summary(self) -> dict:
        out = {"config": self.config.as_dict(),
               "session_drawdown": str(self.drawdown) if self.drawdown is not None else None}
        if self.load_error:
            out["load_error"] = self.load_error
        cycle_wide = time_of_day_block(self.now, self.bounds, self.config) or drawdown_block(
            self.drawdown, self.config, self.bounds[1] if self.bounds else None)
        if cycle_wide:
            out["cycle_block"] = cycle_wide["reason"]
        return out


def load_entry_protections(db, account, *, now: datetime, rules: dict | None,
                           type_by_id: dict, bounds_for: Callable = session_bounds) -> EntryProtections:
    """Load fills/orders/equity once per cycle and build the evaluator.

    Any load failure fails closed for entries only (``check`` withholds every
    BUY); the caller's sell/exit path is unaffected.
    """
    from app.models.stock_paper import StockPaperEquitySnapshot, StockPaperFill, StockPaperOrder

    now = _utc(now)
    config = protection_config(rules)
    protections = EntryProtections(now=now, config=config)
    try:
        protections.bounds = bounds_for(now.astimezone(ET).date())
        since = now - timedelta(days=HISTORY_DAYS)
        fills = [
            row for row in db.query(StockPaperFill).filter(
                StockPaperFill.account_id == account.id, StockPaperFill.filled_at >= since,
            ).all()
            if getattr(row, "filled_at", None) is not None and _utc(row.filled_at) >= since
        ]
        order_ids = {row.order_id for row in fills if getattr(row, "order_id", None) is not None}
        orders = {
            row.id: row for row in db.query(StockPaperOrder).filter(StockPaperOrder.id.in_(order_ids)).all()
        } if order_ids else {}
        records = []
        for row in fills:
            order = orders.get(row.order_id)
            symbol = str(row.symbol).upper()
            strategy_id = getattr(order, "strategy_id", None) if order is not None else None
            stop_out = bool(order is not None and row.side == "sell" and (
                getattr(order, "leg_role", None) == "stop_loss"
                or _is_protective_stop(getattr(order, "client_order_id", None), symbol,
                                       getattr(order, "created_at", None), row.filled_at)
            ))
            records.append(FillRecord(
                symbol=symbol, side=str(row.side), quantity=Decimal(row.quantity), price=Decimal(row.price),
                at=row.filled_at, fee=Decimal(row.fee) if getattr(row, "fee", None) is not None else Decimal("0"),
                order_id=row.order_id, strategy=type_by_id.get(strategy_id) if strategy_id else None,
                stop_out=stop_out,
            ))
        protections.events, protections.trades = reconstruct_trades(records)
        if config.session_drawdown_lock_pct and protections.bounds is not None:
            opened = _utc(protections.bounds[0])
            snapshots = [
                row for row in db.query(StockPaperEquitySnapshot).filter(
                    StockPaperEquitySnapshot.account_id == account.id,
                    StockPaperEquitySnapshot.observed_at >= opened,
                ).all()
                if getattr(row, "observed_at", None) is not None and opened <= _utc(row.observed_at) <= now
            ]
            protections.drawdown = session_drawdown(
                getattr(account, "equity", None), [row.equity for row in snapshots],
                opening_fallback=getattr(account, "last_equity", None),
            )
    except Exception as exc:  # fail closed for entries only
        logger.warning("entry protections unavailable: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        protections.load_error = f"{exc.__class__.__name__}: {exc}"
    return protections
