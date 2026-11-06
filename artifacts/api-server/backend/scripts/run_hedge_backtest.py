"""Backtest a small inverse-ETF hedge on the paper book while the SPY market gate is closed.

When SPY sits at or below its N-day average the gated strategies take no new
longs; the book is mostly cash and learns nothing from the down market.  This
script asks whether holding a small long in SH (ProShares Short S&P 500, the
daily inverse of SPY) through the ordinary long machinery earns its keep.

Pre-registered decision rule (fixed before the first run):

* A hedge size is **eligible** only if, in every window (all, 2015+, 2020+),
  the book's total profit with the hedge is not lower than without it AND
  the book's max drawdown with the hedge is lower than without it.
* Among eligible sizes the **smallest** is adopted (the hedge is insurance,
  not a bet).  None eligible = no hedge; the strategy row is created paused.
* Expectancy, drawdown and trade counts are reported for every size and
  window either way, plus the hedge leg on its own.

Hedge policy (``app.services.strategies.market_hedge.hedge_state``, the same
function the live strategy evaluates):

* enter on the close of a SPY bar whose close is below its 50-day average and
  whose 10-day average is below the 50-day one; fill at SH's next open;
* exit at the next open after SPY closes back above its 50-day average; a
  10% catastrophe stop on SH; no target; the 250-session barrier bound never
  binds in practice;
* sizes 2% / 5% / 10% of equity, marked to market daily like every book
  position (``strategy_discovery._marked_returns``);
* the hedge is one more long in the book: it takes a slot under the live caps
  (one per symbol, <= 5 per strategy, <= 3 per sector, <= 20 total; SH is
  its own ``inverse`` sector group) in ``run_slot_allocation_backtest.allocate``
  with the 10-session stop cooldown adopted in
  ``reports/slot-allocation-20261106.md``.  An entry the caps refuse is
  counted and reported.

SH history starts 2006-06-21.  Before that the hedge is a **short in SPY**
built from SPY's own bars: each synthetic bar moves by the negative of SPY's
move from the prior close, and the series decays by a 50 bps/year borrow
cost.  The synthetic segment is chained into SH's first open so a trade that
straddles the splice sees no jump.  Fills, costs and stops use the standard
model (``triple_barrier.label_events``: next-open entry, volatility-scaled
costs both sides, stop first, gaps at the open).

The long book is the live one: the nine replayable strategies under the
gates live today (SPY market gates MACD 50 / Channel Breakout 20 / Trend
Pullback 10 / RSI(2) 50, Channel Breakout's VIX < 20 gate, the 10-session
earnings blackout when an earnings cache is present), from
``run_regime_allocation_backtest.candidates`` (read-only), every position
5% of equity.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_hedge_backtest.py --data-dir <dir> [--vix-csv <csv>] [--download] --output <json>
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import gate_replay  # noqa: E402
from app.services import meta_label_primary as primary  # noqa: E402
from app.services.strategies.market_hedge import (  # noqa: E402
    FAST_WINDOW, HEDGE_EXIT_POLICY, HEDGE_SIZES, HEDGE_SYMBOL, TREND_WINDOW, MarketHedgeStrategy, hedge_state,
)
from app.services.strategy_discovery import _executable, _marked_returns  # noqa: E402
from app.services.strategy_scorecard import load_daily_history, normalize_prices, trade_statistics  # noqa: E402
from app.services.triple_barrier import BarrierConfig, label_events  # noqa: E402
from run_regime_allocation_backtest import BOOK_POLICY, candidates as book_candidates, load_frames, load_vix  # noqa: E402
from run_slot_allocation_backtest import BASE_CAPS, allocate  # noqa: E402

WINDOWS = gate_replay.WINDOWS
SLOT = 1.0 / BASE_CAPS["total"]
HEDGE_STRATEGY = MarketHedgeStrategy.slug
BORROW_BPS_PER_YEAR = 50.0
SESSIONS_PER_YEAR = 252
HEDGE_BARRIER = BarrierConfig(take_profit_pct=HEDGE_EXIT_POLICY.take_profit_pct,
                              stop_loss_pct=HEDGE_EXIT_POLICY.stop_loss_pct,
                              max_holding_days=HEDGE_EXIT_POLICY.max_holding_sessions)


# ----------------------------------------------------------------- hedge instrument

def synthetic_inverse(spy: pd.DataFrame, before: str, borrow_bps_per_year: float = BORROW_BPS_PER_YEAR) -> pd.DataFrame:
    """A short-SPY price path on SPY's bars dated before ``before`` (ISO date).

    Bar t moves by the negative of SPY's move from the prior close (open,
    high and low are inverted the same way, so SPY's high is the synthetic
    low) and the series decays by the borrow cost every session.
    """
    frame = spy.loc[spy["date"] < before].reset_index(drop=True)
    if len(frame) < 2:
        return frame.iloc[0:0]
    close = frame["close"].to_numpy(float)
    prior = np.concatenate([[close[0]], close[:-1]])
    decay = 1.0 - borrow_bps_per_year / 10_000 / SESSIONS_PER_YEAR
    level = np.cumprod((1.0 - (close / prior - 1.0)) * decay)
    base = np.concatenate([[1.0], level[:-1]])  # synthetic prior close

    def inverted(column: str) -> np.ndarray:
        return base * (1.0 - (frame[column].to_numpy(float) / prior - 1.0))

    out = pd.DataFrame({"date": frame["date"], "open": inverted("open"), "high": inverted("low"),
                        "low": inverted("high"), "close": level, "volume": frame["volume"].to_numpy()})
    return out.iloc[1:].reset_index(drop=True)


def hedge_frame(spy: pd.DataFrame, sh: Optional[pd.DataFrame]) -> tuple[pd.DataFrame, Optional[str]]:
    """SH bars, with a synthetic short-SPY segment chained in front where SH has no history."""
    if sh is None or sh.empty:
        synthetic = synthetic_inverse(spy, "9999-12-31")
        return synthetic, None
    first = str(sh["date"].iloc[0])
    synthetic = synthetic_inverse(spy, first)
    if synthetic.empty:
        return sh.reset_index(drop=True), first
    # Chain: the synthetic close before SH's first bar is SH's first open undone by the inverse overnight move.
    spy_prior = float(spy.loc[spy["date"] < first, "close"].iloc[-1])
    spy_open = float(spy.loc[spy["date"] == first, "open"].iloc[0])
    target = float(sh["open"].iloc[0]) / (1.0 - (spy_open / spy_prior - 1.0))
    scale = target / float(synthetic["close"].iloc[-1])
    for column in ("open", "high", "low", "close"):
        synthetic[column] = synthetic[column] * scale
    return pd.concat([synthetic, sh], ignore_index=True), first


def hedge_trades(hedge: pd.DataFrame, spy: pd.DataFrame) -> pd.DataFrame:
    """Every executable hedge trade: next-open entry, indicator exit, catastrophe stop, standard costs."""
    state = hedge_state(spy["close"], TREND_WINDOW, FAST_WINDOW)
    index = pd.to_datetime(spy["date"])
    entry = gate_replay._aligned(hedge, pd.Series(state["entry"], index=index), False).astype(bool)
    exit_ = gate_replay._aligned(hedge, pd.Series(state["exit"], index=index), False).astype(bool)
    positions = np.flatnonzero(entry)
    if not len(positions):
        return pd.DataFrame()
    labels = label_events(hedge, positions, HEDGE_BARRIER, exit_signals=exit_)
    if labels.empty:
        return pd.DataFrame()
    return _executable(labels).assign(symbol=HEDGE_SYMBOL, strategy=HEDGE_STRATEGY).reset_index(drop=True)


def with_hedge(rows: pd.DataFrame, hedge: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    """Book candidates plus the hedge rows, the hedge first on its entry day (it is the smallest, surest claim)."""
    if trades.empty:
        return rows
    extra = trades.copy()
    extra["entry_date"] = [hedge["date"].iloc[i] for i in extra["entry_idx"]]
    extra["exit_date"] = [hedge["date"].iloc[i] for i in extra["exit_idx"]]
    extra["live_rank"] = -1
    extra["confidence"] = np.nan
    combined = pd.concat([rows, extra[rows.columns]], ignore_index=True)
    return combined.sort_values(["entry_date", "live_rank"], kind="stable").reset_index(drop=True)


# ----------------------------------------------------------------- metrics

def daily_returns(accepted: pd.DataFrame, frames: Mapping[str, pd.DataFrame], weight_for: dict) -> pd.Series:
    """Daily book return: each accepted trade marked to market times its strategy's weight."""
    returns: dict = defaultdict(float)
    for row in accepted.itertuples(index=False):
        close = frames[row.symbol]["close"].to_numpy(float)
        dates = frames[row.symbol]["date"].to_numpy()
        marked = _marked_returns(pd.DataFrame([{"entry_idx": row.entry_idx, "exit_idx": row.exit_idx,
                                                "entry_price": row.entry_price, "exit_price": row.exit_price}]), close)
        weight = weight_for.get(row.strategy, SLOT)
        for index in range(int(row.entry_idx), int(row.exit_idx) + 1):
            returns[dates[index]] += weight * marked[index]
    return pd.Series(returns, dtype=float).sort_index()


