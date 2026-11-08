"""Step 4 of ``reports/Retail bot edge feasibility.md``: re-verdict the short-horizon sleeve.

Every number here is **in-sample**: every post-2015 replay of IBS and RSI(2)
has been looked at before (the survivorship, sector-reversal and yardstick
reports), and RSI(2)'s scope was chosen on the same windows.  The rule below
binds what happens next, not what is already known.

Pre-registered rule (the report's row, applied as written)
-----------------------------------------------------------
Candidates:

* ``ibs_etf``: IBS mean reversion on SPY, QQQ, IWM, DIA, the live replica
  (``gate_replay``: IBS < 0.1, no trend filter, exit IBS > 0.8, 10 sessions,
  10% stop; next-open entry, open exits).
* ``rsi2_etf``: RSI(2) pullback on the same four ETFs (RSI(2) < 10, close >
  SMA200, exit close > SMA5, 10 sessions, 10% stop, the live 50-day SPY gate).
* ``rsi2_stock_pit``: RSI(2)'s single-stock leg, the same rule and setup, run
  **once** on every point-in-time S&P 500 single stock with yfinance history
  (a bar is enterable only while the stock was a member), with an earnings
  blackout of 10 sessions where an earnings history is cached (today's names;
  the survivorship report's convention).
* ``close_open_variant``: the one new variant.  On each of the four ETFs, buy
  the close of a session whose close-to-close return is at most
  ``VARIANT_Z`` (-1.5) times the standard deviation of the previous 20
  daily returns, sell the next official open.  The live rule would decide on
  15:45 ET prices (before Alpaca's 15:50 closing-auction cutoff); daily bars
  carry no 15:45 price, so the decision uses the official close itself, the
  price it fills at.  That is an approximation with 15 minutes of look-ahead.

Costs: 10 and 25 bps a round trip, and the published model (each trade's two
sides from ``yardstick.trade_costs``, about 27 bps on these ETFs).

Checks, each at a stated cost, over 2015-01-01 to the latest bar:

1. ``gross``: mean gross per trade >= max(30 bps, 3 x cost).  At the model
   the cost is the candidate's mean modelled round trip.
2. ``sharpe``: the sleeve's annualised net Sharpe over T-bills, times 0.5
   (the haircut), >= 0.5.  The sleeve: one slot per ETF (a quarter of the
   sleeve each; one position per symbol), or for the stock leg five slots of
   a fifth (the live per-strategy cap, live candidate order); a slot's idle
   days earn T-bills (BIL, total return), each round trip is charged half on
   its entry day and half on its exit day.  The date-clustered t of the
   per-trade net divided by sqrt(years) is shown beside it (the report's
   "t of about 3.4" reading), not decisive.
3. ``marginal`` (the step-3 rule): a slice of the sleeve, taken from the
   passive arm's T-bill leg and rebalanced monthly, raises the arm's Sharpe
   over T-bills with no more than 2 extra points of maximum drawdown, in
   **both** windows (2008-07-01 and 2015-01-01 to the latest bar), on the
   live arm (20% SPY / 80% T-bills).  The slice is the allocation the sleeve
   would take at that cost: 20% of equity at 25 bps and at the model, 10% at
   10 bps.  A 50/50 arm is shown too, not decisive.

Allocation:

* ``sleeve_20``: all three checks hold at 25 bps **and** at the model.
* else ``keep_paper_size_pending_cost``: all three hold at 10 bps; the rule
  keeps its current paper size until the bot's measured round trip
  (``--measured-cost-bps``, read from the step 1 cost table) is <= 10 bps,
  and becomes ``sleeve_10`` (up to 10% of risk) once it is.
* else ``shadow``.
* The variant also needs post-2021 gross >= max(30 bps, 3 x cost) at the
  passing cost; on any fail it is ``dropped`` for good (no strategy row).
* RSI(2)'s stock leg stays in shadow unless its point-in-time run passes.

Data ($0): the shared yfinance cache (``--data-dir``, used as
``meta_label_history_dir``), the membership CSV under ``<data-dir>/universe``;
optional ``--earnings-dir``.  Cache only (no network).

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_short_horizon_reverdict.py --data-dir <dir> --output <json> [--tables <md>] \\
        [--earnings-dir <dir>] [--measured-cost-bps <x>]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402

INDEX = ("SPY", "QQQ", "IWM", "DIA")
START = "2015-01-01"
WINDOWS = {"2008-07+": "2008-07-01", "2015+": "2015-01-01"}
SPLIT = "2021-01-01"
COSTS = ("10bps", "25bps", "model")
FLAT_BPS = {"10bps": 10.0, "25bps": 25.0}
FLOOR_BPS = 30.0
COST_MULTIPLE = 3.0
HAIRCUT = 0.5
SHARPE_REQUIRED = 0.5
DD_ALLOWANCE = 0.02
ALLOCATION = {"10bps": 0.10, "25bps": 0.20, "model": 0.20}
LIVE_ARM = 0.20
SHOWN_ARM = 0.50
VARIANT_Z = -1.5
VARIANT_VOL_WINDOW = 20
STOCK_SLOTS = 5
# Data hygiene for past members (fixed before the stock-leg numbers were read): yfinance serves some
# delisted tickers' later bars as flat zero-volume prints (CPWR 2009-2010, EP 2003-2009), which a
# stop-or-indicator replay turns into fake +35% trades.  An entry needs a traded decision bar and a
# traded entry bar, and a 20-session median dollar volume of at least $1 million; no S&P 500 member
# trades less (a share-count floor would wrongly drop high-priced names such as NVR).
MIN_MEDIAN_DOLLAR_VOLUME = 1_000_000
VOLUME_WINDOW = 20
TRADING_DAYS = 252
RULE = ("Each candidate, at a stated cost: (i) gross per trade >= max(30 bps, 3 x cost); (ii) net Sharpe over "
        "T-bills x 0.5 >= 0.5; (iii) the step-3 marginal rule in both windows at the allocation the sleeve would "
        "take (20% at 25 bps and the model, 10% at 10 bps), on the live 20/80 arm. sleeve_20 if all three hold at "
        "25 bps and at the model; else keep_paper_size_pending_cost if they hold at 10 bps (sleeve_10 once the "
        "measured round trip is <= 10 bps); else shadow. The variant also needs post-2021 gross >= max(30, 3 x cost) "
        "and is dropped for good on a fail.")


# ----------------------------------------------------------------- decisions (pure, tested)

def required_gross_bps(cost_bps: float) -> float:
    return max(FLOOR_BPS, COST_MULTIPLE * float(cost_bps))


def cost_checks(gross_bps: Optional[float], cost_bps: Optional[float], sharpe: Optional[float],
                marginal: Mapping[str, Mapping], *, post_gross_bps: Optional[float] = None,
                variant: bool = False) -> dict:
    """The three checks (and the variant's post-2021 check) at one cost."""
    need = required_gross_bps(cost_bps or 0.0)
    gross_ok = gross_bps is not None and cost_bps is not None and float(gross_bps) >= need
    haircut = None if sharpe is None else round(HAIRCUT * float(sharpe), 3)
    sharpe_ok = haircut is not None and haircut >= SHARPE_REQUIRED
    marginal_ok = bool(marginal) and all(bool((m or {}).get("passes")) for m in marginal.values())
    out = {"required_gross_bps": round(need, 2), "gross_ok": bool(gross_ok), "haircut_sharpe": haircut,
           "sharpe_ok": bool(sharpe_ok), "marginal_ok": bool(marginal_ok)}
    passes = gross_ok and sharpe_ok and marginal_ok
    if variant:
        post_ok = post_gross_bps is not None and float(post_gross_bps) >= need
        out["post_2021_gross_ok"] = bool(post_ok)
        passes = passes and post_ok
    out["passes"] = bool(passes)
    return out


def outcome(checks: Mapping[str, Mapping], *, measured_cost_bps: Optional[float] = None, variant: bool = False,
            stock_leg: bool = False) -> dict:
    """The allocation the pre-registered rule gives one candidate."""
    at = {cost: bool((checks.get(cost) or {}).get("passes")) for cost in COSTS}
    if at["25bps"] and at["model"]:
        decision = "sleeve_20"
    elif at["10bps"]:
        measured_ok = measured_cost_bps is not None and float(measured_cost_bps) <= FLAT_BPS["10bps"]
        decision = "sleeve_10" if measured_ok else "keep_paper_size_pending_cost"
    else:
        decision = "dropped" if variant else "shadow"
    if stock_leg and decision == "shadow":
        decision = "stays_shadow"
    return {"decision": decision, "passes_at": at, "measured_cost_bps": measured_cost_bps,
            "pending": ("measured round trip <= 10 bps (step 1 cost table)"
                        if decision == "keep_paper_size_pending_cost" else None)}


def marginal_check(sleeve: pd.Series, spy: pd.Series, bills: pd.Series, *, equity_weight: float, slice_: float,
                   start, end=None) -> dict:
    """Step-3 rule: does a ``slice_`` of the sleeve from the arm's T-bill leg raise its Sharpe within +2 pts of DD?"""
    base = blend({"spy": spy, "bills": bills}, {"spy": equity_weight, "bills": 1 - equity_weight}, start, end)
    mixed = blend({"spy": spy, "sleeve": sleeve, "bills": bills},
                  {"spy": equity_weight, "sleeve": slice_, "bills": 1 - equity_weight - slice_}, start, end)
    a, b = series_stats(base, bills), series_stats(mixed, bills)
    passes = (b["sharpe"] is not None and a["sharpe"] is not None and b["sharpe"] > a["sharpe"]
              and b["max_drawdown"] <= a["max_drawdown"] + DD_ALLOWANCE + 1e-12)
    return {"arm": a, "with_slice": b, "slice": slice_, "equity_weight": equity_weight,
            "sharpe_delta": None if a["sharpe"] is None or b["sharpe"] is None else round(b["sharpe"] - a["sharpe"], 4),
            "drawdown_delta": round(b["max_drawdown"] - a["max_drawdown"], 4), "passes": bool(passes)}


# ----------------------------------------------------------------- series

def blend(parts: Mapping[str, pd.Series], weights: Mapping[str, float], start, end=None) -> pd.Series:
    """Monthly rebalanced mix of daily return series (weights reset on each month's last session)."""
    frame = pd.DataFrame(parts).sort_index()
    frame = frame.loc[frame.index >= pd.Timestamp(start)]
    if end is not None:
        frame = frame.loc[frame.index < pd.Timestamp(end)]
    frame = frame.fillna(0.0)
    names = list(frame.columns)
    target = np.array([float(weights[n]) for n in names])
    month = frame.index.to_period("M")
    last = np.r_[month[1:] != month[:-1], True]
    values = frame.to_numpy(float)
    w = target.copy()
    out = np.zeros(len(frame))
    for i in range(len(frame)):
        out[i] = float((w * values[i]).sum())
        w = w * (1 + values[i])
        total = w.sum()
        w = w / total if total > 0 else target.copy()
        if last[i]:
            w = target.copy()
    return pd.Series(out, index=frame.index)


def series_stats(returns: pd.Series, bills: pd.Series) -> dict:
    if returns is None or len(returns) < 2:
        return {"sessions": 0, "cagr": None, "sharpe": None, "max_drawdown": 0.0}
    rf = bills.reindex(returns.index).fillna(0.0)
    excess = returns - rf
    years = len(returns) / TRADING_DAYS
    growth = float(np.prod(1 + returns.to_numpy(float)))
    std = float(excess.std(ddof=1))
    curve = np.concatenate([[1.0], np.cumprod(1 + returns.to_numpy(float))])
    drawdown = float(np.max(1 - curve / np.maximum.accumulate(curve)))
    return {"sessions": int(len(returns)), "cagr": round(growth ** (1 / years) - 1, 5) if years > 0 else None,
            "sharpe": round(float(excess.mean()) / std * math.sqrt(TRADING_DAYS), 4) if std > 0 else None,
            "max_drawdown": round(drawdown, 4)}


def slot_returns(trades: pd.DataFrame, frame: pd.DataFrame, cost: np.ndarray, bills: pd.Series) -> pd.Series:
    """Daily return of one slot that holds ``trades`` (one at a time) and earns T-bills when flat.

    A trade enters at ``entry_raw`` on bar ``entry_idx`` and leaves at
    ``exit_raw`` on bar ``exit_idx``; ``entry_at`` / ``exit_at`` say whether
    the fill is the bar's open or close.  The round trip ``cost`` (fraction) is
    charged half on the entry bar and half on the exit bar.
    """
    dates = pd.DatetimeIndex(pd.to_datetime(frame["date"].astype(str).str[:10]))
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(float)
    rf = bills.reindex(dates).fillna(0.0).to_numpy(float)
    factor = np.ones(len(frame))
    busy = np.zeros(len(frame), dtype=bool)
    for k, row in enumerate(trades.itertuples(index=False)):
        e, x = int(row.entry_idx), int(row.exit_idx)
        half = 1 - float(cost[k]) / 2 if np.isfinite(cost[k]) else 1.0
        entry_raw, exit_raw = float(row.entry_raw), float(row.exit_raw)
        if x == e:
            factor[e] *= exit_raw / entry_raw * half * half
            busy[e] = True
            continue
        factor[e] *= (close[e] / entry_raw) * half
        busy[e] = True
        for j in range(e + 1, x):
            factor[j] *= close[j] / close[j - 1]
            busy[j] = True
        factor[x] *= (exit_raw / close[x - 1]) * half
        if getattr(row, "exit_at", "open") == "close":
            busy[x] = True
        elif not busy[x]:
            factor[x] *= 1 + rf[x]      # the rest of the exit session in T-bills (open exits)
            busy[x] = True
    out = np.where(busy, factor - 1, rf)
    return pd.Series(out, index=dates)


def with_raw_prices(trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Gross fill prices from the replay's labels (next-open entry; ``gross_return`` from that open)."""
    if trades is None or trades.empty:
        return trades
    opens = {s: pd.to_numeric(frames[s]["open"], errors="coerce").to_numpy(float) for s in trades["symbol"].unique()}
    entry_raw = np.array([opens[s][int(i)] for s, i in zip(trades["symbol"], trades["entry_idx"])])
    return trades.assign(entry_raw=entry_raw, exit_raw=entry_raw * (1 + trades["gross_return"].to_numpy(float)),
                         entry_at="open", exit_at=np.where(trades["barrier"].isin(["stop_loss", "take_profit"]),
                                                           "close", "open"))


def sleeve_series(trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame], symbols, cost: np.ndarray,
                  bills: pd.Series, *, slots: Optional[int] = None) -> pd.Series:
    """Equal slots, idle in T-bills: one slot per symbol, or ``slots`` shared slots (the stock leg)."""
    trades = trades.reset_index(drop=True)
    if slots is None:
        series = []
        for symbol in symbols:
            mask = (trades["symbol"] == symbol).to_numpy() if len(trades) else np.zeros(0, dtype=bool)
            sub = trades.loc[mask] if len(trades) else trades
            series.append(slot_returns(sub, frames[symbol], cost[mask] if len(trades) else cost, bills))
        return pd.concat(series, axis=1).mean(axis=1, skipna=True).sort_index()
    # shared slots: each accepted trade occupies one slot of 1/slots; the rest earns T-bills
    calendar = bills.index
    held = pd.Series(0.0, index=calendar)
    count = pd.Series(0.0, index=calendar)
    for symbol, group in trades.groupby("symbol"):
        idx = group.index.to_numpy()
        frame = frames[symbol]
        for k in idx:
            one = slot_returns(trades.loc[[k]], frame, cost[[k]], bills * 0.0)
            e, x = int(trades.at[k, "entry_idx"]), int(trades.at[k, "exit_idx"])
            window = one.iloc[e:x + 1]
            window = window[window.index.isin(calendar)]
            held.loc[window.index] += window.to_numpy()
            count.loc[window.index] += 1.0
    count = count.clip(upper=slots)
    return held / slots + (1 - count / slots) * bills


def trades_per_year(trades: pd.DataFrame, start) -> Optional[float]:
    if trades is None or trades.empty:
        return None
    dates = pd.to_datetime(trades["decision_date"])
    years = max((dates.max() - pd.Timestamp(start)).days / 365.25, 1 / 12)
    return round(len(trades) / years, 1)


# ----------------------------------------------------------------- per candidate

def evaluate(name: str, trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame], symbols, *, spy: pd.Series,
             bills: pd.Series, model_cost: np.ndarray, slots: Optional[int] = None, variant: bool = False,
             measured_cost_bps: Optional[float] = None) -> dict:
    from app.services.strategy_scorecard import trade_statistics
    from app.services.yardstick import clustered_t

    trades = trades.reset_index(drop=True)
    dates = pd.to_datetime(trades["decision_date"])
    in_window = (dates >= pd.Timestamp(START)).to_numpy()
    post = (dates >= pd.Timestamp(SPLIT)).to_numpy()
    pre = in_window & ~post
    gross = trades["gross_return"].to_numpy(float)
    entry_dates = trades["entry_date"].astype(str).str[:10].tolist()
    latest = max(pd.Timestamp(str(frames[s]["date"].iloc[-1])[:10]) for s in symbols)
    years = (latest - pd.Timestamp(START)).days / 365.25

    def window_gross(mask) -> dict:
        g = gross[mask]
        months = dates[mask].dt.to_period("M").astype(str).tolist()
        stats = trade_statistics(g, clusters=months) if len(g) else {"trades": 0}
        return {"trades": int(mask.sum()), "gross_bps": round(float(g.mean()) * 1e4, 2) if len(g) else None,
                "gross_ci_bps": stats.get("expectancy_ci_bps"), "win_rate": stats.get("win_rate"),
                "holding_days": round(float(trades.loc[mask, "holding_days"].mean()), 2) if mask.any() else None}

    out = {"name": name, "symbols": len(list(symbols)), "trades_all_history": int(len(trades)),
           "window": window_gross(in_window), "pre_2021": window_gross(pre), "post_2021": window_gross(post),
           "trades_per_year": trades_per_year(trades.loc[in_window], START), "costs": {}, "checks": {}}
    model_mean = float(np.nanmean(model_cost[in_window])) * 1e4 if in_window.any() else None
    out["model_cost_bps"] = None if model_mean is None else round(model_mean, 2)
    post_model = float(np.nanmean(model_cost[post])) * 1e4 if post.any() else None
    for column in COSTS:
        if column == "model":
            cost, cost_bps = model_cost, model_mean
            post_cost_bps = post_model
        else:
            cost = np.full(len(trades), FLAT_BPS[column] / 1e4)
            cost_bps = post_cost_bps = FLAT_BPS[column]
        net = gross - cost
        months = dates[in_window].dt.to_period("M").astype(str).tolist()
        net_stats = trade_statistics(net[in_window], clusters=months) if in_window.any() else {}
        t = clustered_t(net[in_window], [d for d, k in zip(entry_dates, in_window) if k])
        sleeve = sleeve_series(trades, frames, symbols, cost, bills, slots=slots)
        sleeve_2015 = sleeve.loc[sleeve.index >= pd.Timestamp(START)]
        sharpe = series_stats(sleeve_2015, bills)
        marginal = {w: marginal_check(sleeve, spy, bills, equity_weight=LIVE_ARM, slice_=ALLOCATION[column], start=s)
                    for w, s in WINDOWS.items()}
        shown = {w: marginal_check(sleeve, spy, bills, equity_weight=SHOWN_ARM, slice_=ALLOCATION[column], start=s)
                 for w, s in WINDOWS.items()}
        out["costs"][column] = {
            "cost_bps": None if cost_bps is None else round(float(cost_bps), 2),
            "net_bps": net_stats.get("expectancy_bps"), "net_ci_bps": net_stats.get("expectancy_ci_bps"),
            "net_t_date_clustered": t["t_stat"],
            "t_implied_sharpe": None if t["t_stat"] is None or years <= 0 else round(t["t_stat"] / math.sqrt(years), 3),
            "sleeve": sharpe, "marginal_live_arm": marginal, "marginal_50_50_arm": shown,
            "post_2021_net_bps": round(float(net[post].mean()) * 1e4, 2) if post.any() else None,
            "pre_2021_net_bps": round(float(net[pre].mean()) * 1e4, 2) if pre.any() else None,
        }
        out["checks"][column] = cost_checks(out["window"]["gross_bps"], cost_bps, sharpe["sharpe"], marginal,
                                            post_gross_bps=out["post_2021"]["gross_bps"] if variant else None,
                                            variant=variant)
        if variant:
            out["checks"][column]["post_required_gross_bps"] = round(required_gross_bps(post_cost_bps or 0.0), 2)
            out["checks"][column]["post_2021_gross_ok"] = bool(
                out["post_2021"]["gross_bps"] is not None
                and out["post_2021"]["gross_bps"] >= required_gross_bps(post_cost_bps or 0.0))
            out["checks"][column]["passes"] = bool(out["checks"][column]["passes"]
                                                  and out["checks"][column]["post_2021_gross_ok"])
    out["outcome"] = outcome(out["checks"], measured_cost_bps=measured_cost_bps, variant=variant,
                             stock_leg=name == "rsi2_stock_pit")
    return out


