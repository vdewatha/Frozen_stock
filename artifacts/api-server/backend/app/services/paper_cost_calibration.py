"""Fit the replay cost model to the bot's own paper fills.

Every backtest, gate replay and experiment prices execution with the
volatility-scaled guess in ``backtester.execution_cost_bps`` /
``triple_barrier.side_cost_bps`` (5 bps floor + 5% of the causal daily
volatility + square-root impact, plus a 1 bp fee per side).  Realised costs
decide whether an edge survives, and the paper account records what each fill
actually cost: the broker fill price against the decision price the order was
reserved at (``StockPaperOrder.limit_price``, or the stop price of a
protective leg), plus the fee the broker reported.  This module measures that
gap and turns it into a calibration the replays can use.

1. **Measure.**  Every ``StockPaperFill`` of the last ``N`` sessions (default
   60) is joined to its order and scored per side in basis points:
   ``slippage_bps`` (``trade_attribution.slippage_bps``: positive = worse
   than the decision price) plus the known fee, by symbol, by order kind
   (market, limit entry, bracket take-profit leg, protective stop) and by
   time-of-day bucket (first 30 minutes, mid-day, last 30 minutes).  Next to
   each fill sits what the published model would have charged for the same
   session (volatility and volume from the trusted daily bars *before* that
   session, the fill's own quantity), and the residual ``realised / modelled``.
2. **Calibrate.**  Per cell (symbol class: index ETF, sector ETF, single
   stock; time bucket) the raw multiplier is the ratio of mean realised to
   mean modelled slippage.  Cells with fewer than ``MIN_FILLS`` fills are not
   fitted; the rest are shrunk toward 1.0 with ``SHRINK_FILLS`` pseudo-fills
   and capped to ``MULTIPLIER_BOUNDS``.  The same fit is made per class over
   the whole day and over everything, so a sparse cell falls back to a coarser
   one.  The result is a ``paper_cost_calibration`` audit row (no migration)
   carrying the fills it was fitted on and the date range.
3. **Apply.**  ``CostCalibration.for_symbol`` resolves the multipliers one
   replayed symbol needs (``triple_barrier.CostMultipliers``: the next-open
   entry takes the opening bucket, exits the whole-day one).
   ``gate_replay.simulate`` reads the latest calibration through
   ``latest_calibration`` so the experiment engine replays with realised costs.

A weekly job (Saturday, before the Sunday experiment run) recalibrates;
``GET /stock-paper/cost-calibration`` and the weekly report show the latest
calibration and the measurement table.  Nothing here places, blocks or exits
an order; strategies are never written.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import logging
import math
from typing import Any, Callable, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditLog
from app.models.stock_paper import StockPaperFill, StockPaperOrder
from app.services.audit import write_audit_log
from app.services.backtester import _liquidity
from app.services.exchange_sessions import session_bounds
from app.services.gate_cost import daily_frame, last_completed_session, sessions_back
from app.services.trade_attribution import slippage_bps
from app.services.trading_universe import ETF_SYMBOLS, SECTOR_GROUPS
from app.services.triple_barrier import BarrierConfig, CostMultipliers, side_cost_bps

logger = logging.getLogger(__name__)
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
EVENT = "stock_paper"
ACTION = "paper_cost_calibration"
CALIBRATION_VERSION = 1

INDEX_ETF, SECTOR_ETF, SINGLE_STOCK = "index_etf", "sector_etf", "single_stock"
SYMBOL_CLASSES = (INDEX_ETF, SECTOR_ETF, SINGLE_STOCK)
OPEN, MIDDAY, CLOSE, OFF_HOURS = "open", "midday", "close", "off_hours"
TIME_BUCKETS = (OPEN, MIDDAY, CLOSE, OFF_HOURS)
ALL = "all"
EDGE_MINUTES = 30
ORDER_KINDS = ("market", "limit", "bracket_take_profit", "protective_stop", "unknown")
STOP_TYPES = {"stop", "stop_limit", "trailing_stop"}

MIN_FILLS = 20          # a cell is fitted only with at least this many usable fills
SHRINK_FILLS = 20       # pseudo-fills at 1.0 the raw ratio is blended with
MULTIPLIER_BOUNDS = (0.5, 3.0)
MAX_STORED_FILLS = 2000
# The replay enters at the next session's open; exits land anywhere in the day.
ENTRY_BUCKET = OPEN
EXIT_BUCKET = ALL


# ------------------------------------------------------------------ buckets

def symbol_class(symbol: Any) -> str:
    name = str(symbol or "").strip().upper()
    if SECTOR_GROUPS.get(name) == "index":
        return INDEX_ETF
    if name in ETF_SYMBOLS:
        return SECTOR_ETF
    return SINGLE_STOCK


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def session_date(filled_at: datetime) -> date:
    return _utc(filled_at).astimezone(ET).date()


def time_bucket(filled_at: datetime) -> str:
    """Opening 30 minutes, closing 30 minutes, mid-day, or outside the regular session."""
    stamp = _utc(filled_at)
    bounds = session_bounds(stamp.astimezone(ET).date())
    if bounds is None:
        return OFF_HOURS
    opened, closed = _utc(bounds[0]), _utc(bounds[1])
    if stamp < opened or stamp > closed:
        return OFF_HOURS
    if stamp < opened + timedelta(minutes=EDGE_MINUTES):
        return OPEN
    if stamp > closed - timedelta(minutes=EDGE_MINUTES):
        return CLOSE
    return MIDDAY


def order_kind(order: Optional[StockPaperOrder]) -> str:
    if order is None:
        return "unknown"
    role = getattr(order, "leg_role", None)
    order_type = str(getattr(order, "order_type", "") or "").lower()
    if role == "take_profit":
        return "bracket_take_profit"
    if role == "stop_loss" or order_type in STOP_TYPES:
        return "protective_stop"
    if order_type == "market":
        return "market"
    return "limit"


def _f(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def decision_price(order: Optional[StockPaperOrder]) -> Optional[float]:
    """The price the order was decided at: the reserved limit, or a protective leg's stop."""
    if order is None:
        return None
    if order_kind(order) == "protective_stop":
        return _f(getattr(order, "stop_price", None)) or _f(getattr(order, "limit_price", None))
    return _f(getattr(order, "limit_price", None))