def curve_metrics(series: pd.Series, start) -> dict:
    sub = series if start is None else series.loc[pd.to_datetime(series.index) >= start]
    equity = np.concatenate([[1.0], np.cumprod(1 + sub.to_numpy())]) if len(sub) else np.array([1.0])
    drawdown = float(np.max(1 - equity / np.maximum.accumulate(equity)))
    return {"total_return": round(float(equity[-1] - 1), 4), "max_drawdown": round(drawdown, 4)}


def trade_metrics(trades: pd.DataFrame, start) -> dict:
    sub = trades if start is None else trades.loc[pd.to_datetime(trades["decision_date"]) >= start]
    if sub.empty:
        return {"trades": 0, "win_rate": None, "expectancy_bps": None, "expectancy_ci_bps": None}
    months = pd.to_datetime(sub["decision_date"]).dt.to_period("M").astype(str).tolist()
    stats = trade_statistics(sub["net_return"].to_numpy(float), clusters=months)
    return {"trades": stats["trades"], "win_rate": stats["win_rate"], "expectancy_bps": stats["expectancy_bps"],
            "expectancy_ci_bps": stats.get("expectancy_ci_bps")}


def hedge_leg_metrics(trades: pd.DataFrame, hedge: pd.DataFrame, series: pd.Series, start) -> dict:
    out = trade_metrics(trades, start)
    sub = trades if start is None else trades.loc[pd.to_datetime(trades["decision_date"]) >= start]
    sessions = hedge if start is None else hedge.loc[pd.to_datetime(hedge["date"]) >= start]
    held = int(sum(int(row.exit_idx) - int(row.entry_idx) + 1 for row in sub.itertuples(index=False)))
    out["sessions_hedged_pct"] = round(100.0 * held / max(len(sessions), 1), 1)
    out["stopped"] = int(sub["barrier"].astype(str).str.startswith("stop_loss").sum()) if len(sub) else 0
    out["mean_holding_sessions"] = round(float((sub["exit_idx"] - sub["entry_idx"]).mean()), 1) if len(sub) else None
    out.update(curve_metrics(series, start))
    return out


