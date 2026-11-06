"""Backtest the position-sizing modes on the live paper book, per strategy.

``app/services/position_sizing.py`` gives each strategy a ``sizing_mode``:
``fixed_fraction`` (today's rule), ``volatility_target`` (loss at the stop =
``risk_per_trade`` of equity) and ``fractional_kelly`` (quarter Kelly from a
calibrated probability and the strategy's empirical payoff ratio).  Every
mode is capped at the fixed-fraction notional.  This script sizes the same
accepted book trades under each mode so that a mode is adopted per strategy
on evidence.

Pre-registered decision rule (fixed before the first run):

* The baseline sizes every position at the slot fraction (1 / 20 of equity,
  the live book's fixed fraction in this simulation).
* Candidate arms: ``volatility_target`` at 0.25% / 0.5% / 1% risk per trade
  (notional = risk / the strategy's stop distance, capped at the slot) and
  ``fractional_kelly`` with a simulated calibrated probability at the ends
  and the middle of a 55-60% band (0.55 / 0.575 / 0.60; quarter Kelly with
  the strategy's payoff ratio from its book trades closed strictly before the
  entry day, at least 30 of them, else the 0.5% volatility target; capped at
  the slot).
* A candidate is tested *per strategy*: that strategy's trades take the
  candidate's size while every other strategy keeps the fixed fraction.
  The candidate is eligible for the strategy only if, in every window (all,
  2015+, 2020+), the book's total profit is not lower than the baseline's,
  the book's max drawdown is not worse, and the candidate changed the size
  of at least one of the strategy's trades (an arm that never fires is no
  evidence).
* Among eligible candidates the one with the largest minimum total-profit
  gain across the windows is adopted; none eligible = keep ``fixed_fraction``.
* The whole-book arms (every strategy on the candidate at once) are
  reported with trades, expectancy per unit of notional, total return and
  max drawdown together, as information only.

Book and data (all read-only reuse):

* Candidates: every executable trade of the 9 replayable live strategies
  from ``gate_replay.simulate`` under the gates live today
  (``run_regime_allocation_backtest.candidates``).
* Book: the live caps, slots in the live cycle order and the 10-session
  stop-out cooldown (``run_slot_allocation_backtest.allocate``).
* Stop distance of a strategy: its replay barrier (``gate_replay.barrier_for``:
  3% for the technical strategies, 10% for RSI(2) and IBS), the same stop the
  paper exit policy sends to the broker.
* Returns are marked to market daily; a position's weight is its fraction
  of equity.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_position_sizing_backtest.py --data-dir <dir> \\
        [--vix-csv <date,close csv>] [--download] --output <json>
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import gate_replay, position_sizing  # noqa: E402
from app.services import meta_label_primary as primary  # noqa: E402
from app.services.strategy_scorecard import trade_statistics  # noqa: E402
from run_regime_allocation_backtest import candidates, load_frames, load_vix  # noqa: E402
from run_slot_allocation_backtest import BASE_CAPS, allocate  # noqa: E402

WINDOWS = gate_replay.WINDOWS
BOOK_POLICY = {"stop_cooldown": 10}
SLOT = 1.0 / BASE_CAPS["total"]
RISK_LEVELS = (0.0025, 0.005, 0.01)
PROBABILITY_BAND = (0.55, 0.575, 0.60)
KELLY_FALLBACK_RISK = 0.005
BASELINE = "fixed_fraction"


def arms() -> dict[str, dict]:
    """Candidate arms: name -> the strategy parameters the arm stands for (plus the simulated probability)."""
    out = {BASELINE: {"sizing_mode": position_sizing.FIXED_FRACTION}}
    for risk in RISK_LEVELS:
        out[f"volatility_target_{risk:.2%}"] = {"sizing_mode": position_sizing.VOLATILITY_TARGET,
                                                "risk_per_trade": risk}
    for p in PROBABILITY_BAND:
        out[f"fractional_kelly_p{p:.3f}"] = {"sizing_mode": position_sizing.FRACTIONAL_KELLY,
                                             "risk_per_trade": KELLY_FALLBACK_RISK, "probability": p}
    return out


ARMS = arms()


# ----------------------------------------------------------------- sizing (causal)

def stop_distances() -> dict[str, float]:
    return {strategy: float(gate_replay.barrier_for(strategy).stop_loss_pct) for strategy in gate_replay.STRATEGIES}


class PayoffRecords:
    """Per strategy: book trades sorted by exit date, for the payoff ratio of trades closed before any day."""

    def __init__(self, rows: pd.DataFrame) -> None:
        self.by_strategy = {}
        for strategy, group in rows.groupby("strategy"):
            group = group.sort_values("exit_date", kind="stable")
            returns = group["net_return"].to_numpy(float)
            wins = np.cumsum(np.where(returns > 0, returns, 0.0))
            win_count = np.cumsum(returns > 0)
            losses = np.cumsum(np.where(returns <= 0, -returns, 0.0))
            loss_count = np.cumsum(returns <= 0)
            self.by_strategy[strategy] = (pd.to_datetime(group["exit_date"]).to_numpy(), wins, win_count,
                                          losses, loss_count)

    def payoff(self, strategy: str, day) -> dict:
        """``position_sizing.payoff_ratio`` of the strategy's trades closed strictly before ``day``."""
        if strategy not in self.by_strategy:
            return {"trades": 0, "wins": 0, "losses": 0, "payoff_ratio": None}
        dates, wins, win_count, losses, loss_count = self.by_strategy[strategy]
        n = int(np.searchsorted(dates, pd.Timestamp(day).to_datetime64(), side="left"))
        if n == 0:
            return {"trades": 0, "wins": 0, "losses": 0, "payoff_ratio": None}
        w, lw = int(win_count[n - 1]), int(loss_count[n - 1])
        avg_win = wins[n - 1] / w if w else None
        avg_loss = losses[n - 1] / lw if lw else None
        ratio = avg_win / avg_loss if avg_win is not None and avg_loss else None
        return {"trades": n, "wins": w, "losses": lw, "payoff_ratio": ratio}


