"""Local full-history walk-forward sweep of every rule family, gate and hand-written strategy.

Written for ``reports/local-sweep-20261110.md``.  Runs offline on cached
yfinance bars (never against the deployed host) and re-uses the app's own
engine:

* ``strategies.rules`` for the discovery library's rule language and
  ``strategy_discovery.candidate_library`` for the library itself;
* ``triple_barrier.label_events`` / ``label_open_session`` for execution
  (decision on the close, fill at the next open, stop first inside a bar,
  gaps fill at the open, indicator exit at the next open), with a *flat*
  per-side cost so the sweep is net of 11 and 25 bps a side;
* the hand-written strategies (RSI(2), IBS, RSI(14), Bollinger, channel
  breakout, trend pullback, MA crossover, MACD, ensemble) re-expressed as
  vectorised entry signals with the exit policy each one is seeded with;
* the gates: ADX regime (``strategies.regime``), SPY-trend market gate
  (``market_gate.above_sma``) and the VIX level gate (``strategies.vix_gate``
  semantics), applied as entry masks.

Usage (from ``artifacts/api-server/backend``)::

    LOCAL_SWEEP_DIR=<scratch> .venv/bin/python scripts/local_sweep.py --workers 8

``LOCAL_SWEEP_DIR`` must hold ``bars/<SYMBOL>.csv`` (``strategy_scorecard
.load_daily_history`` cache), ``bars/earnings/<SYMBOL>.csv``, ``bars/VIX.csv``
and ``fetched/{fomc_dates.csv,cpi_dates.txt,jobs_dates.txt}``.  Results are
written under ``<LOCAL_SWEEP_DIR>/out``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS  # noqa: E402
from app.services import earnings_history  # noqa: E402
from app.services.backtest_statistics import newey_west_t  # noqa: E402
from app.services.market_gate import above_sma  # noqa: E402
from app.services.sector_relative import attach_sector_closes  # noqa: E402
from app.services.strategies import rules  # noqa: E402
from app.services.strategies.base import rsi as simple_rsi  # noqa: E402
from app.services.strategies.regime import REGIME_GATES, wilder_adx  # noqa: E402
from app.services.strategies.short_term_reversion import internal_bar_strength, wilder_rsi  # noqa: E402
from app.services.strategy_discovery import (  # noqa: E402
    NEWS_FAMILIES, TRADE_COLUMNS, _executable, _marked_returns, candidate_library,
)
from app.services.strategy_scorecard import load_daily_history  # noqa: E402
from app.services.trading_universe import ETF_SYMBOLS  # noqa: E402
from app.services.triple_barrier import BarrierConfig, label_events, label_open_session  # noqa: E402

ROOT = Path(os.environ.get("LOCAL_SWEEP_DIR") or (BACKEND / "output" / "local_sweep"))
BARS = ROOT / "bars"
FETCHED = ROOT / "fetched"
OUT = ROOT / "out"
HISTORY_START = "2000-01-01"
OOS_START = "2015-01-01"
COSTS_BPS = (11.0, 25.0)
GATES = ("none", "adx_range", "adx_not_trending", "adx_not_downtrend", "adx_trend_up",
         "spy_sma10", "spy_sma20", "spy_sma50", "vix_below_20", "vix_above_20")
BRACKET = {"stop_loss_pct": 0.03, "take_profit_pct": 0.06, "max_holding_sessions": 20}
REVERSION = {"stop_loss_pct": 0.10, "take_profit_pct": 0.0, "max_holding_sessions": 10}
HAND_WRITTEN = ("rsi2_pullback", "ibs_mean_reversion", "rsi_mean_reversion", "bollinger_mean_reversion",
                "channel_breakout", "trend_pullback", "moving_average_crossover", "macd_momentum", "ensemble")
MIN_FIT_TRADES_ANCHORED = 30
MIN_FIT_TRADES_6M = 10

# Process-wide state (inherited by forked workers).
FRAMES: dict[str, pd.DataFrame] = {}
GATE_MASKS: dict[str, dict[str, np.ndarray]] = {}
EVENT_MASKS: dict[str, dict[str, np.ndarray]] = {}
CALENDAR: pd.DatetimeIndex = pd.DatetimeIndex([])
WINDOWS: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []
CACHES: dict[str, dict] = {}


# ------------------------------------------------------------------ data

def load_frames(symbols) -> tuple[dict, dict]:
    frames, notes = {}, {}
    for symbol in symbols:
        frame = load_daily_history(symbol, BARS, download=False)
        frame = frame[frame["date"] >= HISTORY_START].reset_index(drop=True)
        dates = pd.to_datetime(frame["date"])
        gaps = dates.diff().dt.days
        notes[symbol] = {"first": frame["date"].iloc[0], "last": frame["date"].iloc[-1], "bars": int(len(frame)),
                         "largest_gap_days": int(gaps.max()) if len(gaps) else 0,
                         "gap_over_7d": int((gaps > 7).sum())}
        if symbol not in ETF_SYMBOLS:
            events = earnings_history.load_surprises(symbol, BARS, download=False)
            frame = earnings_history.attach_surprises(frame, events)
            notes[symbol]["earnings_reports"] = int(len(events))
            notes[symbol]["earnings_first"] = str(events["reported_at"].min())[:10] if len(events) else None
        frames[symbol] = frame
    frames, _method = attach_sector_closes(frames)
    return frames, notes


def half_year_windows(start: str, end: str) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    out = []
    cursor = pd.Timestamp(start)
    last = pd.Timestamp(end)
    while cursor <= last:
        nxt = cursor + pd.DateOffset(months=6)
        label = f"{cursor.year}H{1 if cursor.month == 1 else 2}"
        out.append((label, cursor, min(nxt, last + pd.Timedelta(days=1))))
        cursor = nxt
    return out


def build_gate_masks(frames: dict) -> dict[str, dict[str, np.ndarray]]:
    spy = frames["SPY"]
    spy_dates = pd.to_datetime(spy["date"])
    spy_above = {n: pd.Series(above_sma(spy["close"], n), index=spy_dates) for n in (10, 20, 50)}
    vix = pd.read_csv(BARS / "VIX.csv")
    vix_close = pd.Series(pd.to_numeric(vix["close"], errors="coerce").to_numpy(), index=pd.to_datetime(vix["date"]))
    masks: dict[str, dict[str, np.ndarray]] = {gate: {} for gate in GATES}
    for symbol, frame in frames.items():
        dates = pd.to_datetime(frame["date"])
        n = len(frame)
        adx = wilder_adx(frame, 14)
        regime = np.full(n, "neutral", dtype=object)
        a, p, m = adx["adx"].to_numpy(), adx["plus_di"].to_numpy(), adx["minus_di"].to_numpy()
        ok = np.isfinite(a) & np.isfinite(p) & np.isfinite(m)
        regime[ok & (a >= 25) & (p >= m)] = "trend_up"
        regime[ok & (a >= 25) & (p < m)] = "trend_down"
        regime[ok & (a < 20)] = "range"
        masks["none"][symbol] = np.ones(n, dtype=bool)
        for gate, allowed in REGIME_GATES.items():
            masks[f"adx_{gate}"][symbol] = np.isin(regime, list(allowed))
        for window in (10, 20, 50):
            masks[f"spy_sma{window}"][symbol] = spy_above[window].reindex(dates).fillna(False).to_numpy(dtype=bool)
        level = vix_close.reindex(dates).ffill(limit=5).to_numpy()
        known = np.isfinite(level)
        masks["vix_below_20"][symbol] = ~known | (level < 20)   # fails open, as the paper gate does
        masks["vix_above_20"][symbol] = ~known | (level >= 20)
    return masks


def build_event_masks(frames: dict) -> dict[str, dict[str, np.ndarray]]:
    fomc = set(pd.read_csv(FETCHED / "fomc_dates.csv")["date"].astype(str))
    cpi = {line.strip() for line in (FETCHED / "cpi_dates.txt").read_text().splitlines() if line.strip()}
    jobs = {line.strip() for line in (FETCHED / "jobs_dates.txt").read_text().splitlines() if line.strip()}
    masks: dict[str, dict[str, np.ndarray]] = {k: {} for k in ("earnings", "fomc", "cpi", "jobs")}
    for symbol, frame in frames.items():
        dates = frame["date"].astype(str)
        masks["fomc"][symbol] = dates.isin(fomc).to_numpy()
        masks["cpi"][symbol] = dates.isin(cpi).to_numpy()
        masks["jobs"][symbol] = dates.isin(jobs).to_numpy()
        flag = np.zeros(len(frame), dtype=bool)
        if symbol not in ETF_SYMBOLS:
            events = earnings_history.load_surprises(symbol, BARS, download=False)
            positions = earnings_history.reaction_positions(frame["date"], events)
            flag[positions[positions >= 0]] = True
        masks["earnings"][symbol] = flag
    return masks


# ------------------------------------------------------------------ candidates

def hand_written_signals(name: str, frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray | None, dict]:
    """Entry (and indicator exit) decisions of a hand-written strategy on every bar, plus its exit plan.

    Mirrors ``generate_signal`` of each class, including the backtester's
    ``confidence >= 0.55`` entry gate.
    """
    close = pd.to_numeric(frame["close"], errors="coerce").astype(float).reset_index(drop=True)
    n = len(close)
    if name == "rsi2_pullback":
        entry = (close > close.rolling(200).mean()) & (wilder_rsi(close, 2) < 10)
        exit_ = close > close.rolling(5).mean()
        return entry.fillna(False).to_numpy(), exit_.fillna(False).to_numpy(), dict(REVERSION)
    if name == "ibs_mean_reversion":
        ibs = internal_bar_strength(frame.reset_index(drop=True))
        return (ibs < 0.1).fillna(False).to_numpy(), (ibs > 0.8).fillna(False).to_numpy(), dict(REVERSION)
    rsi14 = simple_rsi(close, 14)
    if name == "rsi_mean_reversion":
        return (rsi14 <= 30).fillna(False).to_numpy(), None, dict(BRACKET)
    if name == "bollinger_mean_reversion":
        middle, std = close.rolling(20).mean(), close.rolling(20).std()
        lower = middle - 2 * std
        entry = (std > 0) & (close <= lower) & (rsi14 <= 35)
        return entry.fillna(False).to_numpy(), None, dict(BRACKET)
    if name == "channel_breakout":
        volume = pd.to_numeric(frame["volume"], errors="coerce").astype(float).reset_index(drop=True)
        prior_high = close.rolling(55).max().shift(1)
        avg_volume = volume.rolling(20).mean()
        entry = (close > prior_high) & (avg_volume > 0) & (volume / avg_volume >= 1.05)
        return entry.fillna(False).to_numpy(), None, dict(BRACKET)
    if name == "trend_pullback":
        fast, slow = close.ewm(span=20, adjust=False).mean(), close.ewm(span=100, adjust=False).mean()
        trend = fast / slow - 1
        pullback = close / fast - 1
        confidence = 0.54 + trend * 4 + np.maximum(-pullback, 0) * 3
        entry = (trend > 0) & (pullback >= -0.035) & (pullback <= 0.005) & (rsi14 >= 38) & (rsi14 <= 58) \
            & (confidence >= 0.55)
        entry[: 100] = False
        return entry.fillna(False).to_numpy(), None, dict(BRACKET)
    # Members of the ensemble (and the two momentum strategies on their own).
    short_ma, long_ma = close.rolling(20).mean(), close.rolling(50).mean()
    distance = (short_ma - long_ma) / close
    ma_conf = (0.5 + distance.abs() * 6).clip(0, 1)
    ma_prob = (0.5 + distance * 5).clip(0, 1)
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    scale = hist / close.clip(lower=1)
    macd_conf = (0.5 + scale.abs() * 20).clip(0, 1)
    macd_prob = (0.5 + scale * 15).clip(0, 1)
    rsi_conf = pd.Series(np.where(rsi14 <= 30, (30 - rsi14) / 30 + 0.55, np.where(rsi14 >= 70, (rsi14 - 70) / 30 + 0.55, 0.5)),
                         index=close.index).clip(0, 1)
    rsi_prob = ((50 - rsi14) / 100 + 0.5).clip(0, 1)
    if name == "moving_average_crossover":
        entry = (short_ma > long_ma) & (ma_conf >= 0.55)
        return entry.fillna(False).to_numpy(), None, dict(BRACKET)
    if name == "macd_momentum":
        entry = (hist > 0) & (macd_conf >= 0.55)
        entry[: 35] = False
        return entry.fillna(False).to_numpy(), None, dict(BRACKET)
    if name == "ensemble":
        conf_sum = ma_conf + rsi_conf + macd_conf
        prob = ((ma_prob * ma_conf + rsi_prob * rsi_conf + macd_prob * macd_conf) / conf_sum.where(conf_sum > 0, 1)).clip(0, 1)
        entry = (prob >= 0.57) & (conf_sum / 3 >= 0.55)
        entry[: 50] = False
        return entry.fillna(False).to_numpy(), None, dict(BRACKET)
    raise ValueError(name)


def build_candidates(symbols: list[str]) -> list[dict]:
    out = []
    for candidate in candidate_library(symbols):
        if candidate["family"] in NEWS_FAMILIES:
            continue  # no news-tone history offline: the rule can never fire
        open_timed = rules.timing(candidate["rule"]) == "open"
        for gate in (("none",) if open_timed else GATES):
            out.append({"id": f"{candidate['id']}:{gate}", "base_id": candidate["id"], "family": candidate["family"],
                        "universe": candidate["universe"], "symbols": list(candidate["symbols"]),
                        "rule": candidate["rule"], "hand": None, "gate": gate,
                        "description": rules.describe_rule(candidate["rule"]),
                        "stop_loss_pct": candidate["stop_loss_pct"], "take_profit_pct": candidate["take_profit_pct"],
                        "max_holding_sessions": candidate["max_holding_sessions"], "open_timed": open_timed})
    for name in HAND_WRITTEN:
        plan = REVERSION if name in ("rsi2_pullback", "ibs_mean_reversion") else BRACKET
        for gate in GATES:
            out.append({"id": f"{name}:{gate}", "base_id": name, "family": f"hand:{name}", "universe": "paper_45",
                        "symbols": list(symbols), "rule": None, "hand": name, "gate": gate,
                        "description": f"hand-written {name} (seeded parameters)", **plan, "open_timed": False})
    return out


# ------------------------------------------------------------------ evaluation

def _cost_config(candidate: dict, cost_bps: float) -> BarrierConfig:
    return BarrierConfig(take_profit_pct=float(candidate["take_profit_pct"]),
                         stop_loss_pct=float(candidate["stop_loss_pct"]),
                         max_holding_days=int(candidate["max_holding_sessions"]),
                         cost_model="flat", slippage_bps=float(cost_bps), fees_bps=0.0)


def _signals(candidate: dict, symbol: str, frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray | None]:
    if candidate["hand"]:
        entry, exit_, _plan = hand_written_signals(candidate["hand"], frame)
        return entry, exit_
    cache = CACHES.setdefault(symbol, {})
    entry = np.asarray(rules.entry_signals(frame, candidate["rule"], cache), dtype=bool)
    exit_ = None if candidate["open_timed"] else np.asarray(rules.exit_signals(frame, candidate["rule"], cache), dtype=bool)
    return entry, exit_


def _window_stats(net: np.ndarray, equity_returns: np.ndarray | None) -> dict:
    n = int(len(net))
    if n == 0:
        return {"trades": 0, "net_sum": 0.0, "mean_bps": None, "pf": None, "win_rate": None, "max_dd": None}
    wins, losses = net[net > 0], net[net <= 0]
    pf = float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else (math.inf if len(wins) else None)
    max_dd = None
    if equity_returns is not None and len(equity_returns):
        curve = np.cumprod(1 + equity_returns)
        max_dd = float((1 - curve / np.maximum.accumulate(curve)).max())
    return {"trades": n, "net_sum": float(net.sum()), "mean_bps": float(net.mean() * 1e4),
            "pf": None if pf is None or not math.isfinite(pf) else round(pf, 3),
            "win_rate": float((net > 0).mean()), "max_dd": max_dd}


def evaluate(candidate: dict, cost_bps: float) -> dict:
    """Trades, per-window / per-symbol statistics and OOS daily returns for one candidate at one cost."""
    config = _cost_config(candidate, cost_bps)
    gate = GATE_MASKS[candidate["gate"]]
    horizon = int(candidate["max_holding_sessions"])
    trades, daily = [], {}
    for symbol in candidate["symbols"]:
        frame = FRAMES.get(symbol)
        if frame is None:
            continue
        entry, exit_ = _signals(candidate, symbol, frame)
        mask = gate[symbol]
        if candidate["open_timed"]:
            # The gate is read at the prior close for an open-timed rule.
            mask = np.concatenate([[True], mask[:-1]])
        entries = np.flatnonzero(entry & mask)
        if not len(entries):
            continue
        if candidate["open_timed"]:
            labels = label_open_session(frame, entries, float(candidate["stop_loss_pct"]), config)
        else:
            labels = label_events(frame, entries, config, exit_signals=exit_)
        if labels.empty:
            continue
        labels = _executable(labels)
        close = pd.to_numeric(frame["close"], errors="coerce").astype(float).to_numpy()
        dates = pd.to_datetime(frame["date"].astype(str).str[:10]).to_numpy()
        ev = labels["event_idx"].to_numpy(int)
        en = labels["entry_idx"].to_numpy(int)
        ex = labels["exit_idx"].to_numpy(int)
        pct = np.diff(close) / close[:-1]
        drift = np.concatenate([[np.nan], np.cumsum(pct) / np.arange(1, len(close))])  # mean daily return up to bar t
        causal_drift = np.nan_to_num(drift[ev], nan=0.0)
        next_day = np.where(candidate["open_timed"], close[ev] / close[np.maximum(ev - 1, 0)] - 1,
                            close[np.minimum(en, len(close) - 1)] / close[ev] - 1)
        flags = {}
        for kind, masks in EVENT_MASKS.items():
            flag = masks[symbol]
            cumulative = np.concatenate([[0], np.cumsum(flag)])
            flags[f"{kind}_held"] = (cumulative[ex + 1] - cumulative[en]) > 0           # realised holding window
            ahead = np.minimum(en + horizon, len(flag))
            flags[f"{kind}_ahead"] = (cumulative[ahead] - cumulative[en]) > 0            # known at entry: next H sessions
        labels = labels.assign(symbol=symbol, t0=dates[ev],
                               excess=labels["net_return"] - labels["holding_days"].clip(lower=1) * causal_drift,
                               next_day_return=next_day, usd_1sh=labels["net_return"] * labels["entry_price"],
                               **flags)
        trades.append(labels)
        daily[symbol] = pd.Series(_marked_returns(labels, close), index=dates)
    columns = [*TRADE_COLUMNS, "next_day_return", "usd_1sh",
               *[f"{k}_{s}" for k in EVENT_MASKS for s in ("held", "ahead")]]
    frame = pd.concat(trades, ignore_index=True)[columns] if trades else pd.DataFrame(columns=columns)
    portfolio = (pd.concat(daily.values(), axis=1).mean(axis=1, skipna=True).sort_index()
                 if daily else pd.Series(dtype=float))
    return summarize(candidate, cost_bps, frame, portfolio, daily)


def _slice(frame: pd.DataFrame, start, end) -> pd.DataFrame:
    t0 = frame["t0"]
    return frame[(t0 >= start) & (t0 < end)]


def _series_window(series: pd.Series, start, end) -> np.ndarray:
    if series.empty:
        return np.zeros(0)
    return series[(series.index >= start) & (series.index < end)].to_numpy()


def summarize(candidate: dict, cost_bps: float, trades: pd.DataFrame, portfolio: pd.Series, daily: dict) -> dict:
    oos_start, data_end = pd.Timestamp(OOS_START), CALENDAR[-1] + pd.Timedelta(days=1)
    t0 = pd.to_datetime(trades["t0"]) if len(trades) else pd.Series(dtype="datetime64[ns]")
    trades = trades.assign(t0=t0)
    oos = trades[trades["t0"] >= oos_start]
    net = oos["net_return"].to_numpy(float)
    oos_daily = portfolio[portfolio.index >= oos_start] if not portfolio.empty else portfolio
    oos_aligned = oos_daily.reindex(CALENDAR[CALENDAR >= oos_start]).fillna(0.0).to_numpy(np.float32)
    full = _window_stats(net, oos_aligned)
    nw = newey_west_t(oos_aligned.astype(float), lags=10) if len(oos_aligned) else {"t_stat": None}
    months = oos["t0"].dt.to_period("M").astype(str).to_numpy() if len(oos) else np.array([])
    clustered = _clustered_t(net, months)
    excess = oos["excess"].to_numpy(float)
    excess_t = _clustered_t(excess, months)
    # per window
    windows = {}
    for label, start, end in WINDOWS:
        part = _slice(oos, start, end)
        windows[label] = _window_stats(part["net_return"].to_numpy(float), _series_window(oos_daily, start, end))
    active = [w for w in windows.values() if w["trades"] > 0]
    positive = sum(1 for w in active if w["net_sum"] > 0)
    # anchored (expanding) and trailing-6-month in-sample statistics at each window start
    fit = {}
    for label, start, end in WINDOWS:
        before = trades[trades["t0"] < start]["net_return"].to_numpy(float)
        recent = trades[(trades["t0"] < start) & (trades["t0"] >= start - pd.DateOffset(months=6))]["net_return"].to_numpy(float)
        fit[label] = {"anchored_trades": int(len(before)), "anchored_mean_bps": float(before.mean() * 1e4) if len(before) else None,
                      "m6_trades": int(len(recent)), "m6_mean_bps": float(recent.mean() * 1e4) if len(recent) else None}
    # per symbol (OOS) and per symbol x window
    per_symbol, per_symbol_window = {}, []
    for symbol, group in oos.groupby("symbol"):
        series = daily[symbol]
        s_oos = series[series.index >= oos_start]
        stats = _window_stats(group["net_return"].to_numpy(float), s_oos.to_numpy())
        wins = 0
        act = 0
        for label, start, end in WINDOWS:
            part = _slice(group, start, end)
            w = _window_stats(part["net_return"].to_numpy(float), _series_window(s_oos, start, end))
            if w["trades"]:
                act += 1
                wins += int(w["net_sum"] > 0)
                per_symbol_window.append({"id": candidate["id"], "cost_bps": cost_bps, "symbol": symbol, "window": label, **w})
        stats.update({"windows_active": act, "positive_window_fraction": (wins / act) if act else None,
                      "excess_mean_bps": float(group["excess"].mean() * 1e4)})
        per_symbol[symbol] = stats
    # events (OOS trades)
    events = {}
    for kind in EVENT_MASKS:
        for scope in ("held", "ahead"):
            flag = oos[f"{kind}_{scope}"].to_numpy(bool) if len(oos) else np.zeros(0, bool)
            events[f"{kind}_{scope}"] = {"in": _window_stats(net[flag], None), "out": _window_stats(net[~flag], None)}
    macro_held = (oos[["fomc_held", "cpi_held", "jobs_held"]].any(axis=1).to_numpy() if len(oos) else np.zeros(0, bool))
    macro_ahead = (oos[["fomc_ahead", "cpi_ahead", "jobs_ahead"]].any(axis=1).to_numpy() if len(oos) else np.zeros(0, bool))
    events["macro_held"] = {"in": _window_stats(net[macro_held], None), "out": _window_stats(net[~macro_held], None)}
    events["macro_ahead"] = {"in": _window_stats(net[macro_ahead], None), "out": _window_stats(net[~macro_ahead], None)}
    # causal filters: what the walk-forward record looks like if the bot skipped entries with an event in the next H sessions
    filters = {}
    for name, keep in (("skip_earnings_ahead", ~oos["earnings_ahead"].to_numpy(bool) if len(oos) else np.zeros(0, bool)),
                       ("skip_macro_ahead", ~macro_ahead),
                       ("only_macro_ahead", macro_ahead)):
        sub = oos[keep] if len(oos) else oos
        sub_net = sub["net_return"].to_numpy(float)
        wins_f = act_f = 0
        for label, start, end in WINDOWS:
            part = _slice(sub, start, end)
            if len(part):
                act_f += 1
                wins_f += int(part["net_return"].sum() > 0)
        sub_months = sub["t0"].dt.to_period("M").astype(str).to_numpy() if len(sub) else np.array([])
        filters[name] = {**_window_stats(sub_net, None), "positive_window_fraction": (wins_f / act_f) if act_f else None,
                         "clustered_t": _clustered_t(sub_net, sub_months)}
    recent6 = trades[trades["t0"] >= data_end - pd.DateOffset(months=6)]
    pre = trades[trades["t0"] < oos_start]
    gross = oos["gross_return"].to_numpy(float)
    return {
        "id": candidate["id"], "base_id": candidate["base_id"], "family": candidate["family"], "universe": candidate["universe"],
        "gate": candidate["gate"], "description": candidate["description"], "open_timed": candidate["open_timed"],
        "stop_loss_pct": candidate["stop_loss_pct"], "take_profit_pct": candidate["take_profit_pct"],
        "max_holding_sessions": candidate["max_holding_sessions"], "cost_bps": cost_bps,
        "oos": {**full, "gross_mean_bps": float(gross.mean() * 1e4) if len(gross) else None,
                "excess_mean_bps": float(excess.mean() * 1e4) if len(excess) else None, "excess_t": excess_t,
                "clustered_t": clustered, "nw_t_daily": nw.get("t_stat"),
                "mean_daily_bps": float(oos_aligned.mean() * 1e4) if len(oos_aligned) else None,
                "ann_sharpe": float(oos_aligned.mean() / oos_aligned.std() * math.sqrt(252)) if len(oos_aligned) > 1 and oos_aligned.std() > 0 else None,
                "windows_active": len(active), "positive_windows": positive,
                "positive_window_fraction": (positive / len(active)) if active else None,
                "usd_1sh_mean": float(oos["usd_1sh"].mean()) if len(oos) else None,
                "next_day_mean_bps": float(oos["next_day_return"].mean() * 1e4) if len(oos) else None,
                "mean_hold": float(oos["holding_days"].mean()) if len(oos) else None,
                "symbols_traded": int(oos["symbol"].nunique()) if len(oos) else 0},
        "pre2015": _window_stats(pre["net_return"].to_numpy(float), None),
        "last6m": _window_stats(recent6["net_return"].to_numpy(float), None),
        "windows": windows, "fit": fit, "per_symbol": per_symbol, "events": events, "filters": filters,
        "_per_symbol_window": per_symbol_window, "_oos_daily": oos_aligned,
    }


def _clustered_t(values: np.ndarray, clusters: np.ndarray) -> float | None:
    """t-statistic of the mean with month-clustered standard errors."""
    if len(values) < 10:
        return None
    frame = pd.DataFrame({"v": values, "c": clusters})
    mean = float(values.mean())
    sums = frame.groupby("c")["v"].apply(lambda s: float((s - mean).sum()))
    g = len(sums)
    if g < 2:
        return None
    variance = float((sums ** 2).sum()) * g / (g - 1) / len(values) ** 2
    return mean / math.sqrt(variance) if variance > 0 else None


def _task(args):
    candidate, cost = args
    try:
        return evaluate(candidate, cost)
    except Exception as exc:  # keep the sweep going; report the failure
        return {"id": candidate["id"], "cost_bps": cost, "error": f"{exc.__class__.__name__}: {exc}"}


# ------------------------------------------------------------------ main

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--limit", type=int, default=0, help="evaluate only the first N candidates (smoke test)")
    parser.add_argument("--costs", type=str, default=",".join(str(c) for c in COSTS_BPS))
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    global FRAMES, GATE_MASKS, EVENT_MASKS, CALENDAR, WINDOWS
    started = time.time()
    symbols = list(DEFAULT_PAPER_EXECUTION_SYMBOLS)
    FRAMES, notes = load_frames(symbols)
    CALENDAR = pd.DatetimeIndex(sorted({pd.Timestamp(d) for f in FRAMES.values() for d in f["date"]}))
    WINDOWS = half_year_windows(OOS_START, str(CALENDAR[-1].date()))
    GATE_MASKS = build_gate_masks(FRAMES)
    EVENT_MASKS = build_event_masks(FRAMES)
    candidates = build_candidates(symbols)
    if args.limit:
        candidates = candidates[: args.limit]
    costs = [float(c) for c in args.costs.split(",")]
    tasks = [(c, cost) for cost in costs for c in candidates]
    print(f"{len(candidates)} candidates x {len(costs)} costs = {len(tasks)} evaluations; "
          f"{len(WINDOWS)} test windows {WINDOWS[0][0]}..{WINDOWS[-1][0]}; calendar {CALENDAR[0].date()}..{CALENDAR[-1].date()}",
          flush=True)
    (OUT / "data_notes.json").write_text(json.dumps(notes, indent=1, default=str))
    (OUT / "candidates.json").write_text(json.dumps(candidates, default=str))
    import multiprocessing as mp

    results, daily_rows, psw_rows = [], {}, []
    context = mp.get_context("fork")
    with context.Pool(processes=args.workers) as pool:
        for index, result in enumerate(pool.imap_unordered(_task, tasks, chunksize=2), 1):
            if "error" in result:
                print("ERROR", result["id"], result["error"], flush=True)
                results.append(result)
                continue
            psw_rows.extend(result.pop("_per_symbol_window"))
            daily_rows[(result["id"], result["cost_bps"])] = result.pop("_oos_daily")
            results.append(result)
            if index % 100 == 0 or index == len(tasks):
                print(f"{index}/{len(tasks)} done, {time.time() - started:.0f}s", flush=True)
    (OUT / "results.json").write_text(json.dumps(results, default=str))
    pd.DataFrame(psw_rows).to_csv(OUT / "per_symbol_window.csv.gz", index=False, compression="gzip")
    keys = list(daily_rows)
    matrix = np.stack([daily_rows[k] for k in keys]) if keys else np.zeros((0, 0), np.float32)
    np.save(OUT / "oos_daily.npy", matrix)
    (OUT / "oos_daily_keys.json").write_text(json.dumps([[k[0], k[1]] for k in keys]))
    print(f"finished in {time.time() - started:.0f}s; {len(results)} results", flush=True)


if __name__ == "__main__":
    main()
