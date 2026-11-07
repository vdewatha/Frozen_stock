"""Measure the inputs the forward verdict depends on (experiment 1 of the verdict research).

The research behind ``app/services/verdict_stats.py`` sized its tests from
simulations with assumed inputs (2% per-trade std, unknown tails, no
same-date correlation).  This script measures them on the bot's own data
where it exists and otherwise on a replay of the live strategies:

1. **Per-trade net-return dispersion by strategy**: std of the net return,
   raw and SPY-adjusted over the same hold, tail quantiles, the worst loss,
   and the realistic worst-case loss given the stops: how often an exit was
   a gap through the stop (``barrier == "stop_loss_gap"``) and how far past
   the stop those gaps went.
2. **Paired shadow differences**: for every candidate value of every gate
   and exit template the engine can test, candidate minus live per decision
   (``r_c 1[c trades] - r_l 1[l trades]`` over the union of the two setups'
   decisions; zero where both take the same trade), SPY-adjusted over the
   same hold except for the market-timing gates; std, share nonzero, mean.
3. **Residual same-date correlation** rho: the intra-class correlation of
   SPY- and sector-adjusted trade returns grouped by entry date (one-way
   ANOVA estimator), per strategy and over the book.

Sources, in order: the bot's ledger (``--database-url``: trade attributions
and setup-shadow scores; used when a strategy has at least ``MIN_LEDGER``
trades), else the replay: ``gate_replay.simulate`` of every live replayable
strategy under its live setup on the point-in-time S&P 500 universe
(``point_in_time_universe``), published cost model (``calibration=None``),
cache-only data from ``--data-dir``.  The replays run through
``experiment_engine.run_experiment`` itself (gate and exit templates), so
the same rows feed the multiplicity-hurdle check in
``scripts/replay_verdicts.py``; ``--cache`` pickles them for that script.

Derived inputs (written to the JSON, the report and, with ``--record``, a
``verdict_inputs`` audit row the verdict code reads over its constants):

* ``loss_bound_bps`` per class (technical, reversion): the larger of the
  0.1% and 99.9% quantiles of |SPY-adjusted per-trade return| over the
  class's strategies, rounded up to 100 bps.  A paired difference is a
  single trade's residual return or zero, so this bounds it too; trades
  beyond it are the alarm (reported as the breach rate).
* ``sigma_bps`` per class: the largest per-trade SPY-adjusted std in the
  class x 1.1, rounded up to 50 bps (the Catoni fallback's variance bound).
* ``rho``: the trade-weighted mean of the per-strategy ICCs (2000+), floored at 0.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/measure_verdict_inputs.py --data-dir <dir> --vix-csv <csv> \\
        --output <json> --report ../../../reports/verdict-inputs-<date>.md [--cache <pkl>] [--database-url <url>]
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS, settings  # noqa: E402

MIN_LEDGER = 30
QUANTILES = (0.001, 0.01, 0.05, 0.5, 0.95, 0.99, 0.999)
KEEP_COLUMNS = ("symbol", "decision_date", "entry_date", "exit_date", "barrier", "net_return", "stop_loss_pct")


# ----------------------------------------------------------------- helpers

def hold_returns(trades: pd.DataFrame, frame: pd.DataFrame | None) -> np.ndarray:
    """The reference instrument over each trade's hold (``verdict_stats.hold_returns``)."""
    from app.services.verdict_stats import hold_returns as shared

    return shared(trades, frame)


def adjusted(trades: pd.DataFrame, frames: dict, spy: pd.DataFrame) -> pd.DataFrame:
    """Add ``spy_ret``, ``sector_ret`` (sector ETF over the hold, SPY when unmapped) and the residuals."""
    from app.services.market_gate import sector_symbol

    trades = trades.copy()
    trades["spy_ret"] = hold_returns(trades, spy)
    sector = trades["spy_ret"].to_numpy().copy()
    for symbol, group in trades.groupby("symbol"):
        etf = sector_symbol(symbol)
        if etf and etf in frames:
            sector[group.index.to_numpy()] = hold_returns(group, frames[etf])
    trades["sector_ret"] = np.where(np.isfinite(sector), sector, trades["spy_ret"].to_numpy())
    trades["resid_spy"] = trades["net_return"] - trades["spy_ret"]
    trades["resid_sector"] = trades["net_return"] - trades["sector_ret"]
    return trades