def arm_fraction(arm: dict, strategy: str, stop: float, payoff: dict) -> float:
    """Fraction of equity one trade gets under ``arm`` (equity 1, cap = the slot)."""
    sized = position_sizing.size_notional(
        mode=arm["sizing_mode"], equity=1.0, risk_fraction=SLOT, stop_distance=stop,
        risk_per_trade=arm.get("risk_per_trade", position_sizing.DEFAULT_RISK_PER_TRADE),
        probability=arm.get("probability"), payoff=payoff, enforced=arm.get("probability") is not None)
    return float(sized["notional"])


def size_trades(rows: pd.DataFrame) -> pd.DataFrame:
    """Add a ``w_<arm>`` column per arm (causal: the payoff uses trades closed before the entry day)."""
    records = PayoffRecords(rows)
    stops = stop_distances()
    columns = {name: np.full(len(rows), SLOT) for name in ARMS}
    payoff_trades = np.zeros(len(rows), dtype=int)
    for position, (strategy, day) in enumerate(zip(rows["strategy"], rows["entry_date"])):
        payoff = records.payoff(strategy, day)
        payoff_trades[position] = payoff["trades"]
        for name, arm in ARMS.items():
            if name != BASELINE:
                columns[name][position] = arm_fraction(arm, strategy, stops[strategy], payoff)
    out = rows.copy()
    for name in ARMS:
        out[f"w_{name}"] = columns[name]
    out["payoff_trades"] = payoff_trades
    return out


# ----------------------------------------------------------------- book metrics

