"""Step 1's pass-rule check: re-score the seven failed searches under the new yardstick.

``reports/Retail bot edge feasibility.md`` (step 1, pre-registered): "Re-scored
under the new yardstick, all seven failed searches still fail their net or
excess-return checks.  A failed search that now passes means the gate got
looser; fix it before step 2."  The seven (section 1 and the conclusion: the
rule searches asked whether a candidate beat zero or the stock's own drift,
the book-level searches whether a change beat the existing book):

* rule searches: the discovery library (rule mutation, 422 trials), sector-
  relative reversal, the liquid-universe check (the core list's signals),
  news entries;
* book-level searches: regime allocation, position sizing, the inverse-ETF hedge.

The yardstick (``app/services/yardstick.py``) reads each candidate at 5, 10
and 25 bps a round trip and at the published model, scores it against a
volatility-matched SPY/T-bill arm (month-clustered t on the daily active
return) and on the per-trade SPY residual (entry-date clustered t), adds its
turnover and applies the short-hold veto.  A candidate's **net check** passes
when its net expectancy's month-clustered 95% lower bound is above zero (the
discovery gate's net check); its **excess check** passes when both the excess
over the passive arm and the SPY residual have t >= 2 with a positive mean.
A search passes the new yardstick only if some candidate passes both (and the
short-hold veto); the pre-registered expectation is that none does.

Stages (cache only, no network; ``--data-dir`` is the yfinance cache the
download step filled, ``meta_label_history_dir`` is pointed at it):

* ``book``: today's 45 names, every replayable strategy under its live gates,
  the live slots (``run_slot_allocation_backtest.allocate``): the liquid-
  universe core list (pooled signals), regime allocation's arms, position
  sizing's arms and the SH hedge sizes;
* ``pit``: the point-in-time S&P 500 (``point_in_time_universe``): the
  discovery library (no mutation children; they live in the host's trial
  ledger) and, inside it, sector reversal's default rule.

News entries are not re-run: the tone history is an Alpaca news backfill that
lives on the host, and earnings surprises are not re-downloaded for 700 names.

    PYTHONPATH=. python scripts/run_yardstick_check.py --stage book --data-dir <dir> --vix-csv <csv> \\
        --irx-csv <csv> --output <json>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.services import gate_replay, yardstick  # noqa: E402
from app.services import meta_label_primary as primary  # noqa: E402
from app.services.strategy_scorecard import trade_statistics  # noqa: E402

START_2015 = "2015-01-01"
WINDOWS = {"all": (None, None), "2015+": (START_2015, None)}


def _json(value):
    from app.services.experiment_engine import _clean

    return json.loads(json.dumps(_clean(value), default=str))


def _csv(path: Path | None) -> pd.DataFrame | None:
    if path is None or not path.exists():
        return None
    frame = pd.read_csv(path)
    frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
    return frame.dropna(subset=["close"])


def checks(score: dict, window: str, passive_window: str | None = None) -> dict:
    """The net and excess checks of one candidate in one window, at every cost column."""
    line = yardstick.compact(score, window, passive_window)
    net_ok = {column: bool(ci and ci[0] > 0) for column, ci in (line.get("net_ci_bps") or {}).items()}
    acting = net_ok.get(yardstick.ACTING, False)
    return {**line, "net_check": net_ok, "net_passes_acting": acting,
            "net_passes_any_cost": any(net_ok.values()),
            "passes_new_yardstick": bool(acting and line["excess_passes"] and not line["vetoed"]),
            "passes_at_5bps": bool(net_ok.get("5bps") and line["excess_passes"] and not line["vetoed"])}


# ----------------------------------------------------------------- book stage

def weighted_bracket(trades: pd.DataFrame, weights: np.ndarray, model_cost: np.ndarray, start) -> dict:
    """Per-trade weighted net expectancy (month-clustered CI) at each cost; weights as the book sized them."""
    mask = np.ones(len(trades), dtype=bool) if start is None else \
        (pd.to_datetime(trades["decision_date"]) >= pd.Timestamp(start)).to_numpy()
    w = np.asarray(weights, float)[mask]
    gross = trades["gross_return"].to_numpy(float)[mask]
    net = trades["net_return"].to_numpy(float)[mask]
    months = pd.to_datetime(trades["decision_date"]).dt.to_period("M").astype(str).to_numpy()[mask].tolist()
    out = {}
    columns = {**{c: gross - b / 10_000 for c, b in zip(yardstick.FLAT_COLUMNS, yardstick.BRACKET_BPS)},
               yardstick.MODEL: gross - model_cost[mask], yardstick.ACTING: net}
    for column, values in columns.items():
        ok = np.isfinite(values)
        if not ok.any() or w[ok].mean() <= 0:
            out[column] = {"trades": 0}
            continue
        stats = trade_statistics(w[ok] * values[ok] / w[ok].mean(), clusters=[m for m, k in zip(months, ok) if k])
        out[column] = {"trades": stats["trades"], "expectancy_bps": stats["expectancy_bps"],
                       "expectancy_ci_bps": stats.get("expectancy_ci_bps")}
    return out


def book_daily(book, trades: pd.DataFrame, weights: np.ndarray, start, adjust: np.ndarray | None = None) -> pd.Series:
    """The book's daily return under ``weights`` (``adjust``: extra return per trade booked on its exit day)."""
    w = np.asarray(weights, dtype=float).copy()
    if start is not None:
        w[book.decision < np.datetime64(pd.Timestamp(start))] = 0.0
    daily = np.bincount(book.day_ids, weights=w[book.trade_ids] * book.marks, minlength=len(book.dates))
    if adjust is not None:
        exits = np.searchsorted(book.dates, pd.to_datetime(trades["exit_date"]).to_numpy(dtype="datetime64[ns]"))
        daily = daily + np.bincount(exits, weights=w * np.nan_to_num(adjust), minlength=len(book.dates))
    series = pd.Series(daily, index=pd.DatetimeIndex(book.dates))
    return series.loc[series.index >= pd.Timestamp(start)] if start is not None else series


