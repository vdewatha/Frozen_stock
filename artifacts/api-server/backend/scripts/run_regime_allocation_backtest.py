"""Backtest the regime-aware paper risk budget before it is switched on.

``app/services/regime_allocation.py`` conditions each strategy's allocation
weight on the regime cell (SPY trend x VIX band x breadth) the next BUY
opens in.  This script replays the live book and sizes every accepted trade
three ways, so the setting ``paper_allocation_regime_enabled`` is decided on
evidence, not on the idea.

Pre-registered decision rule (fixed before the first run):

* Three sizing arms over the *same* accepted trades: ``equal`` (every
  position 1/20 of equity), ``performance`` (the live regime-blind weight:
  the strategy's bounded, shrink-first multiplier from its last 20 book
  trades closed strictly before the entry day, within 60 days, with the
  concentration cap) and ``regime`` (``performance`` times the strategy's
  multiplier in the entry day's regime cell from the same trades, clamped to
  the same floor / ceiling, then the concentration cap).
* ``regime`` is adopted only if, in every window (all, 2015+, 2020+), its
  notional-weighted per-trade net expectancy is not lower than *both*
  ``equal`` and ``performance``, and its book max drawdown is not worse than
  *both*.  It must also have changed the size of at least one trade in
  every window (an arm that never fires is no evidence).  Otherwise the
  setting stays off.
* Expectancy, max drawdown and the trade count are reported together for
  every window; the trade count is the same in all arms by construction.

Book and data:

* Candidates: every executable trade of the 9 replayable live strategies
  from ``gate_replay.simulate`` (read-only), under the gates live today:
  the SPY market gates (MACD 50, Channel Breakout 20, Trend Pullback 10,
  RSI(2) 50), the VIX gate (Channel Breakout below 20) and the 10-session
  earnings blackout when an earnings cache is present.
* Book: the live caps (1 per symbol, <= 5 per strategy, <= 3 per sector,
  <= 20 total), slots filled in the live cycle order, and the 10-session
  stop-out cooldown adopted in ``reports/slot-allocation-20261106.md``
  (``run_slot_allocation_backtest.allocate``, read-only).
* Regime cell of a trade: SPY's close above its 50-day average on the
  decision bar; the VIX close on or before the decision date; breadth as
  the share of the 45 paper symbols above their own 200-day average on the
  decision bar (a proxy: the live gauge uses the S&P 500 members, whose
  26-year history is not cached).  Weights come from trades *closed*
  strictly before the entry day, so every arm is causal.
* Returns are marked to market daily; each position is weight / 20 of
  equity, so like the live book the weighted arms may be up to 125% invested.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_regime_allocation_backtest.py --data-dir <dir> \\
        [--vix-csv <date,close csv>] [--download] --output <json>
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

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS, settings  # noqa: E402
from app.services import earnings_history, gate_replay  # noqa: E402
from app.services import meta_label_primary as primary  # noqa: E402
from app.services.market_gate import spy_above_sma  # noqa: E402
from app.services.regime_allocation import TREND_WINDOW, regime_cell  # noqa: E402
from app.services.seed import MARKET_GATED_STRATEGIES, VIX_GATED_STRATEGIES  # noqa: E402
from app.services.strategy_allocation import apply_concentration_cap, bounds, performance_multiplier  # noqa: E402
from app.services.strategy_discovery import _marked_returns  # noqa: E402
from app.services.strategy_scorecard import load_daily_history, normalize_prices, trade_statistics  # noqa: E402
from app.services.trading_universe import ETF_SYMBOLS  # noqa: E402
from run_slot_allocation_backtest import BASE_CAPS, allocate  # noqa: E402

WINDOWS = gate_replay.WINDOWS
ARMS = ("equal", "performance", "regime")
BOOK_POLICY = {"stop_cooldown": 10}
SLOT = 1.0 / BASE_CAPS["total"]
BREADTH_WINDOW = 200


# ----------------------------------------------------------------- data

def live_setup(strategy: str, earnings: bool) -> dict:
    setup = {"market_gate_sma": MARKET_GATED_STRATEGIES.get(strategy, 0)}
    setup.update(VIX_GATED_STRATEGIES.get(strategy, {}))
    if earnings:
        setup["earnings_blackout_sessions"] = settings.paper_earnings_blackout_sessions
    return setup


def load_frames(data_dir: Path, *, download: bool, start: str, end) -> tuple[dict, bool]:
    frames = {}
    earnings_dir = data_dir / "earnings"
    for symbol in DEFAULT_PAPER_EXECUTION_SYMBOLS:
        frame = normalize_prices(load_daily_history(symbol, data_dir, download=download), start=start, end=end)
        frame = frame.reset_index(drop=True)
        if len(frame) <= 260:
            continue
        if symbol not in ETF_SYMBOLS and earnings_dir.exists():
            frame = earnings_history.attach_surprises(frame, earnings_history.load_surprises(symbol, data_dir))
        frames[symbol] = frame
    return frames, earnings_dir.exists()


def load_vix(path: Path | None, *, download: bool) -> pd.DataFrame | None:
    if path is not None and path.exists():
        return pd.read_csv(path)
    if download:
        from app.services.strategies.vix_gate import download_vix_history

        frame = download_vix_history("1990-01-01")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_csv(path, index=False)
        return frame
    return None


def candidates(frames: dict, spy: pd.DataFrame, vix: pd.DataFrame | None, replays: dict, *, earnings: bool) -> pd.DataFrame:
    """Every executable trade per strategy under the live gates, in live cycle order."""
    out = []
    for strategy in gate_replay.STRATEGIES:
        trades = gate_replay.simulate(strategy, frames, spy, live_setup(strategy, earnings), replays, vix=vix)
        if not trades.empty:
            out.append(trades)
    rows = pd.concat(out, ignore_index=True)
    rows["entry_date"] = [frames[s]["date"].iloc[i] for s, i in zip(rows["symbol"], rows["entry_idx"])]
    rows["exit_date"] = [frames[s]["date"].iloc[i] for s, i in zip(rows["symbol"], rows["exit_idx"])]
    symbol_rank = {s: i for i, s in enumerate(DEFAULT_PAPER_EXECUTION_SYMBOLS)}
    strategy_rank = {s: i for i, s in enumerate(gate_replay.STRATEGIES)}
    rows["live_rank"] = [symbol_rank[s] * 100 + strategy_rank[t] for s, t in zip(rows["symbol"], rows["strategy"])]
    rows["confidence"] = np.nan
    return rows.sort_values(["entry_date", "live_rank"], kind="stable").reset_index(drop=True)


# ----------------------------------------------------------------- regime cells

def daily_regime_inputs(frames: dict, spy: pd.DataFrame, vix: pd.DataFrame | None) -> pd.DataFrame:
    """Per calendar date: SPY above its 50-day average, the VIX close, universe breadth (all point in time)."""
    dates = pd.to_datetime(spy["date"])
    trend = pd.Series(spy_above_sma(spy["close"], TREND_WINDOW), index=dates)
    closes = pd.concat({s: pd.Series(f["close"].to_numpy(float), index=pd.to_datetime(f["date"]))
                        for s, f in frames.items()}, axis=1).sort_index()
    sma = closes.rolling(BREADTH_WINDOW, min_periods=BREADTH_WINDOW).mean()
    valid = closes.notna() & sma.notna()
    breadth = (100.0 * ((closes > sma) & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan))
    out = pd.DataFrame({"spy_above_sma": trend}).join(breadth.rename("breadth_pct"), how="left")
    if vix is not None and not vix.empty:
        series = pd.Series(pd.to_numeric(vix["close"], errors="coerce").to_numpy(float),
                           index=pd.to_datetime(vix["date"])).dropna().sort_index()
        out["vix"] = series.reindex(out.index, method="ffill")
    else:
        out["vix"] = np.nan
    return out


def cells_for(rows: pd.DataFrame, inputs: pd.DataFrame) -> list[str]:
    """The regime cell key of each trade on its decision bar."""
    decisions = pd.to_datetime(rows["decision_date"])
    aligned = inputs.reindex(decisions, method="ffill")
    keys = []
    for above, vix, breadth in zip(aligned["spy_above_sma"], aligned["vix"], aligned["breadth_pct"]):
        above = None if above is None or (isinstance(above, float) and np.isnan(above)) else bool(above)
        keys.append(regime_cell(spy_above_sma=above, vix=None if pd.isna(vix) else float(vix),
                                breadth_pct=None if pd.isna(breadth) else float(breadth))["key"])
    return keys


# ----------------------------------------------------------------- causal weights

class Records:
    """Per strategy: book trades sorted by exit date, for a causal lookback before any day."""

    def __init__(self, rows: pd.DataFrame) -> None:
        self.by_strategy = {}
        for strategy, group in rows.groupby("strategy"):
            group = group.sort_values("exit_date", kind="stable")
            self.by_strategy[strategy] = (pd.to_datetime(group["exit_date"]).to_numpy(),
                                          group["net_return"].to_numpy(float), group["cell"].to_numpy())

    def recent(self, strategy: str, day, policy: dict) -> tuple[np.ndarray, np.ndarray]:
        """Returns and cells of the last ``lookback_trades`` trades closed in [day - lookback_days, day)."""
        if strategy not in self.by_strategy:
            return np.array([]), np.array([])
        dates, returns, cells = self.by_strategy[strategy]
        day = pd.Timestamp(day)
        hi = int(np.searchsorted(dates, day.to_datetime64(), side="left"))
        lo = int(np.searchsorted(dates, (day - pd.Timedelta(days=policy["lookback_days"])).to_datetime64(), side="left"))
        lo = max(lo, hi - policy["lookback_trades"])
        return returns[lo:hi], cells[lo:hi]


def weights_for_day(records: Records, strategies: list[str], day, cell: str, policy: dict) -> dict[str, dict]:
    """Every strategy's ``performance`` and ``regime`` weight for entries on ``day`` (both capped)."""
    base, regime = {}, {}
    detail = {}
    for strategy in strategies:
        returns, cells = records.recent(strategy, day, policy)
        overall = performance_multiplier(list(returns), policy)
        in_cell = performance_multiplier(list(returns[cells == cell]), policy)
        factor = 1.0 if in_cell["status"] == "insufficient_evidence" else in_cell["multiplier"]
        base[strategy] = overall["multiplier"]
        regime[strategy] = round(min(policy["ceiling"], max(policy["floor"], overall["multiplier"] * factor)), 6)
        detail[strategy] = {"cell_trades": int((cells == cell).sum()), "regime_factor": factor}
    base = apply_concentration_cap(base, policy["max_share"])
    regime = apply_concentration_cap(regime, policy["max_share"])
    return {s: {"equal": 1.0, "performance": base[s], "regime": regime[s], **detail[s]} for s in strategies}


