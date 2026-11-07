"""Append-only attribution of every closed paper trade.

Two sources are attributed:

* ``trial_lot`` - each fully closed forward-trial lot (governed ML trials).
* ``scheduled_round_trip`` / ``exploration_round_trip`` - each closed
  autotrader round trip rebuilt by the paper-trade scorecard, so losses can
  be tied to the indicator strategy that opened them.

Each record explains the trade: signal quality at entry, slippage against
the decision (reserved reference) price, holding time against the exit rule,
point-in-time regime tags (trend / volatility bucket from daily closes
strictly before the entry date), the exit cause and the net return.

Safety contract:
* Records are hindsight evaluation.  They feed the nightly research agenda,
  meta-label mode reviews and paper allocation weights, all of which act only
  on decisions dated after the outcome was observed.
* Unknown broker costs stay unknown (``net_return`` is NULL); they are never
  treated as zero.
* Rows are immutable.  A trade whose evidence changes (costs become known,
  a late fill) is recorded as a new revision of the same ``source_key``.

Round trips also carry a ``diagnosis`` (``loss_diagnosis``): the cause of the
result measured against SPY and the sector ETF over the same hold, earnings
inside the hold, crowding, clustered stops and whether the exits were the
backtested ones.

``slippage`` also carries the consolidated benchmark (``feed_hygiene``): the
existing ``entry_bps`` / ``exit_bps`` are measured against the decision price,
which for entries is the latest IEX minute close (``reference_feed: "iex"``);
``sip_reference`` holds each side's delayed-SIP reference (the official SIP
open for an entry at the open, else the SIP minute bar containing the fill,
quantity-weighted over the side's fills) and ``sip_bps`` the slippage against
it.  ``entry_time_in_force``, ``paper`` and ``auction`` tag the entry order;
a paper ``opg`` / ``cls`` fill only imitates the auction.  Both are ``None``
until the after-close SIP job has referenced the fills.

Every closed trade (trial lots and round trips) also records what the price
did *after* the exit, up to the planned horizon (``max_holding_sessions``
from the entry session): ``post_exit`` holds the return from the exit price
to the horizon close, the best and worst excursion over that span (``mfe`` /
``mae``), the number of daily bars seen and whether the horizon bar was among
them.  ``excursion`` holds the best and worst excursion over the hold itself,
from the entry price.  Both come from stored daily bars.  A trade whose
horizon has not passed yet records ``None`` and is filled by a later revision
once the bars exist, so a stop that was followed by a recovery
(``premature_stop``) or a time stop that closed a trade past its target
(``held_too_long``) can be diagnosed from evidence, not guessed.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import MarketPrice, StockPaperTrial, StockPaperTrialDecision, Strategy, StrategySignal
from app.models.loss_feedback import StockTradeAttribution
from app.models.stock_paper import StockPaperOrder, StockPaperTrialLot

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
ATTRIBUTION_VERSION = "trade-attribution-v1"
EXIT_CAUSES = ("indicator", "time_stop", "catastrophe_stop", "protection_lock", "take_profit", "operator", "other")
SOURCES = ("trial_lot", "scheduled_round_trip", "exploration_round_trip")

_TIME_REASONS = {"horizon", "trial_window_complete", "paper_session_expired", "time_stop", "max_holding"}
_STOP_REASONS = {"stop", "stop_loss_leg", "protective_stop", "catastrophe_stop"}
_PROTECTION_MARKERS = ("loss", "drawdown", "kill", "protect", "halt", "recovery", "lock", "pause")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _round(value: Optional[float], digits: int = 8) -> Optional[float]:
    return None if value is None else round(value, digits)


# ----------------------------------------------------------------- pure helpers

def exit_cause(raw_reason: Optional[str], *, held_sessions: Optional[int] = None,
               max_holding_sessions: Optional[int] = None) -> str:
    """Map a recorded exit reason onto the attribution exit-cause vocabulary."""
    reason = str(raw_reason or "").strip().lower()
    if reason in _STOP_REASONS:
        return "catastrophe_stop"
    if reason in _TIME_REASONS:
        return "time_stop"
    if reason in {"take_profit_leg", "take_profit"}:
        return "take_profit"
    if reason in {"sell_signal", "indicator"}:
        # A strategy SELL and an exit-policy indicator exit share one path;
        # a lot held to its declared session cap was closed by the time stop.
        if max_holding_sessions and held_sessions is not None and held_sessions >= max_holding_sessions:
            return "time_stop"
        return "indicator"
    if reason in {"operator_stop", "manual"}:
        return "operator"
    if any(marker in reason for marker in _PROTECTION_MARKERS):
        return "protection_lock"
    return "other"


def held_sessions(opened_at: datetime, closed_at: datetime) -> int:
    """NYSE regular sessions after the entry session up to the exit session."""
    from app.services.exchange_sessions import is_nyse_session
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    start = _utc(opened_at).astimezone(et).date()
    end = _utc(closed_at).astimezone(et).date()
    count, day = 0, start
    while day < end and count < 400:
        day += timedelta(days=1)
        if is_nyse_session(day):
            count += 1
    return count


def session_date(moment: datetime) -> date:
    """The New York session date a moment falls in."""
    return _utc(moment).astimezone(ET).date()


def sessions_after(day: date, count: int) -> date:
    """The NYSE session ``count`` sessions after ``day`` (``day`` itself when ``count`` <= 0)."""
    from app.services.exchange_sessions import is_nyse_session

    current, found, scanned = day, 0, 0
    while found < count and scanned < 400:
        current += timedelta(days=1)
        scanned += 1
        if is_nyse_session(current):
            found += 1
    return current


def sessions_between(start: date, end: date) -> int:
    """NYSE sessions in ``(start, end]``."""
    from app.services.exchange_sessions import is_nyse_session

    count, day = 0, start
    while day < end and count < 400:
        day += timedelta(days=1)
        if is_nyse_session(day):
            count += 1
    return count


def excursion(bars: Iterable[dict], reference_price: float) -> Optional[dict]:
    """Best (``mfe`` >= 0) and worst (``mae`` <= 0) excursion of daily ``bars`` from ``reference_price``.

    Each bar has ``close`` and optional ``high`` / ``low`` (the close stands
    in for a missing side).  ``None`` without bars or a usable reference.
    """
    rows = [bar for bar in bars if _f(bar.get("close"))]
    if not rows or not reference_price or reference_price <= 0:
        return None
    highs = [_f(bar.get("high")) or _f(bar["close"]) for bar in rows]
    lows = [_f(bar.get("low")) or _f(bar["close"]) for bar in rows]
    return {"mfe": _round(max(0.0, max(highs) / reference_price - 1)),
            "mae": _round(min(0.0, min(lows) / reference_price - 1)), "bars": len(rows)}


def post_exit_path(bars: Iterable[dict], exit_price: float, *, span_sessions: int,
                   horizon_date: date) -> Optional[dict]:
    """What the price did from the exit to the planned horizon.

    ``bars`` are the daily bars strictly after the exit session, up to and
    including the horizon session, ascending (``price_date``, ``close``,
    optional ``high`` / ``low``).  ``span_sessions`` is how many sessions
    that span holds; a trade closed at or beyond its horizon has an empty
    span and a zero path.  Returns ``None`` when there is nothing to measure
    yet (no bars), so a later revision can fill it in; ``horizon_reached``
    is False while the bars stop short of the horizon session.
    """
    if not exit_price or exit_price <= 0:
        return None
    horizon = horizon_date.isoformat()
    if span_sessions <= 0:
        return {"return": 0.0, "mfe": 0.0, "mae": 0.0, "bars": 0, "horizon_reached": True,
                "sessions": 0, "horizon_date": horizon, "last_bar": None}
    rows = [bar for bar in bars if _f(bar.get("close")) and bar.get("price_date") is not None
            and bar["price_date"] <= horizon_date]
    if not rows:
        return None
    rows.sort(key=lambda bar: bar["price_date"])
    path = excursion(rows, exit_price)
    last = rows[-1]
    return {"return": _round(_f(last["close"]) / exit_price - 1), "mfe": path["mfe"], "mae": path["mae"],
            "bars": len(rows), "horizon_reached": last["price_date"] >= horizon_date,
            "sessions": span_sessions, "horizon_date": horizon, "last_bar": last["price_date"].isoformat()}


def holding_relation(sessions: Optional[int], rule_sessions: Optional[int]) -> str:
    if not rule_sessions:
        return "no_time_rule"
    if sessions is None:
        return "unknown"
    if sessions < rule_sessions:
        return "before_rule"
    return "at_rule" if sessions == rule_sessions else "beyond_rule"


def slippage_bps(fill_price: Optional[float], reference: Optional[float], side: str) -> Optional[float]:
    """Adverse slippage in basis points (positive = worse than the decision price)."""
    if fill_price is None or not reference or reference <= 0:
        return None
    raw = (fill_price - reference) / reference * 10_000
    return round(raw if side == "buy" else -raw, 4)


def sip_slippage(references: dict, *, entry_order: Any, exit_order_ids: Iterable[Optional[int]],
                 entry_price: Optional[float], exit_price: Optional[float]) -> dict:
    """The delayed-SIP benchmark of one trade's entry and exit, next to the IEX decision-price numbers."""
    from app.services import feed_hygiene

    entry_id = getattr(entry_order, "id", None)
    exit_ids = [order_id for order_id in exit_order_ids if order_id]
    by_order = feed_hygiene.order_sip_references(references, [entry_id, *exit_ids]) if references else {}
    entry = by_order.get(entry_id)
    exits = [by_order[order_id] for order_id in exit_ids if order_id in by_order]
    exit_ref = None
    if exits:
        quantity = sum(item["quantity"] for item in exits)
        exit_ref = {"price": _round(sum(item["price"] * item["quantity"] for item in exits) / quantity, 6),
                    "fills": sum(item["fills"] for item in exits), "quantity": _round(quantity, 9),
                    "kinds": sorted({kind for item in exits for kind in item["kinds"]}),
                    "orders": [order_id for order_id in exit_ids if order_id in by_order], "feed": "sip"}
    tif = getattr(entry_order, "time_in_force", None)
    tif = str(tif).lower() if tif else None
    return {
        "reference_feed": "iex",
        "sip_reference": {"entry": entry, "exit": exit_ref},
        "sip_bps": {"entry": slippage_bps(entry_price, entry["price"], "buy") if entry else None,
                    "exit": slippage_bps(exit_price, exit_ref["price"], "sell") if exit_ref else None},
        "entry_time_in_force": tif, "paper": True,
        "auction": tif in feed_hygiene.AUCTION_TIFS,
    }


