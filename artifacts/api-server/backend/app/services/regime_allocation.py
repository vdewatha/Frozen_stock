"""Regime-aware paper risk budget: each strategy's track record per regime cell.

``strategy_allocation`` scales a strategy's paper risk fraction by its recent
net record.  That record is regime-blind: a mean-reversion strategy that
earns in calm markets and bleeds in falling, fearful ones is weighted by the
mix of both.  This module conditions the record on the *regime cell* the
trade was opened in, so the opt-in multiplier
(``paper_allocation_regime_enabled``) can shrink a strategy where it has lost
and leave it alone where the evidence is thin.

A regime cell has three axes, each read from the last completed data before
the entry:

* **trend**: ``up`` / ``down``: SPY's close above or at-or-below its 50-day
  average (the ``market_gate.market_state`` rule);
* **volatility**: ``low`` (VIX < 20), ``mid`` (20-30) or ``high`` (> 30);
* **breadth**: ``strong`` (>= 50% of S&P 500 members above their 200-day
  average) or ``weak``.

An axis whose input is missing is ``unknown``; a cell with an unknown axis
only matches trades that had the same gap, so a data outage falls back to
the overall record instead of inventing one.

Evidence is the same as the overall weight: closed, attributed, scheduled
paper round trips with known costs (``trade_attribution.current_attributions``),
inside the allocation lookback.  For each trade, the VIX and breadth come
from the attribution's diagnosis (``entry_vix`` / ``entry_breadth``, recorded
from the BUY's ``market_context`` features) and the trend from SPY's stored
daily closes strictly before the entry date.  A cell with fewer than
``paper_allocation_min_trades`` trades falls back to the strategy's overall
record (multiplier 1.0 on top of the overall weight).

The regime multiplier is the same bounded, shrink-first
``performance_multiplier`` as the overall weight, and the product of the two
is clamped to the same floor / ceiling, so the budget can never exceed the
ceiling and losses always move it more than gains.  Paper only; nothing here
places, blocks or exits an order.

The pre-registered backtest lives in ``scripts/run_regime_allocation_backtest.py``
and its verdict in ``reports/regime-allocation-<date>.md``; the setting's
default follows that verdict.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
TREND_WINDOW = 50
VIX_LOW = 20.0
VIX_HIGH = 30.0
BREADTH_STRONG_PCT = 50.0
UNKNOWN = "unknown"
TREND_LABELS = ("up", "down")
VOLATILITY_LABELS = ("low", "mid", "high")
BREADTH_LABELS = ("strong", "weak")
AXES = ("trend", "volatility", "breadth")


# ----------------------------------------------------------------- cells

def trend_label(spy_above_sma: Optional[bool]) -> str:
    if spy_above_sma is None:
        return UNKNOWN
    return "up" if spy_above_sma else "down"


def volatility_label(vix: Optional[float]) -> str:
    if vix is None:
        return UNKNOWN
    try:
        value = float(vix)
    except (TypeError, ValueError):
        return UNKNOWN
    if value != value:  # NaN
        return UNKNOWN
    if value < VIX_LOW:
        return "low"
    if value <= VIX_HIGH:
        return "mid"
    return "high"


def breadth_label(breadth_pct: Optional[float]) -> str:
    if breadth_pct is None:
        return UNKNOWN
    try:
        value = float(breadth_pct)
    except (TypeError, ValueError):
        return UNKNOWN
    if value != value:
        return UNKNOWN
    return "strong" if value >= BREADTH_STRONG_PCT else "weak"


def regime_cell(*, spy_above_sma: Optional[bool] = None, vix: Optional[float] = None,
                breadth_pct: Optional[float] = None) -> dict:
    """The regime cell for one entry; ``key`` is ``trend/volatility/breadth``."""
    cell = {"trend": trend_label(spy_above_sma), "volatility": volatility_label(vix),
            "breadth": breadth_label(breadth_pct)}
    cell["key"] = cell_key(cell)
    return cell


def cell_key(cell: dict) -> str:
    return "/".join(str(cell.get(axis) or UNKNOWN) for axis in AXES)


def cell_is_known(cell: dict) -> bool:
    return all(cell.get(axis) not in (None, UNKNOWN) for axis in AXES)


# ----------------------------------------------------------------- the current regime

def _spy_frame(db: Session):
    from app.services.market_gate import MARKET_SYMBOL
    from app.services.trusted_data import trusted_history

    return trusted_history(db, MARKET_SYMBOL, require_active=False)[0]


def current_regime(db: Session, *, now: Optional[datetime] = None, spy_frame=None) -> dict:
    """The cell the next BUY would open in, with the readings behind it.

    Trend from the last completed SPY bar (``market_gate.market_state``),
    VIX and breadth from the latest stored gauges when fresh.  Every input
    that cannot be read leaves its axis unknown; nothing raises.
    """
    from app.services.market_gate import market_state
    from app.services.market_gauges import BREADTH, VIX, latest_gauges

    now = now or datetime.now(UTC)
    readings: dict = {"spy": None, "vix": None, "breadth_above_200d": None, "errors": {}}
    above: Optional[bool] = None
    # Savepoints: a failed read must not roll back the caller's work (the
    # paper cycle computes weights inside its own nested transaction).
    try:
        if spy_frame is None:
            with db.begin_nested():
                spy_frame = _spy_frame(db)
        state = market_state(spy_frame, TREND_WINDOW)
        readings["spy"] = {key: state.get(key) for key in ("close", "sma", "bar", "reason")}
        above = bool(state["allowed"]) if state.get("close") is not None else None
    except Exception as exc:  # unknown trend, never a block
        readings["errors"]["spy"] = exc.__class__.__name__
    vix = breadth = None
    try:
        with db.begin_nested():
            gauges = latest_gauges(db, now)
        for name, key in ((VIX, "vix"), (BREADTH, "breadth_above_200d")):
            gauge = gauges.get(name)
            if gauge:
                readings[key] = {"value": gauge["value"], "as_of": gauge["as_of"], "fresh": gauge["fresh"]}
        vix_gauge, breadth_gauge = gauges.get(VIX) or {}, gauges.get(BREADTH) or {}
        vix = vix_gauge.get("value") if vix_gauge.get("fresh") else None
        breadth = breadth_gauge.get("value") if breadth_gauge.get("fresh") else None
    except Exception as exc:
        readings["errors"]["gauges"] = exc.__class__.__name__
    cell = regime_cell(spy_above_sma=above, vix=vix, breadth_pct=breadth)
    return {"cell": cell, "readings": readings, "as_of": now.isoformat()}


# ----------------------------------------------------------------- cells of past trades

def _spy_closes_before(db: Session, day: date, *, limit: int = TREND_WINDOW + 10) -> list[float]:
    from app.models import MarketPrice
    from app.services.market_gate import MARKET_SYMBOL

    rows = db.execute(
        select(MarketPrice.price_date, MarketPrice.close, MarketPrice.adjusted_close)
        .where(MarketPrice.symbol == MARKET_SYMBOL, MarketPrice.price_date < day)
        .order_by(MarketPrice.price_date.desc(), MarketPrice.id)
        .limit(limit * 3)
    ).all()
    by_day: dict[date, float] = {}
    for price_date, close, adjusted in rows:
        value = _float(adjusted) or _float(close)
        if value and price_date not in by_day:
            by_day[price_date] = value
    return [by_day[key] for key in sorted(by_day)][-limit:]


def spy_trend_before(db: Session, day: date, cache: Optional[dict] = None) -> Optional[bool]:
    """SPY's last close before ``day`` above its 50-day average; None when the history is short."""
    cache = cache if cache is not None else {}
    if day in cache:
        return cache[day]
    try:
        with db.begin_nested():
            closes = _spy_closes_before(db, day)
    except Exception as exc:
        logger.info("regime allocation: SPY history unavailable: %s", exc.__class__.__name__)
        closes = []
    if len(closes) < TREND_WINDOW:
        cache[day] = None
    else:
        sma = sum(closes[-TREND_WINDOW:]) / TREND_WINDOW
        cache[day] = closes[-1] > sma
    return cache[day]


