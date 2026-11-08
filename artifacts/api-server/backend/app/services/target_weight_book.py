"""Target-weight book: the slow book's netted, tranched sleeve engine (paper only).

Step 2 of ``reports/Retail bot edge feasibility.md`` ("build the slow book with
a passive core"); what was built and why is in ``reports/slow-book-20261107.md``.
This engine runs beside the slot and bracket system of
``stock_paper_autotrader`` and never shares a symbol with it.

Sleeves
    A sleeve is a ``Strategy`` row whose parameters carry
    ``sleeve_cadence = "monthly"`` (the step 1 verdict code reads this to
    exempt sleeves from the 20-trade revert) and a ``target_weights`` map
    ``{symbol: weight}``.  A sleeve with capital has ``current_status =
    "target_weight_active"`` (a status the slot cycle, the experiment engine
    and the allocation code never select); any other status means no capital.
    Non-arm sleeves take ``capital_fraction`` of the book; the passive arm takes
    the rest.

Passive arm
    The ``passive_arm`` sleeve holds every dollar the other strategies do not
    use, in ``settings.passive_arm_equity_symbol`` (default VOO) at
    ``settings.passive_arm_equity_weight`` of the arm (default 0.20, a risk
    budget the owner sets) and the rest in ``settings.passive_arm_tbill_symbol``
    (default SGOV).  It never holds SPY, QQQ, IWM or DIA, so the
    one-position-per-symbol rule never blocks IBS or RSI(2), and the book's
    symbols are removed from the slot cycle, so every broker position belongs to
    exactly one side.  Free capital is equity less what the slot strategies hold
    or are buying, less ``target_weight_slot_reserve`` of equity left for their
    entries, less a cash buffer, and never more than the gross-exposure cap
    allows.

Tranches
    The book is four tranches.  Tranche k rebalances once a month, on the k-th
    ``target_weight_rebalance_weekday`` of the month (or the next session of
    that week), so a quarter of the book rebalances each week.  A tranche's
    capital is a quarter of the free capital, bounded by what the other three
    tranches leave free.  Tranche state lives in audit rows (no migration): the
    latest ``tranche_rebalance`` row per tranche holds its shares per sleeve per
    symbol.  The book target per symbol is the sum over tranches; orders are
    that target less the broker holding, netted across sleeves, sells first,
    buys bounded by free cash.  A symbol whose tranche change is under the
    band (``target_weight_rebalance_band`` of the tranche's capital) keeps its
    old shares.  The first run of the day plans the tranche; the later runs
    that day send what is still missing (sale proceeds settle into cash on the
    five-minute reconciliation).

Orders
    Marketable DAY limit orders (decision price ± ``target_weight_limit_collar_bps``)
    through ``reserve_stock_paper_order`` / ``dispatch_reserved_order`` with
    source ``target_weight_book``, every launch, feed, reconciliation and kill
    switch gate unchanged, the sleeve as the order's strategy and an
    idempotency key ``tw:<cycle>:t<k>:<symbol>:<side>:<n>`` numbered per
    attempt (PR #17).  Fractional quantities where Alpaca lists the asset as
    fractionable, whole shares otherwise.  Each fill is benchmarked against the
    delayed SIP tape after the close: the minute bar containing it and the
    session's SIP close.

Governed risk-rule change
    Target-weight BUYs are exempt from the 1%-per-BUY and 10%-per-symbol caps
    while the ``target_weight_risk_exemption`` ``strategy_governance`` action is
    in force (``set_exemption`` revokes it); the 100% gross-exposure cap, the
    kill switch and the daily drawdown limit still apply
    (``risk.approve_target_weight_order``).  No other order is affected.  After
    each tranche rebalance every holding must sit within
    ``target_weight_weight_tolerance`` (1 point of equity) of its target.

Turnover
    One-sided turnover (half of buys plus sells) of the book's own fills over
    the trailing 12 months, in multiples of average equity.  Above
    ``target_weight_turnover_soft`` the bands widen by
    ``target_weight_band_widen_factor``; at ``target_weight_turnover_hard`` only
    risk-reducing sells go out.

Ledger records (``event_type = "target_weight_book"``)
    ``tranche_rebalance``   one per tranche per month: plan, capital, shares, band
    ``tranche_orders``      the orders each run sent and what the ledger said
    ``tranche_check``       post-tranche holdings against target (1-point rule)
    ``fill_benchmark``      per session: each book fill against delayed SIP
    ``distribution_accrual`` one per symbol and ex-date: modeled dividend =
                            amount per share x shares held at the prior close
                            (yfinance dividend history), split to sleeves
    ``sleeve_daily_pnl``    one per session, read by the step 1 harm guardrail:
                            ``payload["sleeves"][<strategy id>]`` =
                            ``{"strategy_type", "pnl", "distributions",
                            "value", "return"}`` and ``payload["passive_arm"]``
                            = ``{"strategy_id", "return", "pnl", "value"}``
                            (the passive-arm return series), plus the book and
                            the same-weights replay
    ``cycle_verdict``       one per month: the step 2 pass rule
    ``engine_pause`` / ``engine_resume``

Pass rule (first three complete monthly cycles)
    Every post-tranche check within 1 point, the month's paper return within
    ``target_weight_tracking_tolerance_bps`` of a same-weights replay (the
    intended shares, traded at the decision price, valued at the daily close,
    with modeled distributions on both sides), and turnover inside the budget.
    A failure pauses the engine (no further rebalance orders; holdings are kept
    and new money stays in cash) and records why; ``resume_engine`` restarts
    the three-cycle clock.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog, IntradayBar, MarketPrice, RiskRule, Strategy
from app.models.stock_paper import StockPaperEquitySnapshot, StockPaperFill, StockPaperOrder, StockPaperPosition
from app.services.audit import write_audit_log
from app.services.exchange_sessions import session_bounds

logger = logging.getLogger(__name__)
UTC = timezone.utc
ET = ZoneInfo("America/New_York")

EVENT = "target_weight_book"
REBALANCE_ACTION = "tranche_rebalance"
ORDERS_ACTION = "tranche_orders"
CHECK_ACTION = "tranche_check"
BENCHMARK_ACTION = "fill_benchmark"
DISTRIBUTION_ACTION = "distribution_accrual"
PNL_ACTION = "sleeve_daily_pnl"
VERDICT_ACTION = "cycle_verdict"
PAUSE_ACTION = "engine_pause"
RESUME_ACTION = "engine_resume"
GOVERNANCE_EVENT = "strategy_governance"
EXEMPTION_ACTION = "target_weight_risk_exemption"
EXEMPTION_REVOKE_ACTION = "target_weight_risk_exemption_revoked"
SOURCE = "target_weight_book"
SLEEVE_CADENCE = "monthly"
SLEEVE_ACTIVE = "target_weight_active"
PASSIVE_ARM_TYPE = "passive_arm"
PASSIVE_ARM_NAME = "Passive Arm"
INDEX_ETFS = frozenset({"SPY", "QQQ", "IWM", "DIA"})
TRANCHES = (1, 2, 3, 4)
REPORT = "reports/slow-book-20261107.md"
FRACTIONAL_INCREMENT = Decimal("0.0001")
WHOLE_SHARE = Decimal("1")
# Rebalances stay clear of the opening and closing auctions.
OPEN_BUFFER = timedelta(minutes=30)
CLOSE_BUFFER = timedelta(minutes=30)
NONTERMINAL = frozenset({"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "pending_replace",
                         "open", "held", "stopped", "calculated", "reserved", "submitting", "unknown"})
BACKFILL_SESSIONS = 10
ZERO = Decimal("0")


# ------------------------------------------------------------------ helpers

def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _d(value: Any, default: Decimal = ZERO) -> Decimal:
    try:
        return Decimal(str(value)) if value is not None else default
    except Exception:
        return default


def _s(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != 0 else "0"


def _f(value: Any, digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def previous_session(day: date) -> date:
    probe = day - timedelta(days=1)
    for _ in range(15):
        if session_bounds(probe) is not None:
            return probe
        probe -= timedelta(days=1)
    return probe


def next_session(day: date) -> date:
    probe = day + timedelta(days=1)
    for _ in range(15):
        if session_bounds(probe) is not None:
            return probe
        probe += timedelta(days=1)
    return probe


def last_session_of_month(year: int, month: int) -> date:
    probe = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    while session_bounds(probe) is None:
        probe -= timedelta(days=1)
    return probe


def last_closed_session(now: datetime) -> Optional[date]:
    """The newest session whose regular close is in the past at ``now``."""
    day = _utc(now).astimezone(ET).date()
    for _ in range(15):
        bounds = session_bounds(day)
        if bounds is not None and _utc(bounds[1]) <= _utc(now):
            return day
        day -= timedelta(days=1)
    return None


def cycle_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


# ---------------------------------------------------------- sleeves, symbols

def is_sleeve(row: Any) -> bool:
    params = getattr(row, "parameters", None) or {}
    return isinstance(params, Mapping) and params.get("sleeve_cadence") == SLEEVE_CADENCE


def passive_symbols() -> tuple[str, str]:
    return settings.passive_arm_equity_symbol.upper(), settings.passive_arm_tbill_symbol.upper()


def passive_weights() -> dict[str, Decimal]:
    equity, tbill = passive_symbols()
    weight = min(max(_d(settings.passive_arm_equity_weight), ZERO), Decimal("1"))
    return {equity: weight, tbill: Decimal("1") - weight}


def sleeve_rows(db: Session) -> list[Strategy]:
    return [row for row in db.query(Strategy).filter(Strategy.is_active.is_(True)).order_by(Strategy.id).all()
            if is_sleeve(row)]


def passive_arm_row(db: Session) -> Optional[Strategy]:
    return db.query(Strategy).filter(Strategy.strategy_type == PASSIVE_ARM_TYPE).order_by(Strategy.id).first()


def sleeve_weights(row: Strategy) -> dict[str, Decimal]:
    """A sleeve's target weights; the passive arm reads the owner's settings."""
    if row.strategy_type == PASSIVE_ARM_TYPE:
        return passive_weights()
    raw = (row.parameters or {}).get("target_weights") or {}
    weights = {}
    for symbol, weight in raw.items() if isinstance(raw, Mapping) else ():
        name = str(symbol).strip().upper()
        value = _d(weight)
        if name and name not in INDEX_ETFS and value > 0:
            weights[name] = value
    total = sum(weights.values(), ZERO)
    return {key: value / total for key, value in weights.items()} if total > 0 else {}


def book_symbols(db: Optional[Session] = None) -> frozenset[str]:
    """Every symbol the book may hold; the slot cycle never trades these."""
    symbols = set(passive_symbols())
    if db is not None:
        try:
            for row in sleeve_rows(db):
                if row.strategy_type != PASSIVE_ARM_TYPE:
                    symbols |= set(sleeve_weights(row))
        except Exception as exc:  # a bad sleeve row never widens or breaks the fence
            logger.info("sleeve symbols unavailable: %s", exc.__class__.__name__)
    return frozenset(symbols - INDEX_ETFS)


# --------------------------------------------------------------- governance

def exemption_active(db: Session) -> bool:
    """Whether the governed target-weight exemption from the 1% and 10% caps is in force."""
    row = db.query(AuditLog).filter(
        AuditLog.event_type == GOVERNANCE_EVENT,
        AuditLog.action.in_((EXEMPTION_ACTION, EXEMPTION_REVOKE_ACTION)),
    ).order_by(AuditLog.id.desc()).first()
    return row is not None and row.action == EXEMPTION_ACTION


EXEMPTION_MESSAGE = (
    f"{REPORT}: target-weight rebalance orders (source target_weight_book, the book's own symbols, an active "
    "sleeve) are exempt from the 1%-of-equity-per-BUY and 10%-per-symbol caps. The 100% gross-exposure cap, "
    "the kill switch and the daily drawdown limit still apply, and every holding must sit within 1 point of "
    "its target after its tranche rebalances. Scheduled and exploration orders are unchanged."
)


def set_exemption(db: Session, *, active: bool, reason: str, actor: str = "operator") -> dict:
    """Grant or revoke the governed exemption (a hash-chained ``strategy_governance`` row)."""
    action = EXEMPTION_ACTION if active else EXEMPTION_REVOKE_ACTION
    row = write_audit_log(
        db, event_type=GOVERNANCE_EVENT, entity_type="risk_rule", action=action, status="complete",
        message=(EXEMPTION_MESSAGE if active else f"Target-weight risk exemption revoked: {reason}"),
        payload={"exempt_caps": ["max_risk_per_trade", "max_symbol_exposure"], "kept_caps": ["max_total_exposure"],
                 "scope": {"source": SOURCE, "sleeve_status": SLEEVE_ACTIVE}, "reason": reason, "actor": actor,
                 "source_report": REPORT, "paper_only": True},
    )
    db.flush()
    return {"action": action, "audit_id": row.id, "exemption_active": active}


def _latest(db: Session, actions: Iterable[str]) -> Optional[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action.in_(tuple(actions))) \
        .order_by(AuditLog.id.desc()).first()


def engine_state(db: Session) -> dict:
    """Paused or running, why, and where the three-cycle clock starts (an audit row id)."""
    row = _latest(db, (PAUSE_ACTION, RESUME_ACTION))
    paused = row is not None and row.action == PAUSE_ACTION
    resume = _latest(db, (RESUME_ACTION,))
    return {
        "enabled": bool(settings.target_weight_book_enabled),
        "paused": paused,
        "reason": (row.message if paused else None),
        "since": (row.created_at.isoformat() if row is not None and row.created_at else None),
        "clock_start_audit_id": resume.id if resume is not None else 0,
    }


def pause_engine(db: Session, *, reasons: list[str], cycle: Optional[str] = None) -> dict:
    state = engine_state(db)
    if state["paused"]:
        return {"status": "already_paused", "reason": state["reason"]}
    message = "Target-weight engine paused; no rebalance orders until resumed: " + "; ".join(reasons)
    row = write_audit_log(db, event_type=EVENT, entity_type="target_weight_book", action=PAUSE_ACTION,
                          status="paused", message=message,
                          payload={"reasons": reasons, "cycle": cycle, "holdings": "kept; new money stays in cash",
                                   "paper_only": True})
    try:
        from app.services.notifications import create_notification

        create_notification(db, category="target_weight_book", severity="warning", source="target_weight_book",
                            title="Slow book paused", message=message[:1000], entity_type="audit_log",
                            entity_id=None, payload={"reasons": reasons, "cycle": cycle})
    except Exception as exc:  # the pause stands without the notification
        logger.info("target-weight pause notification unavailable: %s", exc.__class__.__name__)
    db.flush()
    return {"status": "paused", "audit_id": row.id, "reasons": reasons}


def resume_engine(db: Session, *, reason: str, actor: str = "operator") -> dict:
    """Resume after a fix; the three-cycle clock restarts at this row."""
    changed = []
    arm = passive_arm_row(db)
    if arm is not None and arm.current_status in {"paused", "paper_trading_candidate"}:
        # ``pause_all_strategies`` / ``resume_candidate_strategies`` move every
        # row; the arm's capital status comes back with the engine.
        old, arm.current_status = arm.current_status, SLEEVE_ACTIVE
        write_audit_log(db, event_type=GOVERNANCE_EVENT, entity_type="strategy", entity_id=arm.id,
                        action="passive_arm_reactivation", status="complete",
                        message=f"Passive arm restored to {SLEEVE_ACTIVE} with the engine resume: {reason}",
                        payload={"old_status": old, "new_status": SLEEVE_ACTIVE, "actor": actor, "paper_only": True})
        changed.append(arm.id)
    row = write_audit_log(db, event_type=EVENT, entity_type="target_weight_book", action=RESUME_ACTION,
                          status="running", message=f"Target-weight engine resumed; the three-cycle clock restarts: {reason}",
                          payload={"reason": reason, "actor": actor, "restored_sleeves": changed, "paper_only": True})
    db.flush()
    return {"status": "running", "audit_id": row.id, "restored_sleeves": changed}


# ----------------------------------------------------------------- schedule

def tranche_dates(year: int, month: int, weekday: Optional[int] = None) -> dict[int, date]:
    """Scheduled date of each tranche: the k-th ``weekday`` of the month."""
    weekday = settings.target_weight_rebalance_weekday if weekday is None else weekday
    first = date(year, month, 1)
    offset = (int(weekday) - first.weekday()) % 7
    return {k: first + timedelta(days=offset + 7 * (k - 1)) for k in TRANCHES}


def due_tranche(day: date, weekday: Optional[int] = None) -> Optional[int]:
    """The tranche whose week contains ``day`` (its scheduled date or a later session that week)."""
    if session_bounds(day) is None:
        return None
    for k, scheduled in tranche_dates(day.year, day.month, weekday).items():
        if scheduled <= day < scheduled + timedelta(days=7):
            return k
    return None


def schedule_view(today: date) -> dict:
    dates = tranche_dates(today.year, today.month)
    nxt_month = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    upcoming = [(k, d) for k, d in dates.items() if d >= today] or \
        list(tranche_dates(nxt_month.year, nxt_month.month).items())[:1]
    return {"cycle": cycle_of(today), "weekday": settings.target_weight_rebalance_weekday,
            "dates": {str(k): d.isoformat() for k, d in dates.items()},
            "due_today": due_tranche(today),
            "next": {"tranche": upcoming[0][0], "date": upcoming[0][1].isoformat()} if upcoming else None}


# ------------------------------------------------------------ tranche state

def _rows(db: Session, action: str) -> list[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == action) \
        .order_by(AuditLog.id).all()


def tranche_rows(db: Session) -> list[AuditLog]:
    return _rows(db, REBALANCE_ACTION)


def tranche_states(rows: Iterable[AuditLog], *, through: Optional[date] = None) -> dict[int, dict]:
    """Latest plan per tranche (optionally only plans executed on or before ``through``)."""
    out: dict[int, dict] = {}
    for row in rows:
        payload = row.payload or {}
        if through is not None and date.fromisoformat(payload["executed_on"]) > through:
            continue
        out[int(payload["tranche"])] = payload | {"audit_id": row.id}
    return out


def tranche_shares(state: Mapping) -> dict[str, dict[str, Decimal]]:
    return {str(sid): {sym: _d(qty) for sym, qty in (symbols or {}).items()}
            for sid, symbols in (state.get("sleeves") or {}).items()}


def book_target(states: Mapping[int, Mapping]) -> dict[str, Decimal]:
    """Netted target shares per symbol: the sum over tranches and sleeves."""
    total: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for state in states.values():
        for symbols in tranche_shares(state).values():
            for symbol, qty in symbols.items():
                total[symbol] += qty
    return dict(total)


def sleeve_targets(states: Mapping[int, Mapping]) -> dict[str, dict[str, Decimal]]:
    out: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for state in states.values():
        for sid, symbols in tranche_shares(state).items():
            for symbol, qty in symbols.items():
                out[sid][symbol] += qty
    return {sid: dict(values) for sid, values in out.items()}


def attribution(states: Mapping[int, Mapping], fallback_sleeve: Optional[int] = None) -> dict[str, dict[str, Decimal]]:
    """Split of each symbol's netted position back to sleeves, by target shares."""
    per_sleeve = sleeve_targets(states)
    totals = book_target(states)
    out: dict[str, dict[str, Decimal]] = {}
    for symbol, total in totals.items():
        if total > 0:
            out[symbol] = {sid: values.get(symbol, ZERO) / total for sid, values in per_sleeve.items()
                           if values.get(symbol, ZERO) > 0}
    if fallback_sleeve is not None:
        for symbol in book_symbols():
            out.setdefault(symbol, {str(fallback_sleeve): Decimal("1")})
    return out


