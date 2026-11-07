"""Feed and benchmark hygiene: one tape for data, fills and backtests.

The bot decides on consolidated daily bars (yfinance), takes its decision
price from the free IEX minute feed (about 3% of US volume), is filled by
Alpaca paper, and is judged by backtests that fill at the consolidated open.
Each of those can disagree with the others in a systematic way.  This module
measures and removes the disagreement, paper only:

1. **SIP benchmarks for fills.**  After the close (once the free plan's
   15-minute SIP delay has passed) every paper fill gets a consolidated
   reference price from delayed SIP minute bars of its session:

   * an entry at the open (a BUY entry filled in the first 30 minutes, the
     bucket the replay's next-open entry is calibrated on, or any ``opg``
     order) is referenced to the **official SIP open**: the open of the
     session's first regular-session SIP bar;
   * any other fill is referenced to the **SIP minute bar containing it**:
     its VWAP, or its open when the bar carries no VWAP.

   Both prices are always stored, so either view can be recomputed.  The
   references are ``fill_sip_reference`` audit rows (no migration);
   ``trade_attribution`` adds them to ``slippage.sip_reference`` /
   ``slippage.sip_bps`` and ``paper_cost_calibration`` prices entries and
   market orders against them in preference to the IEX decision price.
2. **Fill tags.**  ``fill_tags`` records each fill's time-in-force, whether
   it was a paper fill and whether it was an auction order (``opg``/``cls``).
   Alpaca paper does not run the opening or closing auction: it fills those
   orders at the quote with random partials and cancels most of them, so a
   paper auction fill is excluded from cost calibration.
3. **IEX-vs-SIP touch log.**  ``record_feed_divergence`` compares, for every
   symbol held during the session and every stop / target level live during
   it, the session's IEX low/high with the SIP low/high, and logs a
   ``feed_divergence`` audit row per session saying where one feed touched a
   level and the other did not.  Broker stops elect on the consolidated tape;
   app-side stops read IEX, so a divergence is a stop that fired (or did not)
   on a feed the backtest never saw.

Nothing here places, changes or cancels an order, and nothing writes strategy
state.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.integrations.alpaca_market_data import (
    SIP_DELAY, AlpacaBarsClient, DelayedSipTooRecent, MarketDataUnavailable,
)
from app.models import AuditLog
from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder, StockPaperPosition
from app.services.audit import write_audit_log
from app.services.exchange_sessions import session_bounds

logger = logging.getLogger(__name__)
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
EVENT = "stock_paper"
SIP_REFERENCE_ACTION = "fill_sip_reference"
DIVERGENCE_ACTION = "feed_divergence"
VERSION = 1

AUCTION_TIFS = frozenset({"opg", "cls"})
OPEN_ENTRY_MINUTES = 30          # same edge as the calibration's opening bucket
REFERENCE_LOOKBACK_SESSIONS = 10
EXIT_LEG_ROLES = frozenset({"stop_loss", "take_profit"})
STOP_ORDER_TYPES = frozenset({"stop", "stop_limit", "trailing_stop"})
NONTERMINAL_ORDER_STATUSES = frozenset({
    "new", "accepted", "pending_new", "partially_filled", "pending_cancel", "pending_replace", "open", "held",
    "stopped", "calculated", "reserved", "submitting", "unknown",
})
DEFAULT_APP_STOP_PCT = 0.03       # stock_paper_autotrader.DEFAULT_POSITION_STOP_LOSS_PCT


# ------------------------------------------------------------------- helpers

def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _f(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def session_of(moment: datetime) -> date:
    return _utc(moment).astimezone(ET).date()


def slippage_bps(fill_price: Optional[float], reference: Optional[float], side: str) -> Optional[float]:
    """Adverse slippage in bps (positive = worse than ``reference``); same sign rule as attribution."""
    fill_price, reference = _f(fill_price), _f(reference)
    if fill_price is None or reference is None or reference <= 0:
        return None
    raw = (fill_price - reference) / reference * 10_000
    return round(raw if str(side).lower() == "buy" else -raw, 4)


def sip_ready(session: date, now: datetime) -> bool:
    """Whether the whole regular session is past the free plan's SIP delay at ``now``."""
    bounds = session_bounds(session)
    return bounds is not None and _utc(bounds[1]) + SIP_DELAY <= _utc(now)