# ----------------------------------------------------------------- replays

def etf_trades(strategy: str, frames, spy_frame, setup) -> pd.DataFrame:
    from app.services import gate_replay

    trades = gate_replay.simulate(strategy, {s: frames[s] for s in INDEX}, spy_frame, setup, calibration=None)
    return with_raw_prices(trades, frames)


def liquid_bars(frame: pd.DataFrame) -> np.ndarray:
    """Decision bars that pass the data-hygiene filter (``MIN_MEDIAN_DOLLAR_VOLUME``): traded today and tomorrow."""
    if "volume" not in frame:
        return np.ones(len(frame), dtype=bool)
    volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
    dollars = volume * pd.to_numeric(frame["close"], errors="coerce").fillna(0.0)
    median = dollars.rolling(VOLUME_WINDOW, min_periods=VOLUME_WINDOW).median().to_numpy(float)
    traded = volume.to_numpy(float) > 0
    tomorrow = np.r_[traded[1:], False]
    return traded & tomorrow & (np.nan_to_num(median, nan=0.0) >= MIN_MEDIAN_DOLLAR_VOLUME)


def stock_trades(frames, spy_frame, setup, mask, *, hygiene: bool = True) -> pd.DataFrame:
    """RSI(2) on every point-in-time single stock (``gate_replay.simulate`` without its fixed scope)."""
    from app.services import gate_replay
    from app.services import meta_label_primary as primary
    from app.services.strategy_discovery import _executable
    from app.services.triple_barrier import label_events

    setup = gate_replay.setup_from_parameters(setup)
    barrier = gate_replay.barrier_for("rsi2_pullback", setup)
    out = []
    for symbol, frame in frames.items():
        replay = primary.replay_signals(frame, ("rsi2_pullback",))
        buy = (gate_replay.entries("rsi2_pullback", frame, replay)
               & gate_replay.spy_gate_on(frame, spy_frame, setup["market_gate_sma"])
               & gate_replay.earnings_gate_on(frame, setup["earnings_blackout_sessions"]))
        buy &= np.asarray(mask(symbol, pd.to_datetime(frame["date"].astype(str).str[:10])), dtype=bool)
        if hygiene:
            buy &= liquid_bars(frame)
        positions = np.flatnonzero(buy)
        if not len(positions):
            continue
        labels = label_events(frame, positions, barrier,
                              exit_signals=primary.replay_exit_signals(frame, "rsi2_pullback"))
        if labels.empty:
            continue
        out.append(_executable(labels).assign(symbol=symbol, strategy="rsi2_pullback"))
    trades = pd.concat(out, ignore_index=True) if out else pd.DataFrame()
    return with_raw_prices(trades, frames)


