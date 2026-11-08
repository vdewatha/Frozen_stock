"""The yardstick: how every verdict is measured (step 1 of ``reports/Retail bot edge feasibility.md``).

Pure measurement.  Nothing here places an order or changes what a strategy
trades; the experiment engine's ``RULE`` and the discovery gate keep acting on
the cost they always used.  This module adds the readings that the feasibility
report found missing from every failed search, and two pre-registered vetoes.

1. **Cost bracket** (``cost_bracket``).  Every replay is re-priced at 5, 10
   and 25 bps a round trip, at the published cost model
   (``triple_barrier.side_cost_bps`` plus the 1 bp fee a side) and, once a
   paper-calibrated (symbol class, time bucket) cell has at least
   ``CALIBRATED_MIN_FILLS`` fills, at that cell's cost (the model scaled by the
   cell's multiplier).  A flat column is ``gross - cost``; the model and
   calibrated columns charge each trade its own two sides.  ``acting`` is the
   net the replay already used (the decision rule's own cost).  Calibrated
   cells inherit paper's biases (NBBO fills with no price improvement, no
   impact, no auction) and a multiplier floored at half the model, so the
   realised costs read straight from the fills sit beside them
   (``realized_cost_table``).
2. **Turnover** (``turnover``): one-sided turnover as a multiple of equity a
   year, ``trades x slot / years``, with the slot of the replayed book
   (``yardstick_slot_fraction``, 5% of equity a trade); the cost drag at each
   bracket is turnover x round-trip cost.
3. **Short-hold veto** (``short_hold_veto``, pre-registered, ``VETO_RULE``):
   a candidate whose median hold is under 20 sessions is vetoed unless its
   out-of-sample gross expectancy per trade is at least
   ``max(30 bps, 3 x round-trip cost)``; the cost is 10 bps a round trip until
   a measured round trip (``measured_round_trip_bps``, at least 20 usable
   fills a side in the class) replaces it.  The engine's out-of-sample window
   is the last ``OOS_YEARS`` years (the discovery holdout's length; the engine
   has no pristine holdout, so this is the most recent evidence, not an unseen
   one); discovery uses its holdout.
4. **Excess and residual scoring** (``passive_scores``): the candidate's daily
   equal-weight return against a risk-matched passive arm (a static SPY/T-bill
   mix whose SPY weight is the candidate's volatility over SPY's, capped at 1),
   with month-clustered standard errors on the daily active return; and the
   per-trade SPY residual (``net - beta x SPY over the same hold``, beta fitted
   on the trades) with entry-date clustered standard errors
   (``verdict_stats.paired_stats``, the CR0 estimator the multiplicity hurdle
   uses).  T-bills are the 13-week yield (``^IRX``, $0 via yfinance); without
   it the arm's cash leg earns 0 and the reading says so.
5. **Trials per family** (``trial_families``): trials counted per strategy
   family across the discovery library (every hash ever evaluated), the
   experiment engine's candidates and the hand-run research libraries
   (``RESEARCH_TRIALS``); the discovery gate and the engine's multiplicity
   hurdle charge each candidate its family's trials made elsewhere on top of
   their own count, so the deflated Sharpe can only get stricter.

Paper only; live trading is never read or touched.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import numpy as np
import pandas as pd

from app.core.config import settings

logger = logging.getLogger(__name__)
UTC = timezone.utc
EVENT = "yardstick"
ACTION_RUN = "run"

BRACKET_BPS = (5.0, 10.0, 25.0)
FLAT_COLUMNS = tuple(f"{c:g}bps" for c in BRACKET_BPS)
MODEL = "model"
CALIBRATED = "calibrated"
ACTING = "acting"
CALIBRATED_MIN_FILLS = 20

VETO_MIN_HOLD_SESSIONS = 20
VETO_FLOOR_BPS = 30.0
VETO_COST_MULTIPLE = 3.0
VETO_DEFAULT_COST_BPS = 10.0
MEASURED_MIN_FILLS = 20
OOS_YEARS = 2
VETO_RULE = ("Short-hold veto: a candidate whose median hold is under 20 sessions is vetoed unless its "
             "out-of-sample gross expectancy per trade is at least max(30 bps, 3 x round-trip cost), at 10 bps a "
             "round trip until a measured round-trip cost replaces it.")

EXCESS_T_REQUIRED = 2.0
PASSIVE_RULE = ("Excess: daily equal-weight return minus a static SPY/T-bill mix with SPY weight = candidate "
                "volatility / SPY volatility (capped at 1), month-clustered t; residual: per-trade net minus "
                "beta x SPY over the same hold, entry-date clustered t. Each passes at t >= 2 with a positive mean.")
TRADING_DAYS = 252

# Strategy rows -> the discovery family their rule belongs to (a ``rule_<id>``
# row carries its own family in ``parameters["discovery"]``).
STRATEGY_FAMILY = {
    "moving_average_crossover": "trend_following", "macd_momentum": "trend_following", "ensemble": "trend_following",
    "channel_breakout": "breakout_momentum", "trend_pullback": "pullback_in_uptrend",
    "rsi_mean_reversion": "oscillator_reversion", "bollinger_mean_reversion": "bollinger_low",
    "rsi2_pullback": "pullback_in_uptrend", "ibs_mean_reversion": "ibs_reversion",
    "sector_reversal": "sector_reversal", "book": "book_protections",
}
# Hand-run research outside the discovery library and the engine ledger, each
# with the report that counted it.  Library candidates (gap fill, earnings
# drift, news tone, sector reversal rules) are already in the library's hashes
# and are not counted again here.
RESEARCH_TRIALS = (
    {"family": "sector_reversal", "trials": 9, "source": "reports/sector-reversal-20261107.md",
     "what": "3 book orders x 3 ranked reversion strategies (the 8 rule variants are library trials)"},
    {"family": "market_timing_gates", "trials": 36, "source": "reports/vix-gate-20261106.md",
     "what": "36 VIX-gate variants (6 strategies x 6 settings)"},
    {"family": "market_timing_gates", "trials": 22, "source": "reports/regime-gate-20261106.md",
     "what": "22 ADX regime-gate variants"},
    {"family": "book_allocation", "trials": 3, "source": "reports/regime-allocation-20261106.md",
     "what": "regime-aware allocation arms"},
    {"family": "position_sizing", "trials": 5, "source": "reports/position-sizing-20261106.md",
     "what": "volatility-target and fractional-Kelly sizing modes"},
    {"family": "inverse_etf_hedge", "trials": 3, "source": "reports/hedge-20261106.md",
     "what": "SH hedge sizes"},
    {"family": "liquid_universe", "trials": 3, "source": "reports/liquid-universe-20261107.md",
     "what": "three point-in-time liquidity terciles"},
)


# ----------------------------------------------------------------- helpers

def _f(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _r(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None or not math.isfinite(float(value)) else round(float(value), digits)


def _decision_dates(trades: pd.DataFrame) -> pd.Series:
    column = "decision_date" if "decision_date" in trades else ("t0" if "t0" in trades else None)
    if column is None:
        return pd.Series(pd.NaT, index=trades.index)
    return pd.to_datetime(trades[column].astype(str).str[:10], errors="coerce")


def _has(trades: Optional[pd.DataFrame], *columns: str) -> bool:
    return trades is not None and len(trades) > 0 and all(column in trades for column in columns)


def hold_dates(trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """``trades`` with ``entry_date`` / ``exit_date`` (from the bar indices when the replay did not stamp them)."""
    if _has(trades, "entry_date", "exit_date"):
        return trades
    if not _has(trades, "symbol", "entry_idx", "exit_idx"):
        return trades
    entry, exit_ = [], []
    for row in trades.itertuples(index=False):
        frame = frames.get(row.symbol)
        dates = frame["date"].astype(str).str[:10].to_numpy() if frame is not None else None
        if dates is None:
            entry.append(None)
            exit_.append(None)
            continue
        i, j = int(row.entry_idx), int(row.exit_idx)
        entry.append(dates[i] if 0 <= i < len(dates) else None)
        exit_.append(dates[j] if 0 <= j < len(dates) else None)
    return trades.assign(entry_date=entry, exit_date=exit_)


# ------------------------------------------------------------- cost bracket

def calibrated_cells(report: Optional[Mapping], *, min_fills: int = CALIBRATED_MIN_FILLS) -> list[dict]:
    """The fitted calibration cells with at least ``min_fills`` fills (the bracket's calibrated column)."""
    return [cell for cell in (report or {}).get("cells") or []
            if cell.get("fitted") and cell.get("multiplier") is not None and int(cell.get("fills") or 0) >= min_fills]


def _calibration_from_cells(cells: Iterable[Mapping]):
    from app.services.paper_cost_calibration import CostCalibration

    cells = list(cells)
    return CostCalibration.from_cells(cells) if cells else None


def _side_costs(sigma: np.ndarray, adv: np.ndarray, quantity: np.ndarray, config) -> np.ndarray:
    """Vectorised ``triple_barrier.side_cost_bps`` (multiplier 1)."""
    base = float(config.slippage_bps)
    if config.cost_model == "flat":
        return np.full(len(sigma), base)
    with np.errstate(divide="ignore", invalid="ignore"):
        participation = np.where(np.isfinite(adv) & (adv > 0) & (quantity > 0), quantity / adv, 0.0)
    impact = config.impact_coefficient * sigma * np.sqrt(participation)
    return np.minimum(base + config.volatility_cost_fraction * sigma + impact, 5_000.0)


def trade_costs(trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame], *, cells: Optional[Iterable[Mapping]] = None,
                config=None, cache: Optional[dict] = None) -> dict:
    """Per-trade round-trip cost (fraction) under the published model and, when cells qualify, calibrated.

    Recomputed from each symbol's bars before the entry and the exit (the same
    arithmetic as ``triple_barrier.label_events``), so the model column is the
    published model whatever calibration the replay itself used.  ``cache``
    keeps each symbol's causal volatility and volume across calls.
    """
    from app.services.triple_barrier import BarrierConfig, prior_liquidity

    n = len(trades)
    model = np.full(n, np.nan)
    calibrated = np.full(n, np.nan)
    if not _has(trades, "symbol", "entry_idx", "exit_idx"):
        return {"model": model, "calibrated": None}
    config = config or BarrierConfig()
    calibration = _calibration_from_cells(cells or [])
    fee = 2.0 * float(config.fees_bps)
    cache = cache if cache is not None else {}
    symbols = trades["symbol"].astype(str).to_numpy()
    entry_idx = pd.to_numeric(trades["entry_idx"], errors="coerce").fillna(-1).astype(int).to_numpy()
    exit_idx = pd.to_numeric(trades["exit_idx"], errors="coerce").fillna(-1).astype(int).to_numpy()
    for symbol in np.unique(symbols):
        frame = frames.get(symbol)
        if frame is None or not len(frame):
            continue
        key = (symbol, len(frame), str(frame["date"].iloc[-1]))
        if key not in cache:
            sigma, adv = prior_liquidity(frame.reset_index(drop=True), config.volatility_lookback)
            cache[key] = (np.asarray(sigma, float), np.asarray(adv, float),
                          pd.to_numeric(frame["open"], errors="coerce").to_numpy(float))
        sigma, adv, opens = cache[key]
        rows = np.flatnonzero(symbols == symbol)
        i, j = entry_idx[rows], exit_idx[rows]
        ok = (i >= 0) & (i < len(opens)) & (j >= 0) & (j < len(opens))
        rows, i, j = rows[ok], i[ok], j[ok]
        ok = opens[i] > 0
        rows, i, j = rows[ok], i[ok], j[ok]
        if not len(rows):
            continue
        quantity = config.position_notional / opens[i]
        entry = _side_costs(sigma[i], adv[i], quantity, config)
        exit_ = _side_costs(sigma[j], adv[j], quantity, config)
        model[rows] = (entry + exit_ + fee) / 10_000
        if calibration is not None:
            m = calibration.for_symbol(symbol)
            calibrated[rows] = (entry * m.entry + exit_ * m.exit + fee) / 10_000
    return {"model": model, "calibrated": calibrated if calibration is not None else None}