# ----------------------------------------------------------------- measure

def model_cost(frame: Optional[pd.DataFrame], session: date, quantity: float,
               config: Optional[BarrierConfig] = None) -> dict:
    """What the published model charges one side on ``session`` (bars strictly before it)."""
    config = config or BarrierConfig()
    sigma_bps, adv, bars = 0.0, None, 0
    if frame is not None and len(frame) and "date" in frame:
        history = frame[frame["date"].astype(str) < session.isoformat()]
        bars = len(history)
        if bars:
            sigma_bps, adv = _liquidity(history, config.backtest_config())
    slippage = side_cost_bps(sigma_bps, adv if adv is not None else math.nan, float(quantity), config)
    return {"slippage_bps": round(slippage, 4), "fee_bps": float(config.fees_bps),
            "total_bps": round(slippage + float(config.fees_bps), 4), "sigma_bps": round(sigma_bps, 4),
            "adv": round(adv, 2) if adv else None, "history_bars": bars}


def observe_fill(fill: StockPaperFill, order: Optional[StockPaperOrder], frame: Optional[pd.DataFrame], *,
                 config: Optional[BarrierConfig] = None) -> dict:
    """One fill scored against its decision price and the modelled cost of the same session."""
    price, quantity = _f(fill.price), _f(fill.quantity) or 0.0
    reference = decision_price(order)
    slippage = slippage_bps(price, reference, fill.side)
    fee = _f(fill.fee)
    notional = (price or 0.0) * quantity
    fee_bps = round(fee / notional * 10_000, 4) if fee is not None and notional > 0 else None
    session = session_date(fill.filled_at)
    modelled = model_cost(frame, session, quantity, config)
    realised_total = round(slippage + (fee_bps or 0.0), 4) if slippage is not None else None
    usable = slippage is not None and modelled["slippage_bps"] > 0
    return {
        "fill_id": fill.id, "order_id": fill.order_id, "symbol": fill.symbol, "side": fill.side,
        "symbol_class": symbol_class(fill.symbol), "order_kind": order_kind(order),
        "strategy_id": getattr(order, "strategy_id", None) if order else None,
        "filled_at": _utc(fill.filled_at).isoformat(), "session": session.isoformat(),
        "time_bucket": time_bucket(fill.filled_at),
        "quantity": quantity, "fill_price": price, "decision_price": reference,
        "slippage_bps": slippage, "fee_bps": fee_bps, "fee_known": fee is not None,
        "realised_total_bps": realised_total,
        "modelled_slippage_bps": modelled["slippage_bps"], "modelled_total_bps": modelled["total_bps"],
        "modelled_sigma_bps": modelled["sigma_bps"], "history_bars": modelled["history_bars"],
        "gap_bps": round(realised_total - modelled["total_bps"], 4) if realised_total is not None else None,
        "residual": round(slippage / modelled["slippage_bps"], 4) if usable else None,
        "usable": usable,
    }