def last_sip_ready_session(now: datetime) -> Optional[date]:
    day = session_of(now)
    for _ in range(14):
        if sip_ready(day, now):
            return day
        day -= timedelta(days=1)
    return None


# ---------------------------------------------------------------- fill tags

def time_in_force(order: Optional[StockPaperOrder], fill: Optional[StockPaperFill] = None) -> Optional[str]:
    value = getattr(order, "time_in_force", None) if order is not None else None
    if not value and fill is not None and isinstance(getattr(fill, "raw_payload", None), Mapping):
        value = fill.raw_payload.get("time_in_force")
    text = str(value).strip().lower() if value else ""
    return text or None


def is_paper_broker(broker: Optional[str]) -> bool:
    """Every stock ledger account is paper unless its broker says otherwise (the app is paper only)."""
    return broker is None or "paper" in str(broker).lower()


def fill_tags(fill: Optional[StockPaperFill], order: Optional[StockPaperOrder], *,
              broker: Optional[str] = None) -> dict:
    """Time-in-force, paper/live and auction flags of one fill, and why it is kept out of calibration."""
    tif = time_in_force(order, fill)
    paper = is_paper_broker(broker)
    auction = tif in AUCTION_TIFS
    return {"time_in_force": tif, "paper_fill": paper, "auction": auction,
            "excluded": "paper_auction" if paper and auction else None}


def is_open_entry(*, side: str, filled_at: datetime, order: Optional[StockPaperOrder] = None,
                  tif: Optional[str] = None) -> bool:
    """A BUY entry the replay prices at the open: an ``opg`` order, or an entry filled in the first 30 minutes."""
    if str(side).lower() != "buy":
        return False
    if order is not None and (getattr(order, "leg_role", None) in EXIT_LEG_ROLES
                              or str(getattr(order, "order_type", "") or "").lower() in STOP_ORDER_TYPES):
        return False
    tif = tif if tif is not None else time_in_force(order)
    if tif == "opg":
        return True
    bounds = session_bounds(session_of(filled_at))
    if bounds is None:
        return False
    opened = _utc(bounds[0])
    return opened <= _utc(filled_at) < opened + timedelta(minutes=OPEN_ENTRY_MINUTES)


# ------------------------------------------------------- SIP reference choice

def regular_bars(bars: Iterable[Mapping], session: date) -> list[Mapping]:
    bounds = session_bounds(session)
    if bounds is None:
        return []
    opened, closed = _utc(bounds[0]), _utc(bounds[1])
    return sorted((bar for bar in bars if opened <= _utc(bar["opened_at"]) < closed),
                  key=lambda bar: _utc(bar["opened_at"]))


def official_open(bars: Iterable[Mapping], session: date) -> Optional[dict]:
    """The official SIP open: the first regular-session SIP bar's open (flagged when it is not the 9:30 bar)."""
    regular = regular_bars(bars, session)
    if not regular:
        return None
    first = regular[0]
    bounds = session_bounds(session)
    return {"price": _round(_f(first["open"])), "opened_at": _utc(first["opened_at"]).isoformat(),
            "at_session_open": _utc(first["opened_at"]) == _utc(bounds[0])}


def containing_bar(bars: Iterable[Mapping], moment: datetime) -> Optional[Mapping]:
    """The one-minute bar whose ``[opened_at, opened_at + 1m)`` contains ``moment``."""
    stamp = _utc(moment)
    for bar in bars:
        opened = _utc(bar["opened_at"])
        if opened <= stamp < opened + timedelta(minutes=1):
            return bar
    return None


def _bar_view(bar: Optional[Mapping]) -> Optional[dict]:
    if bar is None:
        return None
    return {"opened_at": _utc(bar["opened_at"]).isoformat(), "open": _round(_f(bar.get("open"))),
            "high": _round(_f(bar.get("high"))), "low": _round(_f(bar.get("low"))),
            "close": _round(_f(bar.get("close"))), "vwap": _round(_f(bar.get("vwap"))),
            "volume": _f(bar.get("volume"))}


