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
100% invested.  Trades come from ``gate_replay.simulate`` with the gates live
since PR #13 (MACD 50, Channel Breakout 20, Trend Pullback 10, RSI(2) 50).
The book simulation itself (``candidates`` / ``allocate`` / ``metrics``) is
``app.services.book_replay``, which the experiment engine reuses for its
book-level experiments (``entry_cluster_cap``).

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_slot_allocation_backtest.py --data-dir <dir> --output <json>
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_market_gate_backtest as gate_bt  # noqa: E402
from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS  # noqa: E402
from app.services import book_replay  # noqa: E402
from app.services import meta_label_primary as primary  # noqa: E402
from app.services.seed import MARKET_GATED_STRATEGIES  # noqa: E402
from app.services.strategy_scorecard import load_daily_history, normalize_prices  # noqa: E402

WINDOWS = gate_bt.WINDOWS
BASE_CAPS = book_replay.BASE_CAPS
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
    """Every executable trade per strategy (live gates applied) with its decision-day confidence.

    The book simulation lives in ``app.services.book_replay``; this keeps the
    script's setup (the market gates live since PR #13, the published cost
    model) and signature.
    """
    setups = {strategy: {"market_gate_sma": MARKET_GATED_STRATEGIES.get(strategy, 0)} for strategy in gate_bt.STRATEGIES}
    return book_replay.candidates(frames, spy, replays, setups)


def allocate(rows: pd.DataFrame, frames: dict, policy: dict) -> pd.DataFrame:
    return book_replay.allocate(rows, frames, policy)


def metrics(accepted: pd.DataFrame, frames: dict, start, weight: float) -> dict:
    return book_replay.metrics(accepted, frames, start, weight)


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