def _clusters(dates: pd.Series) -> list[str]:
    return dates.dt.to_period("M").astype(str).tolist()


def _stats(values: np.ndarray, months: list[str], *, ci: bool) -> dict:
    from app.services.strategy_scorecard import trade_statistics

    keep = np.isfinite(values)
    values = values[keep]
    months = [m for m, k in zip(months, keep) if k]
    if not len(values):
        return {"trades": 0, "expectancy_bps": None, "expectancy_ci_bps": None, "total_profit_bps": 0.0}
    if ci:
        stats = trade_statistics(values, clusters=months)
        mean, interval = stats["expectancy_bps"], stats.get("expectancy_ci_bps")
    else:
        mean, interval = round(float(values.mean()) * 10_000, 2), None
    return {"trades": int(len(values)), "expectancy_bps": mean, "expectancy_ci_bps": interval,
            "total_profit_bps": round(float(len(values) * mean), 1) if mean is not None else 0.0}


def cost_bracket(trades: Optional[pd.DataFrame], frames: Mapping[str, pd.DataFrame], *,
                 windows: Mapping[str, Optional[tuple]], cells: Optional[Iterable[Mapping]] = None,
                 ci_windows: Optional[Iterable[str]] = None, cache: Optional[dict] = None) -> dict:
    """Per window and cost column: trades, net expectancy (month-clustered CI), total profit.

    ``windows`` maps a name to ``(start, end)`` decision dates (either may be
    None; end exclusive).  Flat columns shift the gross bootstrap by the cost,
    which is exact for a constant charge; model, calibrated and acting each
    get their own bootstrap.  ``ci_windows`` limits the bootstraps (default all).
    """
    cells = list(cells or [])
    if not _has(trades, "gross_return", "net_return"):
        return {"status": "unavailable", "reason": "the replay carries no gross and net returns per trade"}
    dates = _decision_dates(trades)
    gross = pd.to_numeric(trades["gross_return"], errors="coerce").to_numpy(float)
    net = pd.to_numeric(trades["net_return"], errors="coerce").to_numpy(float)
    costs = trade_costs(trades, frames, cells=cells, cache=cache)
    columns = [*FLAT_COLUMNS, MODEL] + ([CALIBRATED] if costs["calibrated"] is not None else []) + [ACTING]
    ci_windows = set(windows if ci_windows is None else ci_windows)
    out: dict = {"status": "ok", "columns": columns, "costs_bps": {c: v for c, v in zip(FLAT_COLUMNS, BRACKET_BPS)},
                 "windows": {}}
    model_cost = costs["model"]
    out["costs_bps"][MODEL] = _r(float(np.nanmean(model_cost)) * 10_000) if np.isfinite(model_cost).any() else None
    if costs["calibrated"] is not None:
        out["costs_bps"][CALIBRATED] = _r(float(np.nanmean(costs["calibrated"])) * 10_000) \
            if np.isfinite(costs["calibrated"]).any() else None
        out["calibrated_cells"] = [{k: c.get(k) for k in ("symbol_class", "time_bucket", "multiplier", "fills")}
                                   for c in cells]
    for name, bounds in windows.items():
        start, end = bounds if bounds else (None, None)
        mask = np.ones(len(trades), dtype=bool)
        if start is not None:
            mask &= (dates >= pd.Timestamp(start)).to_numpy()
        if end is not None:
            mask &= (dates < pd.Timestamp(end)).to_numpy()
        months = _clusters(dates[mask])
        ci = name in ci_windows
        gross_stats = _stats(gross[mask], months, ci=ci)
        row: dict = {"gross": gross_stats}
        for column, cost in zip(FLAT_COLUMNS, BRACKET_BPS):
            mean = gross_stats["expectancy_bps"]
            interval = gross_stats["expectancy_ci_bps"]
            shifted = None if mean is None else round(mean - cost, 2)
            row[column] = {"trades": gross_stats["trades"], "expectancy_bps": shifted,
                           "expectancy_ci_bps": [round(v - cost, 2) for v in interval] if interval else None,
                           "total_profit_bps": round(gross_stats["trades"] * shifted, 1) if shifted is not None else 0.0}
        row[MODEL] = _stats(gross[mask] - model_cost[mask], months, ci=ci)
        if costs["calibrated"] is not None:
            row[CALIBRATED] = _stats(gross[mask] - costs["calibrated"][mask], months, ci=ci)
        row[ACTING] = _stats(net[mask], months, ci=ci)
        out["windows"][name] = row
    return out