def icc(values: np.ndarray, groups: np.ndarray) -> dict:
    """One-way ANOVA intra-class correlation: (MSB - MSW) / (MSB + (n0 - 1) MSW)."""
    frame = pd.DataFrame({"y": values, "g": groups}).dropna()
    if frame.empty:
        return {"rho": None, "groups": 0, "trades": 0}
    stats = frame.groupby("g")["y"].agg(["count", "mean"])
    k, n = len(stats), int(stats["count"].sum())
    if k < 2 or n <= k:
        return {"rho": None, "groups": k, "trades": n}
    grand = frame["y"].mean()
    msb = float((stats["count"] * (stats["mean"] - grand) ** 2).sum() / (k - 1))
    within = frame["y"] - frame["g"].map(stats["mean"])
    msw = float((within ** 2).sum() / (n - k))
    n0 = (n - float((stats["count"] ** 2).sum()) / n) / (k - 1)
    rho = (msb - msw) / (msb + (n0 - 1) * msw) if (msb + (n0 - 1) * msw) > 0 else None
    multi = stats["count"] > 1
    return {"rho": None if rho is None else round(float(rho), 4), "groups": k, "trades": n,
            "mean_cluster_size": round(n / k, 3), "share_trades_in_shared_dates":
                round(float(stats.loc[multi, "count"].sum()) / n, 4),
            "max_cluster_size": int(stats["count"].max())}


def _bps(x) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x) * 10_000, 1)


def dispersion(trades: pd.DataFrame) -> dict:
    if trades is None or trades.empty:
        return {"trades": 0}
    net = trades["net_return"].to_numpy(float)
    resid = trades["resid_spy"].to_numpy(float)
    resid = resid[np.isfinite(resid)]
    barrier = trades["barrier"].astype(str)
    stops = barrier.isin(["stop_loss", "stop_loss_gap"])
    gaps = barrier == "stop_loss_gap"
    stop_pct = trades["stop_loss_pct"].to_numpy(float) if "stop_loss_pct" in trades else np.full(len(trades), np.nan)
    beyond = (-(net[gaps.to_numpy()]) - stop_pct[gaps.to_numpy()]) if gaps.any() else np.array([])
    out = {
        "trades": int(len(trades)), "mean_bps": _bps(net.mean()), "std_bps": _bps(net.std(ddof=1)),
        "resid_mean_bps": _bps(resid.mean()) if resid.size else None,
        "resid_std_bps": _bps(resid.std(ddof=1)) if resid.size > 1 else None,
        "resid_sector_std_bps": _bps(np.nanstd(trades["resid_sector"].to_numpy(float), ddof=1)),
        "quantiles_bps": {f"q{q:g}": _bps(np.quantile(net, q)) for q in QUANTILES},
        "resid_quantiles_bps": {f"q{q:g}": _bps(np.quantile(resid, q)) for q in QUANTILES} if resid.size else {},
        "worst_bps": _bps(net.min()), "best_bps": _bps(net.max()),
        "worst_resid_bps": _bps(resid.min()) if resid.size else None,
        "stop_exits": int(stops.sum()), "gap_through_stop": int(gaps.sum()),
        "gap_share_of_stops": round(float(gaps.sum() / stops.sum()), 4) if stops.sum() else None,
        "gap_share_of_trades": round(float(gaps.mean()), 4),
        "stop_pct": _bps(float(np.nanmedian(stop_pct))) if np.isfinite(stop_pct).any() else None,
        "worst_stop_exit_bps": _bps(net[stops.to_numpy()].min()) if stops.any() else None,
        "gap_beyond_stop_bps": ({"mean": _bps(beyond.mean()), "p95": _bps(np.quantile(beyond, 0.95)),
                                 "max": _bps(beyond.max())} if beyond.size else None),
        "kurtosis": round(float(pd.Series(net).kurt() + 3), 2) if len(net) > 3 else None,
    }
    tail = max(-np.quantile(resid, 0.001), np.quantile(resid, 0.999)) if resid.size else None
    out["abs_resid_q999_bps"] = _bps(tail) if tail is not None else None
    return out


def paired(base: pd.DataFrame, cand: pd.DataFrame, *, adjust: bool) -> dict | None:
    """Candidate minus live per decision over the union of the two setups' decisions (zero where both take it)."""
    column = "resid_spy" if adjust else "net_return"
    if base is None or cand is None:
        return None
    keys = ["symbol", "decision_date"]
    b = base[keys + [column, "entry_date", "exit_date"]].rename(columns={column: "rb"})
    c = cand[keys + [column, "entry_date", "exit_date"]].rename(columns={column: "rc"})
    merged = b.merge(c, on=keys, how="outer", suffixes=("_b", "_c"))
    if merged.empty:
        return None
    d = merged["rc"].fillna(0.0) - merged["rb"].fillna(0.0)
    d = d[np.isfinite(d)]
    if d.size < 2:
        return None
    entry = merged["entry_date_c"].fillna(merged["entry_date_b"]).astype(str)
    exit_ = merged["exit_date_c"].fillna(merged["exit_date_b"]).astype(str)
    return {"decisions": int(d.size), "nonzero_share": round(float((d.abs() > 1e-12).mean()), 4),
            "mean_bps": _bps(d.mean()), "std_bps": _bps(d.std(ddof=1)),
            "abs_q999_bps": _bps(np.quantile(d.abs(), 0.999)), "adjusted_for_spy": adjust,
            "series": pd.DataFrame({"value_bps": d.to_numpy() * 10_000, "entry_date": entry.loc[d.index].to_numpy(),
                                    "exit_date": exit_.loc[d.index].to_numpy()})}