def select_sip_reference(*, filled_at: datetime, bars: Iterable[Mapping], at_open: bool) -> dict:
    """The consolidated reference for one fill from its session's delayed SIP minute bars.

    ``at_open`` entries take the official SIP open; every other fill the SIP
    minute bar containing it (VWAP, else its open).  Both candidates are
    returned; ``kind`` / ``price`` name the one in force (``None`` when the
    tape has no bar for it).
    """
    session = session_of(filled_at)
    bars = list(bars)
    opening = official_open(bars, session)
    minute = _bar_view(containing_bar(bars, filled_at))
    kind = price = None
    if at_open and opening and opening["price"]:
        kind, price = "official_open", opening["price"]
    elif minute is not None:
        if minute["vwap"]:
            kind, price = "minute_vwap", minute["vwap"]
        else:
            kind, price = "minute_open", minute["open"]
    return {"feed": "sip", "delay_minutes": int(SIP_DELAY.total_seconds() // 60), "session": session.isoformat(),
            "at_open": bool(at_open), "kind": kind, "price": price, "official_open": opening,
            "minute_bar": minute, "status": "referenced" if price else "no_sip_bar"}


# ------------------------------------------------------ SIP reference storage

def _reference_rows(db: Session, *, since: Optional[datetime] = None) -> list[AuditLog]:
    query = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == SIP_REFERENCE_ACTION)
    if since is not None:
        query = query.filter(AuditLog.created_at >= _utc(since) - timedelta(days=7))
    return query.order_by(AuditLog.id).all()


def sip_references(db: Session, *, since: Optional[datetime] = None) -> dict[int, dict]:
    """Every stored fill reference keyed by fill id (a later row wins)."""
    out: dict[int, dict] = {}
    for row in _reference_rows(db, since=since):
        for key, value in ((row.payload or {}).get("references") or {}).items():
            try:
                out[int(key)] = value
            except (TypeError, ValueError):
                continue
    return out


def order_sip_references(references: Mapping[int, Mapping], order_ids: Iterable[Optional[int]]) -> dict[int, dict]:
    """Quantity-weighted SIP reference per order from its fills' references."""
    wanted = {int(order_id) for order_id in order_ids if order_id}
    grouped: dict[int, list[Mapping]] = defaultdict(list)
    for value in references.values():
        order_id = value.get("order_id")
        if order_id in wanted and _f(value.get("price")) and _f(value.get("quantity")):
            grouped[int(order_id)].append(value)
    out = {}
    for order_id, rows in grouped.items():
        quantity = sum(float(row["quantity"]) for row in rows)
        price = sum(float(row["price"]) * float(row["quantity"]) for row in rows) / quantity
        fill_price = None
        if all(_f(row.get("fill_price")) for row in rows):
            fill_price = sum(float(row["fill_price"]) * float(row["quantity"]) for row in rows) / quantity
        out[order_id] = {"price": _round(price), "fill_price": _round(fill_price), "fills": len(rows),
                         "quantity": _round(quantity, 9), "kinds": sorted({str(row.get("kind")) for row in rows}),
                         "time_in_force": sorted({str(row.get("time_in_force")) for row in rows
                                                  if row.get("time_in_force")}),
                         "auction": any(row.get("auction") for row in rows),
                         "paper_fill": all(row.get("paper_fill", True) for row in rows),
                         "feed": "sip"}
    return out


def _fills_to_reference(db: Session, start: datetime, end: datetime, done: set[int]):
    rows = db.query(StockPaperFill, StockPaperOrder, StockPaperAccount.broker).outerjoin(
        StockPaperOrder, StockPaperOrder.id == StockPaperFill.order_id,
    ).outerjoin(StockPaperAccount, StockPaperAccount.id == StockPaperFill.account_id).filter(
        StockPaperFill.filled_at >= _utc(start), StockPaperFill.filled_at <= _utc(end),
    ).order_by(StockPaperFill.filled_at, StockPaperFill.id).all()
    return [(fill, order, broker) for fill, order, broker in rows if fill.id not in done]