def _sip_references(db: Session, since: Optional[datetime]) -> dict:
    from app.services import feed_hygiene

    try:
        return feed_hygiene.sip_references(db, since=since)
    except Exception:  # evidence only; the attribution must not fail for want of a benchmark
        db.rollback()
        return {}


def signal_quality(*, probability: Optional[float] = None, threshold: Optional[float] = None,
                   confidence: Optional[float] = None) -> dict:
    """Bucket the entry signal: strong / moderate / marginal / unknown."""
    margin = None
    if probability is not None:
        margin = probability - (threshold if threshold is not None else 0.5)
    if margin is not None:
        bucket = "strong" if margin >= 0.10 else "moderate" if margin >= 0.03 else "marginal"
    elif confidence is not None:
        bucket = "strong" if confidence >= 0.70 else "moderate" if confidence >= 0.55 else "marginal"
    else:
        bucket = "unknown"
    return {"probability": _round(probability, 6), "threshold": threshold, "confidence": _round(confidence, 6),
            "margin": _round(margin, 6), "bucket": bucket}


def regime_tags(closes: list[float]) -> dict:
    """Trend and volatility bucket from daily closes strictly before entry."""
    closes = [value for value in closes if value and value > 0]
    if len(closes) < 21:
        return {"trend": "unknown", "volatility": "unknown", "annualized_volatility": None, "bars": len(closes)}
    window = 50 if len(closes) >= 50 else 20
    sma = sum(closes[-window:]) / window
    trend = "up" if closes[-1] >= sma else "down"
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 20, len(closes))]
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / max(len(returns) - 1, 1)
    vol = math.sqrt(variance) * math.sqrt(252)
    bucket = "low" if vol < 0.15 else "normal" if vol < 0.30 else "high"
    return {"trend": trend, "trend_sma_window": window, "volatility": bucket,
            "annualized_volatility": round(vol, 6), "bars": len(closes)}


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# ----------------------------------------------------------------- data access