class Book:
    """Daily mark-to-market of every accepted trade, once, so any weight vector prices in milliseconds."""

    def __init__(self, rows: pd.DataFrame, frames: dict) -> None:
        self.dates = np.array(sorted({d for f in frames.values() for d in pd.to_datetime(f["date"])}),
                              dtype="datetime64[ns]")
        self.decision = pd.to_datetime(rows["decision_date"]).to_numpy()
        trade_ids, day_ids, marks = [], [], []
        for trade_id, row in enumerate(rows.itertuples(index=False)):
            frame = frames[row.symbol]
            close = frame["close"].to_numpy(float)
            entry, exit_ = int(row.entry_idx), int(row.exit_idx)
            if exit_ == entry:
                series = np.array([row.exit_price / row.entry_price - 1])
            else:
                series = np.empty(exit_ - entry + 1)
                series[0] = close[entry] / row.entry_price - 1
                series[1:-1] = close[entry + 1:exit_] / close[entry:exit_ - 1] - 1
                series[-1] = row.exit_price / close[exit_ - 1] - 1
            days = pd.to_datetime(frame["date"].iloc[entry:exit_ + 1]).to_numpy(dtype="datetime64[ns]")
            trade_ids.append(np.full(len(series), trade_id))
            day_ids.append(np.searchsorted(self.dates, days))
            marks.append(series)
        self.trade_ids = np.concatenate(trade_ids)
        self.day_ids = np.concatenate(day_ids)
        self.marks = np.concatenate(marks)

    def curve(self, weights: np.ndarray, start) -> np.ndarray:
        """Equity curve of the book (1 + the summed weighted daily returns) over trades decided from ``start``."""
        w = np.asarray(weights, dtype=float).copy()
        if start is not None:
            w[self.decision < np.datetime64(pd.Timestamp(start))] = 0.0
        daily = np.bincount(self.day_ids, weights=w[self.trade_ids] * self.marks, minlength=len(self.dates))
        return np.concatenate([[1.0], np.cumprod(1 + daily)])


def book_metrics(book: Book, sized: pd.DataFrame, weights: np.ndarray, start, *, changed_mask=None) -> dict:
    mask = np.ones(len(sized), dtype=bool) if start is None else \
        (pd.to_datetime(sized["decision_date"]).to_numpy() >= np.datetime64(pd.Timestamp(start)))
    if not mask.any():
        return {"trades": 0, "expectancy_bps": None, "weighted_expectancy_bps": None, "total_return": None,
                "max_drawdown": None, "trades_resized": 0}
    curve = book.curve(weights, start)
    drawdown = float(np.max(1 - curve / np.maximum.accumulate(curve)))
    net = sized["net_return"].to_numpy(float)[mask]
    w = np.asarray(weights, dtype=float)[mask]
    months = pd.to_datetime(sized["decision_date"]).dt.to_period("M").astype(str).to_numpy()[mask].tolist()
    weighted = trade_statistics(w * net / w.mean(), clusters=months) if w.mean() > 0 else {"expectancy_bps": None}
    plain = trade_statistics(net, clusters=months)
    resized = int(((np.abs(np.asarray(weights) - SLOT) > 1e-12) & mask).sum()) if changed_mask is None \
        else int((changed_mask & mask).sum())
    return {"trades": int(mask.sum()), "expectancy_bps": plain["expectancy_bps"], "win_rate": plain["win_rate"],
            "weighted_expectancy_bps": weighted["expectancy_bps"],
            "weighted_expectancy_ci_bps": weighted.get("expectancy_ci_bps"),
            "mean_weight": round(float(w.mean()), 5), "trades_resized": resized,
            "total_return": round(float(curve[-1] - 1), 4), "max_drawdown": round(drawdown, 4)}


def whole_book(book: Book, sized: pd.DataFrame) -> dict:
    """Every strategy on the arm at once (information only)."""
    out = {}
    for name in ARMS:
        weights = sized[f"w_{name}"].to_numpy(float)
        out[name] = {window: book_metrics(book, sized, weights, start) for window, start in WINDOWS.items()}
    return out


def per_strategy(book: Book, sized: pd.DataFrame) -> dict:
    """Each strategy on each candidate arm while every other strategy keeps the fixed fraction."""
    base = sized[f"w_{BASELINE}"].to_numpy(float)
    out = {}
    for strategy in sorted(sized["strategy"].unique()):
        member = (sized["strategy"] == strategy).to_numpy()
        out[strategy] = {"trades": int(member.sum()), "arms": {}}
        for name in ARMS:
            if name == BASELINE:
                continue
            weights = np.where(member, sized[f"w_{name}"].to_numpy(float), base)
            changed = member & (np.abs(weights - base) > 1e-12)
            out[strategy]["arms"][name] = {window: book_metrics(book, sized, weights, start, changed_mask=changed)
                                           for window, start in WINDOWS.items()}
    return out


# ----------------------------------------------------------------- the rule