# ------------------------------------------------------------------ pricing

FRESH_PRICE_AGE = timedelta(minutes=15)


def latest_price(db: Session, symbol: str, *, now: Optional[datetime] = None) -> Optional[Decimal]:
    """Close of the newest execution-feed minute bar (the ledger's reference bar).

    With ``now`` a bar older than ``FRESH_PRICE_AGE`` is no price: a plan is
    never sized on a stale quote (the ledger re-checks the feed per order).
    """
    from app.services.intraday_data import MARKET_DATA_PROVIDER

    bar = db.query(IntradayBar).filter_by(symbol=symbol, timeframe="1m", provider=MARKET_DATA_PROVIDER) \
        .order_by(IntradayBar.opened_at.desc()).first()
    if bar is None or not bar.close or bar.close <= 0:
        return None
    if now is not None and _utc(bar.opened_at) < _utc(now) - FRESH_PRICE_AGE:
        return None
    return Decimal(str(bar.close))


def daily_close(db: Session, symbol: str, day: date) -> Optional[Decimal]:
    row = db.query(MarketPrice).filter(MarketPrice.symbol == symbol, MarketPrice.price_date == day,
                                       MarketPrice.close.isnot(None)).order_by(MarketPrice.id.desc()).first()
    return Decimal(str(row.close)) if row is not None and row.close and row.close > 0 else None


