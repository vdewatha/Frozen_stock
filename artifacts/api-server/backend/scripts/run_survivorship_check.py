"""Measure the survivorship bias of replaying today's 45-symbol list, and decide the universe setting.

Every replay so far (``gate_replay``, the experiment engine, strategy
discovery) ran over ``DEFAULT_PAPER_EXECUTION_SYMBOLS``: megacaps that exist in
2026 plus the index and sector ETFs.  ``app/services/point_in_time_universe.py``
adds the S&P 500 as it stood on each date (free, MIT-licensed membership
history from https://github.com/fja05680/sp500) and every past member whose
daily history yfinance still serves.  This script replays each live
replayable strategy both ways and reports the gap, so the setting
``replay_point_in_time_universe`` is decided on evidence.

Pre-registered decision rule (fixed before the first run)
---------------------------------------------------------
``replay_point_in_time_universe`` is switched **on by default** if, and only
if, both hold:

1. **Coverage.**  The mean yearly coverage since 2010 (members of the year
   with at least 60 bars of history overlapping their membership, over all
   members of the year) is at least 70%.
2. **Budget.**  The point-in-time replay completes inside the experiment
   engine's budget: the worst-case engine run (``experiment_engine_max_
   experiments`` = 4 experiments, each replaying one strategy under up to 5
   setups) extrapolated from this run's measured per-strategy replay time,
   plus the history load, must fit in the learning queue's 30-minute soft time
   limit (``jobs.LEARNING_TASK_SOFT_TIME_LIMIT_SECONDS``).

Otherwise the setting stays off and the report says which condition failed.
The size of the measured gap does not enter the decision: a point-in-time
universe is the more honest replay whether the gap is large or small.

What is compared
----------------
* Two arms per strategy and window (all, 2015+, 2020+), same gates, same
  exits, same cost model (``gate_replay.simulate``):
  (a) **today**: today's 45 symbols, no membership mask;
  (b) **point in time**: today's list plus every past S&P 500 member with
  history, a single stock enterable only while it was a member, ETFs always.
* Live setups as in ``reports/regime-allocation-20261106.md``: the SPY market
  gates (MACD 50, Channel Breakout 20, Trend Pullback 10, RSI(2) 50), the VIX
  gate (Channel Breakout below 20) and the account earnings blackout where an
  earnings cache exists (only today's 29 single stocks have one; the extra
  members replay without a blackout in both arms, which flatters neither).
* RSI(2) keeps its fixed widened scope and IBS its index-ETF scope, so for
  those two the point-in-time arm only removes bars outside membership.
* Reported per arm and window: trades, win rate, net expectancy (bps, with
  its month-clustered bootstrap CI), equal-weight portfolio max drawdown, and
  the point-in-time minus today gap.  Also: how many of today's-list trades
  were taken on days the symbol was *not* yet (or no longer) a member, and
  their expectancy against the rest.

Data ($0): adjusted daily bars from the shared yfinance cache (``--data-dir``
is used as ``meta_label_history_dir`` for the run), the membership CSV cached
under ``<data-dir>/universe``, a ``date,close`` VIX csv.  ``--download`` allows
network refreshes (membership monthly, bars older than a week); without it the
run is cache-only.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_survivorship_check.py --data-dir <dir> [--vix-csv <csv>] [--download] \\
        --output <json> [--report <md>]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS, settings  # noqa: E402

ENGINE_MAX_EXPERIMENTS = settings.experiment_engine_max_experiments
SETUPS_PER_EXPERIMENT = 5
ENGINE_BUDGET_SECONDS = 30 * 60  # jobs.LEARNING_TASK_SOFT_TIME_LIMIT_SECONDS
MIN_COVERAGE_SINCE_2010 = 0.70
WINDOWS = ("all", "2015+", "2020+")
METRICS = ("trades", "win_rate", "expectancy_bps", "portfolio_max_drawdown")


def live_setup(strategy: str, earnings: bool) -> dict:
    from app.services.seed import MARKET_GATED_STRATEGIES, VIX_GATED_STRATEGIES

    setup = {"market_gate_sma": MARKET_GATED_STRATEGIES.get(strategy, 0)}
    setup.update(VIX_GATED_STRATEGIES.get(strategy, {}))
    if earnings:
        setup["earnings_blackout_sessions"] = settings.paper_earnings_blackout_sessions
    return setup


def _diff(a, b):
    return None if a is None or b is None else round(float(b) - float(a), 4)


def gap(today: dict, pit: dict) -> dict:
    out = {metric: _diff(today.get(metric), pit.get(metric)) for metric in METRICS}
    out["trades"] = int(out["trades"]) if out["trades"] is not None else None
    return out


def pooled_gaps(strategies: dict) -> dict:
    """Per window, the expectancy gap across the strategies whose scope grows (not RSI(2) / IBS)."""
    out = {}
    for window in WINDOWS:
        gaps = [row["windows"][window]["gap"]["expectancy_bps"] for row in strategies.values()
                if row["symbols_point_in_time"] > row["symbols_today"]
                and row["windows"][window]["gap"]["expectancy_bps"] is not None]
        positive_today = sum(1 for row in strategies.values()
                             if (row["windows"][window]["today"].get("expectancy_ci_bps") or [0])[0] > 0)
        positive_pit = sum(1 for row in strategies.values()
                           if (row["windows"][window]["point_in_time"].get("expectancy_ci_bps") or [0])[0] > 0)
        out[window] = {"strategies": len(gaps), "median_expectancy_gap_bps": round(float(np.median(gaps)), 1) if gaps else None,
                       "mean_expectancy_gap_bps": round(float(np.mean(gaps)), 1) if gaps else None,
                       "ci_above_zero_today": positive_today, "ci_above_zero_point_in_time": positive_pit}
    return out


def outside_membership(trades: pd.DataFrame, frames: dict, mask) -> dict:
    """Today's-list trades decided on a day the symbol was not an index member."""
    if trades is None or trades.empty:
        return {"trades": 0, "share": None, "expectancy_bps": None, "member_expectancy_bps": None}
    flags = np.zeros(len(trades), dtype=bool)
    for symbol, group in trades.groupby("symbol"):
        member = mask(symbol, pd.to_datetime(group["decision_date"]))
        flags[group.index.to_numpy()] = ~np.asarray(member, dtype=bool)
    outside, inside = trades.loc[flags, "net_return"], trades.loc[~flags, "net_return"]
    return {"trades": int(flags.sum()), "share": round(float(flags.mean()), 4),
            "expectancy_bps": round(float(outside.mean() * 1e4), 1) if len(outside) else None,
            "member_expectancy_bps": round(float(inside.mean() * 1e4), 1) if len(inside) else None}