def shared_slot_accept(trades: pd.DataFrame, slots: int) -> pd.DataFrame:
    """Live candidate order (entry date, then symbol): a trade is taken when a slot is free on its entry bar."""
    if trades.empty:
        return trades
    rows = trades.assign(entry_ts=pd.to_datetime(trades["entry_date"]), exit_ts=pd.to_datetime(trades["exit_date"]))
    rows = rows.sort_values(["entry_ts", "symbol"], kind="stable")
    open_exits: list = []
    keep = []
    for row in rows.itertuples():
        open_exits = [x for x in open_exits if x > row.entry_ts]   # an open exit frees the slot that morning
        if len(open_exits) < slots:
            open_exits.append(row.exit_ts)
            keep.append(row.Index)
    return trades.loc[sorted(keep)]


def variant_trades(frames) -> pd.DataFrame:
    """Close-to-open after a volatility-scaled down day on the four ETFs (decided and filled on the close)."""
    out = []
    for symbol in INDEX:
        frame = frames[symbol]
        close = pd.to_numeric(frame["close"], errors="coerce").astype(float)
        opens = pd.to_numeric(frame["open"], errors="coerce").to_numpy(float)
        ret = close.pct_change()
        sigma = ret.rolling(VARIANT_VOL_WINDOW).std().shift(1)
        z = (ret / sigma).to_numpy(float)
        dates = frame["date"].astype(str).str[:10].to_numpy()
        for t in np.flatnonzero(np.isfinite(z) & (z <= VARIANT_Z)):
            if t + 1 >= len(frame) or not np.isfinite(opens[t + 1]):
                continue
            c = float(close.iloc[t])
            out.append({"symbol": symbol, "t0": t, "entry_idx": t, "exit_idx": t + 1, "decision_date": dates[t],
                        "entry_date": dates[t], "exit_date": dates[t + 1], "holding_days": 1, "barrier": "open",
                        "entry_raw": c, "exit_raw": opens[t + 1], "gross_return": opens[t + 1] / c - 1,
                        "entry_at": "close", "exit_at": "open", "z": round(float(z[t]), 3)})
    return pd.DataFrame(out)