def limit_price(reference: Decimal, side: str) -> Decimal:
    collar = Decimal(str(settings.target_weight_limit_collar_bps)) / Decimal("10000")
    if side == "buy":
        return (reference * (1 + collar)).quantize(Decimal("0.01"), rounding=ROUND_UP)
    return (reference * (1 - collar)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def asset_increments(gateway: Any, symbols: Iterable[str]) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Quantity increment per symbol (fractional when Alpaca lists it fractionable) and untradable symbols."""
    increments: dict[str, Decimal] = {}
    blocked: dict[str, str] = {}
    reader = getattr(gateway, "asset", None) if gateway is not None else None
    for symbol in symbols:
        increments[symbol] = WHOLE_SHARE
        if reader is None:
            continue
        try:
            asset = reader(symbol)
        except Exception as exc:  # unknown: whole shares, and the broker decides
            logger.info("asset lookup failed for %s: %s", symbol, exc.__class__.__name__)
            continue
        if asset is None or asset.get("tradable") is False or str(asset.get("status", "active")).lower() != "active":
            blocked[symbol] = "Alpaca does not list the asset as active and tradable"
            continue
        if asset.get("fractionable") is True:
            increments[symbol] = FRACTIONAL_INCREMENT
    return increments, blocked


# ------------------------------------------------------------------ capital

def _active_rules(db: Session) -> dict:
    from app.services.risk import DEFAULT_RISK_RULES

    rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
    return DEFAULT_RISK_RULES | ((rule.value if rule else {}) or {})


def capital_plan(db: Session, account: Any, symbols: Iterable[str], prices: Mapping[str, Decimal]) -> dict:
    """What the book may hold: equity less the slot strategies' holdings, pending buys and reserve."""
    symbols = set(symbols)
    equity = _d(account.equity)
    rules = _active_rules(db)
    positions = db.query(StockPaperPosition).filter(StockPaperPosition.account_id == account.id,
                                                    StockPaperPosition.quantity > 0).all()
    book_value = ZERO
    slot_value = ZERO
    for row in positions:
        if row.symbol in symbols:
            price = prices.get(row.symbol) or _d(row.current_price)
            book_value += _d(row.quantity) * price
        else:
            slot_value += abs(_d(row.market_value))
    pending_slot = sum((_d(row.reserved_cash) for row in db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id, StockPaperOrder.side == "buy",
        StockPaperOrder.status.in_(NONTERMINAL)).all() if row.symbol not in symbols), ZERO)
    reserve = equity * _d(settings.target_weight_slot_reserve)
    buffer = equity * _d(settings.target_weight_cash_buffer)
    ceiling = min(equity - buffer, equity * _d(rules.get("max_total_exposure", 1.0)))
    capital = max(ZERO, ceiling - slot_value - pending_slot - reserve)
    return {"equity": equity, "book_value": book_value, "slot_value": slot_value, "slot_pending_buys": pending_slot,
            "slot_reserve": reserve, "cash_buffer": buffer, "ceiling": ceiling, "book_capital": capital}


def sleeve_allocations(db: Session) -> tuple[list[dict], list[dict]]:
    """Capital share per active sleeve (passive arm takes the rest) and the zero-capital ones."""
    active, shadow = [], []
    rows = sleeve_rows(db)
    others = [row for row in rows if row.strategy_type != PASSIVE_ARM_TYPE]
    used = ZERO
    for row in others:
        weights = sleeve_weights(row)
        fraction = min(max(_d((row.parameters or {}).get("capital_fraction")), ZERO), Decimal("1"))
        entry = {"id": row.id, "strategy_type": row.strategy_type, "weights": weights}
        if row.current_status == SLEEVE_ACTIVE and weights and fraction > 0 and used + fraction <= 1:
            active.append(entry | {"share": fraction})
            used += fraction
        else:
            shadow.append(entry | {"share": ZERO, "status": row.current_status})
    arm = next((row for row in rows if row.strategy_type == PASSIVE_ARM_TYPE), None)
    if arm is not None and arm.current_status == SLEEVE_ACTIVE:
        active.append({"id": arm.id, "strategy_type": PASSIVE_ARM_TYPE, "weights": sleeve_weights(arm),
                       "share": Decimal("1") - used})
    return active, shadow


# --------------------------------------------------------------- the plan

def plan_tranche(*, capital: Decimal, sleeves: list[dict], prices: Mapping[str, Decimal],
                 old: Mapping[str, Mapping[str, Decimal]], band: Decimal, increments: Mapping[str, Decimal],
                 hard_cap: bool = False) -> dict:
    """New shares per sleeve per symbol for one tranche (pure).

    ``band`` is a fraction of the tranche's capital: a symbol whose netted
    change is smaller keeps its old shares (a first funding always trades).
    At the hard turnover cap a symbol may only shrink.
    """
    new: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for sleeve in sleeves:
        alloc = capital * _d(sleeve["share"])
        for symbol, weight in sleeve["weights"].items():
            price = prices[symbol]
            step = increments.get(symbol, WHOLE_SHARE)
            new[str(sleeve["id"])][symbol] = (alloc * _d(weight) / price).quantize(step, rounding=ROUND_DOWN) \
                if price > 0 else ZERO
    old = {str(sid): {sym: _d(qty) for sym, qty in values.items()} for sid, values in old.items()}
    symbols = sorted({sym for values in new.values() for sym in values} | {sym for values in old.values() for sym in values})
    decisions = {}
    for symbol in symbols:
        old_total = sum((values.get(symbol, ZERO) for values in old.values()), ZERO)
        new_total = sum((values.get(symbol, ZERO) for values in new.values()), ZERO)
        price = prices.get(symbol, ZERO)
        change = abs(new_total - old_total) * price
        reason = "trade"
        if hard_cap and new_total > old_total:
            reason = "hard_cap_no_buys"
        elif old_total > 0 and change < band * capital:
            reason = "inside_band"
        if reason != "trade":
            for sid in set(new) | set(old):
                kept = old.get(sid, {}).get(symbol, ZERO)
                if kept or symbol in new.get(sid, {}):
                    new[sid][symbol] = kept
        decisions[symbol] = {"old": _s(old_total), "planned": _s(new_total), "decision": reason,
                             "change_value": _s(change.quantize(Decimal("0.01")))}
    return {"shares": {sid: dict(values) for sid, values in new.items()}, "decisions": decisions}