def fill_rows(db: Session, start: datetime, end: datetime) -> list[tuple[StockPaperFill, Optional[StockPaperOrder]]]:
    return db.query(StockPaperFill, StockPaperOrder).outerjoin(
        StockPaperOrder, StockPaperOrder.id == StockPaperFill.order_id,
    ).filter(
        StockPaperFill.filled_at >= _utc(start), StockPaperFill.filled_at <= _utc(end),
    ).order_by(StockPaperFill.filled_at, StockPaperFill.id).all()


def measure_fills(pairs: Iterable[tuple[StockPaperFill, Optional[StockPaperOrder]]], *,
                  loader: Callable[[str], Optional[pd.DataFrame]],
                  config: Optional[BarrierConfig] = None) -> list[dict]:
    frames: dict[str, Optional[pd.DataFrame]] = {}
    rows = []
    for fill, order in pairs:
        symbol = str(fill.symbol).upper()
        if symbol not in frames:
            try:
                frames[symbol] = loader(symbol)
            except Exception as exc:  # one symbol's bars must not stop the measurement
                logger.warning("cost calibration: daily bars unavailable for %s: %s", symbol, exc)
                frames[symbol] = None
        rows.append(observe_fill(fill, order, frames[symbol], config=config))
    return rows


# --------------------------------------------------------------- summarise

def _stats(values: list[float]) -> dict:
    if not values:
        return {"mean": None, "median": None, "p90": None}
    arr = np.asarray(values, dtype=float)
    return {"mean": round(float(arr.mean()), 4), "median": round(float(np.median(arr)), 4),
            "p90": round(float(np.percentile(arr, 90)), 4)}


def summarise(rows: Iterable[dict]) -> dict:
    """Counts, mean / median / p90 of realised cost, the modelled cost and the gap for one group."""
    rows = list(rows)
    usable = [row for row in rows if row.get("usable")]
    priced = [row for row in rows if row.get("slippage_bps") is not None]
    realised = [row["slippage_bps"] for row in priced]
    modelled = [row["modelled_slippage_bps"] for row in usable]
    realised_usable = [row["slippage_bps"] for row in usable]
    fees = [row["fee_bps"] for row in rows if row.get("fee_bps") is not None]
    totals = [row["realised_total_bps"] for row in priced]
    modelled_totals = [row["modelled_total_bps"] for row in usable]
    gaps = [row["gap_bps"] for row in usable if row.get("gap_bps") is not None]
    residuals = [row["residual"] for row in usable if row.get("residual") is not None]
    modelled_sum = float(sum(modelled))
    return {
        "fills": len(rows), "priced": len(priced), "usable": len(usable),
        "fee_known": sum(1 for row in rows if row.get("fee_known")),
        "realised_slippage_bps": _stats(realised),
        "realised_total_bps": _stats(totals),
        "fee_bps_mean": round(float(np.mean(fees)), 4) if fees else None,
        "modelled_slippage_bps_mean": round(float(np.mean(modelled)), 4) if modelled else None,
        "modelled_total_bps_mean": round(float(np.mean(modelled_totals)), 4) if modelled_totals else None,
        "gap_bps_mean": round(float(np.mean(gaps)), 4) if gaps else None,
        "residual": round(float(sum(realised_usable)) / modelled_sum, 4) if modelled_sum > 0 else None,
        "residual_median": round(float(np.median(residuals)), 4) if residuals else None,
    }


def _grouped(rows: Iterable[dict], key: Callable[[dict], Any]) -> dict:
    groups: dict = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return groups


def measurement_tables(rows: list[dict]) -> dict:
    """The measurement table: overall and by symbol, order kind, time bucket, symbol class and cell."""
    def table(key_names: tuple[str, ...], key: Callable[[dict], tuple]) -> list[dict]:
        out = []
        for value, group in sorted(_grouped(rows, key).items(), key=lambda item: tuple(map(str, item[0]))):
            out.append({**dict(zip(key_names, value)), **summarise(group)})
        return out

    return {
        "overall": summarise(rows),
        "by_symbol": table(("symbol",), lambda row: (row["symbol"],)),
        "by_order_kind": table(("order_kind",), lambda row: (row["order_kind"],)),
        "by_time_bucket": table(("time_bucket",), lambda row: (row["time_bucket"],)),
        "by_symbol_class": table(("symbol_class",), lambda row: (row["symbol_class"],)),
        "by_side": table(("side",), lambda row: (row["side"],)),
        "cells": table(("symbol_class", "time_bucket"), lambda row: (row["symbol_class"], row["time_bucket"])),
    }