def size_trades(rows: pd.DataFrame, policy: dict) -> pd.DataFrame:
    """Add a weight column per arm to the accepted trades (weights are causal: trades closed before the entry day)."""
    records = Records(rows)
    strategies = sorted(rows["strategy"].unique())
    columns = {arm: np.ones(len(rows)) for arm in ARMS}
    cell_trades = np.zeros(len(rows), dtype=int)
    for (day, cell), group in rows.groupby(["entry_date", "cell"], sort=True):
        weights = weights_for_day(records, strategies, day, cell, policy)
        for index, strategy in zip(group.index, group["strategy"]):
            for arm in ARMS:
                columns[arm][index] = weights[strategy][arm]
            cell_trades[index] = weights[strategy]["cell_trades"]
    out = rows.copy()
    for arm in ARMS:
        out[f"w_{arm}"] = columns[arm]
    out["cell_trades"] = cell_trades
    return out


# ----------------------------------------------------------------- metrics

def arm_metrics(sized: pd.DataFrame, frames: dict, start, arm: str) -> dict:
    sub = sized if start is None else sized.loc[pd.to_datetime(sized["decision_date"]) >= start]
    if sub.empty:
        return {"trades": 0, "expectancy_bps": None, "weighted_expectancy_bps": None, "max_drawdown": None}
    weight = sub[f"w_{arm}"].to_numpy(float)
    returns: dict = defaultdict(float)
    for row, w in zip(sub.itertuples(index=False), weight):
        close = frames[row.symbol]["close"].to_numpy(float)
        dates = frames[row.symbol]["date"].to_numpy()
        marked = _marked_returns(pd.DataFrame([{"entry_idx": row.entry_idx, "exit_idx": row.exit_idx,
                                                "entry_price": row.entry_price, "exit_price": row.exit_price}]), close)
        for index in range(int(row.entry_idx), int(row.exit_idx) + 1):
            returns[dates[index]] += SLOT * w * marked[index]
    series = pd.Series(returns).sort_index()
    equity = np.concatenate([[1.0], np.cumprod(1 + series.to_numpy())]) if len(series) else np.array([1.0])
    drawdown = float(np.max(1 - equity / np.maximum.accumulate(equity)))
    months = pd.to_datetime(sub["decision_date"]).dt.to_period("M").astype(str).tolist()
    net = sub["net_return"].to_numpy(float)
    scaled = weight * net / weight.mean()  # mean(scaled) = sum(w r) / sum(w)
    stats = trade_statistics(scaled, clusters=months)
    plain = trade_statistics(net, clusters=months)
    changed = int((np.abs(sub[f"w_{arm}"].to_numpy(float) - sub["w_performance"].to_numpy(float)) > 1e-9).sum()) \
        if arm == "regime" else int((np.abs(weight - 1.0) > 1e-9).sum())
    return {"trades": int(len(sub)), "expectancy_bps": plain["expectancy_bps"],
            "weighted_expectancy_bps": stats["expectancy_bps"], "weighted_expectancy_ci_bps": stats.get("expectancy_ci_bps"),
            "win_rate": plain["win_rate"], "mean_weight": round(float(weight.mean()), 4),
            "trades_resized": changed, "total_return": round(float(equity[-1] - 1), 4),
            "max_drawdown": round(drawdown, 4)}