def run(args) -> dict:
    settings.meta_label_history_dir = str(args.data_dir)
    from app.services import gate_replay
    from app.services import meta_label_primary as primary
    from app.services import point_in_time_universe as pit
    from app.services.earnings_history import load_surprises
    from app.services.gate_replay import window_rows
    from app.services.meta_label_learning import history_dir
    from app.services.strategy_discovery import load_frames
    from app.services.strategy_scorecard import load_daily_history

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

    coverage = pit.coverage(membership, loader=loader, now=now, start=args.start, cache_path=pit.coverage_path(),
                            refresh=args.refresh_coverage)
    base = sorted({s.upper() for s in DEFAULT_PAPER_EXECUTION_SYMBOLS})
    symbols = pit.replay_symbols(base, coverage)
    load_started = time.time()
    frames, missing = load_frames(symbols, loader=loader, start=args.start, end=args.end,
                                  earnings_loader=earnings_loader)
    load_seconds = time.time() - load_started
    today_frames = {s: frames[s] for s in base if s in frames}
    spy = frames.get("SPY")
    if spy is None:
        raise SystemExit("SPY history is required")
    vix = pd.read_csv(args.vix_csv) if args.vix_csv and Path(args.vix_csv).exists() else None
    earnings_cached = (history_dir() / "earnings").exists()
    replay_started = time.time()
    replays = {symbol: primary.replay_signals(frame) for symbol, frame in frames.items()}
    signals_seconds = time.time() - replay_started

    strategies, timings = {}, {}
    for strategy in gate_replay.STRATEGIES:
        setup = live_setup(strategy, earnings_cached)
        t0 = time.time()
        today = gate_replay.simulate(strategy, today_frames, spy, setup, replays, vix=vix)
        t1 = time.time()
        point = gate_replay.simulate(strategy, frames, spy, setup, replays, vix=vix, universe=membership.mask)
        t2 = time.time()
        today_rows, pit_rows = window_rows(today, today_frames), window_rows(point, frames)
        timings[strategy] = {"today_seconds": round(t1 - t0, 2), "point_in_time_seconds": round(t2 - t1, 2)}
        strategies[strategy] = {
            "setup": setup, "symbols_today": len(gate_replay.scope(strategy, list(today_frames))),
            "symbols_point_in_time": len(gate_replay.scope(strategy, list(frames))),
            "windows": {w: {"today": today_rows[w], "point_in_time": pit_rows[w], "gap": gap(today_rows[w], pit_rows[w])}
                        for w in WINDOWS},
            "today_trades_outside_membership": outside_membership(today, today_frames, membership.mask),
        }
    per_strategy = float(np.mean([t["point_in_time_seconds"] for t in timings.values()]))
    worst_case = load_seconds + signals_seconds + ENGINE_MAX_EXPERIMENTS * SETUPS_PER_EXPERIMENT * per_strategy
    coverage_ok = (coverage.get("coverage_since_2010") or 0.0) >= MIN_COVERAGE_SINCE_2010
    budget_ok = worst_case <= ENGINE_BUDGET_SECONDS
    decision = {
        "rule": (f"on by default iff mean yearly coverage since 2010 >= {MIN_COVERAGE_SINCE_2010:.0%} and the worst-case "
                 f"engine run ({ENGINE_MAX_EXPERIMENTS} experiments x {SETUPS_PER_EXPERIMENT} setups, plus the load) "
                 f"fits in {ENGINE_BUDGET_SECONDS // 60} minutes"),
        "coverage_since_2010": coverage.get("coverage_since_2010"), "coverage_ok": coverage_ok,
        "worst_case_engine_seconds": round(worst_case, 1), "budget_ok": budget_ok,
        "replay_point_in_time_universe": bool(coverage_ok and budget_ok),
    }
    return {
        "ran_at": now.isoformat(), "paper_only": True, "data_dir": str(args.data_dir), "start": args.start,
        "end": args.end, "vix": vix is not None, "earnings_cache": earnings_cached,
        "source": dict(membership.source),
        "coverage": {k: coverage[k] for k in ("checked_at", "start", "by_year", "coverage_since_2010")},
        "available_members": len(coverage["available"]), "missing_members": coverage["missing"],
        "symbols_today": len(today_frames), "symbols_point_in_time": len(frames), "missing_history": missing,
        "timings": {"load_seconds": round(load_seconds, 1), "signals_seconds": round(signals_seconds, 1),
                    "per_strategy_point_in_time_seconds": round(per_strategy, 2), "by_strategy": timings,
                    "total_seconds": round(time.time() - started, 1)},
        "strategies": strategies, "pooled": pooled_gaps(strategies), "decision": decision,
    }