def net_orders(target: Mapping[str, Decimal], held: Mapping[str, Decimal], prices: Mapping[str, Decimal],
               increments: Mapping[str, Decimal], *, min_notional: Decimal) -> list[dict]:
    """Netted orders that bring holdings to the book target; sells first (pure)."""
    orders = []
    for symbol in sorted(set(target) | set(held)):
        want, have = _d(target.get(symbol)), _d(held.get(symbol))
        diff = want - have
        price = prices.get(symbol)
        if not price or diff == 0:
            continue
        step = increments.get(symbol, WHOLE_SHARE)
        if diff < 0 and want <= 0:
            quantity = have  # close out exactly, fractional remainder included
        else:
            quantity = abs(diff).quantize(step, rounding=ROUND_DOWN)
        if quantity <= 0 or quantity * price < min_notional:
            continue
        orders.append({"symbol": symbol, "side": "buy" if diff > 0 else "sell", "quantity": quantity,
                       "reference_price": price})
    return sorted(orders, key=lambda order: (order["side"] != "sell", order["symbol"]))


# ----------------------------------------------------------------- turnover

def _book_fills(db: Session, account_id: int, *, since: Optional[datetime] = None,
                until: Optional[datetime] = None) -> list[tuple[StockPaperFill, StockPaperOrder]]:
    query = db.query(StockPaperFill, StockPaperOrder).join(StockPaperOrder, StockPaperOrder.id == StockPaperFill.order_id) \
        .filter(StockPaperFill.account_id == account_id, StockPaperOrder.source == SOURCE)
    if since is not None:
        query = query.filter(StockPaperFill.filled_at >= since)
    if until is not None:
        query = query.filter(StockPaperFill.filled_at < until)
    return query.order_by(StockPaperFill.filled_at, StockPaperFill.id).all()


def _average_equity(db: Session, account: Any, since: datetime) -> Decimal:
    value = db.query(func.avg(StockPaperEquitySnapshot.equity)).filter(
        StockPaperEquitySnapshot.account_id == account.id, StockPaperEquitySnapshot.observed_at >= since).scalar()
    value = _d(value)
    return value if value > 0 else _d(account.equity)


def turnover(db: Session, account: Any, now: datetime, *, days: int = 365) -> dict:
    """Trailing one-sided turnover of the book's fills in multiples of average equity."""
    since = _utc(now) - timedelta(days=days)
    traded = sum((_d(fill.quantity) * _d(fill.price) for fill, _order in _book_fills(db, account.id, since=since)), ZERO)
    average = _average_equity(db, account, since)
    value = (traded / 2 / average) if average > 0 else ZERO
    soft, hard = _d(settings.target_weight_turnover_soft), _d(settings.target_weight_turnover_hard)
    state = "at_hard_cap" if value >= hard else "above_soft_budget" if value > soft else "inside_budget"
    return {"trailing_12m": _f(value, 4), "traded_notional": _f(traded, 2), "average_equity": _f(average, 2),
            "soft_budget": float(soft), "hard_cap": float(hard), "state": state}


def band_in_force(state: str) -> Decimal:
    band = _d(settings.target_weight_rebalance_band)
    if state in {"above_soft_budget", "at_hard_cap"}:
        band *= _d(settings.target_weight_band_widen_factor)
    return band


def _turnover_view(db: Session, account: Any, now: datetime) -> dict:
    usage = turnover(db, account, now)
    return usage | {"band_in_force": float(band_in_force(usage["state"])),
                    "rebalance_buys_allowed": usage["state"] != "at_hard_cap"}


# -------------------------------------------------------------- rebalance

Executor = Callable[..., dict]


def _ledger_executor(db: Session, *, symbol: str, side: str, quantity: Decimal, limit: Decimal, key: str,
                     strategy_id: int) -> dict:
    from app.services.stock_paper_ledger import dispatch_reserved_order, reserve_stock_paper_order

    order = reserve_stock_paper_order(db, symbol=symbol, side=side, quantity=quantity, reference_price=limit,
                                      idempotency_key=key, source=SOURCE, strategy_id=strategy_id)
    submitted = dispatch_reserved_order(db, order.id)
    return {"order_id": submitted.id, "order_status": str(submitted.status)}


def _attempt(db: Session, account_id: int, symbol: str, side: str, day_start: datetime) -> tuple[int, bool]:
    rows = db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account_id, StockPaperOrder.symbol == symbol, StockPaperOrder.side == side,
        StockPaperOrder.source == SOURCE, StockPaperOrder.created_at >= day_start).all()
    in_flight = any(row.status in NONTERMINAL | {"uncertain_submission"} for row in rows)
    return len(rows), in_flight


def _owner(states: Mapping[int, Mapping], symbol: str, fallback: int) -> int:
    targets = sleeve_targets(states)
    best = max(((qty.get(symbol, ZERO), sid) for sid, qty in targets.items()), default=(ZERO, None))
    return int(best[1]) if best[1] is not None and best[0] > 0 else fallback