def _float(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def cell_for_attribution(db: Session, row, *, cache: Optional[dict] = None) -> dict:
    """The regime cell a closed round trip was opened in."""
    detail = row.detail or {}
    diagnosis = detail.get("diagnosis") if isinstance(detail.get("diagnosis"), dict) else {}
    opened = row.opened_at
    opened = opened.replace(tzinfo=UTC) if opened.tzinfo is None else opened
    day = opened.astimezone(ET).date()
    return regime_cell(spy_above_sma=spy_trend_before(db, day, cache),
                       vix=_float(diagnosis.get("entry_vix")), breadth_pct=_float(diagnosis.get("entry_breadth")))


# ----------------------------------------------------------------- records and multipliers

def regime_record(returns_by_cell: dict[str, list[float]], policy: dict) -> dict:
    """Per-cell multipliers from per-cell net returns (cells below ``min_trades`` fall back)."""
    from app.services.strategy_allocation import performance_multiplier

    cells = {}
    for key, returns in sorted(returns_by_cell.items()):
        record = performance_multiplier(list(returns), policy)
        if record["status"] == "insufficient_evidence":
            record = record | {"multiplier": 1.0, "status": "fallback_overall",
                               "mean_return": round(sum(returns) / len(returns), 8) if returns else None}
        cells[key] = record
    return cells


def regime_records(db: Session, strategy_types: Iterable[str], *, now: datetime,
                   policy: Optional[dict] = None) -> dict[str, dict]:
    """Per strategy: the overall record and the record per regime cell, inside the allocation lookback."""
    from app.services.strategy_allocation import bounds, performance_multiplier
    from app.services.trade_attribution import current_attributions

    policy = policy or bounds()
    since = now - timedelta(days=policy["lookback_days"])
    cache: dict = {}
    out: dict[str, dict] = {}
    for strategy_type in sorted(set(strategy_types)):
        rows = [row for row in current_attributions(db, since=since, sources=["scheduled_round_trip"],
                                                    strategy_types=[strategy_type])
                if row.costs_known and row.net_return is not None and row.closed_at is not None]
        rows = rows[: policy["lookback_trades"]]
        by_cell: dict[str, list[float]] = {}
        for row in rows:
            key = cell_for_attribution(db, row, cache=cache)["key"]
            by_cell.setdefault(key, []).append(float(row.net_return))
        overall = performance_multiplier([float(row.net_return) for row in rows], policy)
        out[strategy_type] = {"overall": overall, "cells": regime_record(by_cell, policy),
                              "attribution_ids": [row.id for row in rows]}
    return out


def regime_multiplier(record: Optional[dict], cell: dict) -> dict:
    """The multiplier for ``cell`` from one strategy's ``regime_records`` entry (1.0 when it falls back)."""
    key = cell.get("key") or cell_key(cell)
    cells = (record or {}).get("cells") or {}
    found = cells.get(key)
    if not found or found.get("status") == "fallback_overall":
        return {"regime_cell": key, "regime_multiplier": 1.0, "regime_status": "fallback_overall",
                "regime_trades": int((found or {}).get("trades") or 0),
                "regime_mean_return": (found or {}).get("mean_return")}
    return {"regime_cell": key, "regime_multiplier": float(found["multiplier"]), "regime_status": found["status"],
            "regime_trades": int(found["trades"]), "regime_mean_return": found.get("mean_return")}


def combine(base_multiplier: float, regime: float, policy: dict) -> float:
    """``base x regime`` inside the same floor / ceiling: never above the ceiling, shrink before growing."""
    value = float(base_multiplier) * float(regime)
    return round(min(policy["ceiling"], max(policy["floor"], value)), 6)


# ----------------------------------------------------------------- projection

def _cell_rows(record: dict) -> list[dict]:
    rows = []
    for key, cell in sorted(record.get("cells", {}).items()):
        trend, volatility, breadth = (key.split("/") + [UNKNOWN] * 3)[:3]
        mean = cell.get("mean_return")
        rows.append({"cell": key, "trend": trend, "volatility": volatility, "breadth": breadth,
                     "trades": cell.get("trades", 0), "mean_return": mean,
                     "expectancy_bps": round(mean * 10_000, 2) if mean is not None else None,
                     "multiplier": cell.get("multiplier"), "status": cell.get("status")})
    return rows


def regime_allocation_view(db: Session, *, now: Optional[datetime] = None) -> dict:
    """The per-regime table per strategy and the multiplier in force (read-only)."""
    from app.models import Strategy
    from app.models.loss_feedback import StockAllocationWeight
    from app.services.strategy_allocation import bounds

    now = now or datetime.now(UTC)
    policy = bounds()
    types = sorted({row.strategy_type for row in db.query(Strategy).filter(
        Strategy.current_status == "paper_trading_active").all()})
    regime = current_regime(db, now=now)
    records = regime_records(db, types, now=now, policy=policy)
    session_date = now.astimezone(ET).date()
    stored = {row.strategy_type: row for row in db.scalars(select(StockAllocationWeight).where(
        StockAllocationWeight.session_date == session_date,
        StockAllocationWeight.strategy_type.in_(types))).all()} if types else {}
    strategies = []
    for strategy_type in types:
        record = records.get(strategy_type, {})
        preview = regime_multiplier(record, regime["cell"])
        row = stored.get(strategy_type)
        evidence = (row.evidence or {}) if row is not None else {}
        in_force = {"multiplier": float(row.multiplier) if row is not None else None,
                    "status": row.status if row is not None else None,
                    "regime_cell": evidence.get("regime_cell"),
                    "regime_multiplier": evidence.get("regime_multiplier"),
                    "regime_status": evidence.get("regime_status"),
                    "base_multiplier": evidence.get("base_multiplier")}
        strategies.append({
            "strategy_type": strategy_type,
            "overall": {key: record.get("overall", {}).get(key) for key in ("trades", "mean_return", "multiplier", "status")},
            "cells": _cell_rows(record),
            "current_cell": preview | {"combined_if_enabled": combine(
                record.get("overall", {}).get("multiplier", 1.0), preview["regime_multiplier"], policy)},
            "in_force": in_force,
        })
    return {
        "enabled": bool(settings.paper_allocation_regime_enabled),
        "policy": policy | {"trend_window": TREND_WINDOW, "vix_low": VIX_LOW, "vix_high": VIX_HIGH,
                            "breadth_strong_pct": BREADTH_STRONG_PCT},
        "regime": regime,
        "session_date": session_date.isoformat(),
        "strategies": strategies,
        "paper_only": True,
        "live_sizing_affected": False,
    }