def score_book(book, trades, weights, model_cost, frames, spy, irx) -> dict:
    """One book arm: weighted per-trade bracket, the daily book against the passive arm at the model and 5 bps."""
    out = {}
    for window, (start, _end) in WINDOWS.items():
        row = {"bracket": weighted_bracket(trades, weights, model_cost, start)}
        for label, adjust in (("model", None), ("5bps", model_cost - 5 / 10_000), ("10bps", model_cost - 10 / 10_000),
                              ("25bps", model_cost - 25 / 10_000)):
            series = book_daily(book, trades, weights, start, adjust)
            series = series.loc[series.index >= series.ne(0).idxmax()] if series.ne(0).any() else series
            passive = yardstick.passive_scores(trades, frames, spy, windows={"w": (start, None)}, irx=irx,
                                               portfolio=series)
            excess = (passive.get("windows") or {}).get("w", {}).get("excess_vs_passive") or {}
            curve = np.concatenate([[1.0], np.cumprod(1 + series.to_numpy(float))])
            years = len(series) / 252
            row[f"vs_passive_{label}"] = {
                "book_return_pct_per_year": round((curve[-1] ** (1 / years) - 1) * 100, 2) if years > 0 else None,
                "book_max_drawdown": round(float(np.max(1 - curve / np.maximum.accumulate(curve))), 4),
                **{k: excess.get(k) for k in ("spy_weight", "passive_return_pct_per_year", "active_pct_per_year",
                                               "t_stat_month_clustered", "passes")}}
            if label == "model":
                row["spy_residual"] = (passive.get("windows") or {}).get("w", {}).get("spy_residual")
        acting_ci = row["bracket"].get(yardstick.ACTING, {}).get("expectancy_ci_bps")
        row["net_passes_acting"] = bool(acting_ci and acting_ci[0] > 0)
        row["net_check"] = {c: bool((v.get("expectancy_ci_bps") or [0])[0] > 0) for c, v in row["bracket"].items()}
        row["excess_passes"] = bool(row["vs_passive_model"].get("passes") and (row.get("spy_residual") or {}).get("passes"))
        row["excess_passes_at_5bps"] = bool(row["vs_passive_5bps"].get("passes")
                                            and (row.get("spy_residual") or {}).get("passes"))
        row["passes_new_yardstick"] = bool(row["net_passes_acting"] and row["excess_passes"])
        row["passes_at_5bps"] = bool(row["net_check"].get("5bps") and row["excess_passes_at_5bps"])
        out[window] = row
    return out


