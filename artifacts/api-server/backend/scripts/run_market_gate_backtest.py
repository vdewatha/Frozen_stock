"""Backtest the SPY market-direction gate and tighter position caps on the bot's live strategies.

Pre-registered decision rules (same as reports/regime-filter-20261029.md and
regime-gate-20261106.md), fixed before the first run:

* **Market gate.** For each strategy, gate N in {10, 20, 50} is eligible only
  if net expectancy per trade is not worse than ungated in any window (all,
  2015+, 2020+) AND the equal-weight portfolio max drawdown is not worse than
  1.10x ungated in any window.  Among eligible N, the one with the largest
  minimum expectancy gain across the windows is adopted; none eligible = off.
* **Position cap.** On the whole book (every strategy with its adopted gate,
  one position per symbol, <= 5 per strategy, <= 3 per sector, 5% of equity per
  position), a total cap K < 20 is adopted only if per-trade expectancy is not
  worse in any window AND max drawdown improves in every window.

Each strategy is replayed as it trades on paper: the vectorised replicas in
``meta_label_primary`` (parity-tested against the strategy classes), a BUY
needs confidence >= 0.55 as in the backtester, RSI(14) and Bollinger keep
their live ADX ``range`` regime gate, RSI(2) uses its widened scope and IBS its
seeded IBS < 0.1.  Exits: the tested 3% stop / 6% target / 20 sessions for the
technical strategies, the 10% stop / 10 sessions / indicator exit for RSI(2)
and IBS.  Fills follow ``triple_barrier.label_events``: decision on the close,
next-open entry, volatility-scaled costs both sides, stop first, gaps at the
open.  Model Predictive Long refits a forest per bar and is not replayed.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_market_gate_backtest.py --data-dir <dir> [--download] --output <json>
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS  # noqa: E402
from app.services import meta_label_primary as primary  # noqa: E402
from app.services.market_gate import spy_above_sma  # noqa: E402
from app.services.meta_label import REVERSION_BARRIER  # noqa: E402
from app.services.seed import RSI2_WIDENED_SCOPE  # noqa: E402
from app.services.strategies.regime import DEFAULT_RANGE_ADX, wilder_adx  # noqa: E402
from app.services.strategy_discovery import _executable, _marked_returns  # noqa: E402
from app.services.strategy_scorecard import load_daily_history, normalize_prices, trade_statistics  # noqa: E402
from app.services.trading_universe import sector_group  # noqa: E402
from app.services.triple_barrier import BarrierConfig, label_events  # noqa: E402

INDEX = ("SPY", "QQQ", "IWM", "DIA")
BRACKET = BarrierConfig(take_profit_pct=0.06, stop_loss_pct=0.03, max_holding_days=20)
TECHNICAL = ("moving_average_crossover", "rsi_mean_reversion", "macd_momentum", "ensemble",
             "bollinger_mean_reversion", "channel_breakout", "trend_pullback")
RANGE_GATED = {"rsi_mean_reversion", "bollinger_mean_reversion"}
STRATEGIES = (*TECHNICAL, "rsi2_pullback", "ibs_mean_reversion")
GATES = (0, 10, 20, 50)
WINDOWS = {"all": None, "2015+": "2015-01-01", "2020+": "2020-01-01"}
CAPS = (20, 12, 8, 5)
SLOT = 0.05


def scope(strategy: str, symbols: list[str]) -> list[str]:
    if strategy == "rsi2_pullback":
        return [s for s in symbols if s in RSI2_WIDENED_SCOPE]
    if strategy == "ibs_mean_reversion":
        return [s for s in symbols if s in INDEX]
    return symbols


def entries(strategy: str, frame: pd.DataFrame, replay: dict) -> np.ndarray:
    signal = primary.ibs_mean_reversion(frame, entry_threshold=0.1) if strategy == "ibs_mean_reversion" \
        else replay[strategy]
    buy = (signal["action"].to_numpy() == "BUY") & (signal["confidence"].to_numpy() >= 0.55)
    if strategy in RANGE_GATED:
        adx = wilder_adx(frame)["adx"].to_numpy()
        buy &= np.isfinite(adx) & (adx < DEFAULT_RANGE_ADX)
    return buy


def spy_gate_on(frame: pd.DataFrame, spy: pd.DataFrame, window: int) -> np.ndarray:
    if window <= 0:
        return np.ones(len(frame), dtype=bool)
    above = pd.Series(spy_above_sma(spy["close"], window), index=pd.to_datetime(spy["date"]))
    dates = pd.to_datetime(frame["date"])
    return above.reindex(dates, method="ffill").fillna(False).to_numpy(bool)


def simulate(strategy: str, frames: dict, spy: pd.DataFrame, window: int, replays: dict) -> pd.DataFrame:
    barrier = REVERSION_BARRIER if strategy in ("rsi2_pullback", "ibs_mean_reversion") else BRACKET
    out = []
    for symbol in scope(strategy, list(frames)):
        frame = frames[symbol]
        buy = entries(strategy, frame, replays[symbol]) & spy_gate_on(frame, spy, window)
        positions = np.flatnonzero(buy)
        if not len(positions):
            continue
        labels = label_events(frame, positions, barrier, exit_signals=primary.replay_exit_signals(frame, strategy))
        if labels.empty:
            continue
        labels = _executable(labels).assign(symbol=symbol, strategy=strategy)
        out.append(labels)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def window_metrics(trades: pd.DataFrame, frames: dict, start) -> dict:
    sub = trades if start is None else trades.loc[pd.to_datetime(trades["decision_date"]) >= start]
    months = pd.to_datetime(sub["decision_date"]).dt.to_period("M").astype(str).tolist()
    stats = trade_statistics(sub["net_return"].to_numpy(), clusters=months)
    daily = []
    for symbol, group in sub.groupby("symbol"):
        frame = frames[symbol]
        series = pd.Series(_marked_returns(group, frame["close"].to_numpy(float)), index=pd.to_datetime(frame["date"]))
        daily.append(series if start is None else series.loc[series.index >= start])
    portfolio = pd.concat(daily, axis=1).mean(axis=1).sort_index() if daily else pd.Series(dtype=float)
    equity = np.cumprod(1 + portfolio.to_numpy()) if len(portfolio) else np.array([1.0])
    peak = np.maximum.accumulate(np.concatenate([[1.0], equity]))
    drawdown = float(np.max(1 - np.concatenate([[1.0], equity]) / peak))
    return {"trades": stats["trades"], "win_rate": stats["win_rate"], "expectancy_bps": stats["expectancy_bps"],
            "expectancy_ci_bps": stats.get("expectancy_ci_bps"), "profit_factor": stats["profit_factor"],
            "portfolio_max_drawdown": round(drawdown, 4)}


def decide_gate(rows: dict) -> dict:
    base = rows[0]
    eligible = {}
    for window in GATES[1:]:
        gains, ok = [], True
        for name in WINDOWS:
            b, g = base[name], rows[window][name]
            if g["trades"] == 0 or g["expectancy_bps"] is None or b["expectancy_bps"] is None:
                ok = False
                break
            gains.append(g["expectancy_bps"] - b["expectancy_bps"])
            if g["expectancy_bps"] < b["expectancy_bps"] or g["portfolio_max_drawdown"] > 1.10 * b["portfolio_max_drawdown"]:
                ok = False
        if ok:
            eligible[window] = min(gains)
    adopted = max(eligible, key=eligible.get) if eligible else 0
    return {"adopted": adopted, "eligible": eligible}


def book(trades_by_strategy: dict, frames: dict, cap: int) -> pd.DataFrame:
    """Accept trades in entry order under the live caps (one per symbol, 5 per strategy, 3 per sector, cap)."""
    rows = pd.concat(trades_by_strategy.values(), ignore_index=True)
    rows["entry_date"] = [frames[s]["date"].iloc[i] for s, i in zip(rows["symbol"], rows["entry_idx"])]
    rows["exit_date"] = [frames[s]["date"].iloc[i] for s, i in zip(rows["symbol"], rows["exit_idx"])]
    rows["rank"] = rows["strategy"].map({name: rank for rank, name in enumerate(STRATEGIES)})
    rows = rows.sort_values(["entry_date", "rank", "symbol"], kind="stable")
    open_rows: list[dict] = []
    keep = []
    for row in rows.itertuples(index=False):
        open_rows = [o for o in open_rows if o["exit_date"] > row.entry_date]
        by_strategy = sum(o["strategy"] == row.strategy for o in open_rows)
        by_sector = sum(o["sector"] == sector_group(row.symbol) for o in open_rows)
        if (len(open_rows) < cap and by_strategy < 5 and by_sector < 3
                and all(o["symbol"] != row.symbol for o in open_rows)):
            keep.append(True)
            open_rows.append({"symbol": row.symbol, "strategy": row.strategy, "sector": sector_group(row.symbol),
                              "exit_date": row.exit_date})
        else:
            keep.append(False)
    return rows.loc[keep]


def book_metrics(accepted: pd.DataFrame, frames: dict, start) -> dict:
    sub = accepted if start is None else accepted.loc[pd.to_datetime(accepted["decision_date"]) >= start]
    returns: dict = defaultdict(float)
    for row in sub.itertuples(index=False):
        close = frames[row.symbol]["close"].to_numpy(float)
        dates = frames[row.symbol]["date"].to_numpy()
        marked = _marked_returns(pd.DataFrame([{"entry_idx": row.entry_idx, "exit_idx": row.exit_idx,
                                                "entry_price": row.entry_price, "exit_price": row.exit_price}]), close)
        for index in range(int(row.entry_idx), int(row.exit_idx) + 1):
            returns[dates[index]] += SLOT * marked[index]
    series = pd.Series(returns).sort_index()
    equity = np.cumprod(1 + series.to_numpy()) if len(series) else np.array([1.0])
    peak = np.maximum.accumulate(np.concatenate([[1.0], equity]))
    stops = sub.loc[sub["barrier"].astype(str).str.startswith("stop_loss")]
    cluster_days = int((stops.groupby("exit_date").size() >= 3).sum())
    months = pd.to_datetime(sub["decision_date"]).dt.to_period("M").astype(str).tolist()
    stats = trade_statistics(sub["net_return"].to_numpy(), clusters=months)
    return {"trades": stats["trades"], "win_rate": stats["win_rate"], "expectancy_bps": stats["expectancy_bps"],
            "expectancy_ci_bps": stats.get("expectancy_ci_bps"), "total_return": round(float(equity[-1] - 1), 4),
            "max_drawdown": round(float(np.max(1 - np.concatenate([[1.0], equity]) / peak)), 4),
            "days_with_3plus_stops": cluster_days}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    warnings.filterwarnings("ignore")
    symbols = list(DEFAULT_PAPER_EXECUTION_SYMBOLS)
    frames = {s: normalize_prices(load_daily_history(s, args.data_dir, download=args.download),
                                  start=args.start, end=args.end) for s in symbols}
    frames = {s: f.reset_index(drop=True) for s, f in frames.items() if len(f) > 260}
    spy = frames["SPY"]
    replays = {s: primary.replay_signals(f) for s, f in frames.items()}
    windows = {name: (None if start is None else pd.Timestamp(start)) for name, start in WINDOWS.items()}

    report = {"data_end": max(str(f["date"].iloc[-1]) for f in frames.values()), "start": args.start,
              "symbols": len(frames), "strategies": {}, "book": {}}
    adopted_trades, base_trades = {}, {}
    for strategy in STRATEGIES:
        rows, trades = {}, {}
        for window in GATES:
            trades[window] = simulate(strategy, frames, spy, window, replays)
            rows[window] = {name: window_metrics(trades[window], frames, start) for name, start in windows.items()}
        decision = decide_gate(rows)
        report["strategies"][strategy] = {"rows": rows, "decision": decision}
        base_trades[strategy] = trades[0]
        adopted_trades[strategy] = trades[decision["adopted"]]
        print(strategy, "adopted", decision["adopted"], "eligible", decision["eligible"], flush=True)

    for label, trades in (("ungated", base_trades), ("adopted_gates", adopted_trades)):
        report["book"][label] = {}
        for cap in CAPS:
            accepted = book(trades, frames, cap)
            report["book"][label][str(cap)] = {name: book_metrics(accepted, frames, start)
                                              for name, start in windows.items()}
    current = report["book"]["adopted_gates"]["20"]
    eligible_caps = [cap for cap in CAPS[1:] if all(
        report["book"]["adopted_gates"][str(cap)][w]["expectancy_bps"] >= current[w]["expectancy_bps"]
        and report["book"]["adopted_gates"][str(cap)][w]["max_drawdown"] < current[w]["max_drawdown"]
        for w in WINDOWS)]
    report["cap_decision"] = {"eligible": eligible_caps, "adopted": max(eligible_caps) if eligible_caps else 20}
    print("cap decision", report["cap_decision"], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print("wrote", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