def bracket_decisions(rows: Mapping[str, Mapping[str, dict]], brackets: Mapping[str, Mapping], decide: Callable) -> dict:
    """Which label the engine's rule would adopt at each bracket column (drawdowns kept from the acting replay)."""
    columns = None
    for bracket in brackets.values():
        if (bracket or {}).get("status") == "ok":
            columns = bracket["columns"]
            break
    if not columns or any((brackets.get(label) or {}).get("status") != "ok" for label in rows):
        return {}
    out = {}
    for column in columns:
        if column == ACTING:
            continue
        priced = {}
        for label, windows in rows.items():
            priced[label] = {}
            for window, metrics in windows.items():
                cell = ((brackets[label].get("windows") or {}).get(window) or {}).get(column) or {}
                priced[label][window] = {**metrics, "expectancy_bps": cell.get("expectancy_bps"),
                                         "total_profit_bps": cell.get("total_profit_bps", 0.0)}
        try:
            out[column] = decide(priced).get("adopted")
        except Exception as exc:  # a reading only; never stops the run
            out[column] = f"unavailable: {exc.__class__.__name__}"
    return out


# ------------------------------------------------------------------ turnover

def turnover(trades: Optional[pd.DataFrame], *, start, end, slot_fraction: Optional[float] = None) -> dict:
    """One-sided turnover per year as a multiple of equity: trades x slot / years (and the drag at each cost)."""
    slot = float(settings.yardstick_slot_fraction if slot_fraction is None else slot_fraction)
    count = 0 if trades is None else int(len(trades))
    if start is None or end is None:
        return {"status": "unavailable", "trades": count, "slot_fraction": slot}
    if count and ("decision_date" in trades or "t0" in trades):
        # Only the trades decided inside the span count toward its turnover.
        dates = _decision_dates(trades)
        count = int(((dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))).sum())
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1e-9)
    per_year = count * slot / years
    return {"status": "ok", "trades": count, "years": round(years, 2), "trades_per_year": round(count / years, 1),
            "slot_fraction": slot, "one_sided_turnover_per_year": round(per_year, 2),
            "cost_drag_pct_per_year": {column: round(per_year * cost / 100, 2)
                                       for column, cost in zip(FLAT_COLUMNS, BRACKET_BPS)}}


