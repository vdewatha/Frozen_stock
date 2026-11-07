"""Find the liquid universe size for the exploration lane (ranked experiment 4), and decide ``exploration_universe``.

The bot trades 45 core names under its caps and closes a handful of trades a
week, so every forward verdict takes months; research found that open slots,
not symbol count, cap the evidence rate, and recommended widening only the
*exploration lane* (learning-sized lots) to the most liquid point-in-time S&P
500 names.  Before the lane opens (experiment 7, ``app/services/
exploration_universe.py``) this script measures, on the point-in-time replay
(``point_in_time_universe``, ``gate_replay``), whether the wider names trade
as well as the core list once their wider spreads are paid.

Pre-registered (fixed before the first run)
-------------------------------------------
* **Groups.**  ``core`` = today's 45-symbol list (as traded).  ``T1`` / ``T2``
  / ``T3`` = single stocks *outside* the core list that were S&P 500 members on
  the trade's decision date, by tercile of their 20-day median dollar volume
  among all members with a full window on that date (``point_in_time_universe
  .tercile_panel``; T1 most liquid).  Each trade is placed by its decision
  date, so a name moves between terciles over time; no look-ahead.
* **Cost.**  The standard cost model (``gate_replay.simulate(...,
  calibration=None)``) plus an *extra spread cost* per round trip of
  ``max(0, S - S_core)``: ``S`` the symbol's 20-day Corwin-Schultz spread
  proxy on the decision date, ``S_core`` the median proxy of the core list's
  single stocks on that date.  The standard model is the one the core list's
  own fills calibrate, so only the extra width of a new name is charged.
* **Strategies.**  The seven broad-scope technical strategies of
  ``gate_replay.TECHNICAL`` under their live setups (SPY market gates, the
  Channel Breakout VIX gate, the RSI(14) / Bollinger ADX range gate, the
  earnings blackout where an earnings cache exists), pooled trade-weighted.
  RSI(2) and IBS keep fixed scopes and never reach lane names; they are
  reported for the core book only.
* **Rule.**  Tercile ``k`` qualifies iff its pooled net expectancy (standard
  cost + extra spread) in **both** the 2015+ and 2020+ windows is not worse
  than the core list's by more than **5 bps**, **and** its median
  decision-date spread proxy (2015+) is below a fixed ceiling of **1.00%**.
  The exploration universe is the largest qualifying set of the top terciles
  (T1; T1+T2; T1+T2+T3), all of which must qualify
  (``exploration_universe.select_universe``).  ``k = 0`` keeps
  ``exploration_universe = "core"``; otherwise ``"liquid_pit"`` with
  ``exploration_universe_terciles = k``.
* **Correlation.**  ``rho`` = the *residual* same-date correlation: the
  one-way intraclass correlation of SPY-adjusted trade returns (net return
  minus SPY's open-to-close return over the same entry and exit days) within
  (strategy, decision date) clusters, 2015+, technical strategies, every
  group.  The raw-return value is reported too; the evidence map's default
  ``exploration_effective_rho`` is the larger of the two (verdicts judge raw
  P&L, so the conservative value).
* **Time to 30 effective trades.**  A chronological book simulation, 2020+:
  the core book under the core caps (20 open, 5 per strategy, 3 per sector,
  one position per symbol, all nine strategies) and, for each breadth, the
  lane book on the admitted terciles (``exploration_max_open`` slots, at most
  3 entries per strategy and 3 per sector per entry date, at most
  ``exploration_budget_fraction / paper_exploration_risk_fraction`` = 4 lane
  lots per session, one position per symbol across both books; same-date
  candidates in a seeded random order).  Per strategy, effective trades per
  week = sum over entry dates of ``m / (1 + (m - 1) rho)`` over the weeks of
  the window; weeks to 30 = 30 / rate.
* **Slots.**  ``exploration_max_open`` default = the smallest of 20, 30, 40,
  60, 80 whose median (over the technical strategies) weeks to 30 effective
  trades on the chosen universe is within 10% of the unbounded-slot value.
  (Added after the first run, which chose no tercile: with the lane closed
  the slot table is still computed on T1 for the record, and the default
  stays at 40; it takes effect only if a later run opens the lane.)
* **Gates.**  The live SPY, VIX and ADX gates are re-tested on the widened
  universe: each gated strategy replayed with and without its gate; reported
  (trades, expectancy, and the expectancy of the trades the gate removes),
  not decided here.

Data ($0): the shared yfinance cache (``--data-dir`` becomes
``meta_label_history_dir``), the membership cache under ``<data-dir>/
universe``, a ``date,close`` VIX csv.  Cache-only unless ``--download``.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_liquid_universe_check.py --data-dir <dir> --vix-csv <csv> \\
        --output <json> [--report <md>]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import warnings
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS, settings  # noqa: E402

WINDOWS = {"all": None, "2015+": "2015-01-01", "2020+": "2020-01-01"}
MARGIN_BPS = 5.0
MAX_SPREAD = 0.01
SLOT_CHOICES = (20, 30, 40, 60, 80)
SLOT_TOLERANCE = 0.10
UNBOUNDED = 10**6
TARGET_EFFECTIVE = 30
CORE_CAPS = {"total": 20, "per_strategy": 5, "per_sector": 3}
LANE_CAPS = {"per_strategy_per_date": 3, "per_sector_per_date": 3}
SEED = 20261107
GROUPS = ("core", "T1", "T2", "T3")
BREADTHS = {"core": (), "core+T1": ("T1",), "core+T1+T2": ("T1", "T2"), "core+T1+T2+T3": ("T1", "T2", "T3")}


def live_setup(strategy: str, earnings: bool) -> dict:
    from app.services.seed import MARKET_GATED_STRATEGIES, VIX_GATED_STRATEGIES

    setup = {"market_gate_sma": MARKET_GATED_STRATEGIES.get(strategy, 0)}
    setup.update(VIX_GATED_STRATEGIES.get(strategy, {}))
    if earnings:
        setup["earnings_blackout_sessions"] = settings.paper_earnings_blackout_sessions
    return setup


@contextmanager
def no_range_gate():
    from app.services import gate_replay

    saved = gate_replay.RANGE_GATED
    gate_replay.RANGE_GATED = set()
    try:
        yield
    finally:
        gate_replay.RANGE_GATED = saved


# ------------------------------------------------------------ annotation

def annotate(trades: pd.DataFrame, *, core: set[str], terciles: pd.DataFrame, spread: pd.DataFrame,
             core_spread: pd.Series, spy: pd.DataFrame) -> pd.DataFrame:
    """Group, decision-date spread proxy, extra spread cost and the SPY-adjusted return per trade."""
    from app.services.point_in_time_universe import tercile_on

    if trades is None or trades.empty:
        return pd.DataFrame()
    out = trades.copy().reset_index(drop=True)
    decision = pd.to_datetime(out["decision_date"]).dt.normalize()
    out["group"] = "unranked"
    out["spread"] = np.nan
    for symbol, index in out.groupby("symbol").groups.items():
        days = decision.loc[index]
        if symbol in core:
            out.loc[index, "group"] = "core"
        else:
            tiers = tercile_on(terciles, symbol, days)
            out.loc[index, "group"] = [f"T{t}" if t else "unranked" for t in tiers]
        if symbol in spread.columns:
            out.loc[index, "spread"] = spread[symbol].reindex(days).to_numpy()
    base = core_spread.reindex(decision).to_numpy()
    extra = np.where(np.isfinite(out["spread"].to_numpy(float)) & np.isfinite(base),
                     np.maximum(out["spread"].to_numpy(float) - base, 0.0), 0.0)
    out["extra_cost"] = extra
    out["net_spread"] = out["net_return"].to_numpy(float) - extra
    spy_open = pd.Series(pd.to_numeric(spy["open"]).to_numpy(float), index=pd.to_datetime(spy["date"]).dt.normalize())
    spy_close = pd.Series(pd.to_numeric(spy["close"]).to_numpy(float), index=pd.to_datetime(spy["date"]).dt.normalize())
    entry = spy_open.reindex(pd.to_datetime(out["entry_date"]).dt.normalize()).to_numpy()
    exit_ = spy_close.reindex(pd.to_datetime(out["exit_date"]).dt.normalize()).to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        market = exit_ / entry - 1.0
    out["residual"] = out["net_return"].to_numpy(float) - np.where(np.isfinite(market), market, 0.0)
    return out


def _since(trades: pd.DataFrame, start) -> pd.DataFrame:
    if trades.empty or start is None:
        return trades
    return trades.loc[pd.to_datetime(trades["decision_date"]) >= pd.Timestamp(start)]


def _weeks(spy: pd.DataFrame, start) -> float:
    dates = pd.to_datetime(spy["date"])
    if start is not None:
        dates = dates[dates >= pd.Timestamp(start)]
    return max(len(dates) / 5.0, 1e-9)


def group_metrics(trades: pd.DataFrame, frames: dict, weeks: float) -> dict:
    """Trades, signals / distinct signal dates per week, expectancy (standard, + spread), spread proxy, drawdown."""
    from app.services.gate_replay import window_metrics
    from app.services.strategy_scorecard import trade_statistics

    if trades.empty:
        return {"trades": 0, "signals_per_week": 0.0, "signal_dates_per_week": 0.0, "expectancy_bps": None,
                "expectancy_spread_bps": None, "expectancy_spread_ci_bps": None, "win_rate": None,
                "median_spread": None, "mean_extra_cost_bps": None, "portfolio_max_drawdown": None,
                "residual_spread_bps": None}
    metrics = window_metrics(trades, frames, None)
    months = pd.to_datetime(trades["decision_date"]).dt.to_period("M").astype(str).tolist()
    spread_stats = trade_statistics(trades["net_spread"].to_numpy(), clusters=months)
    spreads = trades["spread"].to_numpy(float)
    return {
        "trades": int(len(trades)),
        "signals_per_week": round(len(trades) / weeks, 3),
        "signal_dates_per_week": round(trades["decision_date"].nunique() / weeks, 3),
        "expectancy_bps": metrics["expectancy_bps"],
        "expectancy_spread_bps": spread_stats["expectancy_bps"],
        "expectancy_spread_ci_bps": spread_stats.get("expectancy_ci_bps"),
        "win_rate": metrics["win_rate"],
        "median_spread": round(float(np.nanmedian(spreads)), 5) if np.isfinite(spreads).any() else None,
        "mean_extra_cost_bps": round(float(np.mean(trades["extra_cost"]) * 1e4), 2),
        "portfolio_max_drawdown": metrics["portfolio_max_drawdown"],
        # SPY-adjusted (net return - SPY over the same days - extra spread): the part the strategy owns.
        "residual_spread_bps": round(float(np.mean(trades["residual"] - trades["extra_cost"]) * 1e4), 1),
    }


def table(trades: pd.DataFrame, frames: dict, spy: pd.DataFrame) -> dict:
    """Per window and group (plus the widened unions)."""
    out = {}
    for window, start in WINDOWS.items():
        sub = _since(trades, start)
        weeks = _weeks(spy, start)
        row = {}
        for group in GROUPS:
            row[group] = group_metrics(sub.loc[sub["group"] == group] if not sub.empty else sub, frames, weeks)
        for name, tiers in BREADTHS.items():
            if not tiers:
                continue
            row[name] = group_metrics(sub.loc[sub["group"].isin(("core", *tiers))] if not sub.empty else sub,
                                      frames, weeks)
        out[window] = row
    return out


# ----------------------------------------------------------- correlation

def correlations(trades: pd.DataFrame) -> dict:
    from app.services.exploration_budget import same_date_correlation

    sub = _since(trades, WINDOWS["2015+"])
    sub = sub.loc[sub["group"].isin(GROUPS)]
    clusters = (sub["strategy"] + "|" + sub["decision_date"].astype(str)).tolist()
    residual = same_date_correlation(sub["residual"].to_numpy(float), clusters)
    raw = same_date_correlation(sub["net_return"].to_numpy(float), clusters)
    any_strategy = same_date_correlation(sub["residual"].to_numpy(float), sub["decision_date"].astype(str).tolist())
    return {"residual": residual, "raw": raw, "residual_any_strategy": any_strategy}


# ------------------------------------------------------------ simulation

def simulate_books(trades: pd.DataFrame, *, lane_groups: tuple, slots: int, rho: float, start: str,
                   sectors: dict, lots_per_session: int, weeks: float, technical: tuple) -> dict:
    """Core book under the core caps plus the lane book on ``lane_groups``; effective trades per strategy."""
    from app.services.exploration_budget import effective_trades
    from app.services.exploration_universe import sector_for

    sub = _since(trades, start)
    sub = sub.loc[sub["group"].isin(("core", *lane_groups))].copy()
    rng = np.random.default_rng(SEED)
    sub["order"] = rng.random(len(sub))
    sub["decision"] = pd.to_datetime(sub["decision_date"]).dt.normalize()
    sub["exit"] = pd.to_datetime(sub["exit_date"]).dt.normalize()
    sub["sector"] = [sector_for(s, sectors) for s in sub["symbol"]]
    open_core: list = []   # (exit, symbol, strategy, sector)
    open_lane: list = []
    accepted = []
    lane_open_counts = []
    for day, group in sub.sort_values(["decision", "order"]).groupby("decision", sort=True):
        open_core = [p for p in open_core if p[0] > day]
        open_lane = [p for p in open_lane if p[0] > day]
        held = {p[1] for p in open_core} | {p[1] for p in open_lane}
        lane_today = {"strategy": {}, "sector": {}, "lots": 0}
        core_rows = group.loc[group["group"] == "core"]
        lane_rows = group.loc[group["group"] != "core"]
        for row in core_rows.itertuples(index=False):
            if row.symbol in held or len(open_core) >= CORE_CAPS["total"]:
                continue
            if sum(1 for p in open_core if p[2] == row.strategy) >= CORE_CAPS["per_strategy"]:
                continue
            if sum(1 for p in open_core if p[3] == row.sector) >= CORE_CAPS["per_sector"]:
                continue
            open_core.append((row.exit, row.symbol, row.strategy, row.sector))
            held.add(row.symbol)
            accepted.append((row.strategy, day, "core"))
        for row in lane_rows.itertuples(index=False):
            if row.strategy not in technical or row.symbol in held or len(open_lane) >= slots:
                continue
            if lane_today["lots"] >= lots_per_session:
                break
            if lane_today["strategy"].get(row.strategy, 0) >= LANE_CAPS["per_strategy_per_date"]:
                continue
            if lane_today["sector"].get(row.sector, 0) >= LANE_CAPS["per_sector_per_date"]:
                continue
            open_lane.append((row.exit, row.symbol, row.strategy, row.sector))
            held.add(row.symbol)
            lane_today["lots"] += 1
            lane_today["strategy"][row.strategy] = lane_today["strategy"].get(row.strategy, 0) + 1
            lane_today["sector"][row.sector] = lane_today["sector"].get(row.sector, 0) + 1
            accepted.append((row.strategy, day, "lane"))
        lane_open_counts.append(len(open_lane))
    by_strategy = {}
    frame = pd.DataFrame(accepted, columns=["strategy", "day", "book"])
    for strategy in sorted(set(trades["strategy"])):
        mine = frame.loc[frame["strategy"] == strategy] if not frame.empty else frame
        effective = effective_trades(mine["day"].tolist(), rho) if len(mine) else 0.0
        rate = effective / weeks
        by_strategy[strategy] = {
            "trades": int(len(mine)), "lane_trades": int((mine["book"] == "lane").sum()) if len(mine) else 0,
            "trades_per_week": round(len(mine) / weeks, 3), "effective_per_week": round(rate, 3),
            "weeks_to_30_effective": round(TARGET_EFFECTIVE / rate, 1) if rate > 0 else None,
        }
    counts = np.array(lane_open_counts or [0])
    technical_weeks = [row["weeks_to_30_effective"] for name, row in by_strategy.items()
                       if name in technical and row["weeks_to_30_effective"] is not None]
    return {"slots": slots if slots < UNBOUNDED else None, "lane_groups": list(lane_groups), "strategies": by_strategy,
            "median_weeks_to_30_technical": round(float(np.median(technical_weeks)), 1) if technical_weeks else None,
            "lane_open_mean": round(float(counts.mean()), 2), "lane_open_p95": float(np.percentile(counts, 95)),
            "lane_open_max": int(counts.max())}


def choose_slots(results: dict) -> dict:
    unbounded = results[UNBOUNDED]["median_weeks_to_30_technical"]
    for slots in SLOT_CHOICES:
        weeks = results[slots]["median_weeks_to_30_technical"]
        if unbounded is not None and weeks is not None and weeks <= unbounded * (1 + SLOT_TOLERANCE):
            return {"exploration_max_open": slots, "weeks": weeks, "unbounded_weeks": unbounded}
    return {"exploration_max_open": SLOT_CHOICES[-1], "weeks": results[SLOT_CHOICES[-1]]["median_weeks_to_30_technical"],
            "unbounded_weeks": unbounded}


# ------------------------------------------------------------------ gates

def gate_tests(base_trades: dict, run_variant, annotate_fn, frames, spy, lane_groups) -> dict:
    """Each live gate on the widened universe: with vs without, and what the gate removes."""
    from app.services.seed import MARKET_GATED_STRATEGIES, VIX_GATED_STRATEGIES

    tests = []
    for strategy, window in MARKET_GATED_STRATEGIES.items():
        tests.append(("spy_market_gate", strategy, f"SPY above its {window}-day average", {"market_gate_sma": 0}, False))
    for strategy, gate in VIX_GATED_STRATEGIES.items():
        tests.append(("vix_gate", strategy, f"VIX {gate['vix_gate']} {gate['vix_level']}", {"vix_gate": None}, False))
    for strategy in ("rsi_mean_reversion", "bollinger_mean_reversion"):
        tests.append(("adx_range_gate", strategy, "ADX(14) below 20", {}, True))
    out = []
    for gate, strategy, label, change, range_off in tests:
        gated = base_trades.get(strategy, pd.DataFrame())
        ungated = annotate_fn(run_variant(strategy, change, range_off))
        row = {"gate": gate, "strategy": strategy, "rule": label, "windows": {}}
        for window in ("2015+", "2020+"):
            start = WINDOWS[window]
            cell = {}
            for scope_name, groups in (("core", ("core",)), ("lane", lane_groups), ("widened", ("core", *lane_groups))):
                g = _since(gated, start)
                u = _since(ungated, start)
                g = g.loc[g["group"].isin(groups)] if not g.empty else g
                u = u.loc[u["group"].isin(groups)] if not u.empty else u
                keys = set(zip(g["symbol"], g["decision_date"])) if not g.empty else set()
                removed = u.loc[[k not in keys for k in zip(u["symbol"], u["decision_date"])]] if not u.empty else u
                cell[scope_name] = {
                    "gated_trades": int(len(g)), "ungated_trades": int(len(u)),
                    "gated_bps": round(float(g["net_spread"].mean() * 1e4), 1) if len(g) else None,
                    "ungated_bps": round(float(u["net_spread"].mean() * 1e4), 1) if len(u) else None,
                    "removed_trades": int(len(removed)),
                    "removed_bps": round(float(removed["net_spread"].mean() * 1e4), 1) if len(removed) else None,
                }
                cell[scope_name]["gate_helps"] = (cell[scope_name]["gated_bps"] is not None
                                                  and cell[scope_name]["ungated_bps"] is not None
                                                  and cell[scope_name]["gated_bps"] >= cell[scope_name]["ungated_bps"])
            row["windows"][window] = cell
        out.append(row)
    return out


# -------------------------------------------------------------------- run

def run(args) -> dict:
    settings.meta_label_history_dir = str(args.data_dir)
    from app.services import exploration_universe as lane
    from app.services import gate_replay
    from app.services import meta_label_primary as primary
    from app.services import point_in_time_universe as pit
    from app.services.earnings_history import load_surprises
    from app.services.meta_label_learning import history_dir
    from app.services.strategy_discovery import load_frames
    from app.services.strategy_scorecard import load_daily_history
    from app.services.trading_universe import ETF_SYMBOLS, SECTOR_GROUPS

    now = datetime.now(timezone.utc)
    started = time.time()
    membership = pit.load_membership(download=args.download, now=now)
    if args.download:
        loader = pit.cache_first_loader(now=now)
    else:
        def loader(ticker: str) -> pd.DataFrame:
            return load_daily_history(pit.yahoo_symbol(ticker), history_dir(), download=False)

    def earnings_loader(symbol: str) -> pd.DataFrame:
        return load_surprises(symbol, history_dir(), download=False)

    coverage = pit.coverage(membership, loader=loader, now=now, start=args.start, cache_path=pit.coverage_path())
    base = sorted({s.upper() for s in DEFAULT_PAPER_EXECUTION_SYMBOLS})
    symbols = pit.replay_symbols(base, coverage)
    frames, missing = load_frames(symbols, loader=loader, start=args.start, end=args.end,
                                  earnings_loader=earnings_loader, news_loader=lambda symbol: None)
    spy = frames.get("SPY")
    if spy is None:
        raise SystemExit("SPY history is required")
    vix = pd.read_csv(args.vix_csv) if args.vix_csv and Path(args.vix_csv).exists() else None
    earnings_cached = (history_dir() / "earnings").exists()
    load_seconds = time.time() - started

    dollar, spread = pit.liquidity_panel(frames)
    terciles = pit.tercile_panel(dollar, membership)
    core = set(base)
    core_stocks = [s for s in base if s not in ETF_SYMBOLS and s in spread.columns]
    core_spread = spread[core_stocks].median(axis=1)
    replays = {symbol: primary.replay_signals(frame) for symbol, frame in frames.items()}
    signals_seconds = time.time() - started - load_seconds

    def annotate_fn(trades):
        return annotate(trades, core=core, terciles=terciles, spread=spread, core_spread=core_spread, spy=spy)

    def run_variant(strategy, change, range_off):
        setup = {**live_setup(strategy, earnings_cached), **change}
        if range_off:
            with no_range_gate():
                return gate_replay.simulate(strategy, frames, spy, setup, replays, vix=vix,
                                            universe=membership.mask, calibration=None)
        return gate_replay.simulate(strategy, frames, spy, setup, replays, vix=vix, universe=membership.mask,
                                    calibration=None)

    annotated, strategies, timings = {}, {}, {}
    for strategy in gate_replay.STRATEGIES:
        t0 = time.time()
        annotated[strategy] = annotate_fn(run_variant(strategy, {}, False))
        timings[strategy] = round(time.time() - t0, 1)
        strategies[strategy] = {"setup": live_setup(strategy, earnings_cached),
                                "windows": table(annotated[strategy], frames, spy)}
    technical = tuple(gate_replay.TECHNICAL)
    pooled_trades = pd.concat([annotated[s] for s in technical if not annotated[s].empty], ignore_index=True)
    all_trades = pd.concat([t for t in annotated.values() if not t.empty], ignore_index=True)
    pooled = table(pooled_trades, frames, spy)

    # The pre-registered rule.
    def row_for(group):
        return {"expectancy_bps": {w: pooled[w][group]["expectancy_spread_bps"] for w in ("2015+", "2020+")},
                "spread": pooled["2015+"][group]["median_spread"]}

    decision = lane.select_universe({1: row_for("T1"), 2: row_for("T2"), 3: row_for("T3")}, row_for("core"),
                                    margin_bps=MARGIN_BPS, max_spread=MAX_SPREAD)
    k = decision["terciles"]
    lane_groups = tuple(f"T{t}" for t in range(1, k + 1))

    corr = correlations(pooled_trades)
    rho_residual = corr["residual"]["rho"] or 0.0
    rho_raw = corr["raw"]["rho"] or 0.0
    rho_evidence = round(max(rho_residual, rho_raw), 2)

    # Sectors for the per-sector caps: SPY-residual correlation with the sector ETFs.
    etfs = {etf: frames.get(etf) for etf in lane.SECTOR_ETFS}
    sectors = lane.assign_sectors({s: f for s, f in frames.items() if s not in ETF_SYMBOLS}, etfs, spy,
                                  known=SECTOR_GROUPS)
    known_check = lane.assign_sectors({s: frames[s] for s in SECTOR_GROUPS if s in frames and s not in ETF_SYMBOLS},
                                      etfs, spy, known={})
    sector_accuracy = np.mean([known_check[s] == SECTOR_GROUPS[s] for s in known_check]) if known_check else None

    lots = int(round(settings.exploration_budget_fraction / settings.paper_exploration_risk_fraction))
    weeks20 = _weeks(spy, WINDOWS["2020+"])
    breadth_sims = {}
    for name, tiers in BREADTHS.items():
        breadth_sims[name] = simulate_books(all_trades, lane_groups=tiers, slots=40, rho=rho_residual,
                                            start=WINDOWS["2020+"], sectors=sectors, lots_per_session=lots,
                                            weeks=weeks20, technical=technical)
    budget_sensitivity = {}
    if lane_groups:
        for lot_cap in (lots, 2 * lots):
            budget_sensitivity[lot_cap] = simulate_books(
                all_trades, lane_groups=lane_groups, slots=40, rho=rho_residual, start=WINDOWS["2020+"],
                sectors=sectors, lots_per_session=lot_cap, weeks=weeks20, technical=technical)
    slot_sims = {}
    chosen_slots = None
    slot_groups = lane_groups or ("T1",)  # with the lane closed: T1, for the record only
    for slots in (*SLOT_CHOICES, UNBOUNDED):
        slot_sims[slots] = simulate_books(all_trades, lane_groups=slot_groups, slots=slots, rho=rho_residual,
                                          start=WINDOWS["2020+"], sectors=sectors, lots_per_session=lots,
                                          weeks=weeks20, technical=technical)
    slot_record = choose_slots(slot_sims)
    chosen_slots = slot_record if lane_groups else None

    gates = gate_tests(annotated, run_variant, annotate_fn, frames, spy, lane_groups or ("T1", "T2", "T3"))

    # Live screen today, for the report: how many names the setting would admit.
    members_today = sorted(membership.members_on(pd.Timestamp(args.end) - pd.Timedelta(days=1)))
    screen = lane.screen_names(members_today, frames, core=core, terciles=max(k, 1), max_spread=MAX_SPREAD,
                               max_price=100_000 * settings.paper_min_one_share_equity_fraction,
                               min_price=settings.news_discovery_min_price)
    screened = {"members": len(screen), "admitted": sum(1 for r in screen if r["admitted"]),
                "reasons": lane._reason_counts(screen),
                "skipped_one_share": [r["symbol"] for r in screen if str(r.get("reason") or "").startswith("one share")]}

    tercile_sizes = {f"T{t}": int(round(float((terciles == t).sum(axis=1)[terciles.index >= "2020-01-01"].mean())))
                     for t in (1, 2, 3)}
    return {
        "ran_at": now.isoformat(), "paper_only": True, "data_dir": str(args.data_dir), "start": args.start,
        "end": args.end, "vix": vix is not None, "earnings_cache": earnings_cached, "source": dict(membership.source),
        "symbols": len(frames), "missing_history": len(missing), "core_symbols": len(core),
        "tercile_sizes_2020": tercile_sizes,
        "core_spread_median_2015": round(float(core_spread[core_spread.index >= "2015-01-01"].median()), 5),
        "strategies": strategies, "pooled": pooled, "decision": decision,
        "correlation": corr, "rho": {"residual": rho_residual, "raw": rho_raw, "evidence_default": rho_evidence},
        "sector_assignment_accuracy_on_known": None if sector_accuracy is None else round(float(sector_accuracy), 3),
        "lots_per_session": lots, "breadths": breadth_sims,
        "budget_sensitivity": {str(k_): v for k_, v in budget_sensitivity.items()},
        "slots": {str(k_ if k_ < UNBOUNDED else "unbounded"): v for k_, v in slot_sims.items()},
        "chosen_slots": chosen_slots, "slot_record": {**slot_record, "groups": list(slot_groups)},
        "gates": gates, "screen_today": screened,
        "timings": {"load_seconds": round(load_seconds, 1), "signals_seconds": round(signals_seconds, 1),
                    "by_strategy": timings, "total_seconds": round(time.time() - started, 1)},
        "settings": {"exploration_universe": decision["exploration_universe"],
                     "exploration_universe_terciles": max(k, 1),
                     "exploration_universe_max_spread": MAX_SPREAD,
                     "exploration_max_open": (chosen_slots or {}).get("exploration_max_open", 40),
                     "exploration_effective_rho": rho_evidence},
    }


# ----------------------------------------------------------------- report

def _bps(value):
    return "n/a" if value is None else f"{float(value):+.1f}"


def _pct(value, digits=1):
    return "n/a" if value is None else f"{100 * float(value):.{digits}f}%"


def _ci(value):
    return "" if not value else f" [{value[0]:+.0f}, {value[1]:+.0f}]"


def _weeks_text(value):
    return "n/a" if value is None else f"{value:.1f}"


def render(result: dict) -> str:
    dec, rho, cfg = result["decision"], result["rho"], result["settings"]
    day = result["ran_at"][:10]
    lines = [f"# Liquid universe check: {day}", "",
             "**Question (ranked experiment 4):** how wide can the exploration lane go? On the point-in-time S&P 500 "
             "replay, do the liquidity terciles outside the 45-name core list trade as well as the core list once "
             "their wider spreads are paid, and how much sooner would each strategy reach 30 effective trades?", "",
             f"**Decision (pre-registered in `scripts/run_liquid_universe_check.py`):** `exploration_universe = "
             f"\"{dec['exploration_universe']}\"`"
             + (f", `exploration_universe_terciles = {dec['terciles']}`" if dec["terciles"] else "")
             + f", `exploration_max_open = {cfg['exploration_max_open']}`"
             + ("" if dec["terciles"] else " (unchanged: the lane is closed)")
             + f", `exploration_effective_rho = {cfg['exploration_effective_rho']}`. Paper only; learning-sized lots "
             "only.", ""]
    for row in dec["decisions"]:
        lines.append(f"* T{row['tercile']}: {'qualifies' if row['qualifies'] else 'does not qualify'} ({row['reason']}).")
    lines += ["", "Rule: a tercile qualifies iff its pooled net expectancy (standard cost model + extra spread cost) "
              f"in both 2015+ and 2020+ is within {MARGIN_BPS:g} bps of the core list's and its median spread proxy "
              f"(2015+) is below {MAX_SPREAD:.2%}; the universe is the largest run of qualifying terciles from the most "
              "liquid down.", "",
              "## 1. Data and method", "",
              f"* {result['symbols']} symbols with history (today's {result['core_symbols']} core plus every past S&P 500 "
              f"member yfinance still serves; {result['missing_history']} without usable history), bars from "
              f"{result['start']}, VIX {'present' if result['vix'] else 'absent'}, earnings cache "
              f"{'present' if result['earnings_cache'] else 'absent'}. A single stock enters only while a member.",
              f"* Terciles of the 20-day median dollar volume among members on each decision date; mean size since "
              f"2020: T1 {result['tercile_sizes_2020']['T1']}, T2 {result['tercile_sizes_2020']['T2']}, T3 "
              f"{result['tercile_sizes_2020']['T3']} names (core names sit mostly in T1 and are counted as core).",
              f"* Spread proxy: 20-day mean Corwin-Schultz high-low estimate. The core list's median since 2015 is "
              f"{_pct(result['core_spread_median_2015'], 2)}: the estimator carries volatility too, so it overstates "
              "the quoted spread of liquid names; only the excess over the core median is charged.",
              "* Signals per week and distinct signal dates per week are over every trading week of the window "
              "(unconstrained replay: one position per symbol per strategy, no book caps).", ""]
    pooled = result["pooled"]
    gap = {w: (pooled[w]["T1"]["expectancy_bps"], pooled[w]["core"]["expectancy_bps"]) for w in ("2015+", "2020+")}
    b = result["breadths"]
    best = min((name for name in b if name != "core"),
               key=lambda name: b[name]["median_weeks_to_30_technical"] or 1e9)
    lines += ["## Reading", "",
              f"* **The lane stays closed.** No tercile is within {MARGIN_BPS:g} bps of the core list. The gap is mostly "
              f"there before any spread is charged: under the standard cost model alone, T1 trades "
              f"{_bps(gap['2015+'][0])} bps against the core's {_bps(gap['2015+'][1])} (2015+), and "
              f"{_bps(gap['2020+'][0])} against {_bps(gap['2020+'][1])} (2020+). The extra spread adds 15-28 bps on top.",
              "* **Most of the core's edge is the core list itself.** The survivorship check found that today's list "
              "flatters these broad technical strategies by 20-70 bps per trade. The terciles here are the same "
              "strategies on the names that did not become today's megacaps, and they lose in every window and "
              "tercile. So the lane would not test the strategies on fresh names. It would pay for trades that, in "
              "the replay, lose money on average. The pre-registered rule exists to refuse exactly that.",
              f"* **The gap survives removing the market.** SPY-adjusted (net of the same days' SPY move and the "
              f"extra spread), the core book is {_bps(pooled['2015+']['core'].get('residual_spread_bps'))} bps per trade "
              f"and T1 {_bps(pooled['2015+']['T1'].get('residual_spread_bps'))} (2015+). Even on the core list these "
              "long-only signals trail simply holding SPY over the same days; what they earn is mostly market "
              "exposure, so judging verdicts on residual returns (the research note's advice) would be stricter "
              "still.",
              f"* **Breadth would have bought speed.** The median technical strategy needs "
              f"{_weeks_text(b['core']['median_weeks_to_30_technical'])} weeks to 30 effective trades on the core book "
              f"alone, against {_weeks_text(b[best]['median_weeks_to_30_technical'])} with the lane on {best}. Most "
              "of that gain comes from T1; T2 and T3 add little, because the lane's 4 lots per session and its 40 "
              "slots bind first. If a strategy with a point-in-time edge appears (the discovery library judges "
              "candidates point in time since PR #30), the lane is ready behind the setting: rerun this check.",
              "* **The live gates hold on the wider names.** Each gate's removed trades are worse than the ones it "
              "keeps, in nearly every window and scope (table 6). Nothing here argues for loosening a gate.",
              f"* **Clustering matters.** Same-date correlation is {rho['raw']} on raw returns and {rho['residual']} "
              f"after removing SPY's move: 10 same-day trades of one strategy carry the information of about "
              f"{10 / (1 + 9 * rho['evidence_default']):.1f} independent ones. The evidence map now counts effective "
              f"trades at rho = {rho['evidence_default']}.", ""]
    lines += ["## 2. Pooled technical strategies by group", "",
              "Net expectancy per trade in bps: standard cost model, then with the extra spread cost (month-clustered "
              "bootstrap CI), then SPY-adjusted with the spread cost (net return minus SPY's open-to-close move over "
              "the same days; reported, not part of the rule); equal-weight portfolio max drawdown.", "",
              "| Window | Group | Trades | Signals/wk | Signal dates/wk | Exp. bps | Exp. + spread bps | Residual + spread bps | Median spread | Extra cost bps | Max DD |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for window, groups in result["pooled"].items():
        for group, m in groups.items():
            lines.append(f"| {window} | {group} | {m['trades']} | {m['signals_per_week']:.2f} | "
                         f"{m['signal_dates_per_week']:.2f} | {_bps(m['expectancy_bps'])} | "
                         f"{_bps(m['expectancy_spread_bps'])}{_ci(m['expectancy_spread_ci_bps'])} | "
                         f"{_bps(m.get('residual_spread_bps'))} | {_pct(m['median_spread'], 2)} | {_bps(m['mean_extra_cost_bps'])} | "
                         f"{_pct(m['portfolio_max_drawdown'])} |")
    lines += ["", "## 3. Per strategy (2015+ and 2020+, expectancy with the extra spread cost)", "",
              "| Strategy | Window | core | T1 | T2 | T3 |", "|---|---|---|---|---|---|"]
    for strategy, row in result["strategies"].items():
        for window in ("2015+", "2020+"):
            cells = [f"{_bps(row['windows'][window][g]['expectancy_spread_bps'])} ({row['windows'][window][g]['trades']})"
                     for g in GROUPS]
            lines.append(f"| {strategy} | {window} | " + " | ".join(cells) + " |")
    lines += ["", "Cells: bps per trade (trades). RSI(2) and IBS keep fixed scopes, so they never trade T1-T3.", ""]
    corr = result["correlation"]
    lines += ["## 4. Same-date correlation", "",
              f"One-way intraclass correlation of trade returns within (strategy, decision date), 2015+, technical "
              f"strategies, every group: residual (SPY-adjusted) rho = **{rho['residual']}** over "
              f"{corr['residual']['clusters']} clusters (mean size {corr['residual'].get('mean_cluster')}); raw rho = "
              f"{rho['raw']}; residual across strategies by date alone = {corr['residual_any_strategy']['rho']}. "
              f"The time-to-verdict estimate uses the residual value; the evidence map's default is the larger, "
              f"{rho['evidence_default']} (verdicts judge raw P&L).", ""]
    lines += ["## 5. Time to 30 effective trades per breadth (2020+ book simulation)", "",
              f"Core book under the core caps (20 open, 5 per strategy, 3 per sector), lane book with 40 slots, 3 "
              f"entries per strategy and per sector per date, {result['lots_per_session']} lane lots per session (the "
              "exploration budget), rho = residual. Weeks to 30 effective trades per strategy (core + lane):", "",
              "| Strategy | " + " | ".join(BREADTHS) + " |", "|---|" + "---|" * len(BREADTHS)]
    names = sorted(next(iter(result["breadths"].values()))["strategies"])
    for strategy in names:
        lines.append(f"| {strategy} | " + " | ".join(
            _weeks_text(result["breadths"][b]["strategies"][strategy]["weeks_to_30_effective"]) for b in BREADTHS) + " |")
    lines.append("| median (technical) | " + " | ".join(
        _weeks_text(result["breadths"][b]["median_weeks_to_30_technical"]) for b in BREADTHS) + " |")
    lines.append("| lane open (mean / p95) | " + " | ".join(
        f"{result['breadths'][b]['lane_open_mean']:.1f} / {result['breadths'][b]['lane_open_p95']:.0f}" for b in BREADTHS) + " |")
    if result["slots"]:
        record = result["slot_record"]
        lines += ["", f"Slots on {'the chosen universe' if result['chosen_slots'] else '+'.join(record['groups']) + ' (for the record; the lane is closed)'} "
                  "(median weeks to 30 effective, technical strategies):", "",
                  "| Slots | Median weeks | Lane open mean | p95 |", "|---|---|---|---|"]
        for slots, row in result["slots"].items():
            lines.append(f"| {slots} | {_weeks_text(row['median_weeks_to_30_technical'])} | {row['lane_open_mean']:.1f} | "
                         f"{row['lane_open_p95']:.0f} |")
        chosen = result["chosen_slots"] or record
        lines += ["", f"Pre-registered slot rule: the smallest of {', '.join(map(str, SLOT_CHOICES))} within "
                  f"{SLOT_TOLERANCE:.0%} of the unbounded median ({_weeks_text(chosen['unbounded_weeks'])} weeks): "
                  f"{chosen['exploration_max_open']}"
                  + ("." if result["chosen_slots"] else "; not applied while the lane is closed (default stays 40).")]
        if result["budget_sensitivity"]:
            lines += ["", "Budget sensitivity (40 slots): " + "; ".join(
                f"{lots} lots/session -> median {_weeks_text(row['median_weeks_to_30_technical'])} weeks, lane open mean "
                f"{row['lane_open_mean']:.1f}" for lots, row in result["budget_sensitivity"].items()) + "."]
    lines += ["", "## 6. The live gates on the widened universe", "",
              "Each gated strategy replayed with and without its gate (expectancy with the extra spread cost). "
              "'Removed' are the trades the gate takes away; a gate earns its place when they are worse than what it "
              "keeps.", "",
              "| Gate | Strategy | Window | Scope | Gated (trades, bps) | Ungated | Removed | Helps |",
              "|---|---|---|---|---|---|---|---|"]
    for row in result["gates"]:
        for window, cells in row["windows"].items():
            for scope_name, c in cells.items():
                lines.append(f"| {row['gate']} ({row['rule']}) | {row['strategy']} | {window} | {scope_name} | "
                             f"{c['gated_trades']}, {_bps(c['gated_bps'])} | {c['ungated_trades']}, {_bps(c['ungated_bps'])} | "
                             f"{c['removed_trades']}, {_bps(c['removed_bps'])} | {'yes' if c['gate_helps'] else 'no'} |")
    s = result["screen_today"]
    lines += ["", "## 7. Today's screen", "",
              f"Of {s['members']} S&P 500 members on {result['end']}, "
              f"{'the setting would admit' if result['decision']['terciles'] else 'T1 alone (were it admitted) would give'} "
              f"{s['admitted']} names "
              f"(100k equity, one-share floor {100_000 * settings.paper_min_one_share_equity_fraction:,.0f}). Not admitted: "
              + ", ".join(f"{reason} {count}" for reason, count in s["reasons"].items())
              + ". Skipped because one share exceeds the learning floor: "
              + (", ".join(s["skipped_one_share"]) or "none") + ".",
              f"Sector assignment (SPY-residual correlation with the sector ETFs) agrees with the static map on "
              f"{_pct(result['sector_assignment_accuracy_on_known'])} of the core single stocks.", ""]
    t = result["timings"]
    lines += ["## 8. Caveats", "",
              "* The core list is today's survivors: its own replay is flattered by 20-70 bps per trade "
              "(reports/survivorship-20261106.md), so comparing the terciles with it is conservative, against "
              "widening.",
              "* The replay applies no price floor (the live screen requires $10, per the research note); sub-$10 S&P "
              "500 members are rare after 2010.",
              "* Before 2010 the point-in-time universe is itself only about half covered (names Yahoo dropped); the "
              "decision windows (2015+, 2020+) are the better covered ones.",
              "* The book simulation ignores the core exploration lots and news-found names that share the session "
              "budget, and fills every candidate at the replay's next-open price; it is an evidence-rate estimate, "
              "not a P&L forecast.",
              "* Lane lots are 0.25% of equity: the lane is for evidence, not profit, until a strategy's verdict says "
              "otherwise.",
              f"* Runtime: load {t['load_seconds']}s, signals {t['signals_seconds']}s, total {t['total_seconds']}s.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--vix-csv", type=Path, default=None)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None, help="exclusive; default today")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    args.end = args.end or date.today().isoformat()
    warnings.filterwarnings("ignore")
    result = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, default=str))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(render(result))
    print(json.dumps(result["settings"]), json.dumps(result["decision"]["decisions"]))


if __name__ == "__main__":
    main()