def run_rebalance(db: Session, *, now: Optional[datetime] = None, executor: Optional[Executor] = None,
                  gateway: Any = None) -> dict:
    """Plan the due tranche (first run of its day) and send the netted orders still missing."""
    from app.services.stock_paper_ledger import StockPaperError, active_paper_account

    now = _utc(now or datetime.now(UTC))
    base = {"job": "target_weight_rebalance_job", "paper_only": True, "live_trading": False}
    if not settings.target_weight_book_enabled:
        return base | {"status": "skipped", "reason": "target-weight book is disabled"}
    state = engine_state(db)
    if state["paused"]:
        return base | {"status": "skipped", "reason": f"engine is paused: {state['reason']}"}
    account = active_paper_account(db)
    if account is None:
        return base | {"status": "skipped", "reason": "stock paper account is not initialized"}
    if account.status != "reconciled" or account.reconciliation_required or account.unexplained_residual:
        return base | {"status": "skipped", "reason": "stock paper account is not reconciled"}
    local = now.astimezone(ET)
    today = local.date()
    bounds = session_bounds(today)
    if bounds is None or not (_utc(bounds[0]) + OPEN_BUFFER <= now <= _utc(bounds[1]) - CLOSE_BUFFER):
        return base | {"status": "skipped", "reason": "outside the rebalance window (regular session, clear of the auctions)"}
    rules = _active_rules(db)
    if bool(rules.get("kill_switch_enabled")):
        return base | {"status": "skipped", "reason": "kill switch is on"}
    arm = passive_arm_row(db)
    if arm is None or not arm.is_active or arm.current_status != SLEEVE_ACTIVE:
        return base | {"status": "skipped", "reason": "passive arm is not active; holding"}
    tranche = due_tranche(today)
    if tranche is None:
        return base | {"status": "skipped", "reason": "no tranche is due this week"}
    cycle = cycle_of(today)
    base |= {"cycle": cycle, "tranche": tranche}
    rows = tranche_rows(db)
    existing = next((row for row in reversed(rows) if (row.payload or {}).get("cycle") == cycle
                     and int((row.payload or {}).get("tranche", 0)) == tranche), None)
    if existing is not None and (existing.payload or {}).get("executed_on") != today.isoformat():
        return base | {"status": "skipped",
                       "reason": f"tranche {tranche} already rebalanced on {existing.payload.get('executed_on')}"}
    active, shadow = sleeve_allocations(db)
    symbols = sorted({sym for sleeve in active for sym in sleeve["weights"]}
                     | set(book_target(tranche_states(rows))))
    prices = {symbol: latest_price(db, symbol, now=now) for symbol in symbols}
    missing = sorted(symbol for symbol, price in prices.items() if price is None)
    if missing:
        return base | {"status": "blocked", "reason": f"no fresh reference price for {', '.join(missing)}"}
    if gateway is None and executor is None:
        try:
            from app.services.stock_paper_ledger import active_paper_gateway

            gateway = active_paper_gateway()
        except Exception as exc:
            logger.info("paper gateway unavailable for asset lookup: %s", exc.__class__.__name__)
    increments, untradable = asset_increments(gateway, symbols)
    if untradable:
        return base | {"status": "blocked", "reason": "; ".join(f"{k}: {v}" for k, v in sorted(untradable.items()))}
    usage = turnover(db, account, now)
    band = band_in_force(usage["state"])
    hard_cap = usage["state"] == "at_hard_cap"
    if existing is None:
        states = tranche_states(rows)
        old = tranche_shares(states.get(tranche, {}))
        capital = capital_plan(db, account, symbols, prices)
        others = sum((qty * prices.get(sym, ZERO) for k, st in states.items() if k != tranche
                      for values in tranche_shares(st).values() for sym, qty in values.items()), ZERO)
        tranche_capital = max(ZERO, min(capital["book_capital"] / len(TRANCHES), capital["book_capital"] - others))
        plan = plan_tranche(capital=tranche_capital, sleeves=active, prices=prices, old=old, band=band,
                            increments=increments, hard_cap=hard_cap)
        funding = not any(qty > 0 for values in old.values() for qty in values.values())
        payload = {
            "cycle": cycle, "tranche": tranche, "scheduled_date": tranche_dates(today.year, today.month)[tranche].isoformat(),
            "executed_on": today.isoformat(), "planned_at": now.isoformat(),
            "capital": {key: _s(value.quantize(Decimal("0.01"))) for key, value in capital.items()},
            "tranche_capital": _s(tranche_capital.quantize(Decimal("0.01"))), "other_tranches_value": _s(others.quantize(Decimal("0.01"))),
            "sleeves": {sid: {sym: _s(qty) for sym, qty in values.items()} for sid, values in plan["shares"].items()},
            "previous": {sid: {sym: _s(qty) for sym, qty in values.items()} for sid, values in old.items()},
            "weights": {str(sleeve["id"]): {sym: _s(w) for sym, w in sleeve["weights"].items()} for sleeve in active},
            "sleeve_shares": {str(sleeve["id"]): _s(sleeve["share"]) for sleeve in active},
            "shadow_sleeves": [{"id": sleeve["id"], "strategy_type": sleeve["strategy_type"],
                                "weights": {sym: _s(w) for sym, w in sleeve["weights"].items()}} for sleeve in shadow],
            "prices": {sym: _s(price) for sym, price in prices.items()},
            "increments": {sym: _s(step) for sym, step in increments.items()},
            "decisions": plan["decisions"], "band": _s(band), "turnover": usage, "hard_cap": hard_cap,
            "funding": funding, "passive_arm_equity_weight": settings.passive_arm_equity_weight,
            "paper_only": True,
        }
        write_audit_log(db, event_type=EVENT, entity_type="target_weight_tranche", entity_id=tranche,
                        action=REBALANCE_ACTION, status="planned",
                        message=(f"Tranche {tranche} of {cycle}: {tranche_capital:.2f} of capital planned "
                                 f"({'first funding' if funding else 'monthly rebalance'}, band {band})"),
                        payload=payload)
        db.flush()
        rows = tranche_rows(db)
        base["planned"] = True
    states = tranche_states(rows)
    target = book_target(states)
    held = {row.symbol: _d(row.quantity) for row in db.query(StockPaperPosition).filter(
        StockPaperPosition.account_id == account.id, StockPaperPosition.symbol.in_(symbols)).all()}
    orders = net_orders(target, held, prices, increments, min_notional=_d(settings.target_weight_min_order_notional))
    if hard_cap:
        orders = [order for order in orders if order["side"] == "sell"]
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    cash = _d(account.cash) - sum((_d(row.reserved_cash) for row in db.query(StockPaperOrder).filter(
        StockPaperOrder.account_id == account.id, StockPaperOrder.side == "buy",
        StockPaperOrder.status.in_(NONTERMINAL)).all()), ZERO) - _d(account.equity) * _d(settings.target_weight_cash_buffer)
    run = executor or (lambda **kwargs: _ledger_executor(db, **kwargs))
    results = []
    for order in orders:
        symbol, side = order["symbol"], order["side"]
        result = {"symbol": symbol, "side": side, "planned_quantity": _s(order["quantity"]),
                  "reference_price": _s(order["reference_price"])}
        attempts, in_flight = _attempt(db, account.id, symbol, side, day_start)
        if in_flight:
            results.append(result | {"status": "waiting", "reason": "an order for this symbol is still in flight"})
            continue
        limit = limit_price(order["reference_price"], side)
        quantity = order["quantity"]
        if side == "buy":
            step = increments.get(symbol, WHOLE_SHARE)
            affordable = (max(cash, ZERO) / limit).quantize(step, rounding=ROUND_DOWN) if limit > 0 else ZERO
            quantity = min(quantity, affordable)
            if quantity <= 0 or quantity * limit < _d(settings.target_weight_min_order_notional):
                results.append(result | {"status": "waiting", "reason": "free cash does not cover the buy yet"})
                continue
        key = f"tw:{cycle}:t{tranche}:{symbol}:{side}:{attempts}"
        try:
            outcome = run(symbol=symbol, side=side, quantity=quantity, limit=limit, key=key,
                          strategy_id=_owner(states, symbol, arm.id))
        except StockPaperError as exc:
            results.append(result | {"status": "blocked", "reason": str(exc), "idempotency_key": key})
            continue
        except Exception as exc:  # one symbol must not stop the others
            db.rollback()
            results.append(result | {"status": "failed", "reason": f"{exc.__class__.__name__}: {exc}"})
            continue
        sent = str(outcome.get("order_status")) not in {"unknown", "cancelled", "rejected"}
        if side == "buy" and sent:
            cash -= quantity * limit
        results.append(result | {"status": "submitted" if sent else "blocked", "quantity": _s(quantity),
                                 "limit_price": _s(limit), "idempotency_key": key, **outcome})
    if results:
        write_audit_log(db, event_type=EVENT, entity_type="target_weight_tranche", entity_id=tranche,
                        action=ORDERS_ACTION, status="sent" if any(r["status"] == "submitted" for r in results) else "held",
                        message=f"Tranche {tranche} of {cycle}: {sum(r['status'] == 'submitted' for r in results)} of "
                                f"{len(results)} netted orders sent",
                        payload={"cycle": cycle, "tranche": tranche, "orders": results, "turnover": usage,
                                 "hard_cap": hard_cap, "paper_only": True})
    db.commit()
    return base | {"status": "complete", "orders": results, "turnover": usage,
                   "target": {sym: _s(qty) for sym, qty in target.items()}}


# ------------------------------------------------------------ daily ledger

def _fills_by_symbol(db: Session, account_id: int, symbols: Iterable[str]) -> dict[str, list[StockPaperFill]]:
    rows = db.query(StockPaperFill).filter(StockPaperFill.account_id == account_id,
                                           StockPaperFill.symbol.in_(list(symbols))) \
        .order_by(StockPaperFill.filled_at, StockPaperFill.id).all()
    out: dict[str, list[StockPaperFill]] = defaultdict(list)
    for row in rows:
        out[row.symbol].append(row)
    return out


def shares_before(fills: Iterable[StockPaperFill], moment: datetime) -> Decimal:
    total = ZERO
    for fill in fills:
        if _utc(fill.filled_at) < _utc(moment):
            total += _d(fill.quantity) * (1 if fill.side == "buy" else -1)
    return max(total, ZERO)