def reference_fill(fill: StockPaperFill, order: Optional[StockPaperOrder], bars: Iterable[Mapping], *,
                   broker: Optional[str] = None) -> dict:
    """The stored reference for one fill: tags, the SIP choice and the slippage against it."""
    tags = fill_tags(fill, order, broker=broker)
    at_open = is_open_entry(side=fill.side, filled_at=fill.filled_at, order=order, tif=tags["time_in_force"])
    reference = select_sip_reference(filled_at=fill.filled_at, bars=bars, at_open=at_open)
    price = _f(fill.price)
    minute = reference["minute_bar"] or {}
    opening = reference["official_open"] or {}
    return {**reference, **tags, "fill_id": fill.id, "order_id": fill.order_id, "symbol": str(fill.symbol).upper(),
            "side": str(fill.side).lower(), "filled_at": _utc(fill.filled_at).isoformat(),
            "quantity": _f(fill.quantity), "fill_price": price,
            "sip_bps": slippage_bps(price, reference["price"], fill.side),
            "minute_bps": slippage_bps(price, minute.get("vwap") or minute.get("open"), fill.side),
            "open_bps": slippage_bps(price, opening.get("price"), fill.side)}


def record_sip_references(db: Session, *, now: Optional[datetime] = None,
                          sessions: int = REFERENCE_LOOKBACK_SESSIONS,
                          client: Optional[AlpacaBarsClient] = None) -> dict:
    """Reference every unreferenced fill of the last ``sessions`` SIP-ready sessions (one audit row)."""
    from app.services.gate_cost import sessions_back

    now = _utc(now or datetime.now(UTC))
    last = last_sip_ready_session(now)
    if last is None:
        return {"status": "waiting", "recorded": 0, "reason": "no session is past the SIP delay"}
    first = sessions_back(last, max(int(sessions) - 1, 0))
    start = datetime.combine(first, datetime.min.time(), ET)
    end = _utc(session_bounds(last)[1]) + timedelta(hours=4)   # after-hours fills of the last session
    done = set(sip_references(db, since=start))
    pending = [(fill, order, broker) for fill, order, broker in _fills_to_reference(db, start, end, done)
               if sip_ready(session_of(fill.filled_at), now)]
    if not pending:
        return {"status": "complete", "recorded": 0, "pending": 0, "first_session": first.isoformat(),
                "last_session": last.isoformat()}
    client = client or AlpacaBarsClient()
    by_session: dict[date, list] = defaultdict(list)
    for item in pending:
        by_session[session_of(item[0].filled_at)].append(item)
    references: dict[str, dict] = {}
    failures = []
    for session, items in sorted(by_session.items()):
        bounds = session_bounds(session)
        if bounds is None:  # a fill dated on a closed day has no regular session to benchmark against
            for fill, order, broker in items:
                references[str(fill.id)] = reference_fill(fill, order, [], broker=broker) | {"status": "no_session"}
            continue
        symbols = sorted({str(fill.symbol).upper() for fill, _order, _broker in items})
        try:
            bars = client.bars(symbols, _utc(bounds[0]), _utc(bounds[1]), feed="sip")
        except (MarketDataUnavailable, DelayedSipTooRecent) as exc:
            failures.append({"session": session.isoformat(), "symbols": symbols, "error": str(exc)[:200],
                             "status_code": getattr(exc, "status_code", None)})
            if getattr(exc, "status_code", None) == 429:
                break
            continue
        for fill, order, broker in items:
            references[str(fill.id)] = reference_fill(fill, order, bars.get(str(fill.symbol).upper(), []),
                                                      broker=broker)
    status = "recorded" if references else "unavailable"
    if references or failures:
        referenced = sum(1 for value in references.values() if value.get("price"))
        write_audit_log(
            db, event_type=EVENT, action=SIP_REFERENCE_ACTION, status=status,
            message=(f"delayed SIP references for {len(references)} paper fills ({referenced} priced), "
                     f"{len(failures)} session fetches failed"),
            payload={"version": VERSION, "generated_at": now.isoformat(), "first_session": first.isoformat(),
                     "last_session": last.isoformat(), "references": references, "failures": failures,
                     "requests": getattr(client, "requests", None), "paper_only": True},
        )
        db.commit()
    return {"status": "complete" if not failures else "partial", "recorded": len(references),
            "priced": sum(1 for value in references.values() if value.get("price")),
            "failures": len(failures), "first_session": first.isoformat(), "last_session": last.isoformat()}


# ----------------------------------------------------------- feed divergence