INTRABAR_EXITS = ("stop_loss", "take_profit")


def cost_artifacts(trades: pd.DataFrame) -> np.ndarray:
    """Trades no real fill can produce: an intrabar stop or target exit losing more than 2 x the stop + 100 bps.

    An intrabar stop fills at the stop level, so such a loss can only come
    from the cost model blowing up on corrupt bars (a few delisted names in
    the cached history: CPWR, EP, PARA, SII, GENZ).  Gaps through the stop
    (``stop_loss_gap``) are real and kept.
    """
    if trades is None or trades.empty or "stop_loss_pct" not in trades:
        return np.zeros(0 if trades is None else len(trades), dtype=bool)
    stop = trades["stop_loss_pct"].fillna(0.0).to_numpy(float)
    return (trades["barrier"].astype(str).isin(INTRABAR_EXITS).to_numpy()
            & (trades["net_return"].to_numpy(float) < -(2 * stop + 0.01)))


def clean_trades(trades: dict) -> tuple[dict, dict]:
    """Drop cost artifacts from every replayed setup; returns the cleaned map and what was dropped."""
    out, dropped, symbols = {}, 0, {}
    for key, frame in trades.items():
        mask = cost_artifacts(frame)
        if mask.any():
            dropped += int(mask.sum())
            for symbol, count in frame.loc[mask, "symbol"].value_counts().items():
                symbols[symbol] = max(symbols.get(symbol, 0), int(count))
        out[key] = frame.loc[~mask].reset_index(drop=True) if mask.any() else frame
    return out, {"trades_dropped_all_setups": dropped, "symbols": dict(sorted(symbols.items(), key=lambda kv: -kv[1])),
                 "rule": "intrabar stop/target exit losing more than 2 x stop + 100 bps (cost model on corrupt bars)"}


def _round_up(value: float, step: float) -> float:
    return float(math.ceil(value / step) * step)


# ----------------------------------------------------------------- replay

def live_parameters(strategy: str) -> dict:
    from app.services.seed import MARKET_GATED_STRATEGIES, VIX_GATED_STRATEGIES

    params = {"market_gate_sma": MARKET_GATED_STRATEGIES.get(strategy, 0)}
    params.update(VIX_GATED_STRATEGIES.get(strategy, {}))
    return params