def decide(results: dict) -> dict:
    """The pre-registered rule: ``regime`` must be no worse than both other arms everywhere, and must fire."""
    reasons = []
    margins = []
    for window in WINDOWS:
        candidate = results["regime"][window]
        if not candidate.get("trades") or candidate.get("weighted_expectancy_bps") is None:
            reasons.append(f"{window}: no trades")
            continue
        if not candidate.get("trades_resized"):
            reasons.append(f"{window}: the regime multiplier never changed a size")
        for other in ("equal", "performance"):
            reference = results[other][window]
            if candidate["weighted_expectancy_bps"] < reference["weighted_expectancy_bps"]:
                reasons.append(f"{window}: expectancy below {other} "
                               f"({candidate['weighted_expectancy_bps']} < {reference['weighted_expectancy_bps']} bps)")
            if candidate["max_drawdown"] > reference["max_drawdown"]:
                reasons.append(f"{window}: drawdown worse than {other} "
                               f"({candidate['max_drawdown']} > {reference['max_drawdown']})")
            margins.append(candidate["weighted_expectancy_bps"] - reference["weighted_expectancy_bps"])
    adopt = not reasons
    return {"adopt": adopt, "paper_allocation_regime_enabled": adopt, "reasons": reasons,
            "min_expectancy_margin_bps": round(min(margins), 2) if margins else None}