def _session_window(day: date) -> tuple[datetime, datetime]:
    """From one session's open to the next session's open (after-hours fills belong to the day)."""
    bounds = session_bounds(day)
    start = _utc(bounds[0]) if bounds else datetime.combine(day, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    nxt = session_bounds(next_session(day))
    return start, (_utc(nxt[0]) if nxt else start + timedelta(days=1))


def _distribution_rows(db: Session) -> list[AuditLog]:
    return _rows(db, DISTRIBUTION_ACTION)


def default_dividend_fetcher(symbol: str) -> list[tuple[date, Decimal]]:
    """Ex-dates and cash amounts per share from yfinance (the $0 data budget's dividend history)."""
    import yfinance as yf

    series = yf.Ticker(symbol).dividends
    out = []
    for stamp, amount in series.items():
        try:
            day = stamp.tz_convert(ET).date() if getattr(stamp, "tzinfo", None) else stamp.date()
        except Exception:
            day = stamp.date()
        if amount and float(amount) > 0:
            out.append((day, Decimal(str(float(amount)))))
    return out


def accrue_distributions(db: Session, account: Any, through: date, *,
                         fetcher: Optional[Callable[[str], list[tuple[date, Decimal]]]] = None) -> dict:
    """Book modeled distributions: ex-date x shares held at the prior close, once per symbol and ex-date."""
    fetcher = fetcher or default_dividend_fetcher
    symbols = sorted(book_symbols(db))
    fills = _fills_by_symbol(db, account.id, symbols)
    done = {((row.payload or {}).get("symbol"), (row.payload or {}).get("ex_date")) for row in _distribution_rows(db)}
    rows = tranche_rows(db)
    arm = passive_arm_row(db)
    recorded, failures = [], []
    for symbol in symbols:
        if not fills.get(symbol):
            continue
        first_day = _utc(fills[symbol][0].filled_at).astimezone(ET).date()
        try:
            history = fetcher(symbol)
        except Exception as exc:
            failures.append({"symbol": symbol, "error": f"{exc.__class__.__name__}: {exc}"[:200]})
            continue
        for ex_date, amount in sorted(history):
            if not (first_day < ex_date <= through) or (symbol, ex_date.isoformat()) in done:
                continue
            start, _end = _session_window(ex_date) if session_bounds(ex_date) else (
                datetime.combine(ex_date, datetime.min.time(), tzinfo=ET).astimezone(UTC), None)
            shares = shares_before(fills[symbol], start)
            if shares <= 0:
                continue
            states = tranche_states(rows, through=previous_session(ex_date))
            split = attribution(states, fallback_sleeve=arm.id if arm else None).get(symbol) or {}
            intended = book_target(states).get(symbol, ZERO)
            cash = (shares * _d(amount)).quantize(Decimal("0.0001"))
            payload = {"symbol": symbol, "ex_date": ex_date.isoformat(), "amount_per_share": _s(_d(amount)),
                       "shares": _s(shares), "amount": _s(cash), "intended_shares": _s(intended),
                       "replay_amount": _s((intended * _d(amount)).quantize(Decimal("0.0001"))),
                       "sleeves": {sid: _s((cash * share).quantize(Decimal("0.0001"))) for sid, share in split.items()},
                       "source": "yfinance dividend history", "modeled": True,
                       "note": "Alpaca paper does not simulate dividends; this is a modeled ledger entry, not cash.",
                       "paper_only": True}
            write_audit_log(db, event_type=EVENT, entity_type="target_weight_symbol", action=DISTRIBUTION_ACTION,
                            status="accrued", message=f"Modeled {symbol} distribution {amount} x {shares} on {ex_date}",
                            payload=payload)
            done.add((symbol, ex_date.isoformat()))
            recorded.append(payload)
    db.flush()
    return {"recorded": len(recorded), "failures": failures, "distributions": recorded}


def _distributions_on(db: Session, day: date) -> dict[str, dict]:
    return {(row.payload or {})["symbol"]: row.payload for row in _distribution_rows(db)
            if (row.payload or {}).get("ex_date") == day.isoformat()}


def daily_pnl(db: Session, account: Any, day: date) -> Optional[dict]:
    """One session's book, sleeve and same-weights replay P&L (None when a close is missing)."""
    previous = previous_session(day)
    symbols = sorted(book_symbols(db))
    fills = _fills_by_symbol(db, account.id, symbols)
    rows = tranche_rows(db)
    prev_states = tranche_states(rows, through=previous)
    day_states = tranche_states(rows, through=day)
    prev_target, day_target = book_target(prev_states), book_target(day_states)
    start, end = _session_window(day)
    distributions = _distributions_on(db, day)
    arm = passive_arm_row(db)
    split = attribution(day_states or prev_states, fallback_sleeve=arm.id if arm else None)
    per_symbol = {}
    book = {"pnl": ZERO, "denominator": ZERO, "value": ZERO, "distributions": ZERO}
    replay = {"pnl": ZERO, "denominator": ZERO, "value": ZERO}
    sleeves: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"pnl": ZERO, "denominator": ZERO, "value": ZERO,
                                                                  "distributions": ZERO})
    trade_refs: dict[str, Decimal] = {}
    for row in rows:
        payload = row.payload or {}
        if payload.get("executed_on") == day.isoformat():
            trade_refs.update({sym: _d(price) for sym, price in (payload.get("prices") or {}).items()})
    for symbol in symbols:
        held_prev = shares_before(fills.get(symbol, []), start)
        todays = [fill for fill in fills.get(symbol, []) if start <= _utc(fill.filled_at) < end]
        if held_prev <= 0 and not todays and prev_target.get(symbol, ZERO) <= 0 and day_target.get(symbol, ZERO) <= 0:
            continue
        close, close_prev = daily_close(db, symbol, day), daily_close(db, symbol, previous)
        if close is None or (close_prev is None and (held_prev > 0 or prev_target.get(symbol, ZERO) > 0)):
            return None
        close_prev = close_prev or close
        held_now = held_prev + sum((_d(f.quantity) * (1 if f.side == "buy" else -1) for f in todays), ZERO)
        dist = distributions.get(symbol) or {}
        dist_cash = _d(dist.get("amount"))
        dist_replay = _d(dist.get("replay_amount"))
        flow = sum((_d(f.quantity) * _d(f.price) * (1 if f.side == "buy" else -1) for f in todays), ZERO)
        pnl = held_prev * (close - close_prev) + sum(
            (_d(f.quantity) * (close - _d(f.price)) * (1 if f.side == "buy" else -1) for f in todays), ZERO) + dist_cash
        denominator = held_prev * close_prev + flow / 2
        intended_prev, intended_now = prev_target.get(symbol, ZERO), day_target.get(symbol, ZERO)
        ref = trade_refs.get(symbol, close_prev)
        replay_trade = intended_now - intended_prev
        replay_pnl = intended_prev * (close - close_prev) + replay_trade * (close - ref) + dist_replay
        replay_denominator = intended_prev * close_prev + replay_trade * ref / 2
        per_symbol[symbol] = {"close": _s(close), "previous_close": _s(close_prev), "shares_previous": _s(held_prev),
                              "shares": _s(held_now), "fills": len(todays), "net_flow": _s(flow.quantize(Decimal("0.01"))),
                              "pnl": _s(pnl.quantize(Decimal("0.0001"))), "distribution": _s(dist_cash),
                              "intended_previous": _s(intended_prev), "intended": _s(intended_now),
                              "replay_pnl": _s(replay_pnl.quantize(Decimal("0.0001")))}
        book["pnl"] += pnl
        book["denominator"] += denominator
        book["value"] += held_now * close
        book["distributions"] += dist_cash
        replay["pnl"] += replay_pnl
        replay["denominator"] += replay_denominator
        replay["value"] += intended_now * close
        for sid, share in (split.get(symbol) or {}).items():
            sleeves[sid]["pnl"] += pnl * share
            sleeves[sid]["denominator"] += denominator * share
            sleeves[sid]["value"] += held_now * close * share
            sleeves[sid]["distributions"] += dist_cash * share

    def ret(entry):
        return _f(entry["pnl"] / entry["denominator"], 8) if entry["denominator"] > 0 else None

    types = {str(row.id): row.strategy_type for row in db.query(Strategy).filter(
        Strategy.id.in_([int(sid) for sid in sleeves] or [-1])).all()}
    sleeve_out = {sid: {"strategy_type": types.get(sid), "pnl": _f(entry["pnl"], 4),
                        "distributions": _f(entry["distributions"], 4), "value": _f(entry["value"], 2),
                        "return": ret(entry)} for sid, entry in sleeves.items()}
    arm_id = str(arm.id) if arm is not None else None
    arm_entry = sleeves.get(arm_id) if arm_id else None
    return {
        "session": day.isoformat(), "previous_session": previous.isoformat(), "symbols": per_symbol,
        "sleeves": sleeve_out,
        "passive_arm": {"strategy_id": int(arm_id) if arm_id else None,
                        "return": ret(arm_entry) if arm_entry else None,
                        "pnl": _f(arm_entry["pnl"], 4) if arm_entry else None,
                        "value": _f(arm_entry["value"], 2) if arm_entry else None},
        "book": {"pnl": _f(book["pnl"], 4), "distributions": _f(book["distributions"], 4),
                 "value": _f(book["value"], 2), "return": ret(book)},
        "replay": {"pnl": _f(replay["pnl"], 4), "value": _f(replay["value"], 2), "return": ret(replay)},
        "paper_only": True,
    }


def record_daily_pnl(db: Session, account: Any, through: date) -> dict:
    done = {(row.payload or {}).get("session") for row in _rows(db, PNL_ACTION)}
    rows = tranche_rows(db)
    if not rows:
        return {"recorded": 0, "reason": "the book has not traded yet"}
    first = min(date.fromisoformat((row.payload or {})["executed_on"]) for row in rows)
    sessions, day = [], through
    for _ in range(BACKFILL_SESSIONS):
        if day < first:
            break
        if day.isoformat() not in done:
            sessions.append(day)
        day = previous_session(day)
    recorded, missing = [], []
    for day in sorted(sessions):
        result = daily_pnl(db, account, day)
        if result is None:
            missing.append(day.isoformat())
            continue
        write_audit_log(db, event_type=EVENT, entity_type="target_weight_book", action=PNL_ACTION, status="recorded",
                        message=f"Sleeve daily P&L for {day} (book return {result['book']['return']}, "
                                f"replay {result['replay']['return']})", payload=result)
        recorded.append(day.isoformat())
    db.flush()
    return {"recorded": recorded, "missing_closes": missing}