def span(frames: Mapping[str, pd.DataFrame], symbols: Optional[Iterable[str]] = None, *, start=None, end=None) -> tuple:
    """(first, last) date of the replayed history (clipped to ``start`` / ``end``)."""
    names = list(symbols) if symbols is not None else list(frames)
    firsts, lasts = [], []
    for name in names:
        frame = frames.get(name)
        if frame is None or not len(frame):
            continue
        firsts.append(pd.Timestamp(str(frame["date"].iloc[0])[:10]))
        lasts.append(pd.Timestamp(str(frame["date"].iloc[-1])[:10]))
    if not firsts:
        return None, None
    first, last = min(firsts), max(lasts)
    if start is not None:
        first = max(first, pd.Timestamp(start))
    if end is not None:
        last = min(last, pd.Timestamp(end))
    return first, last


# ---------------------------------------------------------- short-hold veto

def veto_cost_bps(measured: Optional[Mapping] = None, symbol_classes: Optional[Iterable[str]] = None) -> dict:
    """The round-trip cost the veto uses: the measured one for the candidate's classes, else 10 bps."""
    measured = measured or {}
    values = [measured.get(cls) for cls in (symbol_classes or ())]
    values = [v for v in values if v is not None]
    if not values and symbol_classes is None and measured.get("all") is not None:
        values = [measured["all"]]
    if values:
        return {"cost_bps": round(float(max(values)), 2), "source": "measured"}
    return {"cost_bps": VETO_DEFAULT_COST_BPS, "source": "default_10bps"}


def short_hold_veto(trades: Optional[pd.DataFrame], *, oos_start, oos_end=None, cost_bps: float = VETO_DEFAULT_COST_BPS,
                    cost_source: str = "default_10bps") -> dict:
    """The pre-registered veto on one candidate's replayed trades (``VETO_RULE``)."""
    threshold = max(VETO_FLOOR_BPS, VETO_COST_MULTIPLE * float(cost_bps))
    base = {"rule": VETO_RULE, "min_hold_sessions": VETO_MIN_HOLD_SESSIONS, "required_gross_bps": round(threshold, 2),
            "cost_bps": round(float(cost_bps), 2), "cost_source": cost_source,
            "oos_start": str(pd.Timestamp(oos_start).date()) if oos_start is not None else None}
    if not _has(trades, "holding_days", "gross_return"):
        return base | {"status": "unavailable", "vetoed": False,
                       "reason": "the replay carries no holding period and gross return per trade"}
    hold = pd.to_numeric(trades["holding_days"], errors="coerce").to_numpy(float)
    median_hold = float(np.nanmedian(hold)) if np.isfinite(hold).any() else None
    dates = _decision_dates(trades)
    mask = np.ones(len(trades), dtype=bool) if oos_start is None else (dates >= pd.Timestamp(oos_start)).to_numpy()
    if oos_end is not None:
        mask &= (dates < pd.Timestamp(oos_end)).to_numpy()
    gross = pd.to_numeric(trades["gross_return"], errors="coerce").to_numpy(float)[mask]
    gross = gross[np.isfinite(gross)]
    oos_bps = round(float(gross.mean()) * 10_000, 2) if len(gross) else None
    short = median_hold is not None and median_hold < VETO_MIN_HOLD_SESSIONS
    if not short:
        vetoed, reason = False, f"median hold {median_hold:g} sessions >= {VETO_MIN_HOLD_SESSIONS}" \
            if median_hold is not None else "no holding periods"
    elif oos_bps is None:
        vetoed, reason = True, f"median hold {median_hold:g} sessions and no out-of-sample trades"
    elif oos_bps < threshold:
        vetoed, reason = True, (f"median hold {median_hold:g} sessions; out-of-sample gross {oos_bps:+.1f} bps a trade "
                                f"< {threshold:.1f} required")
    else:
        vetoed, reason = False, (f"median hold {median_hold:g} sessions but out-of-sample gross {oos_bps:+.1f} bps "
                                 f">= {threshold:.1f}")
    return base | {"status": "ok", "vetoed": vetoed, "reason": reason, "median_hold_sessions": median_hold,
                   "oos_trades": int(len(gross)), "oos_gross_bps": oos_bps}


# ---------------------------------------------------- excess and residual

def tbill_daily(irx: Optional[pd.DataFrame], dates: pd.DatetimeIndex) -> tuple[np.ndarray, bool]:
    """Daily T-bill return on ``dates`` from the 13-week yield (percent, ``date, close``); zeros when unknown."""
    if irx is None or not len(irx) or "close" not in irx:
        return np.zeros(len(dates)), False
    series = pd.Series(pd.to_numeric(irx["close"], errors="coerce").to_numpy(float),
                       index=pd.to_datetime(irx["date"].astype(str).str[:10])).sort_index()
    series = series[~series.index.duplicated()].reindex(dates, method="ffill")
    rate = series.fillna(0.0).to_numpy(float) / 100.0 / TRADING_DAYS
    return rate, True