def _closes_before(db: Session, symbol: str, day: date, *, limit: int = 60) -> list[float]:
    rows = db.execute(
        select(MarketPrice.price_date, MarketPrice.close, MarketPrice.adjusted_close)
        .where(MarketPrice.symbol == symbol, MarketPrice.price_date < day)
        .order_by(MarketPrice.price_date.desc(), MarketPrice.id)
        .limit(limit * 3)
    ).all()
    by_day: dict[date, float] = {}
    for price_date, close, adjusted in rows:
        value = _f(adjusted) or _f(close)
        if value and price_date not in by_day:
            by_day[price_date] = value
    return [by_day[key] for key in sorted(by_day)][-limit:]


def _daily_bars(db: Session, symbol: str, start: date, end: date) -> list[dict]:
    """One raw daily bar per session in ``[start, end]`` (the lowest id wins a duplicate date)."""
    if end < start:
        return []
    rows = db.execute(
        select(MarketPrice.price_date, MarketPrice.high, MarketPrice.low, MarketPrice.close)
        .where(MarketPrice.symbol == symbol, MarketPrice.price_date >= start, MarketPrice.price_date <= end)
        .order_by(MarketPrice.price_date, MarketPrice.id)
    ).all()
    seen: dict[date, dict] = {}
    for price_date, high, low, close in rows:
        if price_date not in seen and _f(close):
            seen[price_date] = {"price_date": price_date, "high": _f(high), "low": _f(low), "close": _f(close)}
    return [seen[key] for key in sorted(seen)]


def post_exit_from_daily_bars(db: Session, symbol: str, *, entry_session: date, exit_session: date,
                              max_hold: Optional[int], exit_price: float, now: datetime) -> Optional[dict]:
    """``post_exit`` for a closed trade from stored daily bars; ``None`` until the horizon has passed.

    The horizon is ``max_hold`` sessions after the entry session.  The span
    runs over the sessions after the exit session up to the horizon; it is
    measured only once the horizon session is over (its bar is complete).
    """
    if not max_hold or max_hold <= 0 or not exit_price or exit_price <= 0:
        return None
    horizon = sessions_after(entry_session, int(max_hold))
    span = sessions_between(exit_session, horizon)
    if span <= 0:
        return post_exit_path([], exit_price, span_sessions=0, horizon_date=horizon)
    if horizon >= session_date(now):
        return None  # the horizon bar is not complete yet; a later revision fills this in
    bars = _daily_bars(db, symbol, exit_session + timedelta(days=1), horizon)
    return post_exit_path(bars, exit_price, span_sessions=span, horizon_date=horizon)


