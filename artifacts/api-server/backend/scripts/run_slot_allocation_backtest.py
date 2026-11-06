"""Backtest how the paper book allocates its position slots.

reports/market-gate-20261106.md found that under the live caps (one position
per symbol, <= 5 per strategy, <= 3 per sector, <= 20 total) the book accepts
its worst trades: accepted -46 bps per trade, turned away +16.6, all -2.3.
This script tests allocation policies against that baseline.

Pre-registered decision rule (fixed before the first run):

* A policy is eligible only if, in every window (all, 2015+, 2020+):
  per-trade net expectancy is higher than the baseline's, the book's max
  drawdown is <= 1.10x the baseline's, and it takes >= 50% of the baseline's
  trades (the bot must keep learning).
* Among eligible policies the one with the largest minimum expectancy gain
  is adopted.  None eligible = keep the live allocation.

Policies (each a change to the baseline only in what it names):

* ``baseline``: live caps, slots filled in the live cycle order (execution
  symbol list, then strategy id).
* ``rank_confidence``: same-day candidates ordered by signal confidence.
* ``rank_track_record``: same-day candidates ordered by their strategy's mean
  net return over trades closed before that day (causal).
* ``stop_cooldown_5`` / ``stop_cooldown_10``: no new entry on a symbol for N
  sessions after a stop-loss exit on it.
* ``max_new_2`` / ``max_new_4``: at most N new entries per day.
* ``loose_caps``: <= 10 per strategy, <= 6 per sector, <= 40 total.
* ``symbol_only``: one position per symbol, no other cap.

Every position gets 1 / total-cap of equity, so each policy can be at most
100% invested.  Trades come from ``run_market_gate_backtest.simulate`` with
the gates live since PR #13 (MACD 50, Channel Breakout 20, Trend Pullback 10,
RSI(2) 50).

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_slot_allocation_backtest.py --data-dir <dir> --output <json>
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_market_gate_backtest as gate_bt  # noqa: E402
from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS  # noqa: E402
from app.services import meta_label_primary as primary  # noqa: E402
from app.services.seed import MARKET_GATED_STRATEGIES  # noqa: E402
from app.services.strategy_discovery import _marked_returns  # noqa: E402
from app.services.strategy_scorecard import load_daily_history, normalize_prices, trade_statistics  # noqa: E402
from app.services.trading_universe import sector_group  # noqa: E402

WINDOWS = gate_bt.WINDOWS
BASE_CAPS = {"total": 20, "strategy": 5, "sector": 3}
POLICIES = {
    "baseline": {},
    "rank_confidence": {"order": "confidence"},
    "rank_track_record": {"order": "track_record"},
    "stop_cooldown_5": {"stop_cooldown": 5},
    "stop_cooldown_10": {"stop_cooldown": 10},
    "max_new_2": {"max_new_per_day": 2},
    "max_new_4": {"max_new_per_day": 4},
    "loose_caps": {"caps": {"total": 40, "strategy": 10, "sector": 6}},
    "symbol_only": {"caps": {"total": 45, "strategy": 10_000, "sector": 10_000}},
}


def candidates(frames: dict, spy: pd.DataFrame, replays: dict) -> pd.DataFrame:
    """Every executable trade per strategy (live gates applied) with its decision-day confidence."""
    out = []
    for strategy in gate_bt.STRATEGIES:
        trades = gate_bt.simulate(strategy, frames, spy, MARKET_GATED_STRATEGIES.get(strategy, 0), replays)
        if trades.empty:
            continue
        confidence = []
        for symbol, index in zip(trades["symbol"], trades["event_idx"]):
            signal = primary.ibs_mean_reversion(frames[symbol], entry_threshold=0.1) \
                if strategy == "ibs_mean_reversion" else replays[symbol][strategy]
            confidence.append(float(signal["confidence"].iloc[int(index)]))
        out.append(trades.assign(confidence=confidence))
    rows = pd.concat(out, ignore_index=True)
    rows["entry_date"] = [frames[s]["date"].iloc[i] for s, i in zip(rows["symbol"], rows["entry_idx"])]
    rows["exit_date"] = [frames[s]["date"].iloc[i] for s, i in zip(rows["symbol"], rows["exit_idx"])]
    symbol_rank = {s: i for i, s in enumerate(DEFAULT_PAPER_EXECUTION_SYMBOLS)}
    strategy_rank = {s: i for i, s in enumerate(gate_bt.STRATEGIES)}
    rows["live_rank"] = [symbol_rank[s] * 100 + strategy_rank[t] for s, t in zip(rows["symbol"], rows["strategy"])]
    return rows.sort_values(["entry_date", "live_rank"], kind="stable").reset_index(drop=True)


def _track_records(rows: pd.DataFrame) -> dict:
    """strategy -> (sorted exit dates, cumulative sums, counts) for a causal mean before any day."""
    out = {}
    for strategy, group in rows.groupby("strategy"):
        group = group.sort_values("exit_date")
        out[strategy] = (group["exit_date"].to_numpy(), np.cumsum(group["net_return"].to_numpy()))
    return out


def _mean_before(records: dict, strategy: str, day) -> float:
    dates, sums = records.get(strategy, (np.array([]), np.array([])))
    n = int(np.searchsorted(dates, day, side="left"))  # trades closed strictly before ``day``
    return float(sums[n - 1] / n) if n else 0.0


def allocate(rows: pd.DataFrame, frames: dict, policy: dict) -> pd.DataFrame:
    caps = policy.get("caps", BASE_CAPS)
    order = policy.get("order", "live")
    cooldown = int(policy.get("stop_cooldown", 0))
    max_new = int(policy.get("max_new_per_day", 0))
    records = _track_records(rows) if order == "track_record" else {}
    session_index = {s: {d: i for i, d in enumerate(f["date"])} for s, f in frames.items()}
    blocked_until: dict[str, int] = {}
    open_rows: list[dict] = []
    keep = np.zeros(len(rows), dtype=bool)
    for day, group in rows.groupby("entry_date", sort=True):
        if order == "confidence":
            group = group.sort_values(["confidence", "live_rank"], ascending=[False, True], kind="stable")
        elif order == "track_record":
            score = group["strategy"].map(lambda s: _mean_before(records, s, day))
            group = group.assign(_score=score).sort_values(["_score", "live_rank"], ascending=[False, True],
                                                           kind="stable")
        closing = [o for o in open_rows if o["exit_date"] <= day]
        for o in closing:
            if cooldown and o["stopped"]:
                blocked_until[o["symbol"]] = session_index[o["symbol"]][o["exit_date"]] + cooldown
        open_rows = [o for o in open_rows if o["exit_date"] > day]
        taken = 0
        for index, row in group.iterrows():
            if max_new and taken >= max_new:
                break
            if cooldown and session_index[row.symbol].get(day, -1) < blocked_until.get(row.symbol, -1):
                continue
            if any(o["symbol"] == row.symbol for o in open_rows):
                continue
            sector = sector_group(row.symbol)
            if (len(open_rows) >= caps["total"]
                    or sum(o["strategy"] == row.strategy for o in open_rows) >= caps["strategy"]
                    or sum(o["sector"] == sector for o in open_rows) >= caps["sector"]):
                continue
            keep[index] = True
            taken += 1
            open_rows.append({"symbol": row.symbol, "strategy": row.strategy, "sector": sector,
                              "exit_date": row.exit_date, "stopped": str(row.barrier).startswith("stop_loss")})
    return rows.loc[keep]


def metrics(accepted: pd.DataFrame, frames: dict, start, weight: float) -> dict:
    sub = accepted if start is None else accepted.loc[pd.to_datetime(accepted["decision_date"]) >= start]
    returns: dict = defaultdict(float)
    for row in sub.itertuples(index=False):
        close = frames[row.symbol]["close"].to_numpy(float)
        dates = frames[row.symbol]["date"].to_numpy()
        marked = _marked_returns(pd.DataFrame([{"entry_idx": row.entry_idx, "exit_idx": row.exit_idx,
                                                "entry_price": row.entry_price, "exit_price": row.exit_price}]), close)
        for index in range(int(row.entry_idx), int(row.exit_idx) + 1):
            returns[dates[index]] += weight * marked[index]
    series = pd.Series(returns).sort_index()
    equity = np.concatenate([[1.0], np.cumprod(1 + series.to_numpy())]) if len(series) else np.array([1.0])
    drawdown = float(np.max(1 - equity / np.maximum.accumulate(equity)))
    months = pd.to_datetime(sub["decision_date"]).dt.to_period("M").astype(str).tolist()
    stats = trade_statistics(sub["net_return"].to_numpy(), clusters=months)
    stops = sub.loc[sub["barrier"].astype(str).str.startswith("stop_loss")]
    return {"trades": stats["trades"], "win_rate": stats["win_rate"], "expectancy_bps": stats["expectancy_bps"],
            "expectancy_ci_bps": stats.get("expectancy_ci_bps"), "total_return": round(float(equity[-1] - 1), 4),
            "max_drawdown": round(drawdown, 4),
            "days_with_3plus_stops": int((stops.groupby("exit_date").size() >= 3).sum())}


def decide(results: dict) -> dict:
    base = results["baseline"]
    eligible = {}
    for name, rows in results.items():
        if name == "baseline":
            continue
        gains = []
        for window in WINDOWS:
            b, p = base[window], rows[window]
            if (p["expectancy_bps"] is None or p["expectancy_bps"] <= b["expectancy_bps"]
                    or p["max_drawdown"] > 1.10 * b["max_drawdown"] or p["trades"] < 0.5 * b["trades"]):
                break
            gains.append(p["expectancy_bps"] - b["expectancy_bps"])
        else:
            eligible[name] = min(gains)
    return {"eligible": eligible, "adopted": max(eligible, key=eligible.get) if eligible else "baseline"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    warnings.filterwarnings("ignore")
    frames = {s: normalize_prices(load_daily_history(s, args.data_dir, download=args.download),
                                  start=args.start, end=args.end).reset_index(drop=True)
              for s in DEFAULT_PAPER_EXECUTION_SYMBOLS}
    frames = {s: f for s, f in frames.items() if len(f) > 260}
    replays = {s: primary.replay_signals(f) for s, f in frames.items()}
    rows = candidates(frames, frames["SPY"], replays)
    windows = {name: (None if start is None else pd.Timestamp(start)) for name, start in WINDOWS.items()}
    report = {"data_end": max(str(f["date"].iloc[-1]) for f in frames.values()), "candidates": int(len(rows)),
              "all_signals": {name: metrics(rows, frames, start, 1 / 45) for name, start in windows.items()},
              "policies": {}}
    for name, policy in POLICIES.items():
        accepted = allocate(rows, frames, policy)
        weight = 1 / policy.get("caps", BASE_CAPS)["total"]
        report["policies"][name] = {w: metrics(accepted, frames, start, weight) for w, start in windows.items()}
        print(name, {w: (m["trades"], m["expectancy_bps"], m["max_drawdown"]) for w, m in report["policies"][name].items()},
              flush=True)
    report["decision"] = decide(report["policies"])
    print("decision", report["decision"], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
