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
6. **Strategy drawdown lock** — when the peak-to-trough drawdown of a
   strategy's cumulative realized P&L over its last
   ``strategy_drawdown_trades`` closed round trips (all symbols) reaches
   ``strategy_drawdown_lock_pct`` of the strategy's allocated notional
   (``strategy_allocation_fraction`` x account equity), that strategy's new
   entries are blocked for the rest of the session in which the latest of
   those trades closed plus the next ``strategy_drawdown_lock_sessions`` NYSE
   sessions.  The lock is anchored on the latest closed trade, so it expires
   even without new trades and re-arms only when a new close keeps the
   window in drawdown.  The backtester applies the same rule when
   ``BacktestConfig.strategy_drawdown_lock_pct`` > 0 (off by default).
7. **Model monitoring lock** — while a ``model.*`` monitoring check (drift,
   calibration or realized hit rate of the probabilistic model) is breaching,
   strategies that trade that model (``MODEL_DEPENDENT_STRATEGIES``) take no
   new entries.  Model breaches pause only these strategies, not the whole
   stock path, and the lock lifts when the check clears.
8. **Market gate** — a strategy row with ``market_gate_sma = N`` takes no new
   entries while SPY's last completed close is at or below its N-day average
   (``market_gate.py``); SPY history that cannot be loaded fails closed for
   those strategies only.
9. **Stop-out cooldown** — after a stop-out on a symbol (any strategy), no
   strategy re-enters it until the ``stop_cooldown_sessions``-th NYSE session
   after the stop's session.  In reports/slot-allocation-20261106.md 55% of
   the book's trades were re-entries within 10 sessions of a stop on the same
   symbol and averaged -46.5 bps, against +34.5 bps for the rest.
10. **Sector gate** — a strategy row with ``sector_gate_sma = N`` takes no new
    entry in a symbol while that symbol's sector ETF (``market_gate
    .sector_symbol``) closed at or below its N-day average.  Each needed
    ETF's history is loaded once per cycle; an ETF that cannot be loaded
    fails closed for gated strategies only, like the market gate.  Symbols
    without a sector ETF (index ETFs, the sector ETFs themselves, unmapped
    names) are never sector-gated.
11. **Breadth gate** — a strategy row with ``breadth_gate_min = P`` takes no
    new entry while the latest breadth gauge (``market_gauges``: share of
    S&P 500 members above their 200-day average) is below P percent.  A
    missing or stale gauge fails *open*, as the VIX gate does, and the
    state says so.
12. **Session entry cap** — ``max_new_entries_per_session`` (book-level, 0 =
    off): once that many new entries filled in the regular session (distinct
    buy orders with a fill since the open, plus the BUYs this cycle already
    submitted), every further BUY waits for the next session.  The
    ``clustered_stops`` lesson: positions opened together were one bet.

13. **Exploration date caps** (``ExplorationDateCaps``) — exploration
    (learning-sized) entries only: at most
    ``exploration_max_entries_per_strategy_per_date`` per strategy and
    ``exploration_max_entries_per_sector_per_date`` per sector group per entry
    date (this session's exploration BUYs plus the cycle's own); the
    exploration lane's names use their assigned sector ETF
    (``exploration_universe.sector_for``).  Read from the settings only.

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
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services.backtest_statistics import realized_drawdown
from app.services.exchange_sessions import is_nyse_session
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
    "strategy_drawdown_trades",
    "strategy_drawdown_lock_sessions",
    "stop_cooldown_sessions",
    "max_new_entries_per_session",
)
DECIMAL_KEYS = ("session_drawdown_lock_pct", "strategy_drawdown_lock_pct", "strategy_allocation_fraction")


def protective_stop_idempotency_key(session_key: str, symbol: str) -> str:
    """Idempotency key of the autotrader's app-side protective stop sell."""
    return f"scheduled-paper:{session_key}:{symbol}:protective_stop:sell"


# A stale exit sell is cancelled and re-issued under a numbered key (Alpaca
# never accepts a client order id twice); bounded so a falling price cannot
# be chased all session and so attribution can enumerate every variant.
SELL_RETRY_LIMIT = 8