def hold_excursion_from_daily_bars(db: Session, symbol: str, *, entry_session: date, exit_session: date,
                                   entry_price: float) -> Optional[dict]:
    """Best and worst excursion over the hold from the entry price, from daily bars (entry and exit sessions included)."""
    path = excursion(_daily_bars(db, symbol, entry_session, exit_session), entry_price)
    return path | {"source": "daily_bars"} if path else None


def _regime(db: Session, symbol: str, opened_at: datetime, cache: dict) -> dict:
    from zoneinfo import ZoneInfo

    day = _utc(opened_at).astimezone(ZoneInfo("America/New_York")).date()
    key = (symbol, day)
    if key not in cache:
        cache[key] = regime_tags(_closes_before(db, symbol, day)) | {"as_of_before": day.isoformat()}
    return cache[key]


def _persist(db: Session, *, source: str, source_key: str, record: dict) -> Optional[StockTradeAttribution]:
    """Insert a new revision only when the attribution content changed."""
    sha = digest(record)
    latest = db.scalar(select(StockTradeAttribution).where(StockTradeAttribution.source_key == source_key)
                       .order_by(StockTradeAttribution.revision.desc()))
    if latest is not None and latest.attribution_sha256 == sha:
        return None
    returns = record["returns"]
    row = StockTradeAttribution(
        source=source, source_key=source_key, revision=(latest.revision + 1) if latest else 1,
        attribution_sha256=sha, strategy_id=record.get("strategy_id"), strategy_type=record["strategy_type"],
        trial_id=record.get("trial_id"), symbol=record["symbol"],
        opened_at=datetime.fromisoformat(record["opened_at"]), closed_at=datetime.fromisoformat(record["closed_at"]),
        exit_cause=record["exit"]["cause"],
        net_return=Decimal(str(returns["net_return"])) if returns["net_return"] is not None else None,
        gross_return=Decimal(str(returns["gross_return"])),
        net_pnl=Decimal(str(returns["net_pnl"])) if returns["net_pnl"] is not None else None,
        costs_known=bool(returns["costs_known"]), is_loss=bool(returns["is_loss"]),
        detail=record, paper_only=True,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None  # a concurrent writer recorded the same revision
    return row


def _record(**values) -> dict:
    gross_return = values.pop("gross_return")
    net_return = values.pop("net_return")
    net_pnl = values.pop("net_pnl")
    costs_known = values.pop("costs_known")
    effective = net_return if net_return is not None else gross_return
    return {
        "version": ATTRIBUTION_VERSION,
        **values,
        "returns": {
            "gross_return": _round(gross_return), "net_return": _round(net_return),
            "net_pnl": _round(net_pnl), "fees": values.get("fees"), "costs_known": costs_known,
            "is_loss": effective <= 0,
        },
        "paper_only": True,
        "hindsight_only": True,
    }


# ----------------------------------------------------------------- trial lots

def attribute_trial(db: Session, trial: StockPaperTrial, *, regime_cache: Optional[dict] = None,
                    now: Optional[datetime] = None, sip_references: Optional[dict] = None) -> list[StockTradeAttribution]:
    """Attribute every fully closed lot of one forward trial (idempotent)."""
    from app.services.trade_mistakes import classify_trade, trial_lot_trades

    cache = regime_cache if regime_cache is not None else {}
    references = sip_references if sip_references is not None else _sip_references(db, None)
    strategy = db.get(Strategy, trial.strategy_id) if trial.strategy_id else None
    strategy_type = strategy.strategy_type if strategy else "stock_forward_trial"
    created = []
    for trade in trial_lot_trades(db, trial, now=now):
        lot = db.get(StockPaperTrialLot, trade["lot_id"])
        entry_order = db.get(StockPaperOrder, lot.entry_order_id) if lot and lot.entry_order_id else None
        decision = db.get(StockPaperTrialDecision, trade["entry_decision_id"])
        lineage = (decision.lineage if decision else None) or {}
        opened_at = _utc(datetime.fromisoformat(trade["entered_at"]))
        closed_at = _utc(datetime.fromisoformat(trade["exited_at"]))
        sessions = held_sessions(opened_at, closed_at)
        entry_price, exit_price = trade["entry_price"], trade["exit_price"]
        fees = trade["fees"]
        quantity = trade["quantity"]
        cost = entry_price * quantity
        gross_pnl = (exit_price - entry_price) * quantity
        net_pnl = gross_pnl - fees if fees is not None else None
        threshold = _f(lineage.get("threshold")) or _f((trial.policy or {}).get("probability_threshold")) or 0.5
        classification = trade.get("classification") or classify_trade(trade)
        record = _record(
            strategy_id=trial.strategy_id, strategy_type=strategy_type, trial_id=trial.id,
            symbol=trade["symbol"], opened_at=opened_at.isoformat(), closed_at=closed_at.isoformat(),
            quantity=quantity, fees=fees,
            signal_quality=signal_quality(probability=trade.get("probability"), threshold=threshold),
            slippage={
                "decision_price": _f(entry_order.limit_price) if entry_order else None,
                "entry_price": entry_price,
                "entry_bps": slippage_bps(entry_price, _f(entry_order.limit_price) if entry_order else None, "buy"),
                "exit_reference_price": _f(lot.exit_reference_price) if lot else None,
                "exit_price": exit_price,
                "exit_bps": slippage_bps(exit_price, _f(lot.exit_reference_price) if lot else None, "sell"),
                **sip_slippage(references, entry_order=entry_order,
                               exit_order_ids=[lot.exit_order_id] if lot else [],
                               entry_price=entry_price, exit_price=exit_price),
            },
            holding={"sessions": sessions, "minutes": round((closed_at - opened_at).total_seconds() / 60, 2),
                     "rule_sessions": trade.get("planned_horizon_sessions"),
                     "relation": holding_relation(sessions, trade.get("planned_horizon_sessions"))},
            regime=_regime(db, trade["symbol"], opened_at, cache),
            exit={"cause": exit_cause(trade.get("exit_reason")), "raw_reason": trade.get("exit_reason")},
            post_exit=trade.get("post_exit"),
            excursion=trade.get("excursion"),
            mistakes={"primary": classification.get("primary_mistake"), "all": classification.get("mistakes") or []},
            lineage={"lot_id": trade["lot_id"], "entry_decision_id": trade["entry_decision_id"],
                     "entry_order_id": lot.entry_order_id if lot else None},
            gross_return=gross_pnl / cost if cost > 0 else 0.0,
            net_return=(net_pnl / cost) if net_pnl is not None and cost > 0 else None,
            net_pnl=net_pnl, costs_known=fees is not None,
        )
        row = _persist(db, source="trial_lot", source_key=f"trial_lot:{trade['lot_id']}", record=record)
        if row is not None:
            created.append(row)
    return created


def attribute_trials(db: Session, *, since: Optional[datetime] = None,
                     now: Optional[datetime] = None) -> list[StockTradeAttribution]:
    statement = select(StockPaperTrial).order_by(StockPaperTrial.created_at)
    if since is not None:
        statement = statement.where(StockPaperTrial.created_at >= since - timedelta(days=60))
    cache: dict = {}
    created = []
    references = _sip_references(db, since)
    for trial in db.scalars(statement).all():
        created.extend(attribute_trial(db, trial, regime_cache=cache, now=now, sip_references=references))
    return created


# ----------------------------------------------------------------- autotrader round trips

def _sector_symbol(symbol: str) -> Optional[str]:
    from app.services.trading_universe import sector_group

    group = sector_group(symbol)
    return None if group in {"index", symbol} else group


def _earnings_lookup():
    from app.core.config import settings
    from app.services.earnings_history import runtime_surprises

    return runtime_surprises if settings.loss_diagnosis_earnings_lookup else None


def _diagnosis_context(lots: list, trades: dict, *, now: datetime) -> tuple[dict, dict]:
    """Per entry order id: other lots overlapping on its symbol; per ET date: stop exits."""
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    spans = defaultdict(list)
    for lot in lots:
        if lot.entered_at is None:
            continue
        end = max(item["filled_at"] for item in lot.exits) if lot.closed and lot.exits else now
        spans[lot.symbol].append((lot.entry_order.id, _utc(lot.entered_at), _utc(end)))
    overlaps = {}
    for members in spans.values():
        for order_id, start, end in members:
            overlaps[order_id] = sum(1 for other, s, e in members if other != order_id and s < end and start < e)
    stops = Counter()
    for trade in trades.values():
        if trade["exit_reason"] in _STOP_EXIT_REASONS:
            stops[_utc(datetime.fromisoformat(trade["exited_at"])).astimezone(et).date()] += 1
    return overlaps, stops


_STOP_EXIT_REASONS = {"stop_loss_leg", "protective_stop"}


def attribute_round_trips(db: Session, *, account_id: Optional[int] = None,
                          since: Optional[datetime] = None, now: Optional[datetime] = None,
                          earnings_lookup=...) -> list[StockTradeAttribution]:
    """Attribute closed scheduled / exploration round trips (idempotent)."""
    from zoneinfo import ZoneInfo

    from app.services import paper_trade_scorecard as scorecard
    from app.services import loss_diagnosis, market_events
    from app.services.strategy_execution_policy import exit_policy_for
    from app.services.trade_mistakes import classify_round_trip

    now = now or datetime.now(UTC)
    lookup = _earnings_lookup() if earnings_lookup is ... else earnings_lookup
    try:
        calendar = market_events.cached_events(db)
    except Exception:  # evidence only; an unreadable calendar is "unknown"
        db.rollback()
        calendar = None

    account = scorecard._account(db, account_id)
    if account is None:
        return []
    rebuilt = scorecard.reconstruct_round_trips(db, account.id)
    strategies = rebuilt["strategies"]
    orders = rebuilt["orders"]
    curve = scorecard._EquityCurve([])
    lots = [lot for lot in rebuilt["lots"] if lot.closed and lot.entry_order.id and lot.entry_order.id > 0
            and lot.source in {"scheduled", "exploration"}]
    signal_ids = {lot.entry_order.signal_id for lot in lots if lot.entry_order.signal_id}
    signals = {row.id: row for row in db.query(StrategySignal).filter(StrategySignal.id.in_(signal_ids)).all()} \
        if signal_ids else {}
    trades = {}
    for lot in lots:
        signal = signals.get(lot.entry_order.signal_id)
        trades[lot.entry_order.id] = scorecard._trade_row(lot, strategies, curve,
                                                          _f(signal.probability_up) if signal else None)
    overlaps, stops_by_day = _diagnosis_context(rebuilt["lots"], trades, now=now)
    prices = loss_diagnosis.PriceLookup(db)
    references = _sip_references(db, since)
    et = ZoneInfo("America/New_York")
    cache: dict = {}
    policies: dict = {}
    created = []
    for lot in lots:
        closed_at = max(item["filled_at"] for item in lot.exits)
        if since is not None and closed_at < since:
            continue
        signal = signals.get(lot.entry_order.signal_id)
        probability = _f(signal.probability_up) if signal else None
        trade = trades[lot.entry_order.id]
        strategy = strategies.get(lot.strategy_id)
        strategy_type = (strategy.strategy_type if strategy else None) or "unattributed"
        parameters = (strategy.parameters or {}) if strategy else {}
        if strategy is not None and lot.strategy_id not in policies:
            policies[lot.strategy_id] = exit_policy_for(strategy)
        policy = policies.get(lot.strategy_id)
        max_hold = int(parameters["max_holding_sessions"]) if _f(parameters.get("max_holding_sessions")) else \
            (policy.max_holding_sessions if policy is not None else None)
        opened_at = _utc(lot.entered_at)
        sessions = held_sessions(opened_at, closed_at)
        entry_session, exit_session = session_date(opened_at), session_date(closed_at)
        exit_ids = sorted({item["order_id"] for item in lot.exits if item["order_id"]})
        exit_refs = []
        for order_id in exit_ids:
            order = orders.get(order_id)
            if order is not None:
                exit_refs.append(_f(order.stop_price) if order.leg_role == "stop_loss" and order.stop_price
                                 else _f(order.limit_price))
        exit_refs = [value for value in exit_refs if value]
        exit_reference = sum(exit_refs) / len(exit_refs) if exit_refs else None
        decision_price = _f(lot.entry_order.limit_price)
        features = (signal.features or {}) if signal else {}
        meta = features.get("meta_label") if isinstance(features.get("meta_label"), dict) else None
        post_exit = post_exit_from_daily_bars(db, lot.symbol, entry_session=entry_session, exit_session=exit_session,
                                              max_hold=max_hold, exit_price=trade["exit_price"], now=now)
        hold_path = hold_excursion_from_daily_bars(db, lot.symbol, entry_session=entry_session,
                                                   exit_session=exit_session, entry_price=trade["entry_price"])
        cause = exit_cause(trade["exit_reason"], held_sessions=sessions, max_holding_sessions=max_hold)
        classification = classify_round_trip(
            db, trade, post_exit_return=post_exit["return"] if post_exit else None,
        ) if trade["net_pnl"] <= 0 else {"primary_mistake": None, "mistakes": []}
        costs_known = bool(trade["costs_known"])
        cost = float(lot.cost)
        gross_return = trade["gross_pnl"] / cost if cost > 0 else 0.0
        effective = trade["net_return"] if costs_known else gross_return
        news_used, news_sentiment = loss_diagnosis.news_from_features(features)
        market = features.get("market_context") if isinstance(features.get("market_context"), dict) else {}
        sector = _sector_symbol(lot.symbol)
        diagnosis = loss_diagnosis.diagnose(
            gross_return=gross_return, is_loss=effective <= 0,
            benchmark_return=prices.window_return("SPY", opened_at, closed_at),
            sector_symbol=sector, sector_return=prices.window_return(sector, opened_at, closed_at) if sector else None,
            earnings_in_hold=loss_diagnosis.earnings_between(lot.symbol, opened_at, closed_at, lookup),
            news_used=news_used, news_sentiment=news_sentiment,
            concurrent_same_symbol=overlaps.get(lot.entry_order.id, 0),
            same_session_stops=stops_by_day[closed_at.astimezone(et).date()]
            if trade["exit_reason"] in _STOP_EXIT_REASONS else 0,
            exit_tested=loss_diagnosis.exit_tested(
                policy, entry_price=decision_price or trade["entry_price"],
                stop_price=_f(lot.entry_order.stop_price), take_profit_price=_f(lot.entry_order.take_profit_price)),
            macro_events_in_hold=market_events.events_between(calendar, opened_at, closed_at),
            entry_vix=_f(market.get("vix")), entry_breadth=_f(market.get("breadth_above_200d")),
            exit_cause=cause, post_exit=post_exit, hold_mfe=hold_path["mfe"] if hold_path else None,
            take_profit_pct=_f(policy.take_profit_pct) if policy is not None else None,
        )
        record = _record(
            strategy_id=lot.strategy_id, strategy_type=strategy_type, trial_id=None,
            symbol=lot.symbol, opened_at=opened_at.isoformat(), closed_at=closed_at.isoformat(),
            quantity=trade["quantity"], fees=trade["fees"] if costs_known else None,
            signal_quality=signal_quality(confidence=_f(signal.confidence) if signal else None)
            | {"probability_up": _round(probability, 6)},
            slippage={
                "decision_price": decision_price, "entry_price": trade["entry_price"],
                "entry_bps": slippage_bps(trade["entry_price"], decision_price, "buy"),
                "exit_reference_price": _round(exit_reference, 6), "exit_price": trade["exit_price"],
                "exit_bps": slippage_bps(trade["exit_price"], exit_reference, "sell"),
                **sip_slippage(references, entry_order=lot.entry_order, exit_order_ids=exit_ids,
                               entry_price=trade["entry_price"], exit_price=trade["exit_price"]),
            },
            holding={"sessions": sessions, "minutes": trade["holding_minutes"], "rule_sessions": max_hold,
                     "relation": holding_relation(sessions, max_hold)},
            regime=_regime(db, lot.symbol, opened_at, cache),
            exit={"cause": cause, "raw_reason": trade["exit_reason"], "reasons_by_quantity": trade["exit_reasons"]},
            post_exit=post_exit,
            excursion=hold_path,
            meta_label=({"decision": meta.get("decision"), "probability": meta.get("probability"),
                         "mode": meta.get("mode"), "status": meta.get("status")} if meta else None),
            mistakes={"primary": classification.get("primary_mistake"), "all": classification.get("mistakes") or []},
            diagnosis=diagnosis,
            lineage={"entry_order_id": lot.entry_order.id, "exit_order_ids": exit_ids,
                     "signal_id": lot.entry_order.signal_id, "order_source": lot.source},
            gross_return=gross_return,
            net_return=trade["net_return"] if costs_known else None,
            net_pnl=trade["net_pnl"] if costs_known else None, costs_known=costs_known,
        )
        row = _persist(db, source=f"{lot.source}_round_trip", source_key=f"round_trip:{lot.entry_order.id}",
                       record=record)
        if row is not None:
            created.append(row)
    return created


def run_trade_attribution(db: Session, *, now: Optional[datetime] = None, lookback_days: int = 120) -> dict:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=lookback_days)
    trial_rows = attribute_trials(db, since=since, now=now)
    trip_rows = attribute_round_trips(db, since=since, now=now)
    return {"trial_lots_recorded": len(trial_rows), "round_trips_recorded": len(trip_rows),
            "coverage": attribution_coverage(db, since=since)}