def _daily_spy(spy: pd.DataFrame, dates: pd.DatetimeIndex) -> np.ndarray:
    close = pd.Series(pd.to_numeric(spy["close"], errors="coerce").to_numpy(float),
                      index=pd.to_datetime(spy["date"].astype(str).str[:10]))
    close = close[~close.index.duplicated()].sort_index()
    return close.pct_change().reindex(dates).fillna(0.0).to_numpy(float)


def clustered_t(values: np.ndarray, clusters: Iterable) -> dict:
    """Mean and CR0 clustered t of ``values`` (``verdict_stats.paired_stats``; values in fractions)."""
    from app.services.verdict_stats import paired_stats

    frame = pd.DataFrame({"value_bps": np.asarray(values, dtype=float) * 10_000,
                          "entry_date": [str(c) for c in clusters]})
    frame = frame[np.isfinite(frame["value_bps"])]
    stats = paired_stats(frame)
    return {"observations": stats.get("decisions"), "clusters": stats.get("clusters"),
            "mean_bps": stats.get("mean_bps"), "t_stat": stats.get("t_stat")}


def portfolio_series(trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame],
                     symbols: Optional[Iterable[str]] = None) -> pd.Series:
    """Equal-weight daily return across the candidate's symbols, each fully in when it holds (0 when flat)."""
    from app.services.strategy_discovery import _marked_returns

    names = sorted(set(symbols) if symbols is not None else set(trades["symbol"]) if _has(trades, "symbol") else set())
    series = []
    by_symbol = dict(tuple(trades.groupby("symbol"))) if _has(trades, "symbol") else {}
    for name in names:
        frame = frames.get(name)
        if frame is None or not len(frame):
            continue
        group = by_symbol.get(name)
        index = pd.to_datetime(frame["date"].astype(str).str[:10])
        values = (_marked_returns(group, pd.to_numeric(frame["close"], errors="coerce").to_numpy(float))
                  if group is not None and len(group) else np.zeros(len(frame)))
        series.append(pd.Series(values, index=index))
    if not series:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    return pd.concat(series, axis=1).mean(axis=1, skipna=True).sort_index()


def passive_scores(trades: Optional[pd.DataFrame], frames: Mapping[str, pd.DataFrame], spy: Optional[pd.DataFrame], *,
                   windows: Mapping[str, Optional[tuple]], irx: Optional[pd.DataFrame] = None,
                   portfolio: Optional[pd.Series] = None, symbols: Optional[Iterable[str]] = None) -> dict:
    """Excess over a volatility-matched SPY/T-bill arm (daily) and the per-trade SPY residual, per window."""
    from app.services.verdict_stats import hold_returns

    if spy is None or trades is None or not len(trades) or "net_return" not in trades:
        return {"status": "unavailable", "reason": "no trades or no SPY history", "rule": PASSIVE_RULE}
    trades = hold_dates(trades, frames)
    if portfolio is None:
        portfolio = portfolio_series(trades, frames, symbols)
    dates = _decision_dates(trades)
    spy_hold = hold_returns(trades, spy) if _has(trades, "entry_date", "exit_date") else np.full(len(trades), np.nan)
    net = pd.to_numeric(trades["net_return"], errors="coerce").to_numpy(float)
    entry_dates = trades["entry_date"].astype(str).str[:10].tolist() if "entry_date" in trades else \
        [str(d)[:10] for d in dates]
    out = {"status": "ok", "rule": PASSIVE_RULE, "windows": {}}
    tbill_known = None
    for name, bounds in windows.items():
        start, end = bounds if bounds else (None, None)
        row: dict = {}
        # Daily: the candidate's equal-weight book against the risk-matched passive arm.
        series = portfolio
        if start is not None:
            series = series.loc[series.index >= pd.Timestamp(start)]
        if end is not None:
            series = series.loc[series.index < pd.Timestamp(end)]
        if len(series) > 20:
            idx = pd.DatetimeIndex(series.index)
            spy_daily = _daily_spy(spy, idx)
            bills, tbill_known = tbill_daily(irx, idx)
            vol_c = float(np.std(series.to_numpy(float), ddof=1))
            vol_s = float(np.std(spy_daily, ddof=1))
            weight = min(1.0, vol_c / vol_s) if vol_s > 0 else 0.0
            passive = weight * spy_daily + (1.0 - weight) * bills
            active = series.to_numpy(float) - passive
            t = clustered_t(active, idx.to_period("M").astype(str))
            years = len(series) / TRADING_DAYS
            growth_c = float(np.prod(1 + series.to_numpy(float)))
            growth_p = float(np.prod(1 + passive))
            row["excess_vs_passive"] = {
                "sessions": int(len(series)), "spy_weight": round(weight, 4),
                "candidate_return_pct_per_year": _r((growth_c ** (1 / years) - 1) * 100) if years > 0 else None,
                "passive_return_pct_per_year": _r((growth_p ** (1 / years) - 1) * 100) if years > 0 else None,
                "mean_active_bps_per_day": t["mean_bps"],
                "active_pct_per_year": _r((t["mean_bps"] or 0.0) * TRADING_DAYS / 100) if t["mean_bps"] is not None else None,
                "t_stat_month_clustered": t["t_stat"], "clusters": t["clusters"],
                "passes": bool((t["mean_bps"] or 0) > 0 and (t["t_stat"] or 0) >= EXCESS_T_REQUIRED)}
        else:
            row["excess_vs_passive"] = {"sessions": int(len(series)), "passes": False,
                                        "reason": "fewer than 21 sessions"}
        # Per trade: net minus beta x SPY over the same hold.
        mask = np.isfinite(net) & np.isfinite(spy_hold)
        if start is not None:
            mask &= (dates >= pd.Timestamp(start)).to_numpy()
        if end is not None:
            mask &= (dates < pd.Timestamp(end)).to_numpy()
        if mask.sum() >= 10:
            x, y = spy_hold[mask], net[mask]
            var = float(np.var(x, ddof=1))
            beta = float(np.cov(x, y, ddof=1)[0, 1] / var) if var > 0 else 0.0
            residual = y - beta * x
            t = clustered_t(residual, [d for d, k in zip(entry_dates, mask) if k])
            row["spy_residual"] = {"trades": int(mask.sum()), "beta": round(beta, 4), "mean_bps": t["mean_bps"],
                                   "t_stat_date_clustered": t["t_stat"], "clusters": t["clusters"],
                                   "passes": bool((t["mean_bps"] or 0) > 0 and (t["t_stat"] or 0) >= EXCESS_T_REQUIRED)}
        else:
            row["spy_residual"] = {"trades": int(mask.sum()), "passes": False, "reason": "fewer than 10 trades"}
        row["passes"] = bool(row["excess_vs_passive"]["passes"] and row["spy_residual"]["passes"])
        out["windows"][name] = row
    out["tbill"] = "13-week T-bill yield (^IRX)" if tbill_known else "unavailable: the arm's cash leg earns 0"
    return out