# --------------------------------------------------------------- calibrate

def shrink(raw: Optional[float], fills: int, *, min_fills: int = MIN_FILLS, shrink_fills: int = SHRINK_FILLS,
           bounds: tuple[float, float] = MULTIPLIER_BOUNDS) -> Optional[float]:
    """Blend ``raw`` with ``shrink_fills`` pseudo-fills at 1.0, capped; ``None`` under ``min_fills``."""
    if raw is None or fills < max(int(min_fills), 1) or not math.isfinite(raw):
        return None
    blended = (fills * raw + shrink_fills * 1.0) / (fills + shrink_fills)
    low, high = bounds
    return round(min(max(blended, low), high), 4)


def fit_cells(rows: list[dict], *, min_fills: int = MIN_FILLS, shrink_fills: int = SHRINK_FILLS,
              bounds: tuple[float, float] = MULTIPLIER_BOUNDS) -> list[dict]:
    """One multiplier per (symbol class, time bucket), plus the class-wide, bucket-wide and global fallbacks."""
    usable = [row for row in rows if row.get("usable")]
    keys = {(row["symbol_class"], row["time_bucket"]) for row in usable}
    keys |= {(cls, ALL) for cls, _ in keys} | {(ALL, bucket) for _, bucket in keys} | {(ALL, ALL)}
    for cls in SYMBOL_CLASSES:
        keys.add((cls, ALL))
    out = []
    for cls, bucket in sorted(keys):
        group = [row for row in usable
                 if (cls == ALL or row["symbol_class"] == cls) and (bucket == ALL or row["time_bucket"] == bucket)]
        modelled = float(sum(row["modelled_slippage_bps"] for row in group))
        raw = round(float(sum(row["slippage_bps"] for row in group)) / modelled, 4) if modelled > 0 else None
        multiplier = shrink(raw, len(group), min_fills=min_fills, shrink_fills=shrink_fills, bounds=bounds)
        out.append({"symbol_class": cls, "time_bucket": bucket, "fills": len(group), "raw": raw,
                    "multiplier": multiplier, "fitted": multiplier is not None})
    return out


@dataclass(frozen=True)
class CostCalibration:
    """Fitted multipliers keyed by (symbol class, time bucket); unfitted cells fall back to coarser ones."""
    multipliers: Mapping[tuple[str, str], float] = field(default_factory=dict)
    as_of: Optional[str] = None
    fills: int = 0
    window: Optional[dict] = None
    version: int = CALIBRATION_VERSION

    def multiplier(self, cls: str, bucket: str) -> float:
        for key in ((cls, bucket), (cls, ALL), (ALL, bucket), (ALL, ALL)):
            value = self.multipliers.get(key)
            if value is not None:
                return float(value)
        return 1.0

    def for_symbol(self, symbol: Any) -> CostMultipliers:
        cls = symbol_class(symbol)
        return CostMultipliers(entry=self.multiplier(cls, ENTRY_BUCKET), exit=self.multiplier(cls, EXIT_BUCKET))

    @property
    def is_identity(self) -> bool:
        return not self.multipliers

    def to_dict(self) -> dict:
        return {"version": self.version, "as_of": self.as_of, "fills": self.fills, "window": self.window,
                "multipliers": [{"symbol_class": cls, "time_bucket": bucket, "multiplier": value}
                                for (cls, bucket), value in sorted(self.multipliers.items())]}

    @classmethod
    def from_dict(cls, payload: Optional[Mapping]) -> Optional["CostCalibration"]:
        if not isinstance(payload, Mapping):
            return None
        multipliers = {}
        for item in payload.get("multipliers") or []:
            value = _f(item.get("multiplier")) if isinstance(item, Mapping) else None
            if value is not None and value > 0:
                multipliers[(str(item.get("symbol_class")), str(item.get("time_bucket")))] = value
        return cls(multipliers=multipliers, as_of=payload.get("as_of"), fills=int(payload.get("fills") or 0),
                   window=payload.get("window"), version=int(payload.get("version") or CALIBRATION_VERSION))

    @classmethod
    def from_cells(cls, cells: Iterable[Mapping], **extra) -> "CostCalibration":
        return cls(multipliers={(c["symbol_class"], c["time_bucket"]): float(c["multiplier"])
                                for c in cells if c.get("fitted") and c.get("multiplier") is not None}, **extra)