def sell_retry_key(base: str, attempt: int) -> str:
    return base if attempt <= 0 else f"{base}:retry{attempt}"


def sell_key_variants(base: str) -> list[str]:
    return [sell_retry_key(base, attempt) for attempt in range(SELL_RETRY_LIMIT + 1)]


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
            for key in sell_key_variants(protective_stop_idempotency_key(day.isoformat(), symbol)):
                if _client_order_id(key) == client_order_id:
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
    strategy_drawdown_trades: int = 10
    strategy_drawdown_lock_sessions: int = 3
    strategy_drawdown_lock_pct: Decimal = Decimal("0.05")
    strategy_allocation_fraction: Decimal = Decimal("0.10")
    stop_cooldown_sessions: int = 10
    max_new_entries_per_session: int = 0

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


def stop_cooldown_block(events: Iterable[ExitEvent], symbol: str, now: datetime, config: ProtectionConfig,
                       is_session: Callable = is_nyse_session) -> dict | None:
    """No re-entry on ``symbol`` until the N-th session after its latest stop-out session."""
    if not config.stop_cooldown_sessions:
        return None
    now = _utc(now)
    symbol = symbol.upper()
    stops = [_utc(row.at) for row in events if row.symbol == symbol and row.stop_out and _utc(row.at) <= now]
    if not stops:
        return None
    latest = max(stops)
    until = strategy_lock_end(latest, config.stop_cooldown_sessions - 1, is_session)
    if now < until:
        return _block("stop_cooldown", (
            f"{symbol} was stopped out on {latest.astimezone(ET):%Y-%m-%d}; no re-entry for "
            f"{config.stop_cooldown_sessions} sessions"
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


def strategy_lock_end(closed_at: datetime, lock_sessions: int,
                      is_session: Callable = is_nyse_session) -> datetime:
    """Midnight ET that ends a lock: rest of the close's session + N sessions."""
    day = _utc(closed_at).astimezone(ET).date()
    counted = 0
    for _ in range(lock_sessions * 7 + 14):  # bounded walk over weekends/holidays
        day += timedelta(days=1)
        if is_session(day):
            counted += 1
            if counted > lock_sessions:
                break
    return datetime(day.year, day.month, day.day, tzinfo=ET).astimezone(UTC)


def strategy_drawdown_state(trades: Iterable[ClosedTrade], strategy: str, now: datetime,
                            config: ProtectionConfig, allocated: Decimal | None,
                            is_session: Callable = is_nyse_session) -> dict | None:
    """Active lock dict for ``strategy`` or None (disabled / not in drawdown)."""
    if (not config.strategy_drawdown_trades or not config.strategy_drawdown_lock_pct
            or not config.strategy_drawdown_lock_sessions or not allocated or allocated <= 0):
        return None
    now = _utc(now)
    rows = sorted((row for row in trades if row.strategy == strategy and _utc(row.closed_at) <= now),
                  key=lambda row: _utc(row.closed_at))
    recent = rows[-config.strategy_drawdown_trades:]
    if not recent:
        return None
    drawdown = Decimal(realized_drawdown(row.net_pnl for row in recent))
    limit = Decimal(allocated) * config.strategy_drawdown_lock_pct
    if drawdown < limit:
        return None
    until = strategy_lock_end(recent[-1].closed_at, config.strategy_drawdown_lock_sessions, is_session)
    if now >= until:
        return None
    return {"strategy": strategy, "drawdown": str(drawdown.quantize(Decimal("0.01"))),
            "limit": str(limit.quantize(Decimal("0.01"))), "trades": len(recent),
            "locked_until": until.isoformat()}


def strategy_drawdown_block(trades: Iterable[ClosedTrade], strategy: str | None, now: datetime,
                            config: ProtectionConfig, allocated: Decimal | None) -> dict | None:
    if not strategy:
        return None
    state = strategy_drawdown_state(trades, strategy, now, config, allocated)
    if state is None:
        return None
    return _block("strategy_drawdown", (
        f"{strategy} realized drawdown {state['drawdown']} over its last {state['trades']} closed trades "
        f"reached the lock ({config.strategy_drawdown_lock_pct:.2%} of allocated notional = {state['limit']}); "
        f"new entries locked for {config.strategy_drawdown_lock_sessions} sessions"
    ), datetime.fromisoformat(state["locked_until"]))


# --------------------------------------------------------------------------
# per-cycle evaluator


# Strategies whose signals come from ``probabilistic_model.predict_probabilities``,
# the model that the ``model.*`` monitoring checks evaluate.
MODEL_DEPENDENT_STRATEGIES = frozenset({"model_predictive_long"})


def model_monitoring_block(breaches: Iterable[str], strategy: str | None) -> dict | None:
    breaches = sorted(breaches)
    if not breaches or strategy not in MODEL_DEPENDENT_STRATEGIES:
        return None
    return _block("model_monitoring", (
        f"model monitoring breach ({', '.join(breaches)}); {strategy} entries paused until the check clears"
    ))


def entry_cap_block(entries: int, config: ProtectionConfig, session_close: datetime | None = None) -> dict | None:
    """Block once ``entries`` new entries were taken this session (``max_new_entries_per_session``; 0 = off)."""
    cap = int(config.max_new_entries_per_session or 0)
    if cap <= 0 or entries < cap:
        return None
    return _block("entry_cap", (
        f"{entries} new entries already this session (cap {cap}); further BUYs wait for the next session"
    ), session_close)


@dataclass
class EntryProtections:
    now: datetime
    config: ProtectionConfig
    events: list[ExitEvent] = field(default_factory=list)
    trades: list[ClosedTrade] = field(default_factory=list)
    bounds: tuple[datetime, datetime] | None = None
    drawdown: Decimal | None = None
    allocated_notional: Decimal | None = None
    load_error: str | None = None
    model_breaches: list[str] = field(default_factory=list)
    # strategy_type -> SMA window, and window -> market_gate.market_state(...)
    market_gates: dict = field(default_factory=dict)
    market_states: dict = field(default_factory=dict)
    # The SPY frame behind ``market_states``; ``setup_shadow`` decides the
    # candidate windows from it without a second load.
    market_history: Any = field(default=None, repr=False)
    # strategy_type -> sector SMA window; sector ETF -> its daily frame (None
    # when it could not be loaded); (ETF, window) -> market_gate.sector_state
    sector_gates: dict = field(default_factory=dict)
    sector_history: dict = field(default_factory=dict, repr=False)
    sector_states: dict = field(default_factory=dict)
    # strategy_type -> breadth floor (percent); the cycle's latest gauges
    # (``market_gauges.latest_gauges``); floor -> market_gauges.breadth_state
    breadth_gates: dict = field(default_factory=dict)
    gauges: dict = field(default_factory=dict, repr=False)
    breadth_states: dict = field(default_factory=dict)
    # New entries filled this session before the cycle, and the BUYs this
    # cycle submitted (``record_entry``), for ``max_new_entries_per_session``.
    session_entries: int = 0
    cycle_entries: int = 0

    def market_gate_block(self, strategy: str | None) -> dict | None:
        window = self.market_gates.get(strategy, 0) if strategy else 0
        if not window:
            return None
        state = self.market_states.get(window) or {"allowed": False, "reason": "market state unavailable"}
        if state["allowed"]:
            return None
        return _block("market_gate", f"{state['reason']}; {strategy} takes no new longs while the market falls")

    def sector_gate_state(self, symbol: str, window: int) -> dict | None:
        """The sector gate's state for ``symbol`` at ``window``; None when the symbol has no sector ETF."""
        from app.services.market_gate import sector_state, sector_symbol

        etf = sector_symbol(symbol)
        if etf is None or not window:
            return None
        key = (etf, int(window))
        if key not in self.sector_states:
            self.sector_states[key] = sector_state(self.sector_history.get(etf), int(window), etf)
        return self.sector_states[key]

    def sector_gate_block(self, symbol: str, strategy: str | None) -> dict | None:
        window = self.sector_gates.get(strategy, 0) if strategy else 0
        if not window:
            return None
        state = self.sector_gate_state(symbol, window)
        if state is None or state["allowed"]:
            return None
        return _block("sector_gate", (f"{state['reason']}; {strategy} takes no new longs in {symbol.upper()} "
                                      f"while its sector falls"))

    def breadth_gate_state(self, minimum: float) -> dict:
        from app.services.market_gauges import breadth_state

        key = float(minimum)
        if key not in self.breadth_states:
            self.breadth_states[key] = breadth_state(self.gauges, key)
        return self.breadth_states[key]

    def breadth_gate_block(self, strategy: str | None) -> dict | None:
        minimum = self.breadth_gates.get(strategy, 0.0) if strategy else 0.0
        if not minimum:
            return None
        state = self.breadth_gate_state(minimum)
        if state["allowed"]:
            return None
        return _block("breadth_gate", f"{state['reason']}; {strategy} takes no new longs while breadth is weak")

    def record_entry(self, symbol: str | None = None) -> int:
        """A BUY this cycle submitted: counts toward the session entry cap for the rest of the cycle."""
        self.cycle_entries += 1
        return self.session_entries + self.cycle_entries

    def check(self, symbol: str, strategy: str | None) -> dict | None:
        """Block dict for a new BUY entry, or None to let existing gates decide."""
        if self.load_error:
            return _block("unavailable", f"protection state could not be loaded ({self.load_error}); entry withheld")
        for outcome in (
            model_monitoring_block(self.model_breaches, strategy),
            self.market_gate_block(strategy),
            self.sector_gate_block(symbol, strategy),
            self.breadth_gate_block(strategy),
            entry_cap_block(self.session_entries + self.cycle_entries, self.config,
                            self.bounds[1] if self.bounds else None),
            time_of_day_block(self.now, self.bounds, self.config),
            drawdown_block(self.drawdown, self.config, self.bounds[1] if self.bounds else None),
            stoploss_guard_block(self.events, strategy, self.now, self.config),
            cooldown_block(self.events, symbol, self.now, self.config),
            stop_cooldown_block(self.events, symbol, self.now, self.config),
            low_profit_block(self.trades, symbol, strategy, self.now, self.config),
            strategy_drawdown_block(self.trades, strategy, self.now, self.config, self.allocated_notional),
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
        out["strategy_locks"] = self.strategy_locks()
        if self.market_states:
            out["market_gate"] = {"gates": dict(sorted(self.market_gates.items())),
                                  "states": {str(k): v for k, v in sorted(self.market_states.items())}}
        if self.sector_gates:
            out["sector_gate"] = {"gates": dict(sorted(self.sector_gates.items())),
                                  "loaded": sorted(etf for etf, frame in self.sector_history.items() if frame is not None),
                                  "unavailable": sorted(etf for etf, frame in self.sector_history.items() if frame is None),
                                  "states": {f"{etf}:{window}": state
                                             for (etf, window), state in sorted(self.sector_states.items())}}
        if self.breadth_gates:
            out["breadth_gate"] = {"gates": dict(sorted(self.breadth_gates.items())),
                                   "states": {f"{k:g}": v for k, v in sorted(self.breadth_states.items())}}
        if self.config.max_new_entries_per_session:
            out["entry_cap"] = {"cap": self.config.max_new_entries_per_session,
                                "session_entries": self.session_entries, "cycle_entries": self.cycle_entries}
        if self.model_breaches:
            out["model_monitoring_lock"] = {"breaches": sorted(self.model_breaches),
                                            "strategies": sorted(MODEL_DEPENDENT_STRATEGIES)}
        return out

    def strategy_locks(self) -> list[dict]:
        """Active per-strategy drawdown locks (entries blocked, exits allowed)."""
        strategies = sorted({row.strategy for row in self.trades})
        return [state for state in (
            strategy_drawdown_state(self.trades, name, self.now, self.config, self.allocated_notional)
            for name in strategies
        ) if state is not None]


def load_entry_protections(db, account, *, now: datetime, rules: dict | None,
                           type_by_id: dict, bounds_for: Callable = session_bounds,
                           market_gates: dict | None = None,
                           market_frame: Callable | None = None,
                           sector_gates: dict | None = None, symbols: Iterable[str] | None = None,
                           sector_frame: Callable | None = None,
                           breadth_gates: dict | None = None, gauges: dict | None = None) -> EntryProtections:
    """Load fills/orders/equity once per cycle and build the evaluator.

    ``sector_gates`` (strategy -> window) with ``symbols`` (the cycle's
    symbols) loads each needed sector ETF once via ``sector_frame(etf)``
    (default: the trusted daily history); ``breadth_gates`` (strategy ->
    floor) is decided from ``gauges`` (``market_gauges.latest_gauges``).
    Any load failure fails closed for entries only (``check`` withholds every
    BUY); the caller's sell/exit path is unaffected.
    """
    from app.models import StockMonitoringBreach
    from app.models.stock_paper import StockPaperEquitySnapshot, StockPaperFill, StockPaperOrder

    now = _utc(now)
    config = protection_config(rules)
    protections = EntryProtections(now=now, config=config)
    try:
        protections.bounds = bounds_for(now.astimezone(ET).date())
        protections.market_gates = {k: int(v) for k, v in (market_gates or {}).items() if v}
        if protections.market_gates:
            from app.services.market_gate import MARKET_SYMBOL, market_state

            try:
                if market_frame is None:
                    from app.services.trusted_data import trusted_history

                    spy = trusted_history(db, MARKET_SYMBOL, require_active=False)[0]
                else:
                    spy = market_frame()
            except Exception as exc:  # gated strategies fail closed; others unaffected
                logger.warning("market gate: %s history unavailable: %s", MARKET_SYMBOL, exc)
                spy = None
            protections.market_states = {window: market_state(spy, window)
                                         for window in sorted(set(protections.market_gates.values()))}
            protections.market_history = spy
        protections.sector_gates = {k: int(v) for k, v in (sector_gates or {}).items() if v}
        if protections.sector_gates:
            from app.services.market_gate import sector_symbol

            etfs = sorted({etf for etf in (sector_symbol(s) for s in (symbols or ())) if etf is not None})
            for etf in etfs:
                try:
                    if sector_frame is None:
                        from app.services.trusted_data import trusted_history

                        frame = trusted_history(db, etf, require_active=False)[0]
                    else:
                        frame = sector_frame(etf)
                except Exception as exc:  # this ETF's symbols fail closed for gated strategies; others unaffected
                    logger.warning("sector gate: %s history unavailable: %s", etf, exc)
                    frame = None
                protections.sector_history[etf] = frame
            for window in sorted(set(protections.sector_gates.values())):
                for symbol in symbols or ():
                    protections.sector_gate_state(symbol, window)
        protections.breadth_gates = {k: float(v) for k, v in (breadth_gates or {}).items() if v}
        protections.gauges = dict(gauges or {})
        for minimum in sorted(set(protections.breadth_gates.values())):
            protections.breadth_gate_state(minimum)
        protections.model_breaches = [
            row.breach_key for row in db.query(StockMonitoringBreach).filter(
                StockMonitoringBreach.breach_key.like("model.%"),
                StockMonitoringBreach.status.in_(("observed", "persistent")),
            ).all()
        ]
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
        if config.max_new_entries_per_session and protections.bounds is not None:
            opened = _utc(protections.bounds[0])
            session_buys = [row for row in records if row.side == "buy" and _utc(row.at) >= opened]
            protections.session_entries = len({row.order_id if row.order_id is not None else id(row)
                                               for row in session_buys})
        equity = getattr(account, "equity", None)
        if equity is not None and Decimal(equity) > 0:
            protections.allocated_notional = Decimal(equity) * config.strategy_allocation_fraction
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


# --------------------------------------------------------------------------
# per-entry-date caps on exploration entries


@dataclass
class ExplorationDateCaps:
    """At most ``per_strategy`` exploration entries per strategy and ``per_sector`` per sector on one entry date.

    Same-day learning lots of one strategy, or in one sector, are one bet
    (a market-wide move stops them together), so the exploration lane spreads
    them over dates.  Counts are this session's exploration-source BUY orders
    that were not cancelled or rejected, plus the BUYs this cycle submits
    (``record``).  Only exploration entries are checked; scheduled entries
    keep the core caps.  0 disables a cap.
    """

    per_strategy: int = 0
    per_sector: int = 0
    by_strategy: dict = field(default_factory=dict)
    by_sector: dict = field(default_factory=dict)
    load_error: str | None = None

    def check(self, strategy: str | None, sector: str | None) -> dict | None:
        if self.load_error:
            return _block("exploration_date_cap",
                          f"exploration entry counts could not be loaded ({self.load_error}); entry withheld")
        if self.per_strategy and strategy and self.by_strategy.get(strategy, 0) >= self.per_strategy:
            return _block("exploration_date_cap", (
                f"{strategy} already took {self.by_strategy[strategy]} exploration entries this session "
                f"(cap {self.per_strategy} per strategy per entry date)"))
        if self.per_sector and sector and self.by_sector.get(sector, 0) >= self.per_sector:
            return _block("exploration_date_cap", (
                f"sector {sector} already has {self.by_sector[sector]} exploration entries this session "
                f"(cap {self.per_sector} per sector per entry date)"))
        return None

    def record(self, strategy: str | None, sector: str | None) -> None:
        if strategy:
            self.by_strategy[strategy] = self.by_strategy.get(strategy, 0) + 1
        if sector:
            self.by_sector[sector] = self.by_sector.get(sector, 0) + 1

    def summary(self) -> dict:
        out = {"per_strategy": self.per_strategy, "per_sector": self.per_sector,
               "by_strategy": dict(sorted(self.by_strategy.items())), "by_sector": dict(sorted(self.by_sector.items()))}
        if self.load_error:
            out["load_error"] = self.load_error
        return out


DEAD_ORDER_STATUSES = frozenset({"cancelled", "canceled", "rejected", "expired", "unknown"})


def load_exploration_date_caps(db, account, *, now: datetime, type_by_id: dict,
                               sector_of: Callable[[str], str], bounds_for: Callable = session_bounds,
                               per_strategy: int | None = None, per_sector: int | None = None) -> ExplorationDateCaps:
    """This session's exploration BUYs by strategy and sector (``sector_of(symbol)``); fails closed for exploration entries only."""
    from app.models.stock_paper import StockPaperOrder
    from app.services.stock_paper_ledger import EXPLORATION_SOURCE

    caps = ExplorationDateCaps(
        per_strategy=int(settings.exploration_max_entries_per_strategy_per_date if per_strategy is None else per_strategy),
        per_sector=int(settings.exploration_max_entries_per_sector_per_date if per_sector is None else per_sector))
    if not caps.per_strategy and not caps.per_sector:
        return caps
    now = _utc(now)
    try:
        bounds = bounds_for(now.astimezone(ET).date())
        start = _utc(bounds[0]) if bounds is not None else datetime.combine(
            now.astimezone(ET).date(), datetime.min.time(), tzinfo=ET).astimezone(UTC)
        rows = db.query(StockPaperOrder).filter(
            StockPaperOrder.account_id == account.id, StockPaperOrder.source == EXPLORATION_SOURCE,
            StockPaperOrder.side == "buy", StockPaperOrder.created_at >= start,
        ).all()
        for row in rows:
            if str(row.status) in DEAD_ORDER_STATUSES or _utc(row.created_at) < start:
                continue
            caps.record(type_by_id.get(row.strategy_id) if row.strategy_id is not None else None,
                        sector_of(str(row.symbol).upper()))
    except Exception as exc:  # fail closed for exploration entries only
        logger.warning("exploration date caps unavailable: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        caps.load_error = f"{exc.__class__.__name__}: {exc}"
    return caps