# ----------------------------------------------------------------- report

def _pct(value, digits=1):
    return "n/a" if value is None else f"{100 * float(value):.{digits}f}%"


def _bps(value):
    return "n/a" if value is None else f"{float(value):+.1f}"


def _ci(value):
    return "" if not value else f" [{value[0]:+.0f}, {value[1]:+.0f}]"


def _signed_pct(value):
    return "n/a" if value is None else f"{100 * float(value):+.1f}pp"


def render(result: dict) -> str:
    src, cov, dec = result["source"], result["coverage"], result["decision"]
    day = result["ran_at"][:10]
    lines = [f"# Survivorship check: {day}", "",
             "**Question:** how much does replaying today's 45-symbol list flatter the live strategies, and should "
             "the experiment engine and strategy discovery replay the S&P 500 as it stood on each date instead?", "",
             f"**Decision (pre-registered in `scripts/run_survivorship_check.py`):** `replay_point_in_time_universe = "
             f"{dec['replay_point_in_time_universe']}`. Coverage since 2010 {_pct(dec['coverage_since_2010'])} "
             f"({'passes' if dec['coverage_ok'] else 'fails'} the 70% bar); worst-case engine run "
             f"{dec['worst_case_engine_seconds'] / 60:.1f} min ({'inside' if dec['budget_ok'] else 'outside'} the "
             f"30-minute budget). Paper only.", "",
             "## 1. Source and cached data", "",
             f"* Membership: `{src.get('source')}` from {src.get('repository')}, licence {src.get('licence')}, "
             f"downloaded {str(src.get('downloaded_at', ''))[:10]}, {src.get('rows')} membership intervals, "
             f"{src.get('first_date')} to {src.get('last_date')}. `end_date` is the first day out (exclusive); "
             "FB's days are credited to META.",
             "* Cached under `meta_label_learning.history_dir()/universe/`: `sp500_membership.csv` (the source file), "
             "`sp500_membership.json` (source, licence, download date), `sp500_coverage.json` (per-year coverage, "
             "available and missing tickers), refreshed monthly. Daily bars share the yfinance cache "
             "(`<YAHOO>.csv`, dotted tickers as `BRK-B`). Format documented in `app/services/point_in_time_universe.py`.",
             f"* Replay data: bars from {result['start']} (end {result['end'] or 'yesterday'}), VIX "
             f"{'present' if result['vix'] else 'absent'}, earnings cache {'present' if result['earnings_cache'] else 'absent'} "
             f"(today's single stocks only). Today's list: {result['symbols_today']} symbols with history; point in time: "
             f"{result['symbols_point_in_time']} ({result['available_members']} past members with history, "
             f"{len(result['missing_members'])} without).", "",
             "## 2. Coverage per year (members with history / members)", "",
             "| Year | Members | With data | Coverage |", "|---|---|---|---|"]
    for year, row in cov["by_year"].items():
        lines.append(f"| {year} | {row['members']} | {row['with_data']} | {_pct(row['coverage'])} |")
    lines += ["", f"Mean since 2010: {_pct(cov['coverage_since_2010'])}. Before 2010 the point-in-time universe is "
              "itself only half covered: the names Yahoo dropped are the acquired and bankrupt ones, so even arm (b) "
              "keeps some survivorship, and the gap below is a lower bound.", "",
              f"Missing tickers ({len(result['missing_members'])}, no yfinance history): "
              + ", ".join(result["missing_members"][:60]) + (" ..." if len(result["missing_members"]) > 60 else "")
              + " (full list in the JSON).", "",
              "## 3. Live strategies: today's list (a) vs point in time (b)", "",
              "Trades, win rate, net expectancy per trade (bps, month-clustered bootstrap CI) and equal-weight "
              "portfolio max drawdown per window; gap = (b) - (a).", ""]
    for strategy, row in result["strategies"].items():
        lines += [f"### {strategy}", "",
                  f"Setup `{json.dumps(row['setup'])}`; symbols {row['symbols_today']} -> {row['symbols_point_in_time']}.", "",
                  "| Window | Arm | Trades | Win rate | Expectancy bps | Max DD |", "|---|---|---|---|---|---|"]
        for window, cell in row["windows"].items():
            for arm in ("today", "point_in_time"):
                m = cell[arm]
                lines.append(f"| {window} | {arm} | {m['trades']} | {_pct(m['win_rate'])} | "
                             f"{_bps(m['expectancy_bps'])}{_ci(m.get('expectancy_ci_bps'))} | {_pct(m['portfolio_max_drawdown'])} |")
            g = cell["gap"]
            lines.append(f"| {window} | gap | {g['trades']:+} | {_signed_pct(g['win_rate'])} | {_bps(g['expectancy_bps'])} | "
                         f"{_signed_pct(g['portfolio_max_drawdown'])} |")
        out = row["today_trades_outside_membership"]
        lines += ["", f"Today's-list trades decided while the symbol was not an S&P 500 member: {out['trades']} "
                  f"({_pct(out['share'])}), expectancy {_bps(out['expectancy_bps'])} bps vs {_bps(out['member_expectancy_bps'])} "
                  "bps on member days.", ""]
    lines += ["## 4. The gap, pooled", "",
              "Across the strategies whose scope grows (every technical strategy; RSI(2) and IBS keep fixed scopes), "
              "the point-in-time minus today expectancy gap per window, and how many of the 9 strategies have a "
              "bootstrap CI above zero in each arm:", "",
              "| Window | Strategies | Median gap bps | Mean gap bps | CI > 0 today | CI > 0 point in time |",
              "|---|---|---|---|---|---|"]
    for window, row in result["pooled"].items():
        lines.append(f"| {window} | {row['strategies']} | {_bps(row['median_expectancy_gap_bps'])} | "
                     f"{_bps(row['mean_expectancy_gap_bps'])} | {row['ci_above_zero_today']} | {row['ci_above_zero_point_in_time']} |")
    lines += ["", "Two readings. First, the flattery is in *which* names are on today's list, not in the days before "
              "they joined: the trades today's list takes on pre-membership days are mostly worse than its member-day "
              "trades. Second, the broad-scope technical strategies' positive expectancy on today's list does not "
              "survive the point-in-time universe in any window; what the experiment engine replays from now on is "
              "the harder number, so its baselines and candidate rows will move on the first run with the setting "
              "on. The sized live book trades today's list under the same rules, so the paper results, not this "
              "replay, remain the forward evidence.", ""]
    t = result["timings"]
    lines += ["## 5. Budget", "",
              f"Load {t['load_seconds']}s, signals {t['signals_seconds']}s, point-in-time replay "
              f"{t['per_strategy_point_in_time_seconds']}s per strategy and setup; the whole check took "
              f"{t['total_seconds']}s. Worst-case engine run ({ENGINE_MAX_EXPERIMENTS} experiments x "
              f"{SETUPS_PER_EXPERIMENT} setups): {dec['worst_case_engine_seconds'] / 60:.1f} min of the 30-minute "
              "learning-queue limit. The first run with the setting on also downloads every past member's history "
              "once (about 20 minutes single-threaded, then monthly); later runs refresh only bars older than a week "
              "and fetch at most 60 new earnings histories per run.", "",
              "## 6. Caveats", "",
              "* Membership before 2001 may miss a few names (the source's own note), and coverage before 2010 is "
              "about half, so the early windows still carry survivorship.",
              "* The extra members replay without an earnings blackout (no cached earnings history); the blackout "
              "binds only on today's 29 single stocks, in both arms.",
              "* RSI(2) and IBS keep their fixed live scopes, so their point-in-time arm only drops bars outside "
              "membership; it never adds symbols.",
              "* The three windows overlap and every CI is a bootstrap over the same history; this is a measurement "
              "of bias, not a new strategy claim.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--vix-csv", type=Path, default=None)
    parser.add_argument("--download", action="store_true", help="allow network refreshes (membership, stale bars)")
    parser.add_argument("--refresh-coverage", action="store_true", help="recompute the coverage cache")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None, help="exclusive; default yesterday's close")
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
    dec = result["decision"]
    print(f"coverage since 2010 {dec['coverage_since_2010']}, worst-case engine {dec['worst_case_engine_seconds']}s, "
          f"replay_point_in_time_universe={dec['replay_point_in_time_universe']}")


if __name__ == "__main__":
    main()