def session_extremes(bars: Iterable[Mapping], session: date) -> Optional[dict]:
    """Regular-session high and low of one feed's minute bars, with the minute each was first printed."""
    regular = regular_bars(bars, session)
    if not regular:
        return None
    high_bar = max(regular, key=lambda bar: (_f(bar["high"]), -_utc(bar["opened_at"]).timestamp()))
    low_bar = min(regular, key=lambda bar: (_f(bar["low"]), _utc(bar["opened_at"]).timestamp()))
    return {"high": _round(_f(high_bar["high"])), "low": _round(_f(low_bar["low"])), "bars": len(regular),
            "high_at": _utc(high_bar["opened_at"]).isoformat(), "low_at": _utc(low_bar["opened_at"]).isoformat()}


def _first_touch(bars: list[Mapping], kind: str, level: float) -> Optional[str]:
    for bar in bars:
        if (kind == "stop" and _f(bar["low"]) <= level) or (kind == "target" and _f(bar["high"]) >= level):
            return _utc(bar["opened_at"]).isoformat()
    return None


def compare_level(level: Mapping, iex_bars: Iterable[Mapping], sip_bars: Iterable[Mapping], session: date) -> dict:
    """Did IEX and SIP agree on whether one stop (low <= level) or target (high >= level) was touched?"""
    price = float(level["price"])
    kind = level["kind"]
    out = {**level, "session": session.isoformat()}
    for name, bars in (("iex", iex_bars), ("sip", sip_bars)):
        regular = regular_bars(bars, session)
        extremes = session_extremes(regular, session)
        if extremes is None:
            out[name] = {"bars": 0, "touched": None}
            continue
        touched = extremes["low"] <= price if kind == "stop" else extremes["high"] >= price
        out[name] = {**extremes, "touched": bool(touched),
                     "first_touch_at": _first_touch(regular, kind, price) if touched else None}
    iex, sip = out["iex"]["touched"], out["sip"]["touched"]
    if iex is None or sip is None:
        out["touched_by"] = "missing_iex" if iex is None else "missing_sip"
        out["divergent"] = False
    else:
        out["touched_by"] = {(True, True): "both", (False, False): "neither",
                             (True, False): "iex_only", (False, True): "sip_only"}[(iex, sip)]
        out["divergent"] = iex != sip
    if out.get("sip", {}).get("low") is not None and kind == "stop":
        out["sip_distance_bps"] = _round((out["sip"]["low"] - price) / price * 10_000, 2)
    elif out.get("sip", {}).get("high") is not None and kind == "target":
        out["sip_distance_bps"] = _round((price - out["sip"]["high"]) / price * 10_000, 2)
    return out


def detect_divergence(levels: Iterable[Mapping], iex: Mapping[str, list], sip: Mapping[str, list],
                      session: date) -> list[dict]:
    return [compare_level(level, iex.get(level["symbol"], []), sip.get(level["symbol"], []), session)
            for level in levels]


def _app_stop_pct(db: Session) -> float:
    try:
        from app.models import RiskRule

        rule = db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).first()
        value = _f(((rule.value if rule else None) or {}).get("position_stop_loss_pct"))
    except Exception:  # evidence only; an unreadable rule falls back to the default budget
        db.rollback()
        value = None
    return value if value and 0 < value < 1 else DEFAULT_APP_STOP_PCT