def attribution_coverage(db: Session, *, account_id: Optional[int] = None,
                         since: Optional[datetime] = None) -> dict:
    """Closed scheduled / exploration round trips vs the ones attributed (a learning-health check)."""
    from app.services import paper_trade_scorecard as scorecard

    account = scorecard._account(db, account_id)
    if account is None:
        return {"closed_round_trips": 0, "attributed": 0, "missing_entry_order_ids": []}
    closed = [lot for lot in scorecard.reconstruct_round_trips(db, account.id)["lots"]
              if lot.closed and lot.entry_order.id and lot.entry_order.id > 0
              and lot.source in {"scheduled", "exploration"}
              and (since is None or max(item["filled_at"] for item in lot.exits) >= since)]
    keys = set(db.scalars(select(StockTradeAttribution.source_key).where(
        StockTradeAttribution.source_key.like("round_trip:%"))).all())
    missing = sorted(lot.entry_order.id for lot in closed if f"round_trip:{lot.entry_order.id}" not in keys)
    return {"closed_round_trips": len(closed), "attributed": len(closed) - len(missing),
            "missing_entry_order_ids": missing[:50]}


# ----------------------------------------------------------------- projections

def current_attributions(db: Session, *, since: Optional[datetime] = None,
                         sources: Optional[Iterable[str]] = None,
                         strategy_types: Optional[Iterable[str]] = None,
                         trial_ids: Optional[Iterable[str]] = None) -> list[StockTradeAttribution]:
    """Latest revision per trade, newest close first."""
    latest = (select(StockTradeAttribution.source_key, func.max(StockTradeAttribution.revision).label("revision"))
              .group_by(StockTradeAttribution.source_key).subquery())
    statement = select(StockTradeAttribution).join(
        latest, (StockTradeAttribution.source_key == latest.c.source_key)
        & (StockTradeAttribution.revision == latest.c.revision))
    if since is not None:
        statement = statement.where(StockTradeAttribution.closed_at >= since)
    if sources is not None:
        statement = statement.where(StockTradeAttribution.source.in_(list(sources)))
    if strategy_types is not None:
        statement = statement.where(StockTradeAttribution.strategy_type.in_(list(strategy_types)))
    if trial_ids is not None:
        statement = statement.where(StockTradeAttribution.trial_id.in_(list(trial_ids)))
    return list(db.scalars(statement.order_by(StockTradeAttribution.closed_at.desc(), StockTradeAttribution.id.desc())).all())