IRX_FILE = "IRX_close.csv"
IRX_FRESH_FOR = timedelta(days=7)


def load_irx(directory: Optional[Path] = None, *, download: bool = True, now: Optional[datetime] = None) -> Optional[pd.DataFrame]:
    """The 13-week T-bill yield history (``date, close`` in percent), cached weekly; None when unavailable."""
    try:
        from app.services.meta_label_learning import history_dir

        directory = Path(directory or history_dir())
        path = directory / IRX_FILE
        now = now or datetime.now(UTC)
        fresh = path.exists() and now - datetime.fromtimestamp(path.stat().st_mtime, UTC) < IRX_FRESH_FOR
        if download and not fresh:
            import yfinance as yf  # research-only dependency, $0

            frame = yf.download("^IRX", start="1990-01-01", progress=False, auto_adjust=False, threads=False)
            if isinstance(frame.columns, pd.MultiIndex):
                frame.columns = frame.columns.get_level_values(0)
            frame = frame.reset_index()
            frame.columns = [str(c).lower() for c in frame.columns]
            if len(frame) and "close" in frame:
                directory.mkdir(parents=True, exist_ok=True)
                frame[["date", "close"]].to_csv(path, index=False)
        if not path.exists():
            return None
        frame = pd.read_csv(path)
        frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
        return frame.dropna(subset=["close"])
    except Exception as exc:  # the reading degrades to a zero-yield cash leg, never fails the run
        logger.warning("yardstick: T-bill history unavailable: %s", exc)
        return None


# ------------------------------------------------------------- one candidate

def score_candidate(trades: Optional[pd.DataFrame], frames: Mapping[str, pd.DataFrame], *, spy: Optional[pd.DataFrame],
                    windows: Mapping[str, Optional[tuple]], passive_windows: Optional[Mapping[str, Optional[tuple]]] = None,
                    ci_windows: Optional[Iterable[str]] = None, turnover_span: tuple = (None, None), oos_start=None,
                    oos_end=None, veto_cost: Optional[Mapping] = None, cells: Optional[Iterable[Mapping]] = None,
                    irx: Optional[pd.DataFrame] = None, portfolio: Optional[pd.Series] = None,
                    symbols: Optional[Iterable[str]] = None, cache: Optional[dict] = None) -> dict:
    """Every yardstick reading for one candidate's replayed trades; each part degrades to ``unavailable``."""
    veto_cost = dict(veto_cost or {"cost_bps": VETO_DEFAULT_COST_BPS, "source": "default_10bps"})
    out: dict = {}
    parts = {
        "bracket": lambda: cost_bracket(trades, frames, windows=windows, cells=cells, ci_windows=ci_windows,
                                        cache=cache),
        "turnover": lambda: turnover(trades, start=turnover_span[0], end=turnover_span[1]),
        "short_hold_veto": lambda: short_hold_veto(trades, oos_start=oos_start, oos_end=oos_end,
                                                   cost_bps=float(veto_cost["cost_bps"]),
                                                   cost_source=str(veto_cost["source"])),
        "passive": lambda: passive_scores(trades, frames, spy, windows=passive_windows or windows, irx=irx,
                                          portfolio=portfolio, symbols=symbols),
    }
    for name, compute in parts.items():
        try:
            out[name] = compute()
        except Exception as exc:  # a reading must never stop the run it is reported beside
            logger.warning("yardstick: %s unavailable: %s", name, exc)
            out[name] = {"status": "unavailable", "error": f"{exc.__class__.__name__}: {exc}"[:200],
                         **({"vetoed": False} if name == "short_hold_veto" else {})}
    return out


def compact(score: Optional[Mapping], window: str, passive_window: Optional[str] = None) -> dict:
    """One line of a candidate's yardstick: net per column, turnover, veto, excess and residual."""
    score = score or {}
    bracket = ((score.get("bracket") or {}).get("windows") or {}).get(window) or {}
    passive = ((score.get("passive") or {}).get("windows") or {}).get(passive_window or window) or {}
    veto = score.get("short_hold_veto") or {}
    excess, residual = passive.get("excess_vs_passive") or {}, passive.get("spy_residual") or {}
    return {"trades": (bracket.get(ACTING) or {}).get("trades"),
            "net_bps": {column: (bracket.get(column) or {}).get("expectancy_bps")
                        for column in (*FLAT_COLUMNS, MODEL, CALIBRATED, ACTING) if column in bracket},
            "net_ci_bps": {column: (bracket.get(column) or {}).get("expectancy_ci_bps")
                           for column in (*FLAT_COLUMNS, MODEL, CALIBRATED, ACTING) if column in bracket},
            "gross_bps": (bracket.get("gross") or {}).get("expectancy_bps"),
            "turnover_per_year": (score.get("turnover") or {}).get("one_sided_turnover_per_year"),
            "vetoed": bool(veto.get("vetoed")), "veto_reason": veto.get("reason"),
            "median_hold_sessions": veto.get("median_hold_sessions"), "oos_gross_bps": veto.get("oos_gross_bps"),
            "excess_pct_per_year": excess.get("active_pct_per_year"), "excess_t": excess.get("t_stat_month_clustered"),
            "spy_weight": excess.get("spy_weight"), "residual_bps": residual.get("mean_bps"),
            "residual_t": residual.get("t_stat_date_clustered"), "beta": residual.get("beta"),
            "excess_passes": bool(passive.get("passes"))}


