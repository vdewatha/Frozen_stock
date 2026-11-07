"""Backtest sector-relative short-term reversal, and sector-relative ranking inside the reversion strategies.

Ranked experiment 5 of the research priorities: in liquid US large caps after
2015 the best-documented short-horizon edge that survives is short-term
reversal measured *against the stock's industry or sector* (Hameed & Mian
2015; Da, Liu & Schaumburg 2014; Blitz et al. 2023), strongest when the VIX is
high (Nagel 2012), with turnover the main cost risk.  This script tests it on
the point-in-time S&P 500 and decides two things.

Pre-registered decision rules (fixed before the first run)
----------------------------------------------------------
1. **Admission of ``sector_reversal``.**  The strategy (its default rule,
   ``strategies/sector_reversal.DEFAULT_RULE``: close > SMA200 and
   ``rel_z_5 < -2``; exit when ``rel_rsi_2 > 70`` or close > SMA5, a
   10-session time stop or a 10% stop) is admitted as a learning-sized
   ``paper_trading_candidate`` **only if** it passes, on its own, every check
   of the discovery gate except the deflated Sharpe ratio: the ``explore``
   tier of ``strategy_discovery.gate`` (>= 100 discovery trades, month-
   clustered bootstrap lower bound of net expectancy > 0 and of the excess
   over the stock's own drift > 0, positive since 2015 with >= 30 trades,
   positive on the 2-year holdout with >= 20 trades).  Otherwise the row is
   created ``paused``.  Only the default rule decides; the other seven
   variants are trials (they enter the discovery library, where weekly
   discovery can admit any of them through its own gate).
2. **Sector-relative ranking for an existing reversion strategy** (RSI(2)
   pullback, IBS mean reversion, trend pullback, each on its live scope and
   setup).  When several symbols signal the strategy on the same day and the
   book's slots are limited, the candidates are ordered by ``rel_z_5`` (most
   negative first) instead of the live order (execution-list order).  Ranking
   is adopted for a strategy **only if**, in every window (all, 2015+, 2020+),
   the book's net expectancy per trade is not lower and its max drawdown is
   not worse than with the live order.  A ranking that changes no accepted
   trade has nothing to adopt and is not adopted.
3. **Trials.**  Every variant evaluated here is a trial: the 8 sector-
   reversal variants (4 entries x 2 exits) and, per ranked strategy, 3 book
   orders (live, raw signal strength = confidence, ``rel_z_5``).  Each
   variant's deflated Sharpe ratio is reported against that count, on its
   equal-weight (sector reversal) or book (ranking) daily returns before the
   holdout.  Neither rule above reads it: the explore tier is the bar for a
   forward test, not a claim of edge.

What is measured
----------------
* Universe: today's list plus every past S&P 500 member with yfinance
  history (``point_in_time_universe``), a single stock enterable only while
  it was a member.  Single stocks only for sector reversal; the sector ETFs
  are benchmarks.  Sector of each stock: the static map of today's names
  (``trading_universe.SECTOR_GROUPS``, the loss diagnosis' map), and for past
  members the causal statistical assignment of ``sector_relative`` (the
  sector ETF whose SPY-residual returns tracked the stock best over the
  trailing year, refreshed monthly).
* VIX state of each trade's decision bar: ``high`` when the VIX close is
  above the median of its last 252 closes, else ``low`` (Nagel 2012).
* Per variant, window and VIX state: trades, win rate, net expectancy (bps,
  month-clustered bootstrap CI), excess over drift, gross expectancy and the
  cost per trade, mean holding days, trades per year, and the max drawdown
  of the equal-weight daily portfolio.  The default rule also runs through
  the live book (``book_replay``: <= 5 per strategy, <= 3 per sector, <= 20
  total, ranked by ``rel_z_5``) to show what the slots realise.
* Costs: the latest paper cost calibration when one is recorded
  (``gate_replay.cost_calibration``), else the published volatility-scaled
  model.  Fills: decision on the close, next-open entry.

Data ($0): the shared yfinance cache (``--data-dir`` is used as
``meta_label_history_dir``), the membership CSV under ``<data-dir>/universe``,
a ``date,close`` VIX csv.  Cache only (no network).

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_sector_reversal_backtest.py --data-dir <dir> --vix-csv <csv> \\
        --output <json> [--report <md>]
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

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS, settings  # noqa: E402

WINDOWS = {"all": None, "2015+": "2015-01-01", "2020+": "2020-01-01"}
VIX_STATES = ("all", "high", "low")
VIX_MEDIAN_WINDOW = 252
RANKED_STRATEGIES = ("rsi2_pullback", "ibs_mean_reversion", "trend_pullback")
BOOK_ORDERS = ("live", "confidence", "rel_z_5")
ADMIT_TIERS = frozenset({"explore", "proven"})


# ----------------------------------------------------------------- decisions (pure, tested)

def admission_decision(gate_result: Mapping) -> dict:
    """Rule 1: admitted iff the default rule reaches the explore tier (every check but the deflated Sharpe)."""
    checks = dict(gate_result.get("checks") or {})
    explore_checks = {key: bool(checks.get(key)) for key in sorted(_explore_checks())}
    admitted = gate_result.get("tier") in ADMIT_TIERS and all(explore_checks.values())
    return {"admitted": bool(admitted), "status": "paper_trading_candidate" if admitted else "paused",
            "tier": gate_result.get("tier"), "explore_checks": explore_checks,
            "failed": [key for key, value in explore_checks.items() if not value]}


def _explore_checks() -> frozenset:
    from app.services.strategy_discovery import EXPLORE_CHECKS

    return EXPLORE_CHECKS


def ranking_decision(live: Mapping[str, Mapping], ranked: Mapping[str, Mapping], *, changed: bool) -> dict:
    """Rule 2: adopt iff ranking changed the book and, in every window, expectancy >= and drawdown <= live."""
    windows = {}
    for window in WINDOWS:
        a, b = live.get(window) or {}, ranked.get(window) or {}
        ea, eb = a.get("expectancy_bps"), b.get("expectancy_bps")
        da, db = a.get("portfolio_max_drawdown"), b.get("portfolio_max_drawdown")
        expectancy_ok = ea is not None and eb is not None and float(eb) >= float(ea)
        drawdown_ok = da is not None and db is not None and float(db) <= float(da)
        windows[window] = {"expectancy_ok": expectancy_ok, "drawdown_ok": drawdown_ok,
                           "expectancy_delta_bps": None if ea is None or eb is None else round(float(eb) - float(ea), 2),
                           "drawdown_delta": None if da is None or db is None else round(float(db) - float(da), 4)}
    passes = all(w["expectancy_ok"] and w["drawdown_ok"] for w in windows.values())
    reason = ("no_effect" if not changed else "passes" if passes
              else "fails: " + ", ".join(f"{name} {'expectancy' if not w['expectancy_ok'] else 'drawdown'}"
                                         for name, w in windows.items()
                                         if not (w["expectancy_ok"] and w["drawdown_ok"])))
    return {"adopted": bool(changed and passes), "reason": reason, "windows": windows}


def vix_states(vix: Optional[pd.DataFrame], window: int = VIX_MEDIAN_WINDOW) -> pd.Series:
    """Date -> ``high`` / ``low``: VIX close above the median of its last ``window`` closes (inclusive)."""
    if vix is None or vix.empty:
        return pd.Series(dtype=object)
    close = pd.Series(pd.to_numeric(vix["close"], errors="coerce").to_numpy(float),
                      index=pd.DatetimeIndex(pd.to_datetime(vix["date"].astype(str).str[:10]))).sort_index()
    median = close.rolling(window, min_periods=window // 2).median()
    state = pd.Series(np.where(close > median, "high", "low"), index=close.index, dtype=object)
    return state.where(median.notna(), None)


def state_of(dates, states: pd.Series) -> np.ndarray:
    if states is None or states.empty:
        return np.full(len(dates), None, dtype=object)
    index = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates).astype(str).str[:10]))
    return states.reindex(index, method="ffill").to_numpy(dtype=object)


# ----------------------------------------------------------------- statistics

def _portfolio(trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame], start=None,
               end=None) -> pd.Series:
    """Equal-weight daily portfolio over every symbol of ``frames`` (the discovery convention)."""
    from app.services.strategy_discovery import _marked_returns

    daily = []
    for symbol, group in trades.groupby("symbol"):
        frame = frames[symbol]
        dates = pd.DatetimeIndex(pd.to_datetime(frame["date"].astype(str).str[:10]))
        series = pd.Series(_marked_returns(group, frame["close"].to_numpy(float)), index=dates)
        if start is not None:
            series = series.loc[series.index >= pd.Timestamp(start)]
        if end is not None:
            series = series.loc[series.index < pd.Timestamp(end)]
        daily.append(series)
    if not daily:
        return pd.Series(dtype=float)
    return pd.concat(daily, axis=1).fillna(0.0).sum(axis=1).sort_index() / max(len(frames), 1)


def _drawdown(series: pd.Series) -> float:
    if series is None or not len(series):
        return 0.0
    curve = np.concatenate([[1.0], np.cumprod(1 + series.to_numpy(float))])
    return round(float(np.max(1 - curve / np.maximum.accumulate(curve))), 4)


def trade_stats(trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame], *, universe_size: int,
                start=None) -> dict:
    from app.services.strategy_scorecard import trade_statistics

    empty = {"trades": 0, "win_rate": None, "expectancy_bps": None, "expectancy_ci_bps": None,
             "excess_bps": None, "excess_ci_bps": None, "gross_bps": None, "cost_bps": None,
             "holding_days": None, "trades_per_year": None, "portfolio_max_drawdown": 0.0}
    if trades is None or trades.empty:
        return empty
    months = pd.to_datetime(trades["decision_date"]).dt.to_period("M").astype(str).tolist()
    net = trade_statistics(trades["net_return"].to_numpy(), clusters=months)
    excess = trade_statistics(trades["excess"].to_numpy(), clusters=months)
    dates = pd.to_datetime(trades["decision_date"])
    years = max((dates.max() - (pd.Timestamp(start) if start else dates.min())).days / 365.25, 1 / 12)
    portfolio = _portfolio(trades, {s: frames[s] for s in trades["symbol"].unique()}, start)
    portfolio = portfolio * len(trades["symbol"].unique()) / max(universe_size, 1)
    return {"trades": net["trades"], "win_rate": net["win_rate"], "expectancy_bps": net["expectancy_bps"],
            "expectancy_ci_bps": net.get("expectancy_ci_bps"), "excess_bps": excess["expectancy_bps"],
            "excess_ci_bps": excess.get("expectancy_ci_bps"),
            "gross_bps": round(float(trades["gross_return"].mean() * 1e4), 1),
            "cost_bps": round(float((trades["gross_return"] - trades["net_return"]).mean() * 1e4), 1),
            "holding_days": round(float(trades["holding_days"].mean()), 2),
            "trades_per_year": round(len(trades) / years, 1), "portfolio_max_drawdown": _drawdown(portfolio)}


def split_rows(trades: pd.DataFrame, frames, states: pd.Series, *, universe_size: int) -> dict:
    """window -> VIX state -> statistics."""
    out = {}
    for window, start in WINDOWS.items():
        sub = trades if start is None or trades.empty else trades.loc[pd.to_datetime(trades["decision_date"]) >= start]
        tags = state_of(sub["decision_date"], states) if len(sub) else np.array([], dtype=object)
        out[window] = {}
        for state in VIX_STATES:
            part = sub if state == "all" else sub.loc[tags == state]
            out[window][state] = trade_stats(part, frames, universe_size=universe_size, start=start)
    return out


def with_excess(trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """``excess`` = net return minus the symbol's mean daily drift over the holding days (the gate's definition)."""
    if trades is None or trades.empty:
        return trades
    drift = {s: float(pd.to_numeric(frames[s]["close"], errors="coerce").astype(float).pct_change().mean())
             for s in trades["symbol"].unique()}
    return trades.assign(excess=trades["net_return"] - trades["holding_days"].clip(lower=1)
                         * trades["symbol"].map(drift))


def variant_trades(rule: dict, plan: Mapping, frames: Mapping[str, pd.DataFrame], symbols, mask, caches: dict,
                   calibration=None) -> pd.DataFrame:
    """Every executable trade of a rule (``strategy_discovery.evaluate_candidate``'s fills, all label columns)."""
    from app.services.strategies import rules
    from app.services.strategy_discovery import _executable
    from app.services.triple_barrier import BarrierConfig, label_events

    barrier = BarrierConfig(take_profit_pct=float(plan["take_profit_pct"]), stop_loss_pct=float(plan["stop_loss_pct"]),
                            max_holding_days=int(plan["max_holding_sessions"]))
    out = []
    for symbol in symbols:
        frame = frames[symbol]
        cache = caches.setdefault(symbol, {})
        signals = np.asarray(rules.entry_signals(frame, rule, cache), dtype=bool)
        if mask is not None:
            signals &= np.asarray(mask(symbol, pd.to_datetime(frame["date"].astype(str).str[:10])), dtype=bool)
        entries = np.flatnonzero(signals)
        if not len(entries):
            continue
        costs = calibration.for_symbol(symbol) if calibration is not None else None
        labels = label_events(frame, entries, barrier, exit_signals=rules.exit_signals(frame, rule, cache),
                              calibration=costs)
        if labels.empty:
            continue
        out.append(_executable(labels).assign(symbol=symbol))
    return with_excess(pd.concat(out, ignore_index=True), frames) if out else pd.DataFrame()


def sharpe_moments(series: pd.Series) -> dict:
    from app.services.backtest_statistics import return_moments

    return return_moments(series.to_numpy(float)) if len(series) > 1 else {
        "sharpe_per_period": 0.0, "observations": 0, "skewness": 0.0, "kurtosis": 3.0}


def deflated(moments: Mapping, trials: int, sharpes: list[float]) -> Optional[float]:
    from app.services.backtest_statistics import deflated_sharpe_ratio

    result = deflated_sharpe_ratio(moments["sharpe_per_period"], moments["observations"], trials=trials,
                                   trial_sharpes=sharpes, skewness=moments["skewness"], kurtosis=moments["kurtosis"])
    return result["deflated_sharpe_ratio"]


# ----------------------------------------------------------------- book (ranking)

def rel_z_at(rows: pd.DataFrame, frames: Mapping[str, pd.DataFrame], caches: dict) -> np.ndarray:
    """``rel_z_5`` on each candidate's decision bar (NaN without a sector benchmark)."""
    from app.services.strategies import rules

    out = np.full(len(rows), np.nan)
    for symbol, group in rows.groupby("symbol"):
        values = rules.feature(frames[symbol], "rel_z_5", caches.setdefault(symbol, {})).to_numpy(float)
        out[group.index.to_numpy()] = values[group["event_idx"].to_numpy(int)]
    return out


def order_rows(rows: pd.DataFrame, order: str) -> tuple[pd.DataFrame, str]:
    """Candidate rows and the ``book_replay.allocate`` order that replays ``order``.

    ``rel_z_5`` sorts by the most negative value first, unrankable candidates
    last; ties keep the live order.
    """
    if order == "live":
        return rows, "live"
    if order == "confidence":
        return rows, "confidence"
    score = -rows["rel_z_5"].to_numpy(float)
    return rows.assign(confidence=np.where(np.isfinite(score), score, -np.inf)), "confidence"


def book_series(accepted: pd.DataFrame, frames: Mapping[str, pd.DataFrame], weight: float, end=None) -> pd.Series:
    from app.services.strategy_discovery import _marked_returns

    returns: dict = {}
    for row in accepted.itertuples(index=False):
        close = frames[row.symbol]["close"].to_numpy(float)
        dates = pd.to_datetime(frames[row.symbol]["date"].astype(str).str[:10]).to_numpy()
        marked = _marked_returns(pd.DataFrame([{"entry_idx": row.entry_idx, "exit_idx": row.exit_idx,
                                                "entry_price": row.entry_price, "exit_price": row.exit_price}]), close)
        for index in range(int(row.entry_idx), int(row.exit_idx) + 1):
            returns[dates[index]] = returns.get(dates[index], 0.0) + weight * marked[index]
    series = pd.Series(returns, dtype=float).sort_index()
    return series if end is None else series.loc[series.index < pd.Timestamp(end)]


def book_split(accepted: pd.DataFrame, frames, states: pd.Series, weight: float) -> dict:
    from app.services import book_replay

    out = {}
    tags = state_of(accepted["decision_date"], states) if len(accepted) else np.array([], dtype=object)
    for state in VIX_STATES:
        part = accepted if state == "all" else accepted.loc[tags == state]
        out[state] = book_replay.book_rows(part, frames, weight=weight)
    return out


# ----------------------------------------------------------------- run

def live_setup(strategy: str, earnings: bool) -> dict:
    from app.services.seed import MARKET_GATED_STRATEGIES, VIX_GATED_STRATEGIES

    setup = {"market_gate_sma": MARKET_GATED_STRATEGIES.get(strategy, 0)}
    setup.update(VIX_GATED_STRATEGIES.get(strategy, {}))
    if earnings:
        setup["earnings_blackout_sessions"] = settings.paper_earnings_blackout_sessions
    return setup


def run(args) -> dict:
    settings.meta_label_history_dir = str(args.data_dir)
    from app.services import book_replay, gate_replay
    from app.services import meta_label_primary as primary
    from app.services import point_in_time_universe as pit
    from app.services import strategy_discovery as discovery
    from app.services.earnings_history import load_surprises
    from app.services.meta_label_learning import history_dir
    from app.services.sector_relative import attach_sector_closes
    from app.services.strategy_scorecard import load_daily_history
    from app.services.trading_universe import ETF_SYMBOLS

    warnings.simplefilter("ignore", RuntimeWarning)
    started = time.time()
    now = datetime.now(timezone.utc)
    membership = pit.load_membership(download=False, now=now)

    def loader(ticker: str) -> pd.DataFrame:
        return load_daily_history(pit.yahoo_symbol(ticker), history_dir(), download=False)

    coverage = pit.coverage(membership, loader=loader, now=now, start=args.start, cache_path=pit.coverage_path())
    base = sorted({s.upper() for s in DEFAULT_PAPER_EXECUTION_SYMBOLS})
    symbols = pit.replay_symbols(base, coverage)
    frames, missing = discovery.load_frames(
        symbols, loader=loader, start=args.start, end=args.end, sector_close=False,
        earnings_loader=lambda s: load_surprises(s, history_dir(), download=False), news_loader=lambda s: None)
    frames, method = attach_sector_closes(frames)
    load_seconds = time.time() - started
    spy = frames.get("SPY")
    if spy is None:
        raise SystemExit("SPY history is required")
    vix = pd.read_csv(args.vix_csv) if args.vix_csv and Path(args.vix_csv).exists() else None
    states = vix_states(vix)
    try:
        calibration = gate_replay.cost_calibration()
    except Exception:  # no ledger reachable: the published model
        calibration = None
    end = max(pd.Timestamp(str(f["date"].iloc[-1])[:10]) for f in frames.values())
    holdout_start = end - pd.DateOffset(years=discovery.HOLDOUT_YEARS)
    singles = sorted(s for s in frames if s not in ETF_SYMBOLS and method.get(s, "none") != "none")
    mask = membership.mask

    # 1. sector reversal variants ------------------------------------------------
    caches: dict = {}
    variants = discovery.sector_reversal_variants()
    sector_rows, sector_trades, sector_summaries, moments = {}, {}, {}, {}
    for name, rule, plan in variants:
        candidate = {"id": discovery.candidate_id(rule, plan, "single_stocks"), "family": "sector_reversal",
                     "hypothesis": "", "rule": rule, "universe": "single_stocks", "symbols": singles, **plan}
        evaluation = discovery.evaluate_candidate(candidate, frames, caches, universe=mask)
        sector_summaries[name] = discovery.summarize_candidate(candidate, evaluation, holdout_start)
        trades = variant_trades(rule, plan, frames, singles, mask, caches, calibration)
        sector_trades[name] = trades
        sector_rows[name] = split_rows(trades, frames, states, universe_size=len(singles))
        moments[f"sector_reversal:{name}"] = {k: sector_summaries[name][k] for k in
                                              ("sharpe_per_period", "observations", "skewness", "kurtosis")}
    sector_seconds = time.time() - started - load_seconds

    # 2. ranking inside the reversion strategies ------------------------------
    earnings_cached = (history_dir() / "earnings").exists()
    replays = {symbol: primary.replay_signals(frame) for symbol, frame in frames.items()}
    weight = 1.0 / book_replay.BASE_CAPS["total"]
    policy = book_replay.book_policy({})
    ranking, books = {}, {}
    for strategy in RANKED_STRATEGIES:
        setup = live_setup(strategy, earnings_cached)
        rows = book_replay.candidates(frames, spy, replays, {strategy: setup}, vix=vix, universe=mask,
                                      calibration=calibration)
        if not rows.empty:
            rows = with_excess(rows, frames)
            rows["rel_z_5"] = rel_z_at(rows, frames, caches)
        accepted = {}
        for order in BOOK_ORDERS:
            ordered, allocate_order = order_rows(rows, order) if not rows.empty else (rows, order)
            accepted[order] = book_replay.allocate(ordered, frames, dict(policy, order=allocate_order)) \
                if not rows.empty else rows
            moments[f"{strategy}:{order}"] = dict(sharpe_moments(book_series(accepted[order], frames, weight,
                                                                             holdout_start)))
        changed = not rows.empty and set(accepted["live"].index) != set(accepted["rel_z_5"].index)
        live_rows = book_replay.book_rows(accepted["live"], frames, weight=weight)
        ranked_rows = book_replay.book_rows(accepted["rel_z_5"], frames, weight=weight)
        ranking[strategy] = {
            "setup": setup, "candidates": int(len(rows)),
            "rankable_share": round(float(np.isfinite(rows["rel_z_5"]).mean()), 4) if not rows.empty else None,
            "accepted": {order: int(len(accepted[order])) for order in BOOK_ORDERS},
            "changed_trades": int(len(set(accepted["live"].index) ^ set(accepted["rel_z_5"].index))) if changed else 0,
            "books": {order: book_split(accepted[order], frames, states, weight) for order in BOOK_ORDERS},
            "decision": ranking_decision(live_rows, ranked_rows, changed=changed),
        }
        books[strategy] = accepted

    # the default rule through the live book, ranked by rel_z_5 -----------------
    default_name = variants[0][0]
    default_trades = sector_trades[default_name]
    book_default = {}
    if not default_trades.empty:
        rows = default_trades.assign(strategy="sector_reversal", confidence=0.6)
        rows["entry_date"] = [frames[s]["date"].iloc[int(i)] for s, i in zip(rows["symbol"], rows["entry_idx"])]
        rows["exit_date"] = [frames[s]["date"].iloc[int(i)] for s, i in zip(rows["symbol"], rows["exit_idx"])]
        rows["live_rank"] = 0
        rows = rows.sort_values(["entry_date", "symbol"], kind="stable").reset_index(drop=True)
        rows["rel_z_5"] = rel_z_at(rows, frames, caches)
        ordered, allocate_order = order_rows(rows, "rel_z_5")
        accepted = book_replay.allocate(ordered, frames, dict(policy, order=allocate_order))
        book_default = {"candidates": int(len(rows)), "accepted": int(len(accepted)),
                        "books": book_split(accepted, frames, states, weight)}

    # 3. trials and deflated Sharpe ------------------------------------------------
    trials = len(moments)
    sharpes = [m["sharpe_per_period"] for m in moments.values() if m["observations"] > 1]
    dsr = {key: deflated(m, trials, sharpes) if m["observations"] > 1 else None for key, m in moments.items()}
    gates = {name: discovery.gate(summary, trials=trials, trial_sharpes=sharpes)
             for name, summary in sector_summaries.items()}
    admission = admission_decision(gates[default_name])

    by_method = {how: sum(1 for s in singles if method.get(s) == how) for how in ("static", "statistical")}
    return {
        "ran_at": now.isoformat(), "paper_only": True, "data_dir": str(args.data_dir), "start": args.start,
        "end": str(end.date()), "holdout_start": str(holdout_start.date()), "vix": vix is not None,
        "calibration": None if calibration is None else "latest recorded",
        "universe": {"symbols": len(frames), "single_stocks_with_sector": len(singles), "sector_method": by_method,
                     "missing_history": len(missing), "coverage_since_2010": coverage.get("coverage_since_2010")},
        "trials": trials, "default_variant": default_name,
        "sector_reversal": {name: {"rule": rule, "plan": plan, "windows": sector_rows[name],
                                   "gate": gates[name], "deflated_sharpe": dsr[f"sector_reversal:{name}"],
                                   "discovery": sector_summaries[name]["discovery"],
                                   "post_period": sector_summaries[name]["post_period"],
                                   "holdout": sector_summaries[name]["holdout"]}
                            for name, rule, plan in variants},
        "default_book": book_default,
        "ranking": {strategy: dict(row, deflated_sharpe={order: dsr[f"{strategy}:{order}"] for order in BOOK_ORDERS})
                    for strategy, row in ranking.items()},
        "decisions": {"sector_reversal_admission": admission,
                      "sector_ranking": {s: row["decision"]["adopted"] for s, row in ranking.items()}},
        "timings": {"load_seconds": round(load_seconds, 1), "sector_seconds": round(sector_seconds, 1),
                    "total_seconds": round(time.time() - started, 1)},
    }


# ----------------------------------------------------------------- report

def _pct(value, digits=1):
    return "n/a" if value is None else f"{100 * float(value):.{digits}f}%"


def _bps(value):
    return "n/a" if value is None else f"{float(value):+.1f}"


def _ci(value):
    return "" if not value else f" [{value[0]:+.0f}, {value[1]:+.0f}]"


def _num(value, digits=3):
    return "n/a" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{float(value):.{digits}f}"


def render(result: dict) -> str:
    dec = result["decisions"]
    adm = dec["sector_reversal_admission"]
    uni = result["universe"]
    day = result["ran_at"][:10]
    adopted = [s for s, flag in dec["sector_ranking"].items() if flag]
    lines = [f"# Sector-relative reversal: {day}", "",
             "**Question:** does short-term reversal measured against the stock's sector (the best-documented "
             "surviving short-horizon edge in liquid large caps) pay after costs on the point-in-time S&P 500, and "
             "does ranking same-day candidates by it help the existing reversion strategies?", "",
             f"**Decisions (pre-registered in `scripts/run_sector_reversal_backtest.py`):** `sector_reversal` is "
             f"**{'admitted as a learning-sized paper_trading_candidate' if adm['admitted'] else 'created paused'}** "
             f"(gate tier `{adm['tier']}`" + (f"; failed: {', '.join(adm['failed'])}" if adm["failed"] else "") + "). "
             f"Sector-relative ranking adopted for: {', '.join(adopted) if adopted else 'none'}. Paper only.", "",
             "## 1. Data and method", "",
             f"* Bars {result['start']} to {result['end']}, holdout from {result['holdout_start']}; VIX "
             f"{'present' if result['vix'] else 'absent'}; costs: "
             f"{'latest recorded paper cost calibration' if result['calibration'] else 'published volatility-scaled model (no paper cost calibration recorded in this ledger)'}.",
             f"* Point-in-time universe: {uni['symbols']} symbols with history (coverage since 2010 "
             f"{_pct(uni['coverage_since_2010'])}); {uni['single_stocks_with_sector']} single stocks with a sector "
             f"benchmark: {uni['sector_method'].get('static', 0)} on today's static map, "
             f"{uni['sector_method'].get('statistical', 0)} past members assigned statistically (`sector_relative`).",
             f"* Trials counted: {result['trials']} (8 sector-reversal variants + 3 book orders x 3 strategies).", ""]
    lines += ["## 2. Sector reversal variants", "",
              "Per variant: discovery-gate tier, deflated Sharpe over all trials, then trades / win rate / net "
              "expectancy (bps, month-clustered CI) / excess over drift / gross and cost per trade / holding days / "
              "trades per year / equal-weight max drawdown, by window and VIX state (VIX above or below its "
              "trailing 252-day median).", ""]
    for name, row in result["sector_reversal"].items():
        g = row["gate"]
        failed = [k for k, v in g["checks"].items() if not v]
        lines += [f"### `{name}`" + (" (default rule)" if name == result["default_variant"] else ""), "",
                  f"Tier **{g['tier']}**; deflated Sharpe {_num(row['deflated_sharpe'])}; failed checks: "
                  f"{', '.join(failed) if failed else 'none'}. Holdout {row['holdout']['trades']} trades at "
                  f"{_bps(row['holdout']['expectancy_bps'])} bps.", "",
                  "| Window | VIX | Trades | Win | Net bps | Excess bps | Gross | Cost | Hold d | /yr | Max DD |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for window, cells in row["windows"].items():
            for state, m in cells.items():
                lines.append(f"| {window} | {state} | {m['trades']} | {_pct(m['win_rate'])} | "
                             f"{_bps(m['expectancy_bps'])}{_ci(m['expectancy_ci_bps'])} | "
                             f"{_bps(m['excess_bps'])}{_ci(m['excess_ci_bps'])} | {_bps(m['gross_bps'])} | "
                             f"{_bps(m['cost_bps'])} | {_num(m['holding_days'], 1)} | {_num(m['trades_per_year'], 0)} | "
                             f"{_pct(m['portfolio_max_drawdown'])} |")
        lines.append("")
    book = result.get("default_book") or {}
    if book:
        lines += ["### Default rule through the live book", "",
                  f"{book['candidates']} executable trades compete for the slots (<= 5 per strategy, <= 3 per sector, "
                  f"<= 20 total, most negative `rel_z_5` first); {book['accepted']} are taken. Book statistics "
                  "(each position 5% of equity):", "",
                  "| Window | VIX | Trades | Win | Net bps | Total return | Max DD |", "|---|---|---|---|---|---|---|"]
        for state, windows in book["books"].items():
            for window, m in windows.items():
                lines.append(f"| {window} | {state} | {m['trades']} | {_pct(m['win_rate'])} | "
                             f"{_bps(m['expectancy_bps'])}{_ci(m['expectancy_ci_bps'])} | {_pct(m['total_return'])} | "
                             f"{_pct(m['max_drawdown'])} |")
        lines.append("")
    lines += ["## 3. Ranking inside the reversion strategies", "",
              "Each strategy alone through the live book on its live scope and setup; the same candidates in three "
              "orders: `live` (execution-list order, today's behaviour), `confidence` (raw signal strength) and "
              "`rel_z_5` (most negative first, unrankable last).", ""]
    for strategy, row in result["ranking"].items():
        d = row["decision"]
        lines += [f"### {strategy}", "",
                  f"Setup `{json.dumps(row['setup'])}`; {row['candidates']} candidates, rankable share "
                  f"{_pct(row['rankable_share'])}; accepted {row['accepted']}; trades changed by ranking "
                  f"{row['changed_trades']}. Decision: **{'adopted' if d['adopted'] else 'not adopted'}** ({d['reason']}). "
                  f"Deflated Sharpe: " + ", ".join(f"{o} {_num(v)}" for o, v in row["deflated_sharpe"].items()) + ".", "",
                  "| Window | VIX | Order | Trades | Win | Net bps | Total return | Max DD |",
                  "|---|---|---|---|---|---|---|---|"]
        for window in WINDOWS:
            for state in VIX_STATES:
                for order in BOOK_ORDERS:
                    m = row["books"][order][state][window]
                    lines.append(f"| {window} | {state} | {order} | {m['trades']} | {_pct(m['win_rate'])} | "
                                 f"{_bps(m['expectancy_bps'])}{_ci(m['expectancy_ci_bps'])} | {_pct(m['total_return'])} | "
                                 f"{_pct(m['max_drawdown'])} |")
        lines.append("")
    t = result["timings"]
    lines += ["## 4. Run", "", f"Load {t['load_seconds']}s, sector variants {t['sector_seconds']}s, total "
              f"{t['total_seconds']}s. JSON next to this report holds every number.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--vix-csv", type=Path, default=None)
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None, help="exclusive; default yesterday's close")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    result = run(args)
    args.output.write_text(json.dumps(result, indent=2, default=str))
    if args.report:
        args.report.write_text(render(result))
    print(json.dumps({"decisions": result["decisions"], "timings": result["timings"]}, indent=2, default=str))


if __name__ == "__main__":
    main()