def decide(results: dict) -> dict:
    """Smallest size whose total return is not lower and max drawdown lower than the baseline in every window."""
    base = results["baseline"]
    eligible = []
    for size in HEDGE_SIZES:
        rows = results[f"hedge_{size:g}"]
        if all(rows[w]["total_return"] >= base[w]["total_return"] and rows[w]["max_drawdown"] < base[w]["max_drawdown"]
               for w in WINDOWS):
            eligible.append(size)
    return {"eligible": eligible, "adopted": min(eligible) if eligible else None}


# ----------------------------------------------------------------- main

def run(frames: dict, hedge: pd.DataFrame, vix: Optional[pd.DataFrame], *, earnings: bool) -> dict:
    spy = frames["SPY"]
    replays = {s: primary.replay_signals(f) for s, f in frames.items()}
    rows = book_candidates(frames, spy, vix, replays, earnings=earnings)
    trades = hedge_trades(hedge, spy)
    all_frames = {**frames, HEDGE_SYMBOL: hedge}
    base_accepted = allocate(rows, frames, BOOK_POLICY)
    accepted = allocate(with_hedge(rows, hedge, trades), all_frames, BOOK_POLICY)
    book_part = accepted.loc[accepted["strategy"] != HEDGE_STRATEGY]
    hedge_part = accepted.loc[accepted["strategy"] == HEDGE_STRATEGY]
    windows = {name: (None if start is None else pd.Timestamp(start)) for name, start in WINDOWS.items()}
    base_series = daily_returns(base_accepted, frames, {})
    report = {"candidates": int(len(rows)), "hedge_signals": int(len(trades)),
              "hedge_accepted": int(len(hedge_part)), "hedge_refused_by_caps": int(len(trades) - len(hedge_part)),
              "book_trades_without_hedge": int(len(base_accepted)), "book_trades_with_hedge": int(len(book_part)),
              "results": {"baseline": {}}, "hedge_leg": {}}
    for name, start in windows.items():
        report["results"]["baseline"][name] = {**curve_metrics(base_series, start), **trade_metrics(base_accepted, start)}
    for size in HEDGE_SIZES:
        series = daily_returns(accepted, all_frames, {HEDGE_STRATEGY: size})
        hedge_series = daily_returns(hedge_part, all_frames, {HEDGE_STRATEGY: size})
        key = f"hedge_{size:g}"
        report["results"][key] = {name: {**curve_metrics(series, start), **trade_metrics(book_part, start)}
                                 for name, start in windows.items()}
        report["hedge_leg"][key] = {name: hedge_leg_metrics(hedge_part, hedge, hedge_series, start)
                                   for name, start in windows.items()}
    report["decision"] = decide(report["results"])
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--vix-csv", type=Path, default=None)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    warnings.filterwarnings("ignore")
    frames, earnings = load_frames(args.data_dir, download=args.download, start=args.start, end=args.end)
    vix = load_vix(args.vix_csv, download=args.download)
    try:
        sh = normalize_prices(load_daily_history(HEDGE_SYMBOL, args.data_dir, download=args.download),
                              start=args.start, end=args.end).reset_index(drop=True)
    except FileNotFoundError:
        sh = None
    hedge, sh_first = hedge_frame(frames["SPY"], sh)
    report = run(frames, hedge, vix, earnings=earnings)
    report.update({"data_end": max(str(f["date"].iloc[-1]) for f in frames.values()), "symbols": len(frames),
                   "earnings_blackout": earnings, "vix_history": vix is not None, "book": BOOK_POLICY,
                   "hedge_symbol": HEDGE_SYMBOL, "sh_history_from": sh_first,
                   "synthetic_borrow_bps_per_year": BORROW_BPS_PER_YEAR, "sizes": list(HEDGE_SIZES)})
    for key, rows in report["results"].items():
        print(key, {w: (m["total_return"], m["max_drawdown"], m["trades"], m["expectancy_bps"]) for w, m in rows.items()},
              flush=True)
    for key, rows in report["hedge_leg"].items():
        print("leg", key, {w: (m["trades"], m["expectancy_bps"], m["total_return"], m["sessions_hedged_pct"])
                           for w, m in rows.items()}, flush=True)
    print("decision", report["decision"], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