def variant_model_cost(trades: pd.DataFrame, frames) -> np.ndarray:
    """Published model, one side at the close (decision bar) and one at the next open."""
    from app.services.triple_barrier import BarrierConfig, prior_liquidity
    from app.services.yardstick import _side_costs

    config = BarrierConfig()
    out = np.full(len(trades), np.nan)
    for symbol, group in trades.groupby("symbol"):
        frame = frames[symbol].reset_index(drop=True)
        sigma, adv = prior_liquidity(frame, config.volatility_lookback)
        sigma, adv = np.asarray(sigma, float), np.asarray(adv, float)
        i = group["entry_idx"].to_numpy(int)
        j = group["exit_idx"].to_numpy(int)
        quantity = config.position_notional / group["entry_raw"].to_numpy(float)
        cost = (_side_costs(sigma[i], adv[i], quantity, config) + _side_costs(sigma[j], adv[j], quantity, config)
                + 2.0 * float(config.fees_bps)) / 1e4
        out[group.index.to_numpy()] = cost
    return out


def run(args) -> dict:
    settings.meta_label_history_dir = str(args.data_dir)
    from app.services import point_in_time_universe as pit
    from app.services import strategy_discovery as discovery
    from app.services.earnings_history import load_surprises
    from app.services.meta_label_learning import history_dir
    from app.services.seed import MARKET_GATED_STRATEGIES, RSI2_WIDENED_SCOPE
    from app.services.strategy_scorecard import load_daily_history
    from app.services.trading_universe import ETF_SYMBOLS
    from app.services.yardstick import trade_costs

    warnings.simplefilter("ignore", RuntimeWarning)
    started = time.time()
    now = datetime.now(timezone.utc)
    membership = pit.load_membership(download=False, now=now)

    def loader(ticker: str) -> pd.DataFrame:
        return load_daily_history(pit.yahoo_symbol(ticker), history_dir(), download=False)

    def earnings(symbol: str):
        if not args.earnings_dir:
            return None
        try:
            return load_surprises(symbol, Path(args.earnings_dir), download=False)
        except Exception:
            return None

    coverage = pit.coverage(membership, loader=loader, now=now, start="2000-01-01", cache_path=pit.coverage_path())
    base = sorted(set(RSI2_WIDENED_SCOPE))
    symbols = pit.replay_symbols(base, coverage)
    frames, missing = discovery.load_frames(symbols + ["BIL"], loader=loader, start="2000-01-01", end=args.end,
                                            sector_close=False, earnings_loader=earnings, news_loader=lambda s: None)
    load_seconds = time.time() - started
    spy_frame = frames["SPY"]
    spy = pd.Series(pd.to_numeric(spy_frame["close"]).to_numpy(float),
                    index=pd.DatetimeIndex(pd.to_datetime(spy_frame["date"].astype(str).str[:10]))).pct_change()
    bil_frame = frames.pop("BIL")
    bills = pd.Series(pd.to_numeric(bil_frame["close"]).to_numpy(float),
                      index=pd.DatetimeIndex(pd.to_datetime(bil_frame["date"].astype(str).str[:10]))).pct_change()
    calendar = spy.index[spy.index >= bills.dropna().index.min()]
    spy, bills = spy.reindex(calendar).fillna(0.0), bills.reindex(calendar).fillna(0.0)
    earnings_on = args.earnings_dir is not None

    results: dict = {}
    # 1. IBS and RSI(2) on the four ETFs -------------------------------------------
    setups = {"ibs_etf": ("ibs_mean_reversion", {"market_gate_sma": 0}),
              "rsi2_etf": ("rsi2_pullback", {"market_gate_sma": MARKET_GATED_STRATEGIES["rsi2_pullback"]})}
    for name, (strategy, setup) in setups.items():
        trades = etf_trades(strategy, frames, spy_frame, setup)
        model = trade_costs(trades, frames)["model"]
        results[name] = evaluate(name, trades, frames, INDEX, spy=spy, bills=bills, model_cost=model,
                                 measured_cost_bps=args.measured_cost_bps)
        results[name]["setup"] = setup
    etf_seconds = time.time() - started - load_seconds

    # 2. RSI(2)'s stock leg, once, point in time ----------------------------------------
    singles = {s: f for s, f in frames.items() if s not in ETF_SYMBOLS}
    stock_setup = {"market_gate_sma": MARKET_GATED_STRATEGIES["rsi2_pullback"],
                   "earnings_blackout_sessions": settings.paper_earnings_blackout_sessions if earnings_on else 0}
    all_stock = stock_trades(singles, spy_frame, stock_setup, membership.mask)
    accepted = shared_slot_accept(all_stock, STOCK_SLOTS).reset_index(drop=True)
    model_all = trade_costs(all_stock, frames)["model"]
    model_accepted = trade_costs(accepted, frames)["model"]
    stock = evaluate("rsi2_stock_pit", accepted, frames, sorted(accepted["symbol"].unique()), spy=spy, bills=bills,
                     model_cost=model_accepted, slots=STOCK_SLOTS, measured_cost_bps=args.measured_cost_bps)
    # check (i) reads every executable trade of the rule, not only the five-slot sleeve's
    every = all_stock.reset_index(drop=True)
    every_dates = pd.to_datetime(every["decision_date"])
    every_window = (every_dates >= pd.Timestamp(START)).to_numpy()
    every_gross = float(every.loc[every_window, "gross_return"].mean()) * 1e4
    every_model = float(np.nanmean(model_all[every_window])) * 1e4
    stock["every_trade"] = {"trades": int(every_window.sum()), "gross_bps": round(every_gross, 2),
                            "model_cost_bps": round(every_model, 2), "symbols": int(every["symbol"].nunique()),
                            "net_model_bps": round(every_gross - every_model, 2)}
    for column in COSTS:
        cost_bps = every_model if column == "model" else FLAT_BPS[column]
        checks = stock["checks"][column]
        checks["sleeve_gross_ok"] = checks["gross_ok"]
        checks["gross_ok"] = bool(every_gross >= required_gross_bps(cost_bps))
        checks["required_gross_bps"] = round(required_gross_bps(cost_bps), 2)
        checks["passes"] = bool(checks["gross_ok"] and checks["sharpe_ok"] and checks["marginal_ok"])
    stock["outcome"] = outcome(stock["checks"], measured_cost_bps=args.measured_cost_bps, stock_leg=True)
    stock["setup"] = stock_setup
    raw = stock_trades(singles, spy_frame, stock_setup, membership.mask, hygiene=False)
    raw_window = (pd.to_datetime(raw["decision_date"]) >= pd.Timestamp(START)).to_numpy()
    stock["hygiene"] = {"rule": f"decision and entry bars traded, {VOLUME_WINDOW}-session median dollar volume >= "
                                f"${MIN_MEDIAN_DOLLAR_VOLUME:,}",
                        "trades_without_filter": int(len(raw)), "trades_with_filter": int(len(all_stock)),
                        "trades_2015_without_filter": int(raw_window.sum()),
                        "gross_2015_without_filter_bps": round(float(raw.loc[raw_window, "gross_return"].mean()) * 1e4, 2),
                        "symbols_removed_entirely": sorted(set(raw["symbol"]) - set(all_stock["symbol"]))}
    stock["universe"] = {"single_stocks_loaded": len(singles), "coverage_since_2010": coverage.get("coverage_since_2010"),
                         "missing_history": len(missing)}
    results["rsi2_stock_pit"] = stock
    stock_seconds = time.time() - started - load_seconds - etf_seconds

    # 3. the close-to-open variant --------------------------------------------------------
    variant = variant_trades(frames)
    results["close_open_variant"] = evaluate("close_open_variant", variant, frames, INDEX, spy=spy, bills=bills,
                                             model_cost=variant_model_cost(variant, frames), variant=True,
                                             measured_cost_bps=args.measured_cost_bps)
    results["close_open_variant"]["setup"] = {"z": VARIANT_Z, "vol_window": VARIANT_VOL_WINDOW,
                                              "decision": "official close (stands in for 15:45 ET)",
                                              "entry": "official close", "exit": "next official open"}
    end = max(pd.Timestamp(str(f["date"].iloc[-1])[:10]) for f in frames.values())
    return {"ran_at": now.isoformat(), "paper_only": True, "in_sample": True, "rule": RULE, "start": START,
            "end": str(end.date()), "windows": WINDOWS, "split": SPLIT, "allocation": ALLOCATION,
            "live_arm_equity_weight": LIVE_ARM, "shown_arm_equity_weight": SHOWN_ARM,
            "measured_cost_bps": args.measured_cost_bps, "earnings_blackout": earnings_on,
            "results": results,
            "timings": {"load_seconds": round(load_seconds, 1), "etf_seconds": round(etf_seconds, 1),
                        "stock_seconds": round(stock_seconds, 1), "total_seconds": round(time.time() - started, 1)}}