# ------------------------------------------------------------------- report

def _stored_fill(row: dict) -> dict:
    keys = ("fill_id", "order_id", "symbol", "side", "symbol_class", "order_kind", "session", "filled_at",
            "time_bucket", "quantity", "fill_price", "decision_price", "slippage_bps", "fee_bps",
            "realised_total_bps", "modelled_slippage_bps", "modelled_total_bps", "gap_bps", "residual", "usable")
    return {key: row.get(key) for key in keys}


def build_calibration(db: Session, *, now: Optional[datetime] = None, sessions: Optional[int] = None,
                      min_fills: Optional[int] = None, loader: Optional[Callable] = None,
                      config: Optional[BarrierConfig] = None) -> dict:
    """Measure the last ``sessions`` sessions of fills and fit the multipliers (nothing stored)."""
    now = _utc(now or datetime.now(UTC))
    sessions = int(sessions if sessions is not None else settings.paper_cost_calibration_lookback_sessions)
    min_fills = int(min_fills if min_fills is not None else settings.paper_cost_calibration_min_fills)
    config = config or BarrierConfig()
    as_of = last_completed_session(now)
    first = sessions_back(as_of, max(sessions - 1, 0))
    start = datetime.combine(first, datetime.min.time(), ET)
    loader = loader or (lambda symbol: daily_frame(db, symbol))
    rows = measure_fills(fill_rows(db, start, now), loader=loader, config=config)
    cells = fit_cells(rows, min_fills=min_fills)
    window = {"sessions": sessions, "first_session": first.isoformat(), "last_session": as_of.isoformat(),
              "start": start.astimezone(UTC).isoformat(), "end": now.isoformat()}
    calibration = CostCalibration.from_cells(cells, as_of=as_of.isoformat(), fills=sum(1 for r in rows if r["usable"]),
                                             window=window)
    stored = rows[-MAX_STORED_FILLS:]
    return {
        "version": CALIBRATION_VERSION, "as_of": as_of.isoformat(), "generated_at": now.isoformat(),
        "window": window,
        "settings": {"min_fills": min_fills, "shrink_fills": SHRINK_FILLS, "bounds": list(MULTIPLIER_BOUNDS),
                     "entry_bucket": ENTRY_BUCKET, "exit_bucket": EXIT_BUCKET, "edge_minutes": EDGE_MINUTES},
        "model": config.to_dict(),
        "fills": {"count": len(rows), "usable": calibration.fills,
                  "first_fill_id": rows[0]["fill_id"] if rows else None,
                  "last_fill_id": rows[-1]["fill_id"] if rows else None,
                  "rows": [_stored_fill(row) for row in stored], "truncated": len(rows) - len(stored)},
        "tables": measurement_tables(rows),
        "cells": cells,
        "calibration": calibration.to_dict(),
        "fitted_cells": sum(1 for cell in cells if cell["fitted"]),
        "method": ("Each paper fill is scored against the decision price its order was reserved at (a protective "
                   "leg against its stop price) plus the reported fee, and against the published volatility-scaled "
                   "cost of the same session from the trusted daily bars before it. Per symbol class and time "
                   "bucket the multiplier is mean realised / mean modelled slippage, shrunk toward 1.0 with "
                   f"{SHRINK_FILLS} pseudo-fills, fitted only with at least {min_fills} fills and capped to "
                   f"[{MULTIPLIER_BOUNDS[0]}, {MULTIPLIER_BOUNDS[1]}]."),
        "paper_only": True, "mutates_strategy_state": False,
    }


def _latest_row(db: Session) -> Optional[AuditLog]:
    return db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION).order_by(
        AuditLog.id.desc()).first()


def latest_calibration_report(db: Session) -> Optional[dict]:
    row = _latest_row(db)
    return (row.payload or {}).get("report") if row else None


def latest_calibration(db: Optional[Session] = None) -> Optional[CostCalibration]:
    """The calibration in force (the latest stored report), or ``None`` when none has been recorded.

    Without a session one is opened and closed here; any failure (no table yet,
    database down) yields ``None`` so a replay falls back to the published model.
    """
    if db is not None:
        report = latest_calibration_report(db)
        return CostCalibration.from_dict(report.get("calibration")) if report else None
    session = None
    try:
        from app.db import session as session_module

        session = session_module.SessionLocal()
        return latest_calibration(session)
    except Exception as exc:  # the replay must never fail for want of a calibration
        logger.warning("cost calibration: latest calibration unavailable (%s): %s",
                       type(exc).__name__, str(exc).splitlines()[0][:160] if str(exc) else "")
        return None
    finally:
        if session is not None:
            session.close()