def record_tranche_checks(db: Session, account: Any, through: date) -> dict:
    """Post-tranche check: each holding within the weight tolerance of its target, valued at the close."""
    checked = {(row.payload or {}).get("rebalance_audit_id") for row in _rows(db, CHECK_ACTION)}
    rows = tranche_rows(db)
    tolerance = _d(settings.target_weight_weight_tolerance)
    symbols = sorted(book_symbols(db))
    fills = _fills_by_symbol(db, account.id, symbols)
    out = []
    for row in rows:
        payload = row.payload or {}
        day = date.fromisoformat(payload["executed_on"])
        if row.id in checked or day > through:
            continue
        _start, end = _session_window(day)
        target = book_target(tranche_states(rows, through=day))
        equity = _d(account.equity)
        gaps, missing = {}, []
        for symbol in sorted(set(target) | {s for s in symbols if fills.get(s)}):
            # The day's consolidated close once imported; until then the newest
            # execution-feed bar (the check runs after that session's close).
            price = daily_close(db, symbol, day) or latest_price(db, symbol) \
                or _d((payload.get("prices") or {}).get(symbol)) or None
            if price is None:
                missing.append(symbol)
                continue
            actual = shares_before(fills.get(symbol, []), end)
            gap = (actual - target.get(symbol, ZERO)) * price / equity if equity > 0 else ZERO
            gaps[symbol] = {"target_shares": _s(target.get(symbol, ZERO)), "actual_shares": _s(actual),
                            "target_weight": _f(target.get(symbol, ZERO) * price / equity, 6) if equity > 0 else None,
                            "actual_weight": _f(actual * price / equity, 6) if equity > 0 else None,
                            "gap": _f(gap, 6)}
        worst = max((abs(_d(item["gap"])) for item in gaps.values()), default=ZERO)
        passed = not missing and worst <= tolerance
        result = {"rebalance_audit_id": row.id, "cycle": payload["cycle"], "tranche": payload["tranche"],
                  "session": day.isoformat(), "tolerance": _f(tolerance), "worst_gap": _f(worst, 6),
                  "holdings": gaps, "missing_prices": missing, "passed": passed, "paper_only": True}
        write_audit_log(db, event_type=EVENT, entity_type="target_weight_tranche", entity_id=int(payload["tranche"]),
                        action=CHECK_ACTION, status="passed" if passed else "failed",
                        message=(f"Tranche {payload['tranche']} of {payload['cycle']}: worst holding gap "
                                 f"{float(worst):.2%} of equity (limit {float(tolerance):.0%})"),
                        payload=result)
        out.append(result)
    db.flush()
    return {"checked": len(out), "failed": [item["tranche"] for item in out if not item["passed"]]}


def benchmark_fills(db: Session, account: Any, through: date, now: datetime, *, client: Any = None) -> dict:
    """Each book fill against the delayed SIP tape: its minute bar and the session's SIP close."""
    from app.services.feed_hygiene import containing_bar, regular_bars, sip_ready, slippage_bps

    done = {(row.payload or {}).get("session") for row in _rows(db, BENCHMARK_ACTION)}
    sessions, day = [], through
    for _ in range(5):
        if day.isoformat() not in done and sip_ready(day, now):
            sessions.append(day)
        day = previous_session(day)
    out = []
    for day in sorted(sessions):
        start, end = _session_window(day)
        fills = _book_fills(db, account.id, since=start, until=end)
        if not fills:
            continue
        symbols = sorted({fill.symbol for fill, _order in fills})
        bounds = session_bounds(day)
        try:
            if client is None:
                from app.integrations.alpaca_market_data import AlpacaBarsClient

                client = AlpacaBarsClient()
            bars = client.bars(symbols, _utc(bounds[0]), _utc(bounds[1]), feed="sip")
        except Exception as exc:
            out.append({"session": day.isoformat(), "status": "unavailable", "error": f"{exc.__class__.__name__}"})
            continue
        closes = {}
        for symbol in symbols:
            regular = regular_bars(bars.get(symbol, []), day)
            closes[symbol] = _f(regular[-1]["close"]) if regular else None
        records = []
        for fill, order in fills:
            bar = containing_bar(bars.get(fill.symbol, []), fill.filled_at)
            minute = _f((bar or {}).get("vwap") or (bar or {}).get("open")) if bar else None
            records.append({"fill_id": fill.id, "order_id": order.id, "symbol": fill.symbol, "side": fill.side,
                            "quantity": _f(fill.quantity, 9), "fill_price": _f(fill.price),
                            "limit_price": _f(order.limit_price), "filled_at": _utc(fill.filled_at).isoformat(),
                            "sip_minute_price": minute, "sip_close": closes.get(fill.symbol),
                            "minute_bps": slippage_bps(_f(fill.price), minute, fill.side),
                            "close_bps": slippage_bps(_f(fill.price), closes.get(fill.symbol), fill.side)})
        notional = sum(r["quantity"] * r["fill_price"] for r in records if r["close_bps"] is not None) or 0
        weighted = (sum(r["close_bps"] * r["quantity"] * r["fill_price"] for r in records if r["close_bps"] is not None)
                    / notional) if notional else None
        payload = {"session": day.isoformat(), "feed": "sip", "delay_minutes": 15, "sip_close": closes,
                   "fills": records, "notional_weighted_close_bps": _f(weighted, 3), "paper_only": True}
        write_audit_log(db, event_type=EVENT, entity_type="target_weight_book", action=BENCHMARK_ACTION,
                        status="recorded", message=f"{len(records)} book fills on {day} benchmarked against delayed SIP",
                        payload=payload)
        out.append({"session": day.isoformat(), "status": "recorded", "fills": len(records)})
    db.flush()
    return {"sessions": out}


# ------------------------------------------------------------ verdicts

def _month_turnover(db: Session, account: Any, cycle: str, rows: list[AuditLog]) -> dict:
    year, month = (int(part) for part in cycle.split("-"))
    start = datetime(year, month, 1, tzinfo=ET).astimezone(UTC)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=ET).astimezone(UTC)
    traded = sum((_d(f.quantity) * _d(f.price) for f, _o in _book_fills(db, account.id, since=start, until=end)), ZERO)
    funding = sum((_d((row.payload or {}).get("tranche_capital")) for row in rows
                   if (row.payload or {}).get("cycle") == cycle and (row.payload or {}).get("funding")), ZERO)
    average = _average_equity(db, account, start)
    one_sided = traded / 2 / average if average > 0 else ZERO
    ex_funding = max(ZERO, (traded - funding) / 2 / average) if average > 0 else ZERO
    return {"one_sided": _f(one_sided, 4), "one_sided_ex_funding": _f(ex_funding, 4),
            "annualized_ex_funding": _f(ex_funding * 12, 4), "funding_notional": _f(funding, 2)}


def evaluate_cycle(db: Session, account: Any, cycle: str, *, now: datetime) -> dict:
    rows = tranche_rows(db)
    cycle_rows = [row for row in rows if (row.payload or {}).get("cycle") == cycle]
    state = engine_state(db)
    clock_start = state["clock_start_audit_id"]
    tranches = {int((row.payload or {})["tranche"]) for row in cycle_rows}
    complete = tranches == set(TRANCHES) and all(row.id > clock_start for row in cycle_rows)
    checks = {(row.payload or {}).get("rebalance_audit_id"): row.payload for row in _rows(db, CHECK_ACTION)}
    weight_failures = [int((row.payload or {})["tranche"]) for row in cycle_rows
                       if not (checks.get(row.id) or {}).get("passed")]
    pnl_rows = [row.payload for row in _rows(db, PNL_ACTION)
                if (row.payload or {}).get("session", "").startswith(cycle)]
    paper = replay = Decimal("1")
    days = 0
    for payload in pnl_rows:
        book_r, replay_r = (payload.get("book") or {}).get("return"), (payload.get("replay") or {}).get("return")
        if book_r is None and replay_r is None:
            continue
        paper *= 1 + _d(book_r)
        replay *= 1 + _d(replay_r)
        days += 1
    tracking_bps = (paper - replay) * 10000
    tolerance_bps = _d(settings.target_weight_tracking_tolerance_bps)
    tracking_ok = days > 0 and abs(tracking_bps) <= tolerance_bps
    usage = _month_turnover(db, account, cycle, rows)
    trailing = turnover(db, account, now)
    turnover_ok = usage["annualized_ex_funding"] <= settings.target_weight_turnover_soft and \
        trailing["trailing_12m"] < settings.target_weight_turnover_hard
    reasons = []
    if weight_failures:
        reasons.append(f"post-tranche weights off target by more than {settings.target_weight_weight_tolerance:.0%} "
                       f"of equity in tranche(s) {sorted(weight_failures)}")
    if not tracking_ok:
        reasons.append(f"paper return {float(paper - 1):.4%} vs same-weights replay {float(replay - 1):.4%} "
                       f"({float(tracking_bps):+.1f} bps, limit {float(tolerance_bps):.0f})" if days else
                       "no daily P&L recorded for the month")
    if not turnover_ok:
        reasons.append(f"turnover outside the budget ({usage['annualized_ex_funding']}x a year ex-funding, "
                       f"{trailing['trailing_12m']}x trailing 12 months)")
    passed = not reasons
    prior = [row.payload for row in _rows(db, VERDICT_ACTION) if row.id > clock_start]
    clock = sum(1 for payload in prior if payload.get("counts_toward_clock") and payload.get("passed"))
    counts = complete and passed
    result = {"cycle": cycle, "complete": complete, "tranches": sorted(tranches), "passed": passed,
              "counts_toward_clock": counts, "clock_cycles_passed": clock + (1 if counts else 0),
              "pass_cycles_required": settings.target_weight_pass_cycles,
              "pass_rule_met": clock + (1 if counts else 0) >= settings.target_weight_pass_cycles,
              "weights": {"failures": sorted(weight_failures), "tolerance": settings.target_weight_weight_tolerance},
              "tracking": {"paper_return": _f(paper - 1, 8), "replay_return": _f(replay - 1, 8),
                           "difference_bps": _f(tracking_bps, 3), "tolerance_bps": float(tolerance_bps), "days": days},
              "turnover": usage | {"trailing_12m": trailing["trailing_12m"]}, "reasons": reasons,
              "clock_start_audit_id": clock_start, "paper_only": True}
    write_audit_log(db, event_type=EVENT, entity_type="target_weight_book", action=VERDICT_ACTION,
                    status="passed" if passed else "failed",
                    message=(f"Cycle {cycle}: " + ("passed" if passed else "failed: " + "; ".join(reasons))),
                    payload=result)
    if not passed:
        result["pause"] = pause_engine(db, reasons=reasons, cycle=cycle)
    db.flush()
    return result