# ------------------------------------------------------------ measured cost

def measured_round_trip_bps(report: Optional[Mapping], *, min_fills: int = MEASURED_MIN_FILLS) -> dict:
    """Measured round trip per symbol class: twice the realised per-side cost, when >= ``min_fills`` priced fills."""
    tables = (report or {}).get("tables") or {}
    out: dict = {}
    for row in tables.get("by_symbol_class") or []:
        mean = ((row.get("realised_total_bps") or {}).get("mean"))
        if mean is not None and int(row.get("priced") or 0) >= min_fills:
            out[row["symbol_class"]] = round(2.0 * float(mean), 2)
    overall = tables.get("overall") or {}
    mean = (overall.get("realised_total_bps") or {}).get("mean")
    if mean is not None and int(overall.get("priced") or 0) >= min_fills:
        out["all"] = round(2.0 * float(mean), 2)
    return out


def classes_of(symbols: Iterable[str]) -> list[str]:
    from app.services.paper_cost_calibration import symbol_class

    return sorted({symbol_class(s) for s in symbols})


def realized_cost_table(report: Optional[Mapping], probes: Optional[list[dict]] = None) -> dict:
    """Realised costs read straight from the paper fills (no shrinkage, no floor), with the probes' round trips."""
    tables = (report or {}).get("tables") or {}

    def compact(row: Mapping) -> dict:
        realised = row.get("realised_total_bps") or {}
        return {"fills": row.get("fills"), "priced": row.get("priced"),
                "realised_per_side_bps": {k: realised.get(k) for k in ("mean", "median", "p90")},
                "realised_round_trip_bps": round(2 * realised["mean"], 2) if realised.get("mean") is not None else None,
                "modelled_per_side_bps": row.get("modelled_total_bps_mean"), "residual": row.get("residual")}

    probe_rows = [p for p in probes or [] if p.get("round_trip_bps") is not None]
    by_symbol: dict[str, list[float]] = {}
    for probe in probe_rows:
        by_symbol.setdefault(probe["symbol"], []).append(float(probe["round_trip_bps"]))
    return {
        "as_of": (report or {}).get("as_of"), "window": (report or {}).get("window"),
        "overall": compact(tables.get("overall") or {}) if tables else None,
        "by_symbol_class": [{"symbol_class": row.get("symbol_class"), **compact(row)}
                            for row in tables.get("by_symbol_class") or []],
        "by_symbol": [{"symbol": row.get("symbol"), **compact(row)} for row in tables.get("by_symbol") or []],
        "probes": {"round_trips": len(probe_rows),
                   "by_symbol": {s: {"round_trips": len(v), "mean_bps": round(float(np.mean(v)), 2),
                                     "median_bps": round(float(np.median(v)), 2)} for s, v in sorted(by_symbol.items())}},
        "note": ("Read from the paper fills against their decision price (delayed SIP where recorded) plus the "
                 "reported fee: no shrinkage and no multiplier floor. Paper fills at the NBBO with no price "
                 "improvement, impact or auction, so these are a paper measurement, not a live one."),
    }


# ------------------------------------------------------------ trial counts

def family_of_strategy(strategy_type: Optional[str], parameters: Optional[Mapping] = None) -> str:
    discovery = (parameters or {}).get("discovery") or {}
    if discovery.get("family"):
        return str(discovery["family"])
    return STRATEGY_FAMILY.get(str(strategy_type or ""), str(strategy_type or "unknown"))


def discovery_family_counts(trials_payload: Optional[Mapping]) -> dict:
    """Trials per family in the discovery library (``discovery_trials`` row's ``hash_families``)."""
    payload = trials_payload or {}
    families = payload.get("hash_families") or {}
    counts: dict[str, int] = {}
    for digest in payload.get("hashes") or []:
        family = families.get(digest) or "unattributed"
        counts[family] = counts.get(family, 0) + 1
    return counts


def engine_family_counts(history: Iterable[Mapping], families: Optional[Mapping[str, str]] = None) -> dict:
    """Candidates tried by the experiment engine (baseline excluded), per strategy family."""
    counts: dict[str, int] = {}
    for item in history:
        strategy_type = item.get("strategy_type")
        if not strategy_type:
            continue
        family = (families or {}).get(strategy_type) or family_of_strategy(strategy_type)
        setups = item.get("setups") or item.get("rows") or {}
        counts[family] = counts.get(family, 0) + max(len(setups) - 1, 0)
    return counts


def research_family_counts() -> dict:
    counts: dict[str, int] = {}
    for item in RESEARCH_TRIALS:
        counts[item["family"]] = counts.get(item["family"], 0) + int(item["trials"])
    return counts


def combine_families(discovery: Mapping[str, int], engine: Mapping[str, int], research: Mapping[str, int]) -> dict:
    names = sorted(set(discovery) | set(engine) | set(research))
    return {name: {"discovery": int(discovery.get(name, 0)), "engine": int(engine.get(name, 0)),
                   "research": int(research.get(name, 0)),
                   "total": int(discovery.get(name, 0)) + int(engine.get(name, 0)) + int(research.get(name, 0))}
            for name in names}