def _effective_return(row: StockTradeAttribution) -> float:
    return float(row.net_return if row.net_return is not None else row.gross_return)


def _breakdown(rows: list[StockTradeAttribution], key) -> list[dict]:
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    out = []
    for name, members in groups.items():
        losses = [row for row in members if row.is_loss]
        out.append({
            "key": name,
            "trades": len(members),
            "losses": len(losses),
            "loss_rate": round(len(losses) / len(members), 6),
            "sum_return": round(sum(_effective_return(row) for row in members), 8),
            "loss_return": round(sum(_effective_return(row) for row in losses), 8),
            "net_pnl_known": round(sum(float(row.net_pnl) for row in members if row.net_pnl is not None), 4),
            "costs_unknown": sum(1 for row in members if not row.costs_known),
        })
    out.sort(key=lambda item: (item["loss_return"], -item["losses"], item["key"]))
    return out


def attribution_row_out(row: StockTradeAttribution) -> dict:
    detail = row.detail or {}
    return {
        "id": row.id, "source": row.source, "source_key": row.source_key, "revision": row.revision,
        "strategy_id": row.strategy_id, "strategy_type": row.strategy_type, "trial_id": row.trial_id,
        "symbol": row.symbol, "opened_at": _utc(row.opened_at).isoformat(), "closed_at": _utc(row.closed_at).isoformat(),
        "exit_cause": row.exit_cause, "net_return": float(row.net_return) if row.net_return is not None else None,
        "gross_return": float(row.gross_return), "costs_known": row.costs_known, "is_loss": row.is_loss,
        "signal_quality": detail.get("signal_quality"), "slippage": detail.get("slippage"),
        "holding": detail.get("holding"), "regime": detail.get("regime"), "exit": detail.get("exit"),
        "meta_label": detail.get("meta_label"), "mistakes": detail.get("mistakes"),
        "diagnosis": detail.get("diagnosis"), "post_exit": detail.get("post_exit"),
        "excursion": detail.get("excursion"),
        "attribution_sha256": row.attribution_sha256,
    }