def evaluate_cycles(db: Session, account: Any, through: date, *, now: datetime) -> list[dict]:
    """Judge each month once its last session's P&L is booked (or five sessions later, whatever is missing)."""
    done = {(row.payload or {}).get("cycle") for row in _rows(db, VERDICT_ACTION)}
    cycles = sorted({(row.payload or {}).get("cycle") for row in tranche_rows(db)} - done)
    booked = {(row.payload or {}).get("session") for row in _rows(db, PNL_ACTION)}
    out = []
    for cycle in cycles:
        year, month = (int(part) for part in cycle.split("-"))
        last = last_session_of_month(year, month)
        if through < last:
            continue
        grace = last
        for _ in range(5):
            grace = next_session(grace)
        if last.isoformat() not in booked and through < grace:
            continue
        out.append(evaluate_cycle(db, account, cycle, now=now))
    return out


def run_daily(db: Session, *, now: Optional[datetime] = None, dividend_fetcher=None, bars_client=None) -> dict:
    """After the close: fill benchmarks, modeled distributions, daily P&L, tranche checks, cycle verdicts."""
    from app.services.stock_paper_ledger import active_paper_account

    now = _utc(now or datetime.now(UTC))
    base = {"job": "target_weight_daily_job", "paper_only": True, "live_trading": False}
    account = active_paper_account(db)
    if account is None:
        return base | {"status": "skipped", "reason": "stock paper account is not initialized"}
    session = last_closed_session(now)
    if session is None:
        return base | {"status": "skipped", "reason": "no closed session"}
    result = base | {"session": session.isoformat()}
    steps = (("benchmarks", lambda: benchmark_fills(db, account, session, now, client=bars_client)),
             ("distributions", lambda: accrue_distributions(db, account, session, fetcher=dividend_fetcher)),
             ("pnl", lambda: record_daily_pnl(db, account, session)),
             ("checks", lambda: record_tranche_checks(db, account, session)),
             ("verdicts", lambda: evaluate_cycles(db, account, session, now=now)))
    for name, step in steps:
        try:
            with db.begin_nested():
                result[name] = step()
        except Exception as exc:  # each step stands alone; the next run retries
            logger.warning("target-weight daily step %s failed: %s", name, exc)
            result[name] = {"status": "failed", "error": f"{exc.__class__.__name__}: {exc}"[:300]}
    db.commit()
    return result | {"status": "complete"}


# ---------------------------------------------------------------- viewer

def book_view(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Read-only: settings, sleeves, targets, holdings, schedule, turnover, cycle results."""
    from app.services.stock_paper_ledger import active_paper_account

    now = _utc(now or datetime.now(UTC))
    today = now.astimezone(ET).date()
    account = active_paper_account(db)
    rows = tranche_rows(db)
    states = tranche_states(rows)
    target = book_target(states)
    arm = passive_arm_row(db)
    split = attribution(states, fallback_sleeve=arm.id if arm else None)
    symbols = sorted(book_symbols(db))
    positions = {row.symbol: row for row in db.query(StockPaperPosition).filter(
        StockPaperPosition.account_id == (account.id if account else -1), StockPaperPosition.symbol.in_(symbols)).all()}
    equity = _d(account.equity) if account else ZERO
    holdings = {}
    for symbol in symbols:
        position = positions.get(symbol)
        value = _d(position.market_value) if position else ZERO
        price = _d(position.current_price) if position and position.current_price else (latest_price(db, symbol) or ZERO)
        holdings[symbol] = {
            "shares": _s(_d(position.quantity)) if position else "0", "market_value": _f(value, 2),
            "weight": _f(value / equity, 6) if equity > 0 else None,
            "target_shares": _s(target.get(symbol, ZERO)),
            "target_weight": _f(target.get(symbol, ZERO) * price / equity, 6) if equity > 0 and price else None,
            "sleeves": {sid: _f(share, 6) for sid, share in (split.get(symbol) or {}).items()},
        }
    sleeves = [{"id": row.id, "name": row.name, "strategy_type": row.strategy_type, "status": row.current_status,
                "sleeve_cadence": (row.parameters or {}).get("sleeve_cadence"),
                "target_weights": {sym: _f(w) for sym, w in sleeve_weights(row).items()},
                "capital_fraction": (row.parameters or {}).get("capital_fraction")} for row in sleeve_rows(db)]

    def recent(action, limit):
        return [(row.payload or {}) | {"audit_id": row.id, "status": row.status}
                for row in db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == action)
                .order_by(AuditLog.id.desc()).limit(limit).all()]

    capital = None
    if account is not None:
        prices = {symbol: (latest_price(db, symbol) or _d(getattr(positions.get(symbol), "current_price", None)))
                  for symbol in symbols}
        capital = {key: _f(value, 2) for key, value in capital_plan(db, account, symbols,
                                                                    {k: v for k, v in prices.items() if v}).items()}
    verdicts = recent(VERDICT_ACTION, 24)
    state = engine_state(db)
    clock = [v for v in verdicts if v["audit_id"] > state["clock_start_audit_id"]]
    return {
        "paper_only": True,
        "engine": state | {"exemption_active": exemption_active(db)},
        "settings": {
            "target_weight_book_enabled": settings.target_weight_book_enabled,
            "passive_arm_equity_weight": settings.passive_arm_equity_weight,
            "passive_arm_equity_symbol": settings.passive_arm_equity_symbol,
            "passive_arm_tbill_symbol": settings.passive_arm_tbill_symbol,
            "rebalance_band": settings.target_weight_rebalance_band,
            "band_widen_factor": settings.target_weight_band_widen_factor,
            "turnover_soft": settings.target_weight_turnover_soft,
            "turnover_hard": settings.target_weight_turnover_hard,
            "slot_reserve": settings.target_weight_slot_reserve,
            "cash_buffer": settings.target_weight_cash_buffer,
            "rebalance_weekday": settings.target_weight_rebalance_weekday,
            "weight_tolerance": settings.target_weight_weight_tolerance,
            "tracking_tolerance_bps": settings.target_weight_tracking_tolerance_bps,
            "pass_cycles": settings.target_weight_pass_cycles,
        },
        "sleeves": sleeves,
        "capital": capital,
        "tranches": {str(k): {"cycle": st.get("cycle"), "executed_on": st.get("executed_on"),
                              "tranche_capital": st.get("tranche_capital"), "sleeves": st.get("sleeves"),
                              "funding": st.get("funding"), "band": st.get("band")} for k, st in sorted(states.items())},
        "targets": {sym: _s(qty) for sym, qty in target.items()},
        "holdings": holdings,
        "schedule": schedule_view(today),
        "turnover": _turnover_view(db, account, now) if account else None,
        "cycles": {"verdicts": verdicts, "clock_cycles_passed": sum(1 for v in clock if v.get("counts_toward_clock")
                                                                     and v.get("passed")),
                   "pass_cycles_required": settings.target_weight_pass_cycles},
        "tranche_checks": recent(CHECK_ACTION, 8),
        "daily_pnl": recent(PNL_ACTION, 30),
        "distributions": recent(DISTRIBUTION_ACTION, 12),
        "fill_benchmarks": recent(BENCHMARK_ACTION, 10),
        "recent_orders": recent(ORDERS_ACTION, 8),
    }