def run_replay(args) -> dict:
    settings.meta_label_history_dir = str(args.data_dir)
    from app.services import experiment_engine as engine
    from app.services import gate_replay
    from app.services import meta_label_primary as primary
    from app.services import point_in_time_universe as pit
    from app.services.earnings_history import load_surprises
    from app.services.meta_label_learning import history_dir
    from app.services.strategy_discovery import load_frames
    from app.services.strategy_scorecard import load_daily_history

    now = datetime.now(timezone.utc)
    timings = {}
    t0 = time.time()
    membership = pit.load_membership(download=False, now=now)

    def loader(ticker: str) -> pd.DataFrame:
        return load_daily_history(pit.yahoo_symbol(ticker), history_dir(), download=False)

    def earnings_loader(symbol: str) -> pd.DataFrame:
        return load_surprises(symbol, history_dir(), download=False)

    coverage = pit.coverage(membership, loader=loader, now=now, start=args.start, cache_path=pit.coverage_path())
    base = sorted({s.upper() for s in DEFAULT_PAPER_EXECUTION_SYMBOLS})
    symbols = pit.replay_symbols(base, coverage)
    frames, missing = load_frames(symbols, loader=loader, start=args.start, earnings_loader=earnings_loader)
    spy = frames["SPY"]
    vix = pd.read_csv(args.vix_csv) if args.vix_csv and Path(args.vix_csv).exists() else None
    timings["load_seconds"] = round(time.time() - t0, 1)
    t1 = time.time()
    replays = {symbol: primary.replay_signals(frame) for symbol, frame in frames.items()}
    timings["signals_seconds"] = round(time.time() - t1, 1)
    breadth = None
    try:
        from app.services.market_gauges import breadth_series, load_breadth_history

        breadth = breadth_series(load_breadth_history(now=now, membership=membership, loader=loader))
    except Exception as exc:
        print(f"breadth history unavailable, breadth gate replays fail open: {exc}", file=sys.stderr)

    cache: dict = {}

    def simulate(strategy, frames_, spy_, setup, replays_, **kwargs):
        key = (strategy, json.dumps(setup, sort_keys=True, default=str))
        if key not in cache:
            kwargs["calibration"] = None
            trades = gate_replay.simulate(strategy, frames_, spy_, setup, replays_, **kwargs)
            cache[key] = trades
        return cache[key]

    strategies = [s for s in gate_replay.STRATEGIES if not args.strategies or s in args.strategies]
    templates = [*engine.GATE_TEMPLATES, *engine.EXIT_TEMPLATES]
    experiments = []
    t2 = time.time()
    for strategy in strategies:
        row = SimpleNamespace(id=None, strategy_type=strategy, parameters=live_parameters(strategy))
        for template in templates:
            ts = time.time()
            experiment = engine.run_experiment(row, template, frames=frames, spy=spy, vix=vix, replays=replays,
                                               simulate=simulate, universe=membership.mask, breadth=breadth)
            experiment["seconds"] = round(time.time() - ts, 1)
            experiments.append(experiment)
            print(f"{strategy:26s} {template.name:18s} adopted={experiment['adopted']} "
                  f"({experiment['seconds']}s)", file=sys.stderr, flush=True)
    timings["replay_seconds"] = round(time.time() - t2, 1)
    trades = {}
    for (strategy, setup_json), frame in cache.items():
        if frame is None or frame.empty:
            trades[(strategy, setup_json)] = pd.DataFrame(columns=[*KEEP_COLUMNS, "spy_ret", "sector_ret",
                                                                   "resid_spy", "resid_sector"])
            continue
        kept = frame[[c for c in KEEP_COLUMNS if c in frame]].reset_index(drop=True)
        trades[(strategy, setup_json)] = adjusted(kept, frames, spy)
    return {"experiments": experiments, "trades": trades, "spy": spy[["date", "open", "close"]].copy(),
            "symbols": len(frames), "missing": missing, "timings": timings, "breadth": breadth is not None,
            "vix": vix is not None, "coverage_since_2010": coverage.get("coverage_since_2010")}


def setup_key(strategy: str, setup: dict) -> tuple:
    return (strategy, json.dumps(setup, sort_keys=True, default=str))