def cell_table(sized: pd.DataFrame) -> list[dict]:
    """Per strategy and cell: trades and mean net return over the whole history (descriptive, not causal)."""
    out = []
    for (strategy, cell), group in sized.groupby(["strategy", "cell"]):
        out.append({"strategy": strategy, "cell": cell, "trades": int(len(group)),
                    "expectancy_bps": round(float(group["net_return"].mean() * 10_000), 1)})
    return out


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
    policy = bounds()
    frames, earnings = load_frames(args.data_dir, download=args.download, start=args.start, end=args.end)
    vix = load_vix(args.vix_csv, download=args.download)
    replays = {s: primary.replay_signals(f) for s, f in frames.items()}
    spy = frames["SPY"]
    rows = candidates(frames, spy, vix, replays, earnings=earnings)
    print("candidates", len(rows), flush=True)
    accepted = allocate(rows, frames, BOOK_POLICY).copy()
    inputs = daily_regime_inputs(frames, spy, vix)
    accepted["cell"] = cells_for(accepted, inputs)
    print("accepted", len(accepted), accepted["cell"].value_counts().to_dict(), flush=True)
    sized = size_trades(accepted.reset_index(drop=True), policy)
    windows = {name: (None if start is None else pd.Timestamp(start)) for name, start in WINDOWS.items()}
    report = {"data_end": max(str(f["date"].iloc[-1]) for f in frames.values()), "symbols": len(frames),
              "candidates": int(len(rows)), "accepted": int(len(sized)), "earnings_blackout": earnings,
              "vix_history": vix is not None, "policy": policy, "book": BOOK_POLICY,
              "cells": sized["cell"].value_counts().to_dict(), "arms": {}}
    for arm in ARMS:
        report["arms"][arm] = {w: arm_metrics(sized, frames, start, arm) for w, start in windows.items()}
        print(arm, {w: (m["trades"], m["weighted_expectancy_bps"], m["max_drawdown"], m["total_return"])
                    for w, m in report["arms"][arm].items()}, flush=True)
    report["decision"] = decide(report["arms"])
    report["cell_table"] = cell_table(sized)
    print("decision", report["decision"], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