def attribution_summary(rows: list[StockTradeAttribution]) -> dict:
    """Compact, deterministic loss breakdown used in reports and the API."""
    from app.services.loss_diagnosis import cause_breakdown, repeating_failures

    losses = [row for row in rows if row.is_loss]

    def regime_key(row):
        regime = (row.detail or {}).get("regime") or {}
        return f"{regime.get('trend', 'unknown')}/{regime.get('volatility', 'unknown')}"

    def quality_key(row):
        return ((row.detail or {}).get("signal_quality") or {}).get("bucket", "unknown")

    def holding_key(row):
        return ((row.detail or {}).get("holding") or {}).get("relation", "unknown")

    return {
        "version": ATTRIBUTION_VERSION,
        "trades": len(rows),
        "losses": len(losses),
        "loss_rate": round(len(losses) / len(rows), 6) if rows else None,
        "sum_return": round(sum(_effective_return(row) for row in rows), 8),
        "loss_return": round(sum(_effective_return(row) for row in losses), 8),
        "costs_unknown": sum(1 for row in rows if not row.costs_known),
        "by_strategy": _breakdown(rows, lambda row: row.strategy_type),
        "by_symbol": _breakdown(rows, lambda row: row.symbol),
        "by_exit_cause": _breakdown(rows, lambda row: row.exit_cause),
        "by_regime": _breakdown(rows, regime_key),
        "by_signal_quality": _breakdown(rows, quality_key),
        "by_holding": _breakdown(rows, holding_key),
        "primary_mistakes": dict(Counter(
            ((row.detail or {}).get("mistakes") or {}).get("primary") for row in losses
            if ((row.detail or {}).get("mistakes") or {}).get("primary")
        )),
        "diagnosis": cause_breakdown(rows),
        "repeating_failures": repeating_failures(rows),
        "attribution_ids": sorted(row.id for row in rows)[-200:],
    }


def attribution_projection(db: Session, *, days: int = 30, now: Optional[datetime] = None,
                           strategy_type: Optional[str] = None, symbol: Optional[str] = None,
                           exit_cause_filter: Optional[str] = None, limit: int = 100) -> dict:
    now = now or datetime.now(UTC)
    rows = current_attributions(db, since=now - timedelta(days=max(1, days)),
                                strategy_types=[strategy_type] if strategy_type else None)
    if symbol:
        rows = [row for row in rows if row.symbol == symbol.strip().upper()]
    if exit_cause_filter:
        rows = [row for row in rows if row.exit_cause == exit_cause_filter]
    summary = attribution_summary(rows)
    summary.pop("attribution_ids", None)
    return {
        "as_of": now.isoformat(), "window_days": days,
        "summary": summary,
        "recent_losses": [attribution_row_out(row) for row in rows if row.is_loss][: max(1, min(limit, 500))],
        "exit_causes": list(EXIT_CAUSES),
        "paper_only": True, "live_authorized": False, "hindsight_only": True,
    }