def session_levels(db: Session, session: date, *, account_id: Optional[int] = None) -> list[dict]:
    """Every stop / target level live during ``session``.

    Broker exit legs and stop orders that were open during the session (still
    open, or filled / cancelled during it), their parent entries' declared
    bracket levels, and the app-side protective stop of each position held at
    the end of it (average entry less the stop budget).
    """
    bounds = session_bounds(session)
    if bounds is None:
        return []
    opened, closed = _utc(bounds[0]), _utc(bounds[1])
    query = db.query(StockPaperOrder).filter(StockPaperOrder.side == "sell",
                                            StockPaperOrder.created_at < closed)
    if account_id is not None:
        query = query.filter(StockPaperOrder.account_id == account_id)
    levels: dict[tuple, dict] = {}
    for order in query.all():
        status = str(order.status or "").lower()
        updated = _utc(order.updated_at) if order.updated_at else None
        live = status in NONTERMINAL_ORDER_STATUSES or (updated is not None and updated >= opened)
        if not live:
            continue
        if order.leg_role == "stop_loss" or str(order.order_type or "").lower() in STOP_ORDER_TYPES:
            kind, price, source = "stop", _f(order.stop_price), "broker_stop"
        elif order.leg_role == "take_profit":
            kind, price, source = "target", _f(order.limit_price), "broker_take_profit"
        else:
            continue
        if price and price > 0:
            symbol = str(order.symbol).upper()
            levels.setdefault((symbol, kind, round(price, 4)), {
                "symbol": symbol, "kind": kind, "price": round(price, 6), "source": source,
                "order_id": order.id, "order_status": status})
    positions = db.query(StockPaperPosition).filter(StockPaperPosition.quantity > 0)
    if account_id is not None:
        positions = positions.filter(StockPaperPosition.account_id == account_id)
    stop_pct = _app_stop_pct(db)
    for position in positions.all():
        entry = _f(position.average_entry_price)
        if entry and entry > 0:
            symbol = str(position.symbol).upper()
            price = entry * (1 - stop_pct)
            levels.setdefault((symbol, "stop", round(price, 4)), {
                "symbol": symbol, "kind": "stop", "price": round(price, 6), "source": "app_protective_stop",
                "order_id": None, "stop_pct": stop_pct})
    return sorted(levels.values(), key=lambda level: (level["symbol"], level["kind"], level["price"]))


def _divergence_row(db: Session, session: date) -> Optional[AuditLog]:
    rows = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == DIVERGENCE_ACTION).order_by(
        AuditLog.id.desc()).limit(60).all()
    return next((row for row in rows if (row.payload or {}).get("session") == session.isoformat()), None)


def divergence_counts(comparisons: Iterable[Mapping]) -> dict:
    comparisons = list(comparisons)
    counts = defaultdict(int)
    for row in comparisons:
        counts[row.get("touched_by")] += 1
    return {"levels": len(comparisons), "divergent": sum(1 for row in comparisons if row.get("divergent")),
            "iex_only": counts["iex_only"], "sip_only": counts["sip_only"], "both": counts["both"],
            "neither": counts["neither"],
            "missing": counts["missing_iex"] + counts["missing_sip"],
            "stops_divergent": sum(1 for row in comparisons if row.get("divergent") and row["kind"] == "stop"),
            "targets_divergent": sum(1 for row in comparisons if row.get("divergent") and row["kind"] == "target")}


def record_feed_divergence(db: Session, *, now: Optional[datetime] = None, session: Optional[date] = None,
                           client: Optional[AlpacaBarsClient] = None, force: bool = False) -> dict:
    """Compare IEX and SIP touches of every live level for one SIP-ready session (one audit row)."""
    now = _utc(now or datetime.now(UTC))
    session = session or last_sip_ready_session(now)
    if session is None or not sip_ready(session, now):
        return {"status": "waiting", "recorded": False, "reason": "session is not past the SIP delay"}
    if not force and _divergence_row(db, session) is not None:
        return {"status": "complete", "recorded": False, "session": session.isoformat(),
                "reason": "feed divergence already recorded for this session"}
    from app.services.stock_paper_ledger import active_paper_account

    account = active_paper_account(db)
    levels = session_levels(db, session, account_id=account.id if account else None)
    base = {"version": VERSION, "session": session.isoformat(), "generated_at": now.isoformat(),
            "levels": len(levels), "symbols": sorted({level["symbol"] for level in levels}), "paper_only": True}
    if not levels:
        payload = base | {"comparisons": [], "divergent": [], "counts": divergence_counts([])}
        status = "no_levels"
    else:
        client = client or AlpacaBarsClient()
        bounds = session_bounds(session)
        try:
            iex = client.bars(base["symbols"], _utc(bounds[0]), _utc(bounds[1]), feed="iex")
            sip = client.bars(base["symbols"], _utc(bounds[0]), _utc(bounds[1]), feed="sip")
        except (MarketDataUnavailable, DelayedSipTooRecent) as exc:
            return {"status": "unavailable", "recorded": False, "session": session.isoformat(),
                    "reason": str(exc)[:200]}
        comparisons = detect_divergence(levels, iex, sip, session)
        counts = divergence_counts(comparisons)
        payload = base | {"comparisons": comparisons, "counts": counts,
                          "divergent": [row for row in comparisons if row["divergent"]]}
        status = "divergent" if counts["divergent"] else "agree"
    counts = payload["counts"]
    write_audit_log(
        db, event_type=EVENT, action=DIVERGENCE_ACTION, status=status,
        message=(f"IEX vs SIP touches on {session.isoformat()}: {counts['levels']} levels, "
                 f"{counts['divergent']} divergent ({counts['iex_only']} IEX only, {counts['sip_only']} SIP only)"),
        payload=payload,
    )
    db.commit()
    return {"status": status, "recorded": True, "session": session.isoformat(), **counts}