def measure(replay: dict, *, since: str | None = None) -> dict:
    """Per strategy dispersion and rho, per (strategy, template, candidate) paired differences."""
    from app.services import experiment_engine as engine
    from app.services.verdict_stats import REVERSION_CLASS, TECHNICAL_CLASS, adjusts_for_spy, strategy_class

    def window(frame: pd.DataFrame) -> pd.DataFrame:
        if since is None or frame.empty:
            return frame
        return frame.loc[pd.to_datetime(frame["decision_date"]) >= pd.Timestamp(since)]

    per_strategy, paired_rows, book = {}, [], []
    for experiment in replay["experiments"]:
        strategy = experiment["strategy_type"]
        base_trades = window(replay["trades"][setup_key(strategy, experiment["setups"]["baseline"])])
        if strategy not in per_strategy:
            disp = dispersion(base_trades)
            disp["rho_entry_date"] = icc(base_trades["resid_sector"].to_numpy(float),
                                         base_trades["entry_date"].astype(str).to_numpy()) if len(base_trades) else {}
            disp["rho_entry_date_spy_only"] = icc(base_trades["resid_spy"].to_numpy(float),
                                                  base_trades["entry_date"].astype(str).to_numpy()) \
                if len(base_trades) else {}
            disp["class"] = strategy_class(strategy)
            disp["live_setup"] = experiment["setups"]["baseline"]
            per_strategy[strategy] = disp
            if len(base_trades):
                book.append(base_trades.assign(strategy=strategy))
        template = engine.TEMPLATE_BY_NAME[experiment["template"]]
        adjust = adjusts_for_spy(template.name)
        for label, setup in experiment["setups"].items():
            if label == "baseline":
                continue
            cand = window(replay["trades"][setup_key(strategy, setup)])
            result = paired(base_trades, cand, adjust=adjust)
            if result is None:
                continue
            series = result.pop("series")
            paired_rows.append({"strategy_type": strategy, "class": strategy_class(strategy),
                                "template": template.name, "candidate": label, **result,
                                "rho_entry_date": icc(series["value_bps"].to_numpy(),
                                                      series["entry_date"].to_numpy())["rho"]})
    book_frame = pd.concat(book, ignore_index=True) if book else pd.DataFrame()
    book_rho = icc(book_frame["resid_sector"].to_numpy(float), book_frame["entry_date"].astype(str).to_numpy()) \
        if len(book_frame) else {}
    rhos = [(v["rho_entry_date"].get("rho"), v["rho_entry_date"].get("trades", 0)) for v in per_strategy.values()
            if v.get("rho_entry_date", {}).get("rho") is not None]
    rho = sum(r * n for r, n in rhos) / sum(n for _, n in rhos) if rhos else 0.0
    classes = {TECHNICAL_CLASS: [], REVERSION_CLASS: []}
    for strategy, disp in per_strategy.items():
        classes[disp["class"]].append(disp)
    bounds, sigmas, breach = {}, {}, {}
    for cls, members in classes.items():
        tails = [m["abs_resid_q999_bps"] for m in members if m.get("abs_resid_q999_bps")]
        stds = [m["resid_std_bps"] for m in members if m.get("resid_std_bps")]
        bounds[cls] = _round_up(max(tails), 100.0) if tails else None
        sigmas[cls] = _round_up(1.1 * max(stds), 50.0) if stds else None
    for strategy, disp in per_strategy.items():
        frame = window(replay["trades"][setup_key(strategy, disp["live_setup"])])
        bound = bounds.get(disp["class"])
        if bound and len(frame):
            disp["breach_rate_at_bound"] = round(float((frame["resid_spy"].abs() * 10_000 > bound).mean()), 5)
    paired_by_strategy = {}
    for row in paired_rows:
        entry = paired_by_strategy.setdefault(row["strategy_type"], [])
        entry.append(row["std_bps"])
    return {
        "since": since, "per_strategy": per_strategy, "paired": paired_rows,
        "paired_std_bps": {k: {"median": round(float(np.median(v)), 1), "max": round(float(np.max(v)), 1),
                               "candidates": len(v)} for k, v in paired_by_strategy.items()},
        "book_rho": book_rho, "rho": round(max(rho, 0.0), 4),
        "loss_bound_bps": bounds, "sigma_bps": sigmas,
    }


# ----------------------------------------------------------------- ledger

def measure_ledger(database_url: str) -> dict:
    """The bot's own trades and shadow scores, when a ledger is reachable."""
    settings.database_url = database_url
    from app.db.session import SessionLocal
    from app.services.experiment_engine import SOURCES
    from app.services.gate_cost import daily_frame
    from app.services.setup_shadow import latest_scores
    from app.services.trade_attribution import current_attributions
    from app.services.verdict_stats import residual_trades

    db = SessionLocal()
    try:
        spy = daily_frame(db, "SPY")
        rows = current_attributions(db, sources=SOURCES)
        by_strategy: dict[str, list] = {}
        for row in rows:
            by_strategy.setdefault(row.strategy_type, []).append(row)
        out = {}
        for strategy, members in by_strategy.items():
            raw = np.array([float(r.net_return if r.net_return is not None else r.gross_return) for r in members])
            resid = residual_trades(members, spy=spy)
            values = np.array([v for _, _, v in resid])
            entries = np.array([e for e, _, _ in resid])
            out[strategy] = {"trades": len(members), "std_bps": _bps(raw.std(ddof=1)) if raw.size > 1 else None,
                             "resid_std_bps": round(float(values.std(ddof=1)), 1) if values.size > 1 else None,
                             "rho_entry_date": icc(values, entries) if values.size > 1 else {},
                             "enough": len(members) >= MIN_LEDGER}
        shadow = latest_scores(db) or {}
        return {"reachable": True, "strategies": out, "attributions": len(rows),
                "shadow_decisions": len(shadow.get("decisions") or []), "shadow_as_of": shadow.get("as_of")}
    except Exception as exc:
        return {"reachable": False, "error": f"{exc.__class__.__name__}: {exc}"}
    finally:
        db.close()


# ----------------------------------------------------------------- report

def _fmt(value, digits=0, sign=False) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.{digits}f}" if sign else f"{float(value):.{digits}f}"