def record_calibration(db: Session, *, now: Optional[datetime] = None, force: bool = False, **kwargs) -> dict:
    """Store one calibration per completed session (paper only)."""
    now = _utc(now or datetime.now(UTC))
    as_of = last_completed_session(now).isoformat()
    latest = latest_calibration_report(db)
    if latest and latest.get("as_of") == as_of and not force:
        return {"status": "complete", "recorded": False, "as_of": as_of,
                "reason": "cost calibration already recorded for this session", "paper_only": True}
    report = build_calibration(db, now=now, **kwargs)
    overall = report["tables"]["overall"]
    write_audit_log(
        db, event_type=EVENT, action=ACTION, status="recorded",
        message=(f"paper cost calibration through {as_of}: {report['fills']['count']} fills, "
                 f"{report['fills']['usable']} usable, {report['fitted_cells']} cells fitted, "
                 f"residual {overall['residual'] if overall['residual'] is not None else 'n/a'}"),
        payload={"report": report, "paper_only": True},
    )
    db.commit()
    return {"status": "complete", "recorded": True, "as_of": as_of, "fills": report["fills"]["count"],
            "usable": report["fills"]["usable"], "fitted_cells": report["fitted_cells"],
            "residual": overall["residual"], "paper_only": True}


# -------------------------------------------------------------- projections

def _in_force(report: Optional[Mapping]) -> list[dict]:
    return [cell for cell in (report or {}).get("cells") or [] if cell.get("fitted")]


def calibration_view(db: Session) -> dict:
    """``GET /stock-paper/cost-calibration``: the latest calibration and its measurement table."""
    latest = latest_calibration_report(db)
    return {
        "latest": latest,
        "in_force": _in_force(latest),
        "applies_to": {"entry": f"{ENTRY_BUCKET} bucket of the symbol's class (the replay's next-open entry)",
                       "exit": f"{EXIT_BUCKET}-day multiplier of the symbol's class"},
        "lookback_sessions": int(settings.paper_cost_calibration_lookback_sessions),
        "min_fills": int(settings.paper_cost_calibration_min_fills),
        "shrink_fills": SHRINK_FILLS, "bounds": list(MULTIPLIER_BOUNDS),
        "symbol_classes": list(SYMBOL_CLASSES), "time_buckets": list(TIME_BUCKETS), "order_kinds": list(ORDER_KINDS),
        "paper_only": True,
    }


def weekly_report_section(db: Session) -> dict:
    """The ``cost_calibration`` section of the weekly report: realised vs modelled, multiplier in force."""
    latest = latest_calibration_report(db)
    if not latest:
        return {"status": "waiting", "as_of": None, "fills": 0, "usable": 0,
                "detail": "No cost calibration yet; the first runs on Saturday after the week's fills are in.",
                "realised_total_bps": None, "modelled_total_bps": None, "gap_bps": None, "residual": None,
                "multipliers": []}
    overall = (latest.get("tables") or {}).get("overall") or {}
    fitted = _in_force(latest)
    multipliers = [{"symbol_class": c["symbol_class"], "time_bucket": c["time_bucket"],
                    "multiplier": c["multiplier"], "fills": c["fills"]} for c in fitted]
    realised = (overall.get("realised_total_bps") or {}).get("mean")
    modelled = overall.get("modelled_total_bps_mean")
    detail = (f"{latest['fills']['count']} fills over {latest['window']['sessions']} sessions, "
              f"{latest['fills']['usable']} priced against a decision price; "
              + (f"realised {realised:+.1f} bps per side vs {modelled:.1f} modelled"
                 if realised is not None and modelled is not None else "no priced fills yet")
              + f"; {len(fitted)} cells fitted.")
    return {"status": "ok", "as_of": latest.get("as_of"), "window": latest.get("window"),
            "fills": latest["fills"]["count"], "usable": latest["fills"]["usable"],
            "realised_total_bps": realised, "modelled_total_bps": modelled,
            "gap_bps": overall.get("gap_bps_mean"), "residual": overall.get("residual"),
            "multipliers": multipliers, "detail": detail}