def trial_families(db) -> dict:
    """Trials per family across discovery, the engine and the research libraries (read-only)."""
    from app.services.experiment_engine import experiment_history
    from app.services.strategy_discovery import latest_trials_payload

    try:
        payload = latest_trials_payload(db)
    except Exception as exc:
        db.rollback()
        logger.warning("yardstick: discovery trials unavailable: %s", exc)
        payload = {}
    try:
        history = experiment_history(db)
    except Exception as exc:
        db.rollback()
        logger.warning("yardstick: engine history unavailable: %s", exc)
        history = []
    families = combine_families(discovery_family_counts(payload), engine_family_counts(history),
                                research_family_counts())
    return {"families": families, "total": sum(row["total"] for row in families.values()),
            "research_sources": list(RESEARCH_TRIALS),
            "note": ("The discovery gate charges each candidate its library count plus its family's engine and "
                     "research trials; the engine's hurdle charges its own count plus the family's discovery and "
                     "research trials. Both are only ever stricter than before.")}


def external_family_trials(db, *, exclude: str) -> dict:
    """Per family, the trials made outside ``exclude`` (``"discovery"`` or ``"engine"``); empty when unreadable."""
    try:
        families = trial_families(db)["families"]
    except Exception as exc:
        logger.warning("yardstick: family trials unavailable: %s", exc)
        return {}
    keep = [k for k in ("discovery", "engine", "research") if k != exclude]
    return {name: sum(int(row.get(k, 0)) for k in keep) for name, row in families.items()}


# --------------------------------------------------------------- ledger

def latest_run(db) -> Optional[dict]:
    from app.models import AuditLog

    row = db.query(AuditLog).filter(AuditLog.event_type == EVENT, AuditLog.action == ACTION_RUN).order_by(
        AuditLog.id.desc()).first()
    return dict(row.payload or {}) if row is not None else None


def record_yardstick(db, *, now: Optional[datetime] = None) -> dict:
    """Weekly: the realised-cost table, the measured round trips, the family counts and the dividend check (audit row)."""
    from app.services.audit import write_audit_log
    from app.services.cost_probes import probe_round_trips
    from app.services.paper_cost_calibration import build_calibration, latest_calibration_report

    now = now or datetime.now(UTC)
    try:
        report = build_calibration(db, now=now)
    except Exception as exc:  # fall back to the stored calibration's tables
        db.rollback()
        logger.warning("yardstick: fill measurement unavailable, using the stored calibration: %s", exc)
        report = latest_calibration_report(db)
    try:
        probes = probe_round_trips(db)
    except Exception as exc:
        db.rollback()
        logger.warning("yardstick: probe round trips unavailable: %s", exc)
        probes = []
    payload = {"as_of": now.isoformat(), "realized_costs": realized_cost_table(report, probes),
               "measured_round_trip_bps": measured_round_trip_bps(report),
               "calibrated_cells": [{k: c.get(k) for k in ("symbol_class", "time_bucket", "multiplier", "fills")}
                                    for c in calibrated_cells(report)],
               "trial_families": trial_families(db), "paper_only": True}
    write_audit_log(db, event_type=EVENT, entity_type="yardstick", action=ACTION_RUN, status="complete",
                    message=(f"yardstick: {len(payload['calibrated_cells'])} calibrated cells with >= "
                             f"{CALIBRATED_MIN_FILLS} fills, measured round trips {payload['measured_round_trip_bps']}"),
                    payload=_json(payload))
    db.commit()
    return {"status": "complete", **{k: payload[k] for k in ("measured_round_trip_bps", "calibrated_cells")},
            "paper_only": True}


def _json(value: Any) -> Any:
    import json

    from app.services.experiment_engine import _clean

    return json.loads(json.dumps(_clean(value), default=str))


def measured_costs(db) -> dict:
    """The measured round trips in force for the veto (latest yardstick row, else the stored calibration)."""
    latest = None
    try:
        latest = latest_run(db)
    except Exception as exc:
        db.rollback()
        logger.warning("yardstick: latest run unavailable: %s", exc)
    if latest and latest.get("measured_round_trip_bps") is not None:
        return dict(latest["measured_round_trip_bps"])
    try:
        from app.services.paper_cost_calibration import latest_calibration_report

        return measured_round_trip_bps(latest_calibration_report(db))
    except Exception as exc:
        db.rollback()
        logger.warning("yardstick: calibration report unavailable: %s", exc)
        return {}


def current_cells(db) -> list[dict]:
    try:
        from app.services.paper_cost_calibration import latest_calibration_report

        return calibrated_cells(latest_calibration_report(db))
    except Exception as exc:
        db.rollback()
        logger.warning("yardstick: calibration cells unavailable: %s", exc)
        return []


def yardstick_view(db) -> dict:
    """``GET /stock-paper/yardstick``: the rules, the bracket, measured costs, family counts, dividends, probes."""
    from app.services import cost_probes, dividend_check, sleeve_guardrail

    latest = None
    try:
        latest = latest_run(db)
    except Exception as exc:
        db.rollback()
        logger.warning("yardstick: latest run unavailable: %s", exc)

    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # the view renders without any one section
            db.rollback()
            return {"status": "unavailable", "error": f"{exc.__class__.__name__}: {exc}"}

    return {
        "rules": {"bracket_bps": list(BRACKET_BPS), "calibrated_min_fills": CALIBRATED_MIN_FILLS,
                  "short_hold_veto": VETO_RULE, "short_hold_veto_enabled": bool(settings.yardstick_short_hold_veto),
                  "passive": PASSIVE_RULE, "slot_fraction": float(settings.yardstick_slot_fraction),
                  "oos_years_engine": OOS_YEARS, "sleeve_guardrail": sleeve_guardrail.RULE},
        "latest": latest,
        "measured_round_trip_bps": safe(measured_costs, db),
        "calibrated_cells": safe(current_cells, db),
        "trial_families": safe(trial_families, db),
        "dividends": safe(dividend_check.dividend_status, db),
        "probes": safe(cost_probes.probe_status, db),
        "monthly_sleeves": safe(sleeve_guardrail.guardrail_view, db),
        "paper_only": True,
    }