# ----------------------------------------------------------------- report tables

def _b(value, digits=1):
    return "n/a" if value is None else f"{float(value):+.{digits}f}"


def _p(value, digits=1):
    return "n/a" if value is None else f"{100 * float(value):.{digits}f}%"


def _yes(flag):
    return "pass" if flag else "fail"


def render_tables(result: dict) -> str:
    lines = ["| Candidate | Cost | Cost bps | Gross bps (need) | (i) | Net bps [95% CI] | Sleeve Sharpe x0.5 "
             "(t-implied) | (ii) | 20/80 arm + slice: Sharpe, DD 2008-07+ / 2015+ | (iii) | Passes |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in result["results"].items():
        gross = (r.get("every_trade") or {}).get("gross_bps", r["window"]["gross_bps"])
        for column in COSTS:
            c, k = r["costs"][column], r["checks"][column]
            m = c["marginal_live_arm"]
            cells = " / ".join(f"{m[w]['arm']['sharpe']:.3f}->{m[w]['with_slice']['sharpe']:.3f}, "
                               f"{_p(m[w]['arm']['max_drawdown'])}->{_p(m[w]['with_slice']['max_drawdown'])}"
                               for w in WINDOWS)
            ci = c["net_ci_bps"]
            lines.append(
                f"| {name} | {column} | {c['cost_bps']:.1f} | {_b(gross)} ({k['required_gross_bps']:.0f}) | "
                f"{_yes(k['gross_ok'])} | {_b(c['net_bps'])}" + (f" [{ci[0]:+.0f}, {ci[1]:+.0f}]" if ci else "")
                + f" | {k['haircut_sharpe'] if k['haircut_sharpe'] is not None else 'n/a'} "
                f"({c['t_implied_sharpe']}) | {_yes(k['sharpe_ok'])} | {cells} ({int(ALLOCATION[column] * 100)}%) | "
                f"{_yes(k['marginal_ok'])} | " + ("**yes**" if k["passes"] else "no")
                + (f" (post-2021 gross {_yes(k.get('post_2021_gross_ok'))})" if "post_2021_gross_ok" in k else "")
                + " |")
    lines += ["", "| Candidate | Outcome | Passes at 10 / 25 / model |", "|---|---|---|"]
    for name, r in result["results"].items():
        o = r["outcome"]
        lines.append(f"| {name} | **{o['decision']}** | " + " / ".join(_yes(o['passes_at'][c]) for c in COSTS) + " |")
    lines += ["", "| Candidate | Trades 2015+ | /yr | Hold d | Win | Gross 2015+ [CI] | Gross 2015-20 | Gross 2021+ | "
              "Model cost | Net at model 2015-20 / 2021+ |", "|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in result["results"].items():
        w, pre, post = r["window"], r["pre_2021"], r["post_2021"]
        ci = w["gross_ci_bps"]
        m = r["costs"]["model"]
        lines.append(f"| {name} | {w['trades']} | {r['trades_per_year']} | {w['holding_days']} | {_p(w['win_rate'])} | "
                     f"{_b(w['gross_bps'])}" + (f" [{ci[0]:+.0f}, {ci[1]:+.0f}]" if ci else "")
                     + f" | {_b(pre['gross_bps'])} ({pre['trades']}) | {_b(post['gross_bps'])} ({post['trades']}) | "
                     f"{_b(r['model_cost_bps'])} | {_b(m['pre_2021_net_bps'])} / {_b(m['post_2021_net_bps'])} |")
    lines += ["", "| Candidate | Cost | Sleeve CAGR / Sharpe / DD 2015+ | 50/50 arm + slice: Sharpe, DD 2008-07+ / "
              "2015+ |", "|---|---|---|---|"]
    for name, r in result["results"].items():
        for column in COSTS:
            c = r["costs"][column]
            s = c["sleeve"]
            m = c["marginal_50_50_arm"]
            cells = " / ".join(f"{m[w]['arm']['sharpe']:.3f}->{m[w]['with_slice']['sharpe']:.3f}, "
                               f"{_p(m[w]['arm']['max_drawdown'])}->{_p(m[w]['with_slice']['max_drawdown'])}"
                               + (" ok" if m[w]["passes"] else " x") for w in WINDOWS)
            lines.append(f"| {name} | {column} | {_p(s['cagr'], 2)} / {s['sharpe']} / {_p(s['max_drawdown'])} | "
                         f"{cells} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--earnings-dir", type=Path, default=None)
    parser.add_argument("--measured-cost-bps", type=float, default=None,
                        help="the bot's measured ETF round trip from the step 1 cost table (absent locally)")
    parser.add_argument("--end", default=None, help="exclusive; default today")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tables", type=Path, default=None)
    args = parser.parse_args()
    result = run(args)
    args.output.write_text(json.dumps(result, indent=2, default=str))
    if args.tables:
        args.tables.write_text(render_tables(result))
    print(json.dumps({name: r["outcome"] for name, r in result["results"].items()} | {"timings": result["timings"]},
                     indent=2, default=str))


if __name__ == "__main__":
    main()