def render(result: dict) -> str:
    m, m20 = result["all"], result["2020+"]
    day = result["ran_at"][:10]
    lines = [f"# Verdict inputs: {day}", "",
             "**Question:** what per-trade dispersion, tails, worst-case loss given the stops, paired shadow-"
             "difference variance and same-date correlation do the bot's strategies actually have? The research "
             "behind the always-valid verdict (`app/services/verdict_stats.py`) sized its tests on an assumed 2% "
             "per-trade std and no correlation; these are the measured values the verdict code now reads.", "",
             f"**Source:** {result['source_note']}", "",
             "## Values the verdict code reads", "",
             "| Input | Technical | Reversion | Where it acts |", "|---|---|---|---|",
             f"| Loss bound B (bps, abs residual q99.9, rounded up) | {_fmt(m['loss_bound_bps'].get('technical'))} | "
             f"{_fmt(m['loss_bound_bps'].get('reversion'))} | betting e-process support [-B, B]; a trade beyond it raises "
             "the alarm and switches to the Catoni process |",
             f"| Variance bound sigma (bps, 1.1 x max residual std) | {_fmt(m['sigma_bps'].get('technical'))} | "
             f"{_fmt(m['sigma_bps'].get('reversion'))} | Catoni-style fallback (Wang & Ramdas 2023, Assumption 2) |",
             f"| Same-date residual correlation rho | {_fmt(m['rho'], 3)} (all classes) | | effective trades, "
             "simulation and group-sequential comparator |", "",
             f"Constants in `verdict_stats.MEASURED_INPUTS` (as of {day}); `--record` writes the same values as a "
             "`verdict_inputs` audit row, which the judge reads in preference.", "",
             "## 1. Per-trade dispersion by strategy (2000+, point-in-time universe, live setup)", "",
             "| Strategy | Trades | Mean | Std | Resid std | Resid+sector std | Kurtosis | q0.1% | q1% | q99% | q99.9% | Worst | 2020+ std |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for strategy, d in m["per_strategy"].items():
        q = d.get("quantiles_bps") or {}
        d20 = m20["per_strategy"].get(strategy, {})
        lines.append(f"| {strategy} | {d['trades']} | {_fmt(d.get('mean_bps'), 1, True)} | {_fmt(d.get('std_bps'))} | "
                     f"{_fmt(d.get('resid_std_bps'))} | {_fmt(d.get('resid_sector_std_bps'))} | {_fmt(d.get('kurtosis'), 1)} | "
                     f"{_fmt(q.get('q0.001'))} | {_fmt(q.get('q0.01'))} | {_fmt(q.get('q0.99'))} | {_fmt(q.get('q0.999'))} | "
                     f"{_fmt(d.get('worst_bps'))} | {_fmt(d20.get('std_bps'))} |")
    lines += ["", "All figures in bps of net return per trade (costs on both sides, published cost model). Resid = "
              "minus SPY over the same hold (entry open to exit open/close); resid+sector = minus the sector ETF "
              "where `market_gate.sector_symbol` maps one (today's list), SPY otherwise.", "",
              "## 2. Stops, gaps and the realistic worst case", "",
              "| Strategy | Stop | Stop exits | Gaps through stop | Share of stops | Share of trades | Gap past stop: mean / p95 / max | Worst stop exit | abs resid q99.9 | Breach rate at B |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for strategy, d in m["per_strategy"].items():
        g = d.get("gap_beyond_stop_bps") or {}
        lines.append(f"| {strategy} | {_fmt(d.get('stop_pct'))} | {d.get('stop_exits')} | {d.get('gap_through_stop')} | "
                     f"{_fmt((d.get('gap_share_of_stops') or 0) * 100, 1)}% | {_fmt((d.get('gap_share_of_trades') or 0) * 100, 2)}% | "
                     f"{_fmt(g.get('mean'))} / {_fmt(g.get('p95'))} / {_fmt(g.get('max'))} | {_fmt(d.get('worst_stop_exit_bps'))} | "
                     f"{_fmt(d.get('abs_resid_q999_bps'))} | {_fmt((d.get('breach_rate_at_bound') or 0) * 100, 3)}% |")
    lines += ["", "## 3. Paired shadow differences (candidate minus live, per decision)", "",
              "Over the union of the two setups' decisions, zero where both take the same trade; SPY-adjusted "
              "except for the market-timing gates (market, VIX, breadth), whose mechanism is the market move.", "",
              "| Strategy | Template | Candidate | Decisions | Nonzero | Mean | Std | abs q99.9 | rho (entry date) |",
              "|---|---|---|---|---|---|---|---|---|"]
    for row in m["paired"]:
        lines.append(f"| {row['strategy_type']} | {row['template']} | {row['candidate']} | {row['decisions']} | "
                     f"{_fmt(row['nonzero_share'] * 100, 1)}% | {_fmt(row['mean_bps'], 1, True)} | {_fmt(row['std_bps'])} | "
                     f"{_fmt(row['abs_q999_bps'])} | {_fmt(row['rho_entry_date'], 3)} |")
    lines += ["", "Per strategy, the std of the paired difference across its candidates (median / max):", "",
              "| Strategy | Candidates | Median std | Max std |", "|---|---|---|---|"]
    for strategy, v in m["paired_std_bps"].items():
        lines.append(f"| {strategy} | {v['candidates']} | {_fmt(v['median'])} | {_fmt(v['max'])} |")
    lines += ["", "## 4. Same-date residual correlation", "",
              "| Strategy | rho (SPY + sector) | rho (SPY only) | Entry dates | Mean cluster | Trades on shared dates | Max cluster | rho 2020+ |",
              "|---|---|---|---|---|---|---|---|"]
    for strategy, d in m["per_strategy"].items():
        r, rs = d.get("rho_entry_date") or {}, d.get("rho_entry_date_spy_only") or {}
        r20 = (m20["per_strategy"].get(strategy, {}).get("rho_entry_date") or {})
        lines.append(f"| {strategy} | {_fmt(r.get('rho'), 3)} | {_fmt(rs.get('rho'), 3)} | {r.get('groups')} | "
                     f"{_fmt(r.get('mean_cluster_size'), 2)} | {_fmt((r.get('share_trades_in_shared_dates') or 0) * 100, 1)}% | "
                     f"{r.get('max_cluster_size')} | {_fmt(r20.get('rho'), 3)} |")
    br = m.get("book_rho") or {}
    lines += ["", f"Book (all strategies pooled): rho {_fmt(br.get('rho'), 3)} over {br.get('groups')} entry dates, "
              f"mean cluster {_fmt(br.get('mean_cluster_size'), 2)}. The verdict judges one strategy's change, so the "
              f"per-strategy trade-weighted value ({_fmt(m['rho'], 3)}) is the one it reads.", ""]
    art = result.get("artifacts") or {}
    if art.get("trades_dropped_all_setups"):
        lines += ["## Data hygiene", "",
                  f"Dropped before every measurement: {art['trades_dropped_all_setups']} replayed trades across all "
                  f"setups that no real fill can produce ({art['rule']}), from "
                  + ", ".join(f"{k} ({v})" for k, v in list(art["symbols"].items())[:12])
                  + ". They are corrupt daily bars of delisted names in the cached point-in-time history; at a "
                  "51% loss each they set the technical class's 0.1% tail on their own. The engine's replay rows "
                  "(and the decisions in `reports/verdict-replay-20261107.md`, experiment 3) still contain them, "
                  "mostly on both sides of each comparison.", ""]
    lines += result.get("findings", [])
    led = result.get("ledger") or {}
    lines += ["", "## 5. The bot's own ledger", ""]
    if led.get("reachable"):
        lines.append(f"{led['attributions']} closed paper trades, {led['shadow_decisions']} setup-shadow decisions "
                     f"(as of {led.get('shadow_as_of')}).")
        for strategy, v in led["strategies"].items():
            lines.append(f"* {strategy}: {v['trades']} trades, std {_fmt(v['std_bps'])} bps, residual std "
                         f"{_fmt(v['resid_std_bps'])} bps, rho {_fmt((v['rho_entry_date'] or {}).get('rho'), 3)}"
                         + ("" if v["enough"] else f" (under {MIN_LEDGER}: replay value used)"))
    else:
        lines.append("Not reachable from where this ran (" + str(led.get("error") or "no --database-url") + "); every "
                     "value above is from the replay. Rerun with `--database-url` on the host, and `--record` to store "
                     "the ledger-based values as the `verdict_inputs` row, once the strategies have at least "
                     f"{MIN_LEDGER} closed paper trades each.")
    t = result["timings"]
    lines += ["", "## 6. Run", "",
              f"{result['symbols']} symbols ({len(result['missing'])} without history), VIX {'present' if result['vix'] else 'absent'}, "
              f"breadth history {'present' if result['breadth'] else 'absent (breadth gate replays fail open)'}, "
              f"coverage since 2010 {_fmt((result.get('coverage_since_2010') or 0) * 100, 1)}%. Load {t.get('load_seconds')}s, "
              f"signals {t.get('signals_seconds')}s, replays {t.get('replay_seconds')}s.", ""]
    return "\n".join(lines) + "\n"


def findings(result: dict) -> list[str]:
    m = result["all"]
    stds = [d["std_bps"] for d in m["per_strategy"].values() if d.get("std_bps")]
    rstd = [d["resid_std_bps"] for d in m["per_strategy"].values() if d.get("resid_std_bps")]
    pstd = [r["std_bps"] for r in m["paired"] if r.get("std_bps")]
    nz = [r["nonzero_share"] for r in m["paired"] if r.get("nonzero_share") is not None]
    return ["## What changes against the research's assumptions", "",
            f"* Per-trade std: {min(stds):.0f} to {max(stds):.0f} bps across strategies (median {np.median(stds):.0f}), "
            f"against the assumed 200. SPY adjustment brings it to a median {np.median(rstd):.0f} bps.",
            f"* Paired differences: a median {np.median(pstd):.0f} bps std per decision (range {min(pstd):.0f} to "
            f"{max(pstd):.0f}), because a median {np.median(nz) * 100:.0f}% of decisions differ between the two setups "
            "at all; pairing is where the variance reduction comes from, not the SPY adjustment.",
            f"* Same-date residual correlation: rho = {m['rho']:.3f} (trade-weighted over strategies). The design effect "
            "for a strategy that enters m names on one date is 1 + (m - 1) rho.",
            f"* Loss bounds: technical {_fmt(m['loss_bound_bps'].get('technical'))} bps, reversion "
            f"{_fmt(m['loss_bound_bps'].get('reversion'))} bps; gaps through the stop are reported per strategy in "
            "section 2.", ""]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--vix-csv", type=Path)
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--strategies", nargs="*")
    parser.add_argument("--database-url")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--record", action="store_true", help="write a verdict_inputs audit row (needs --database-url)")
    parser.add_argument("--from-cache", type=Path,
                        help="reuse a --cache pickle (and the JSON of that run via --meta) instead of replaying")
    parser.add_argument("--meta", type=Path, help="the JSON output of the run that wrote --from-cache (timings, symbols)")
    args = parser.parse_args()
    warnings.filterwarnings("ignore")
    ledger = measure_ledger(args.database_url) if args.database_url else {"reachable": False}
    if args.from_cache:
        with open(args.from_cache, "rb") as fh:
            cached = pickle.load(fh)
        meta = json.loads(args.meta.read_text()) if args.meta else {}
        replay = {"experiments": cached["experiments"], "trades": cached["trades"], "spy": cached["spy"],
                  "symbols": meta.get("symbols"), "missing": meta.get("missing") or [],
                  "timings": meta.get("timings") or {}, "breadth": meta.get("breadth"), "vix": meta.get("vix"),
                  "coverage_since_2010": meta.get("coverage_since_2010")}
    else:
        replay = run_replay(args)
    replay["trades"], artifacts = clean_trades(replay["trades"])
    result = {"ran_at": datetime.now(timezone.utc).isoformat(), "paper_only": True, "data_dir": str(args.data_dir),
              "start": args.start, "all": measure(replay), "2020+": measure(replay, since="2020-01-01"),
              "ledger": ledger, "timings": replay["timings"], "symbols": replay["symbols"],
              "missing": replay["missing"], "vix": replay["vix"], "breadth": replay["breadth"],
              "coverage_since_2010": replay["coverage_since_2010"], "artifacts": artifacts}
    enough = [s for s, v in (ledger.get("strategies") or {}).items() if v.get("enough")]
    result["source_note"] = (
        (f"the bot's ledger for {', '.join(enough)}; the replay for the rest. " if enough else
         "the bot's ledger was not reachable or had too few trades, so every value is from the replay: ")
        + f"`gate_replay.simulate` of the 9 live replayable strategies under their live setups on the point-in-time "
          f"S&P 500 universe ({replay['symbols']} symbols, membership mask), {args.start} onward, published cost model, "
          "run through `experiment_engine.run_experiment` for every gate and exit template.")
    result["findings"] = findings(result)
    m = result["all"]
    result["inputs"] = {"as_of": result["ran_at"][:10], "source": "ledger+replay" if enough else "replay_point_in_time",
                        "loss_bound_bps": m["loss_bound_bps"], "sigma_bps": m["sigma_bps"], "rho": m["rho"],
                        "per_trade_std_bps": {s: d.get("std_bps") for s, d in m["per_strategy"].items()},
                        "paired_std_bps": {s: v["median"] for s, v in m["paired_std_bps"].items()}}
    args.output.write_text(json.dumps(result, indent=2, default=str))
    if args.report:
        args.report.write_text(render(result))
    if args.cache and args.cache != args.from_cache:
        with open(args.cache, "wb") as fh:
            pickle.dump({"experiments": replay["experiments"], "trades": replay["trades"], "spy": replay["spy"],
                         "inputs": result["inputs"], "measured": {"all": m, "2020+": result["2020+"]}}, fh)
    if args.record:
        if not args.database_url:
            raise SystemExit("--record needs --database-url")
        from app.db.session import SessionLocal
        from app.services.verdict_stats import record_inputs

        db = SessionLocal()
        try:
            record_inputs(db, result["inputs"])
        finally:
            db.close()
    print(json.dumps(result["inputs"], indent=2))


if __name__ == "__main__":
    main()