def run_book(args) -> dict:
    import run_hedge_backtest as hedge_bt
    import run_position_sizing_backtest as sizing
    import run_regime_allocation_backtest as regime
    from run_slot_allocation_backtest import allocate
    from app.services.strategy_allocation import bounds

    started = time.time()
    frames, earnings = regime.load_frames(args.data_dir, download=False, start=args.start, end=args.end)
    vix, irx = _csv(args.vix_csv), _csv(args.irx_csv)
    spy = frames["SPY"]
    replays = {s: primary.replay_signals(f) for s, f in frames.items()}
    rows = regime.candidates(frames, spy, vix, replays, earnings=earnings)
    print("candidates", len(rows), round(time.time() - started), flush=True)
    data_end = max(str(f["date"].iloc[-1]) for f in frames.values())
    oos_start = pd.Timestamp(data_end) - pd.DateOffset(years=yardstick.OOS_YEARS)
    result = {"stage": "book", "data_end": data_end, "symbols": len(frames), "candidates": int(len(rows)),
              "earnings_blackout": earnings, "vix": vix is not None, "tbill": irx is not None}

    # Liquid universe: the core list's long-only signals, pooled (every strategy's candidate trades).
    score = yardstick.score_candidate(
        rows, frames, spy=spy, windows=WINDOWS, passive_windows=WINDOWS, ci_windows=WINDOWS.keys(),
        turnover_span=yardstick.span(frames, start=START_2015), oos_start=oos_start, irx=irx)
    result["liquid_universe_core"] = {"score": score, "checks": {w: checks(score, w) for w in WINDOWS}}
    per_strategy = {}
    for strategy, group in rows.groupby("strategy"):
        s = yardstick.score_candidate(group.reset_index(drop=True), frames, spy=spy, windows=WINDOWS,
                                      passive_windows=WINDOWS, ci_windows=WINDOWS.keys(),
                                      turnover_span=yardstick.span(frames, start=START_2015), oos_start=oos_start,
                                      irx=irx)
        per_strategy[strategy] = {w: checks(s, w) for w in WINDOWS}
    result["liquid_universe_by_strategy"] = per_strategy
    print("liquid universe scored", round(time.time() - started), flush=True)

    # The live book: the slots, then each search's arms on it.
    accepted = allocate(rows, frames, regime.BOOK_POLICY).reset_index(drop=True)
    costs = yardstick.trade_costs(accepted, frames)["model"]
    book = sizing.Book(accepted, frames)
    result["book_trades"] = int(len(accepted))
    result["book_turnover_2015"] = yardstick.turnover(
        accepted.loc[pd.to_datetime(accepted["decision_date"]) >= START_2015], start=START_2015, end=data_end,
        slot_fraction=sizing.SLOT)

    inputs = regime.daily_regime_inputs(frames, spy, vix)
    with_cells = accepted.copy()
    with_cells["cell"] = regime.cells_for(with_cells, inputs)
    sized_regime = regime.size_trades(with_cells, bounds())
    result["regime_allocation"] = {arm: score_book(book, accepted, regime.SLOT * sized_regime[f"w_{arm}"].to_numpy(float),
                                                   costs, frames, spy, irx) for arm in regime.ARMS}
    print("regime allocation scored", round(time.time() - started), flush=True)

    sized = sizing.size_trades(accepted)
    result["position_sizing"] = {arm: score_book(book, accepted, sized[f"w_{arm}"].to_numpy(float), costs, frames,
                                                 spy, irx) for arm in sizing.ARMS}
    print("position sizing scored", round(time.time() - started), flush=True)

    from app.services.strategy_scorecard import load_daily_history, normalize_prices

    try:
        sh = normalize_prices(load_daily_history("SH", args.data_dir, download=False), start=args.start, end=args.end)
    except FileNotFoundError:
        sh = None
    hedge, _first = hedge_bt.hedge_frame(spy, sh)
    hedge_trades = hedge_bt.hedge_trades(hedge, spy)
    all_frames = {**frames, hedge_bt.HEDGE_SYMBOL: hedge}
    hedge_trades = hedge_trades.assign(entry_date=[hedge["date"].iloc[i] for i in hedge_trades["entry_idx"]],
                                       exit_date=[hedge["date"].iloc[i] for i in hedge_trades["exit_idx"]])
    leg = yardstick.score_candidate(hedge_trades, all_frames, spy=spy, windows=WINDOWS, passive_windows=WINDOWS,
                                    ci_windows=WINDOWS.keys(), turnover_span=yardstick.span(all_frames, start=START_2015),
                                    oos_start=oos_start, irx=irx)
    combined = allocate(hedge_bt.with_hedge(rows, hedge, hedge_trades), all_frames, hedge_bt.BOOK_POLICY).reset_index(drop=True)
    combined_costs = yardstick.trade_costs(combined, all_frames)["model"]
    combined_book = sizing.Book(combined, all_frames)
    hedges = {"leg": {"score": leg, "checks": {w: checks(leg, w) for w in WINDOWS}}, "books": {}}
    for size in hedge_bt.HEDGE_SIZES:
        weights = np.where(combined["strategy"].to_numpy() == hedge_bt.HEDGE_STRATEGY, size, hedge_bt.SLOT)
        hedges["books"][f"hedge_{size:g}"] = score_book(combined_book, combined, weights, combined_costs, all_frames,
                                                        spy, irx)
    hedges["books"]["baseline"] = score_book(book, accepted, np.full(len(accepted), hedge_bt.SLOT), costs, frames,
                                             spy, irx)
    result["hedge"] = hedges
    print("hedge scored", round(time.time() - started), flush=True)
    result["elapsed_seconds"] = round(time.time() - started)
    return result