def decide(baseline: dict, strategies: dict) -> dict:
    """Per strategy: the eligible arms and the adopted one (``fixed_fraction`` when none is eligible)."""
    out = {}
    for strategy, record in strategies.items():
        eligible, reasons = {}, {}
        for name, windows in record["arms"].items():
            gains, why = [], []
            for window in WINDOWS:
                b, c = baseline[window], windows[window]
                if c.get("total_return") is None or b.get("total_return") is None:
                    why.append(f"{window}: no trades")
                    continue
                if c["total_return"] < b["total_return"]:
                    why.append(f"{window}: total profit lower ({c['total_return']:+.4f} < {b['total_return']:+.4f})")
                if c["max_drawdown"] > b["max_drawdown"]:
                    why.append(f"{window}: drawdown worse ({c['max_drawdown']:.4f} > {b['max_drawdown']:.4f})")
                if not c.get("trades_resized"):
                    why.append(f"{window}: the arm never changed a size")
                gains.append(c["total_return"] - b["total_return"])
            if why:
                reasons[name] = why
            else:
                eligible[name] = round(min(gains), 6)
        adopted = max(eligible, key=eligible.get) if eligible else BASELINE
        parameters = {key: value for key, value in ARMS[adopted].items() if key != "probability"}
        out[strategy] = {"adopted": adopted, "parameters": parameters, "eligible": eligible, "rejected": reasons}
    return out


def payoff_table(sized: pd.DataFrame) -> list[dict]:
    """Per strategy over the whole history (descriptive, not causal): payoff ratio and what quarter Kelly would size."""
    out = []
    for strategy, group in sized.groupby("strategy"):
        stats = position_sizing.payoff_ratio(group["net_return"].to_numpy(float))
        ratio = stats["payoff_ratio"]
        kelly = {f"p{p:.3f}": (round(position_sizing.kelly_fraction(p, ratio)["kelly"], 4) if ratio else None)
                 for p in PROBABILITY_BAND}
        out.append({"strategy": strategy, "trades": stats["trades"],
                    "win_rate": round(stats["wins"] / stats["trades"], 4) if stats["trades"] else None,
                    "payoff_ratio": ratio, "quarter_kelly": kelly,
                    "trades_with_payoff_history": int((group["payoff_trades"] >= position_sizing.KELLY_MIN_TRADES).sum())})
    return out


def adopted_parameters(decision: dict) -> dict[str, dict]:
    """``strategy -> {sizing_mode, risk_per_trade}`` for every strategy that leaves the fixed fraction."""
    return {strategy: record["parameters"] for strategy, record in decision.items()
            if record["adopted"] != BASELINE}


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
    replays = {s: primary.replay_signals(f) for s, f in frames.items()}
    rows = candidates(frames, frames["SPY"], vix, replays, earnings=earnings)
    print("candidates", len(rows), flush=True)
    accepted = allocate(rows, frames, BOOK_POLICY).reset_index(drop=True)
    print("accepted", len(accepted), flush=True)
    sized = size_trades(accepted)
    book = Book(sized, frames)
    report = {"data_end": max(str(f["date"].iloc[-1]) for f in frames.values()), "symbols": len(frames),
              "candidates": int(len(rows)), "accepted": int(len(sized)), "earnings_blackout": earnings,
              "vix_history": vix is not None, "book": BOOK_POLICY, "slot": SLOT,
              "stop_distances": stop_distances(), "arms": ARMS,
              "kelly_min_trades": position_sizing.KELLY_MIN_TRADES, "kelly_fraction": position_sizing.KELLY_FRACTION}
    report["whole_book"] = whole_book(book, sized)
    for name, windows in report["whole_book"].items():
        print(name, {w: (m["trades"], m["weighted_expectancy_bps"], m["total_return"], m["max_drawdown"],
                         m["trades_resized"]) for w, m in windows.items()}, flush=True)
    report["strategies"] = per_strategy(book, sized)
    report["decision"] = decide(report["whole_book"][BASELINE], report["strategies"])
    report["adopted"] = adopted_parameters(report["decision"])
    report["payoff_table"] = payoff_table(sized)
    for strategy, record in report["decision"].items():
        print(strategy, record["adopted"], record["eligible"], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