def divergence_summary(db: Session, *, now: Optional[datetime] = None, days: int = 7) -> dict:
    """The week's touch log: sessions checked, sessions and levels where the feeds disagreed."""
    now = _utc(now or datetime.now(UTC))
    since = now - timedelta(days=days)
    rows = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == DIVERGENCE_ACTION,
                                     AuditLog.created_at >= since).order_by(AuditLog.id).all()
    latest: dict[str, Mapping] = {}
    for row in rows:
        payload = row.payload or {}
        if payload.get("session"):
            latest[payload["session"]] = payload
    totals = defaultdict(int)
    examples = []
    for session in sorted(latest):
        counts = latest[session].get("counts") or {}
        for key in ("levels", "divergent", "iex_only", "sip_only", "missing", "stops_divergent",
                    "targets_divergent"):
            totals[key] += int(counts.get(key) or 0)
        for item in latest[session].get("divergent") or []:
            if len(examples) < 10:
                examples.append({"session": session, "symbol": item.get("symbol"), "kind": item.get("kind"),
                                 "price": item.get("price"), "source": item.get("source"),
                                 "touched_by": item.get("touched_by"),
                                 "iex_low": (item.get("iex") or {}).get("low"),
                                 "sip_low": (item.get("sip") or {}).get("low"),
                                 "iex_high": (item.get("iex") or {}).get("high"),
                                 "sip_high": (item.get("sip") or {}).get("high")})
    divergent_sessions = sum(1 for payload in latest.values() if (payload.get("counts") or {}).get("divergent"))
    return {"sessions_checked": len(latest), "divergent_sessions": divergent_sessions, **dict(totals),
            "levels": totals["levels"], "divergent": totals["divergent"], "examples": examples}


# --------------------------------------------------------- signal feed tags

DAILY_SOURCE_FEEDS = {
    "yfinance": "consolidated", "yahoo_chart": "consolidated", "alpaca_sip_daily": "sip",
    "alpaca_iex_daily": "iex",
}


def daily_source_feed(source: Optional[str]) -> str:
    """The tape behind a stored daily-bar source (``database:yfinance`` -> ``consolidated``)."""
    name = str(source or "").strip()
    if name.startswith("database:"):
        name = name.split(":", 1)[1]
    if name.startswith("mixed("):
        feeds = {daily_source_feed(part) for part in name[6:-1].split(",")}
        return feeds.pop() if len(feeds) == 1 else "mixed"
    return DAILY_SOURCE_FEEDS.get(name, "unknown")


def signal_feed(source: Optional[str], observation: Optional[Mapping], *, now: Optional[datetime] = None) -> dict:
    """Which tape each input of one signal came from (stored in its ``features["feed"]``).

    The rule reads daily bars (normally yfinance, consolidated); the decision
    price is the latest execution-feed minute close (IEX on the free plan).
    ``decision_phase`` says whether the signal was decided inside the regular
    session, before it or after its close.
    """
    observation = observation or {}
    phase = None
    if now is not None:
        bounds = session_bounds(session_of(now))
        stamp = _utc(now)
        if bounds is None:
            phase = "closed_day"
        elif stamp < _utc(bounds[0]):
            phase = "pre_open"
        elif stamp >= _utc(bounds[1]):
            phase = "after_close"
        else:
            phase = "session"
    return {"daily_bars": daily_source_feed(source), "daily_source": str(source or "") or None,
            "reference_price": str(observation.get("feed_class") or "") or None,
            "reference_provider": str(observation.get("provider") or "") or None,
            "decision_phase": phase}