# ------------------------------------------------------------------ pit stage

def run_pit(args) -> dict:
    from app.services import point_in_time_universe, strategy_discovery

    started = time.time()
    symbols = sorted({s.upper() for s in settings.paper_execution_symbols})
    context = point_in_time_universe.replay_universe(symbols, download=False)
    frames, missing = strategy_discovery.load_frames(context.symbols, loader=context.loader, start=args.start,
                                                     end=args.end, earnings_loader=lambda s: None,
                                                     news_loader=lambda s: None)
    print("frames", len(frames), "missing", len(missing), round(time.time() - started), flush=True)
    irx = _csv(args.irx_csv)
    library = strategy_discovery.candidate_library(frames.keys())
    if args.families:
        library = [c for c in library if c["family"] in set(args.families.split(","))]
    print("library", len(library), flush=True)
    discovery_context = strategy_discovery.discovery_context(frames, None, irx=irx, download=False)
    family_extra = yardstick.research_family_counts()
    result = strategy_discovery.run_discovery(frames, frames.keys(), library=library, universe=context.mask,
                                              trials=args.trials, context=discovery_context,
                                              family_extra=family_extra)
    print("evaluated", len(result["candidates"]), round(time.time() - started), flush=True)
    rows = []
    for row in result["candidates"]:
        score = row.get("yardstick") or {}
        rows.append({"id": row["id"], "family": row["family"], "universe": row["universe"],
                     "description": row["description"], "tier": row["gate"]["tier"],
                     "failed_checks": [k for k, v in row["gate"]["checks"].items() if not v],
                     "deflated_sharpe": row["gate"]["deflated_sharpe"], "dsr_trials": row["gate"]["dsr_trials"],
                     "trades": row["discovery"]["net"]["trades"],
                     "net_ci_bps": row["discovery"]["net"].get("expectancy_ci_bps"),
                     "excess_over_drift_ci_bps": row["discovery"]["excess_over_drift"].get("expectancy_ci_bps"),
                     "holdout_bps": row["holdout"].get("expectancy_bps"),
                     "discovery": checks(score, "discovery", "discovery"),
                     "since_2015": checks(score, "post_period", "since_2015"),
                     "turnover": score.get("turnover"), "veto": score.get("short_hold_veto"),
                     "rule": row["rule"], "stop_loss_pct": row["stop_loss_pct"],
                     "max_holding_sessions": row["max_holding_sessions"]})
    default = strategy_discovery.sector_reversal_variants()[0][1]
    sector = [r for r in rows if r["family"] == "sector_reversal" and r["rule"] == default]
    full = [row for row in result["candidates"] if row["family"] == "sector_reversal" and row["rule"] == default]
    return {"stage": "pit", "data_end": result["data_end"], "holdout_start": result["holdout_start"],
            "symbols": len(frames), "missing": len(missing), "universe": context.summary(),
            "library": len(library), "trials_charged": args.trials, "family_extra": family_extra,
            "tbill": irx is not None, "candidates": rows,
            "sector_reversal_default": {"row": sector[0] if sector else None,
                                        "yardstick": full[0].get("yardstick") if full else None},
            "elapsed_seconds": round(time.time() - started)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stage", choices=("book", "pit"), required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--vix-csv", type=Path, default=None)
    parser.add_argument("--irx-csv", type=Path, default=None)
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--trials", type=int, default=422, help="library trials charged (the rule-mutation count)")
    parser.add_argument("--families", default=None, help="comma-separated families (pit stage; default all)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    warnings.filterwarnings("ignore")
    settings.meta_label_history_dir = str(args.data_dir)
    gate_replay._calibration_cache.update(at=time.monotonic() + 1e9, value=None)  # published model, no database
    result = run_book(args) if args.stage == "book" else run_pit(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(_json(result), indent=1) + "\n")
    print("wrote", args.output, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
