"""Automated strategy discovery: test a library of rule candidates, admit survivors to paper.

The learning loop can select, size and filter strategies, but it can only
choose among hand-written families, and none of them has a net edge that
survives costs and selection.  This module looks for new ones:

1. ``candidate_library`` enumerates long-only daily-bar hypotheses from the
   published anomaly literature (short-term pullbacks, N-day lows, down
   streaks, turn of the month, Bollinger lows, weekly reversals, breakouts,
   gap-down recoveries, calm-market dips, post-earnings drift on single
   stocks, same-session opening-gap fills), each over the universes it applies
   to (index ETFs, sector ETFs, single stocks).  Every candidate is a
   declarative ``strategies.rules`` rule plus a stop / target / time stop.
2. ``evaluate_candidate`` replays it over adjusted daily history exactly as the
   backtester trades: decision on the close, fill at the next open with
   volatility-scaled costs on both sides, stop first inside a bar, gaps fill at
   the open, indicator exit at the next open, one position per symbol.
   Open-timed rules (gap fills) buy at the open and are flat by the close
   (``triple_barrier.label_open_session``).
3. ``gate`` applies a pass/fail bar that is fixed in advance (``GATE``):
   on the discovery window (everything before the last ``HOLDOUT_YEARS``),
   enough trades, a positive month-clustered bootstrap lower bound on net
   expectancy *and* on the excess over the symbol's own drift (holding the
   stock is not an edge), still positive since 2015, and a deflated Sharpe
   ratio that counts every candidate in the library as a trial.  Then the
   untouched holdout must also be positive.  A candidate that clears every
   check is ``proven``.  One that clears every check except the deflated
   Sharpe ratio (``EXPLORE_CHECKS``) is ``explore``: worth a forward test, not
   a claim of edge.
4. ``admit_survivors`` turns admissible candidates (``explore`` or better, or
   only ``proven`` with ``strategy_discovery_require_proven``) into
   ``rule_<id>`` Strategy rows with status ``paper_trading_candidate``: they
   trade only as small, tagged exploration until the normal forward-trial /
   promotion gates say otherwise.  A discovered candidate that later stops
   qualifying is retired.  Open-timed candidates are graded and reported but
   never admitted: the daily paper path cannot place an order at the open.

5. ``evolve_library`` lets the library grow on its own (``rule_mutation``):
   the parents are the candidates that came closest to the gate (at most one
   failed check, ranked by the lower bound of their net expectancy) plus the
   ``explore`` survivors; each run breeds up to ``MUTATION_CHILDREN`` children
   by one edit each, never one whose canonical rule and universe were tested
   before, evaluates them inside a wall-clock budget and gates them with the
   same bar.  Children that came close (``retained``) are re-evaluated on
   later runs so they can be parents in turn; nothing else is re-tried.

Re-running the same library on more data is not a new trial; growing the
library is, and ``trials`` grows with it: a ``discovery_trials`` audit row
per run lists the hash of every rule ever evaluated (library and children),
and the deflated Sharpe ratio of every candidate is charged for that
cumulative count.  Three runs in a row without a child better than its
parent pause mutation for one run.  The holdout is re-read on every run, so
it is a sanity check, not a pristine test set.  Paper only.

The yardstick (``yardstick.py``): every candidate also carries its cost
bracket (5 / 10 / 25 bps a round trip, the published model, calibrated cells
with 20+ fills), turnover, excess over a volatility-matched SPY/T-bill arm and
SPY residual with clustered errors, and the short-hold veto on its holdout,
which blocks new admissions (never retires an admitted row).  The trial
ledger keeps each hash's family, and the deflated Sharpe charges the family's
trials made outside the library (engine and research) on top of the
library's cumulative count.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from typing import Iterable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.services import earnings_history, news_history, rule_mutation
from app.services.backtest_statistics import deflated_sharpe_ratio, return_moments
from app.services.strategies import rules
from app.services.strategies.rules import RULE_SLUG_PREFIX
from app.services.strategy_scorecard import trade_statistics
from app.services.trading_universe import ETF_SYMBOLS
from app.services.triple_barrier import BarrierConfig, label_events, label_open_session

logger = logging.getLogger(__name__)
UTC = timezone.utc
INDEX_ETFS = ("SPY", "QQQ", "IWM", "DIA")
HOLDOUT_YEARS = 2
POST_PERIOD_START = "2015-01-01"
DISCOVERED_STATUS = "paper_trading_candidate"
# Statuses in which a discovered strategy still occupies a paper slot.
LIVE_STATUSES = frozenset({"paper_trading_candidate", "paper_trading_active", "research", "paused"})


@dataclass(frozen=True)
class Gate:
    min_trades: int = 100
    min_post_period_trades: int = 30
    min_deflated_sharpe: float = 0.95
    min_holdout_trades: int = 20


GATE = Gate()
# Every check except the selection-adjusted one: admissible for a paper forward
# test, where the forward evidence (not the backtest) decides promotion.
EXPLORE_CHECKS = frozenset({"enough_trades", "net_expectancy_ci_low_gt_0", "excess_over_drift_ci_low_gt_0",
                            "post_period_positive", "holdout_positive"})
TIER_ORDER = {"proven": 2, "explore": 1, "rejected": 0}
# Evolution step (rule_mutation): parents per run, children per run, runs
# without an improving child before mutation pauses, near-miss children kept
# for re-evaluation (and parenthood) on later runs.
MUTATION_PARENTS = 10
MUTATION_CHILDREN = 40
MUTATION_STALE_RUNS = 3
RETAINED_MAX = 60
TRIALS_EVENT = "discovery_trials"


# ---------------------------------------------------------------- library

def _c(f: str, op: str, v: float) -> dict:
    return {"f": f, "op": op, "v": v}


TREND = [_c("dist_sma_200", ">", 0)]
FIRST_UP_CLOSE = [_c("ret_1", ">", 0)]
ABOVE_SMA5 = [_c("dist_sma_5", ">", 0)]
SHORT_EXITS = {"first_up_close": FIRST_UP_CLOSE, "close_above_sma5": ABOVE_SMA5}


# News-tone families (reports/news-entries-20261106.md): the ``news_tone`` /
# ``news_articles`` features from ``news_history``; 0 without a tone history,
# so the candidates fire only where the backfill reaches.
NEWS_FAMILIES = frozenset({"positive_news_trend", "negative_news_dip", "post_upgrade_drift"})
# Sector-relative reversal (reports/sector-reversal-20261107.md): the
# ``rel_*`` features against the stock's sector ETF (``sector_relative``);
# NaN on ETFs, so single stocks only.
SECTOR_FAMILIES = frozenset({"sector_reversal"})
SECTOR_REVERSAL_EXITS = {
    "hysteresis": [_c("rel_rsi_2", ">", 70), *ABOVE_SMA5],
    "relative_recovered": [_c("rel_rsi_2", ">", 70)],
}
# Families that only make sense on some universes (ETFs have no earnings and no company news).
FAMILY_UNIVERSES = {"earnings_drift": frozenset({"single_stocks"}),
                    **{family: frozenset({"single_stocks"}) for family in NEWS_FAMILIES},
                    **{family: frozenset({"single_stocks"}) for family in SECTOR_FAMILIES}}


def sector_reversal_variants() -> list[tuple[str, dict, dict]]:
    """(name, rule, exit plan) of every sector-reversal variant; the first is the strategy's default rule."""
    reversion = {"stop_loss_pct": 0.10, "take_profit_pct": 0.0, "max_holding_sessions": 10}
    out = []
    for (feature_name, level), (exit_name, exit_rule) in product(
            [("rel_z_5", -2), ("rel_z_5", -1.5), ("rel_rsi_2", 5), ("rel_rsi_2", 10)], SECTOR_REVERSAL_EXITS.items()):
        out.append((f"{feature_name}<{level:g}/{exit_name}",
                    {"entry": [*TREND, _c(feature_name, "<", level)], "exit": [dict(c) for c in exit_rule]},
                    dict(reversion)))
    return out
GAP_FLOOR = -0.03  # bigger opening gaps are news, not noise, and rarely fill the same day


def _families() -> Iterable[tuple[str, str, dict, dict]]:
    """(family, hypothesis, rule, exit plan) before the universe is chosen."""
    reversion = {"stop_loss_pct": 0.10, "take_profit_pct": 0.0, "max_holding_sessions": 10}
    for (feature_name, level), (exit_name, exit_rule) in product(
            [("rsi_2", 5), ("rsi_2", 10), ("rsi_3", 10), ("rsi_3", 20)], SHORT_EXITS.items()):
        yield ("pullback_in_uptrend", "Short-term oversold dips inside a long-term uptrend revert (Connors).",
               {"entry": [*TREND, _c(feature_name, "<", level)], "exit": exit_rule}, reversion)
    for n, trend, (exit_name, exit_rule) in product((5, 10, 20), (True, False), SHORT_EXITS.items()):
        yield ("n_day_low", "A close at an N-day low tends to bounce.",
               {"entry": [*(TREND if trend else []), _c(f"at_low_{n}", "==", 1)], "exit": exit_rule}, reversion)
    for k, trend, (exit_name, exit_rule) in product((3, 4, 5), (True, False), SHORT_EXITS.items()):
        yield ("down_streak", "Several consecutive down closes tend to reverse.",
               {"entry": [*(TREND if trend else []), _c("down_streak", ">=", k)], "exit": exit_rule}, reversion)
    for k, exit_day, trend in product((1, 2, 3), (3, 5), (True, False)):
        yield ("turn_of_month", "Returns cluster around the turn of the month (pension and payroll flows).",
               {"entry": [*(TREND if trend else []), _c("tdays_left", "==", k)], "exit": [_c("tdom", "==", exit_day)]},
               {"stop_loss_pct": 0.10, "take_profit_pct": 0.0, "max_holding_sessions": k + exit_day + 2})
    for level, trend, (exit_name, exit_rule) in product((0.0, 0.05), (True, False),
                                                        {"pctb_mid": [_c("bb_pctb_20", ">", 0.5)],
                                                         "close_above_sma5": ABOVE_SMA5}.items()):
        yield ("bollinger_low", "A close below the lower Bollinger band overshoots and reverts.",
               {"entry": [*(TREND if trend else []), _c("bb_pctb_20", "<", level)], "exit": exit_rule}, reversion)
    for drop, trend, (exit_name, exit_rule) in product((-0.03, -0.05, -0.08), (True, False), SHORT_EXITS.items()):
        yield ("weekly_reversal", "Large one-week losers rebound the following week.",
               {"entry": [*(TREND if trend else []), _c("ret_5", "<", drop)], "exit": exit_rule}, reversion)
    for n, calm, hold in product((50, 100, 252), (True, False), (20, 40)):
        yield ("breakout_momentum", "New N-day highs keep going (time-series momentum).",
               {"entry": [_c(f"at_high_{n}", "==", 1), *([_c("vol_ratio_20_100", "<", 1.0)] if calm else [])],
                "exit": [_c("dist_sma_50", "<", 0)]},
               {"stop_loss_pct": 0.10, "take_profit_pct": 0.0, "max_holding_sessions": hold})
    for gap, strong, trend in product((-0.01, -0.02), (True, False), (True, False)):
        yield ("gap_down_recovery", "A gap down that closes off its lows has exhausted the selling.",
               {"entry": [*(TREND if trend else []), _c("gap", "<", gap), *([_c("ibs", ">", 0.5)] if strong else [])],
                "exit": FIRST_UP_CLOSE}, reversion)
    for level, (exit_name, exit_rule) in product((5, 10), SHORT_EXITS.items()):
        yield ("calm_market_dip", "Dips revert more reliably when volatility is below its norm.",
               {"entry": [*TREND, _c("vol_ratio_20_100", "<", 1.0), _c("rsi_2", "<", level)], "exit": exit_rule},
               reversion)
    for surprise, reaction, trend, hold in product((0, 5, 10), (None, 0.02), (True, False), (20, 40)):
        yield ("earnings_drift", "Prices keep drifting the way of an earnings surprise for weeks (PEAD).",
               {"entry": [*(TREND if trend else []), _c("earn_surprise", ">", surprise),
                          *([_c("ret_1", ">", reaction)] if reaction is not None else [])], "exit": []},
               {"stop_loss_pct": 0.10, "take_profit_pct": 0.0, "max_holding_sessions": hold})
    # News tone as an entry condition. ``news_tone`` is the bar's own session
    # (stories before that close), ``news_tone_1`` the previous session's.
    for lag, hold in product(("", "_1"), (5, 10)):
        yield ("positive_news_trend", "Positive, well-covered news in an uptrend keeps paying for a few sessions.",
               {"entry": [*TREND, _c(f"news_tone{lag}", ">=", 0.5), _c(f"news_articles{lag}", ">=", 2)],
                "exit": ABOVE_SMA5},
               {"stop_loss_pct": 0.10, "take_profit_pct": 0.0, "max_holding_sessions": hold})
    for lag, trend in product(("", "_1"), (True, False)):
        yield ("negative_news_dip", "A sharp dip on bad news is overdone and reverts (news overreaction).",
               {"entry": [*(TREND if trend else []), _c(f"news_tone{lag}", "<=", -0.5), _c("rsi_2", "<", 10)],
                "exit": ABOVE_SMA5}, reversion)
    for trend, hold in product((True, False), (10, 20)):
        yield ("post_upgrade_drift", "An analyst upgrade met by a gap up keeps drifting (recommendation drift).",
               {"entry": [*(TREND if trend else []), _c("news_tone", ">=", 1.0), _c("gap", ">", 0.01)], "exit": []},
               {"stop_loss_pct": 0.10, "take_profit_pct": 0.0, "max_holding_sessions": hold})
    for _name, rule, plan in sector_reversal_variants():
        yield ("sector_reversal", "A stock's fall that its own sector did not share is a liquidity overshoot "
               "and reverts (Hameed & Mian 2015; Da, Liu & Schaumburg 2014).", rule, plan)
    filters = {"none": [], "trend": TREND, "calm": [_c("vol_ratio_20_100", "<", 1.0)]}
    for gap, (filter_name, conditions), stop in product((-0.003, -0.005, -0.01), filters.items(), (0.01, 0.03)):
        yield ("opening_gap_fill", "Small opening gaps down fill back to the prior close within the session.",
               {"timing": "open", "entry": [*conditions, _c("gap", "<", gap), _c("gap", ">", GAP_FLOOR)]},
               {"stop_loss_pct": stop, "take_profit_pct": 0.0, "max_holding_sessions": 1})


def universes(symbols: Iterable[str]) -> dict[str, tuple[str, ...]]:
    symbols = [s.upper() for s in symbols]
    return {
        "index_etfs": tuple(s for s in symbols if s in INDEX_ETFS),
        "sector_etfs": tuple(s for s in symbols if s in ETF_SYMBOLS and s not in INDEX_ETFS),
        "single_stocks": tuple(s for s in symbols if s not in ETF_SYMBOLS),
    }


def candidate_id(rule: dict, plan: dict, universe: str) -> str:
    payload = json.dumps({"rule": rule, "plan": plan, "universe": universe}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:10]


def candidate_library(symbols: Iterable[str]) -> list[dict]:
    groups = {name: members for name, members in universes(symbols).items() if members}
    out = []
    for family, hypothesis, rule, plan in _families():
        rules.validate_rule(rule)
        for universe, members in groups.items():
            if universe not in FAMILY_UNIVERSES.get(family, groups):
                continue
            out.append({"id": candidate_id(rule, plan, universe), "family": family, "hypothesis": hypothesis,
                        "rule": rule, "universe": universe, "symbols": list(members), **plan})
    return out


# ---------------------------------------------------------------- evaluation

def _executable(labels: pd.DataFrame) -> pd.DataFrame:
    """Non-overlapping trades for one symbol: enter only when flat."""
    keep, busy_until = [], -1
    for row in labels.itertuples(index=False):
        if row.entry_idx > busy_until:
            keep.append(True)
            busy_until = row.exit_idx
        else:
            keep.append(False)
    return labels.loc[keep]


def _marked_returns(labels: pd.DataFrame, close: np.ndarray) -> np.ndarray:
    """Daily mark-to-market return of one symbol's position (0 when flat).

    Entry day: close / cost-inclusive entry price; held days: close to close;
    exit day: cost-inclusive exit price / prior close.  A trade opened and
    closed on one bar is exit / entry on that bar.
    """
    returns = np.zeros(len(close))
    for row in labels.itertuples(index=False):
        entry, exit_ = int(row.entry_idx), int(row.exit_idx)
        if exit_ == entry:
            returns[entry] += row.exit_price / row.entry_price - 1
            continue
        returns[entry] += close[entry] / row.entry_price - 1
        if exit_ - entry > 1:
            returns[entry + 1:exit_] += close[entry + 1:exit_] / close[entry:exit_ - 1] - 1
        returns[exit_] += row.exit_price / close[exit_ - 1] - 1
    return returns


# ``gross_return`` (before costs) and the hold dates feed the yardstick's cost
# bracket, short-hold veto and SPY residual (yardstick.py).
TRADE_COLUMNS = ["symbol", "t0", "entry_idx", "exit_idx", "entry_date", "exit_date", "holding_days", "barrier",
                 "gross_return", "net_return", "excess", "entry_price", "exit_price"]


def evaluate_candidate(candidate: Mapping, frames: Mapping[str, pd.DataFrame],
                       caches: Optional[dict] = None, universe=None) -> dict:
    """Executable trades and the equal-weight daily portfolio series for one candidate.

    ``universe(symbol, dates)`` is the optional point-in-time membership mask
    ANDed into the entry signals (``point_in_time_universe``).
    """
    barrier = BarrierConfig(take_profit_pct=float(candidate["take_profit_pct"]),
                            stop_loss_pct=float(candidate["stop_loss_pct"]),
                            max_holding_days=int(candidate["max_holding_sessions"]))
    caches = caches if caches is not None else {}
    trades, daily = [], []
    for symbol in candidate["symbols"]:
        frame = frames.get(symbol)
        if frame is None or len(frame) <= rules.rule_lookback(candidate["rule"]):
            continue
        cache = caches.setdefault(symbol, {})
        signals = np.asarray(rules.entry_signals(frame, candidate["rule"], cache), dtype=bool)
        if universe is not None:
            signals &= np.asarray(universe(symbol, pd.to_datetime(frame["date"].astype(str).str[:10])), dtype=bool)
        entries = np.flatnonzero(signals)
        if not len(entries):
            continue
        if rules.timing(candidate["rule"]) == "open":
            labels = label_open_session(frame, entries, float(candidate["stop_loss_pct"]))
        else:
            labels = label_events(frame, entries, barrier,
                                  exit_signals=rules.exit_signals(frame, candidate["rule"], cache))
        if labels.empty:
            continue
        labels = _executable(labels)
        close = pd.to_numeric(frame["close"], errors="coerce").astype(float)
        drift = float(close.pct_change().mean())
        dates = pd.to_datetime(frame["date"].astype(str).str[:10]).to_numpy()
        labels = labels.assign(symbol=symbol, t0=dates[labels["event_idx"].to_numpy()],
                               excess=labels["net_return"] - labels["holding_days"].clip(lower=1) * drift)
        trades.append(labels[TRADE_COLUMNS])
        daily.append(pd.Series(_marked_returns(labels, close.to_numpy()), index=dates))
    trade_frame = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame(columns=TRADE_COLUMNS)
    portfolio = (pd.concat(daily, axis=1).mean(axis=1, skipna=True).sort_index() if daily
                 else pd.Series(dtype=float, index=pd.DatetimeIndex([])))  # no trade anywhere: still date-indexed
    return {"trades": trade_frame, "portfolio": portfolio}


def _stats(trades: pd.DataFrame) -> dict:
    months = pd.to_datetime(trades["t0"]).dt.to_period("M").astype(str).tolist()
    net = trade_statistics(trades["net_return"].to_numpy(), clusters=months)
    excess = trade_statistics(trades["excess"].to_numpy(), clusters=months)
    return {"net": net, "excess_over_drift": {key: excess.get(key) for key in
                                               ("trades", "expectancy_bps", "expectancy_ci_bps")}}


def yardstick_scores(candidate: Mapping, evaluation: dict, holdout_start: pd.Timestamp,
                     context: Optional[Mapping]) -> Optional[dict]:
    """The yardstick's readings for one candidate (yardstick.py); None without a context.

    Cost bracket on the discovery window (month-clustered CIs), since 2015 and
    the holdout; turnover over the discovery window; the short-hold veto on the
    holdout (its out-of-sample window); excess over the risk-matched passive
    arm and the SPY residual on the discovery window and since 2015.
    """
    if context is None:
        return None
    from app.services import yardstick

    frames = context["frames"]
    symbols = [s for s in candidate["symbols"] if s in frames]
    return yardstick.score_candidate(
        evaluation["trades"], frames, spy=context.get("spy"),
        windows={"discovery": (None, holdout_start), "post_period": (POST_PERIOD_START, holdout_start),
                 "holdout": (holdout_start, None)},
        passive_windows={"discovery": (None, holdout_start), "since_2015": (POST_PERIOD_START, None)},
        ci_windows=("discovery",), turnover_span=yardstick.span(frames, symbols, end=holdout_start),
        oos_start=holdout_start,
        veto_cost=yardstick.veto_cost_bps(context.get("measured"), yardstick.classes_of(symbols)),
        cells=context.get("cells"), irx=context.get("irx"), portfolio=evaluation.get("portfolio"),
        cache=context.setdefault("cache", {}))


def summarize_candidate(candidate: Mapping, evaluation: dict, holdout_start: pd.Timestamp,
                        context: Optional[dict] = None) -> dict:
    trades = evaluation["trades"]
    score = yardstick_scores(candidate, evaluation, holdout_start, context)
    t0 = pd.to_datetime(trades["t0"])
    discovery = trades.loc[t0 < holdout_start]
    post = trades.loc[(t0 >= pd.Timestamp(POST_PERIOD_START)) & (t0 < holdout_start)]
    holdout = trades.loc[t0 >= holdout_start]
    portfolio = evaluation["portfolio"]
    portfolio = portfolio.loc[portfolio.index < holdout_start]
    moments = return_moments(portfolio.to_numpy()) if len(portfolio) > 1 else {
        "sharpe_per_period": 0.0, "observations": 0, "skewness": 0.0, "kurtosis": 3.0}
    return {
        "id": candidate["id"], "family": candidate["family"], "universe": candidate["universe"],
        "rule": candidate["rule"], "description": rules.describe_rule(candidate["rule"]),
        "stop_loss_pct": candidate["stop_loss_pct"], "take_profit_pct": candidate["take_profit_pct"],
        "max_holding_sessions": candidate["max_holding_sessions"], "symbols": candidate["symbols"],
        "discovery": _stats(discovery), "post_period": trade_statistics(post["net_return"].to_numpy()),
        "holdout": trade_statistics(holdout["net_return"].to_numpy()),
        "sharpe_per_period": float(moments["sharpe_per_period"]),
        "sharpe_annualized": round(float(moments["sharpe_per_period"]) * math.sqrt(252), 4),
        "observations": int(moments["observations"]), "skewness": float(moments["skewness"]),
        "kurtosis": float(moments["kurtosis"]), "hash": rule_mutation.candidate_hash(candidate),
        **({"lineage": candidate["lineage"]} if candidate.get("lineage") else {}),
        **({"yardstick": score} if score is not None else {}),
    }


def gate(summary: dict, *, trials: int, trial_sharpes: list[float], bar: Gate = GATE, family_trials: int = 0) -> dict:
    """The fixed pass/fail bar; every check is recorded, not just the verdict.

    ``family_trials`` are the candidate's family's trials made outside the
    library (experiment engine and hand-run research, ``yardstick
    .trial_families``); they are added to the library's cumulative count, so
    the deflated Sharpe can only get stricter.
    """
    library_trials = int(trials)
    trials = library_trials + max(int(family_trials or 0), 0)
    dsr = deflated_sharpe_ratio(summary["sharpe_per_period"], summary["observations"], trials=trials,
                                trial_sharpes=trial_sharpes, skewness=summary["skewness"],
                                kurtosis=summary["kurtosis"])
    net = summary["discovery"]["net"]
    excess = summary["discovery"]["excess_over_drift"]
    post, holdout = summary["post_period"], summary["holdout"]
    checks = {
        "enough_trades": net["trades"] >= bar.min_trades,
        "net_expectancy_ci_low_gt_0": bool(net.get("expectancy_ci_bps") and net["expectancy_ci_bps"][0] > 0),
        "excess_over_drift_ci_low_gt_0": bool(excess.get("expectancy_ci_bps") and excess["expectancy_ci_bps"][0] > 0),
        "post_period_positive": post["trades"] >= bar.min_post_period_trades and (post["expectancy_bps"] or 0) > 0,
        "deflated_sharpe": dsr["deflated_sharpe_ratio"] is not None
        and dsr["deflated_sharpe_ratio"] >= bar.min_deflated_sharpe,
        "holdout_positive": holdout["trades"] >= bar.min_holdout_trades and (holdout["expectancy_bps"] or 0) > 0,
    }
    proven = all(checks.values())
    explore = all(value for key, value in checks.items() if key in EXPLORE_CHECKS)
    return {"passed": proven, "tier": "proven" if proven else "explore" if explore else "rejected",
            "checks": checks, "deflated_sharpe": dsr["deflated_sharpe_ratio"], "dsr_trials": trials,
            "library_trials": library_trials, "family_trials": max(int(family_trials or 0), 0)}


def rank_key(row: dict) -> tuple:
    excess = row["discovery"]["excess_over_drift"].get("expectancy_ci_bps") or [-1e9, -1e9]
    return (TIER_ORDER[row["gate"]["tier"]], excess[0], row["gate"]["deflated_sharpe"] or 0.0)


def apply_gate(result: dict, *, trials: int, family_extra: Optional[Mapping[str, int]] = None) -> dict:
    """(Re)gate every candidate in ``result`` against ``trials`` and re-rank; the trial Sharpes are this run's.

    ``family_extra`` (family -> trials outside the library) is kept on the
    result so a later re-gate charges the same families.
    """
    if family_extra is not None:
        result["family_extra"] = dict(family_extra)
    extra = result.get("family_extra") or {}
    summaries = result["candidates"]
    sharpes = [row["sharpe_per_period"] for row in summaries if row["observations"] > 1]
    for row in summaries:
        row["gate"] = gate(row, trials=trials, trial_sharpes=sharpes, family_trials=int(extra.get(row["family"], 0)))
    summaries.sort(key=rank_key, reverse=True)
    result.update({"trials": trials, "proven": [row for row in summaries if row["gate"]["tier"] == "proven"],
                   "explore": [row for row in summaries if row["gate"]["tier"] == "explore"]})
    return result


def run_discovery(frames: Mapping[str, pd.DataFrame], symbols: Iterable[str], *,
                  data_end: Optional[str] = None, library: Optional[list[dict]] = None, universe=None,
                  caches: Optional[dict] = None, trials: Optional[int] = None, context: Optional[dict] = None,
                  family_extra: Optional[Mapping[str, int]] = None) -> dict:
    """Evaluate and gate ``library`` (default: the fixed library); ``trials`` defaults to its size.

    ``context`` (frames, SPY, T-bills, calibration cells, measured costs)
    turns on the yardstick readings per candidate; ``family_extra`` charges
    each family's trials outside the library to its deflated Sharpe.
    """
    library = library if library is not None else candidate_library(symbols)
    end = pd.Timestamp(data_end) if data_end else max(pd.Timestamp(str(f["date"].iloc[-1])[:10]) for f in frames.values())
    holdout_start = end - pd.DateOffset(years=HOLDOUT_YEARS)
    caches = caches if caches is not None else {}
    summaries = [summarize_candidate(candidate, evaluate_candidate(candidate, frames, caches, universe=universe),
                                     holdout_start, context) for candidate in library]
    result = {"data_end": str(end.date()), "holdout_start": str(holdout_start.date()), "gate": GATE.__dict__,
              "candidates": summaries}
    return apply_gate(result, trials=max(len(library), trials or 0), family_extra=family_extra)


# ---------------------------------------------------------------- evolution

def failed_checks(row: Mapping) -> list[str]:
    return [key for key, value in row["gate"]["checks"].items() if not value]


def net_ci_low(row: Mapping) -> float:
    ci = row["discovery"]["net"].get("expectancy_ci_bps")
    return float(ci[0]) if ci else -1e9


def near_miss(row: Mapping) -> bool:
    """Paper-executable and at most one failed check: a parent, and a child worth keeping."""
    return paper_executable(row) and len(failed_checks(row)) <= 1


def select_parents(candidates: Iterable[Mapping], k: int = MUTATION_PARENTS) -> list[dict]:
    """Top ``k`` near-misses by the lower bound of their net expectancy, plus every explore / proven survivor."""
    rows = [row for row in candidates if paper_executable(row)]
    near = sorted((row for row in rows if near_miss(row)), key=net_ci_low, reverse=True)[:k]
    survivors = [row for row in rows if row["gate"]["tier"] in ("explore", "proven")]
    out, seen = [], set()
    for row in [*near, *survivors]:
        if row["id"] not in seen:
            seen.add(row["id"])
            out.append(row)
    return out


def improves_on(child: Mapping, parent: Mapping) -> bool:
    """A higher tier, or the same tier with a higher net-expectancy lower bound."""
    tiers = (TIER_ORDER[child["gate"]["tier"]], TIER_ORDER[parent["gate"]["tier"]])
    return tiers[0] > tiers[1] or (tiers[0] == tiers[1] and net_ci_low(child) > net_ci_low(parent))


def candidate_spec(row: Mapping) -> dict:
    """What a later run needs to evaluate the candidate again (no statistics)."""
    return {key: row[key] for key in ("id", "family", "rule", "universe", "stop_loss_pct", "take_profit_pct",
                                      "max_holding_sessions", "lineage")}


def evolve_library(result: dict, frames: Mapping[str, pd.DataFrame], *, seen_hashes: Iterable[str], seed: int,
                   groups: Mapping[str, Sequence[str]], universe=None, caches: Optional[dict] = None,
                   parents: int = MUTATION_PARENTS, children: int = MUTATION_CHILDREN,
                   budget_seconds: float = 300.0, clock=time.monotonic, context: Optional[dict] = None) -> dict:
    """One evolution step: breed children of this run's parents, evaluate them inside the budget, add them to ``result``.

    Children are gated against the current ``result["trials"]`` here so the
    improvement check has a verdict; the caller re-gates everything against
    the cumulative trial count afterwards.  Returns the step's record.
    """
    started = clock()
    parent_rows = select_parents(result["candidates"], parents)
    by_id = {row["id"]: row for row in parent_rows}
    seen = set(seen_hashes) | {row["hash"] for row in result["candidates"]}
    offspring = rule_mutation.evolve(parent_rows, count=children, seed=seed, seen=seen, groups=groups)
    holdout_start = pd.Timestamp(result["holdout_start"])
    caches = caches if caches is not None else {}
    sharpes = [row["sharpe_per_period"] for row in result["candidates"] if row["observations"] > 1]
    evaluated, budget_exhausted = [], False
    for child in offspring:
        if clock() - started > budget_seconds:
            budget_exhausted = True
            break
        row = summarize_candidate(child, evaluate_candidate(child, frames, caches, universe=universe), holdout_start,
                                  context)
        row["gate"] = gate(row, trials=result["trials"] + len(offspring), trial_sharpes=sharpes,
                           family_trials=int((result.get("family_extra") or {}).get(row["family"], 0)))
        row["lineage"] = dict(child["lineage"], improved_on_parent=improves_on(row, by_id[child["lineage"]["parent_id"]]))
        evaluated.append(row)
    result["candidates"].extend(evaluated)
    improved = [row for row in evaluated if row["lineage"]["improved_on_parent"]]
    best = max(evaluated, key=rank_key, default=None)
    return {"status": "complete", "seed": int(seed), "parents": [row["id"] for row in parent_rows],
            "children_generated": len(offspring), "children_evaluated": len(evaluated),
            "children_improved": len(improved), "improved": bool(improved),
            "budget_seconds": budget_seconds, "elapsed_seconds": round(clock() - started, 1),
            "budget_exhausted": budget_exhausted,
            "operators": {name: sum(1 for row in evaluated if row["lineage"]["operator"] == name)
                          for name in rule_mutation.OPERATORS},
            "best_child": None if best is None else {
                "id": best["id"], "parent_id": best["lineage"]["parent_id"], "operator": best["lineage"]["operator"],
                "tier": best["gate"]["tier"], "description": best["description"], "universe": best["universe"],
                "net_ci_low_bps": net_ci_low(best), "parent_net_ci_low_bps": net_ci_low(by_id[best["lineage"]["parent_id"]]),
                "parent_tier": by_id[best["lineage"]["parent_id"]]["gate"]["tier"]}}


def load_trials(db: Session) -> dict:
    """The latest ``discovery_trials`` row: every hash ever evaluated and the retained near-miss children."""
    from app.models import AuditLog
    from sqlalchemy import select

    row = db.scalars(select(AuditLog).where(AuditLog.event_type == TRIALS_EVENT, AuditLog.action == "run")
                     .order_by(AuditLog.id.desc()).limit(1)).first()
    payload = (row.payload if row is not None else None) or {}
    return {"hashes": set(payload.get("hashes") or []), "retained": list(payload.get("retained") or []),
            "runs": int(payload.get("runs") or 0), "hash_families": dict(payload.get("hash_families") or {})}


def latest_trials_payload(db: Session) -> dict:
    """The latest ``discovery_trials`` payload (hashes, their families, retained children); empty before the first run."""
    from app.models import AuditLog
    from sqlalchemy import select

    row = db.scalars(select(AuditLog).where(AuditLog.event_type == TRIALS_EVENT, AuditLog.action == "run")
                     .order_by(AuditLog.id.desc()).limit(1)).first()
    return dict((row.payload if row is not None else None) or {})


def last_run_mutation(db: Session) -> dict:
    from app.models import AuditLog
    from sqlalchemy import select

    row = db.scalars(select(AuditLog).where(AuditLog.event_type == "strategy_discovery", AuditLog.action == "run")
                     .order_by(AuditLog.id.desc()).limit(1)).first()
    return dict(((row.payload or {}).get("mutation") or {})) if row is not None else {}


def retained_children(result: dict, *, limit: int = RETAINED_MAX) -> list[dict]:
    """Children (this run's and earlier runs') that are near-misses now, best first, capped."""
    rows = [row for row in result["candidates"] if row.get("lineage") and near_miss(row)]
    rows.sort(key=rank_key, reverse=True)
    return [candidate_spec(row) for row in rows[:limit]]


def record_trials(db: Session, *, hashes: Iterable[str], retained: list[dict], runs: int, data_end: str,
                  hash_families: Optional[Mapping[str, str]] = None) -> None:
    """One ``discovery_trials`` row; ``hash_families`` maps each hash to its family (trials per family)."""
    from app.services.audit import write_audit_log

    hashes = sorted(set(hashes))
    kept = set(hashes)
    families = {digest: family for digest, family in (hash_families or {}).items() if digest in kept}
    by_family: dict[str, int] = {}
    for digest in hashes:
        family = families.get(digest) or "unattributed"
        by_family[family] = by_family.get(family, 0) + 1
    write_audit_log(db, event_type=TRIALS_EVENT, action="run", status="complete",
                    message=f"discovery trials: {len(hashes)} rules ever evaluated, {len(retained)} children retained",
                    entity_type="strategy_discovery",
                    payload={"hashes": hashes, "count": len(hashes), "retained": retained, "runs": runs,
                             "data_end": data_end, "hash_families": families, "families": by_family,
                             "paper_only": True})


# ---------------------------------------------------------------- admission

def strategy_slug(candidate_id_: str) -> str:
    return f"{RULE_SLUG_PREFIX}{candidate_id_}"


def strategy_parameters(row: dict, *, now: datetime) -> dict:
    return {
        "rule": row["rule"], "stop_loss_pct": row["stop_loss_pct"], "take_profit_pct": row["take_profit_pct"],
        "max_holding_sessions": row["max_holding_sessions"], "symbol_scope": list(row["symbols"]),
        "discovery": {"candidate_id": row["id"], "family": row["family"], "universe": row["universe"],
                      "evaluated_symbols": row.get("evaluated_symbols", len(row["symbols"])),
                      "evaluated_at": now.isoformat(), "gate": row["gate"], "discovery": row["discovery"],
                      "post_period_expectancy_bps": row["post_period"]["expectancy_bps"],
                      "holdout": {k: row["holdout"].get(k) for k in ("trades", "win_rate", "expectancy_bps")},
                      "lineage": row.get("lineage")},
    }


def paper_executable(row: dict) -> bool:
    return rules.timing(row["rule"]) != "open"


def admissible(row: dict, *, require_proven: bool) -> bool:
    if not paper_executable(row):
        return False
    return row["gate"]["tier"] == "proven" or (not require_proven and row["gate"]["tier"] == "explore")


def short_hold_vetoed(row: Mapping) -> Optional[str]:
    """The yardstick's short-hold veto reason for a candidate (``yardstick.VETO_RULE``), or None."""
    veto = (row.get("yardstick") or {}).get("short_hold_veto") or {}
    return veto.get("reason") if veto.get("vetoed") else None


def admit_survivors(db: Session, result: dict, *, now: Optional[datetime] = None, max_new: int = 3,
                    max_live: int = 10, require_proven: bool = False) -> dict:
    """Create ``rule_<id>`` paper candidates for new admissible rows; retire candidates that stopped qualifying.

    With ``yardstick_short_hold_veto`` on, a new admission must also clear the
    short-hold veto (``short_hold_vetoed``); a strategy already admitted is
    still governed by the gate and its forward evidence, so the veto never
    retires one (it is reported in ``vetoed``).
    """
    from app.core.config import settings
    from app.models import Strategy
    from app.services.audit import write_audit_log
    from app.services.forward_experiments import is_registered_row

    now = now or datetime.now(UTC)
    by_id = {row["id"]: row for row in result["candidates"]}
    existing = {row.strategy_type: row for row in
                db.query(Strategy).filter(Strategy.strategy_type.like(f"{RULE_SLUG_PREFIX}%")).all()}
    live = sum(1 for row in existing.values() if row.current_status in LIVE_STATUSES)
    admitted, retired, refreshed, vetoed = [], [], [], []
    for slug, strategy in existing.items():
        row = by_id.get(slug[len(RULE_SLUG_PREFIX):])
        if row is None:
            continue
        evidence = dict(strategy.parameters or {})
        evidence["discovery"] = strategy_parameters(row, now=now)["discovery"]
        strategy.parameters = evidence
        refreshed.append(slug)
        # Only an unpromoted candidate is retired here; a promoted strategy is
        # governed by its forward-trial evidence, not by the backtest.  A row
        # registered as a frozen forward experiment (forward_experiments.py)
        # is governed by its pre-registered review alone and never retired by
        # a re-grade.
        if (not admissible(row, require_proven=require_proven) and strategy.current_status == DISCOVERED_STATUS
                and not is_registered_row(strategy.parameters)):
            strategy.current_status = "retired"
            live -= 1
            retired.append(slug)
            write_audit_log(db, event_type="strategy_discovery", action="retire", status="complete",
                            message=f"{slug} no longer passes the discovery gate", entity_type="strategy",
                            entity_id=strategy.id, payload={"slug": slug, "checks": row["gate"]["checks"],
                                                            "paper_only": True})
    for row in result["candidates"]:
        if not admissible(row, require_proven=require_proven):
            continue
        slug = strategy_slug(row["id"])
        veto = short_hold_vetoed(row)
        if veto is not None:
            enforced = bool(settings.yardstick_short_hold_veto) and slug not in existing
            vetoed.append({"slug": slug, "existing": slug in existing, "reason": veto, "enforced": enforced})
            if enforced:
                continue
        if slug in existing:
            continue
        if len(admitted) >= max_new or live >= max_live:
            break
        strategy = Strategy(name=f"Discovered {row['family']} ({row['universe']}) {row['id']}"[:128],
                            strategy_type=slug, description=f"{row['description']}. Found by strategy discovery.",
                            parameters=strategy_parameters(row, now=now), is_active=True,
                            current_status=DISCOVERED_STATUS)
        db.add(strategy)
        db.flush()
        live += 1
        admitted.append(slug)
        write_audit_log(db, event_type="strategy_discovery", action="admit", status="complete",
                        message=f"{slug} ({row['gate']['tier']}): {row['description']}", entity_type="strategy",
                        entity_id=strategy.id,
                        payload={"slug": slug, "gate": row["gate"], "lineage": row.get("lineage"), "paper_only": True})
    db.flush()
    return {"admitted": admitted, "retired": retired, "refreshed": refreshed, "live": live, "vetoed": vetoed}


# ---------------------------------------------------------------- scheduled run

def load_frames(symbols: Iterable[str], *, loader=None, start: str, end: Optional[str] = None,
                earnings_loader=None, news_loader=None, sector_close: bool = True,
                reasons: Optional[dict] = None) -> tuple[dict, list]:
    """Adjusted daily bars per symbol (shared cache with the meta-label retrainer).

    Single stocks also get the ``earn_surprise`` column and the
    ``news_tone`` / ``news_articles`` columns (``news_history`` cache); a
    missing earnings or news history leaves them empty rather than dropping
    the symbol.  With ``sector_close`` (default) they also get the
    ``sector_close`` column of the sector-relative features
    (``sector_relative.attach_sector_closes``: the static map for today's
    names, the statistical one for past members), when the sector ETFs are
    among ``symbols``.  ``reasons`` (a dict the caller passes) is filled
    with why each missing symbol was dropped: the load error, or how few bars
    it has since ``start`` (that case used to be silent).
    """
    from app.services.meta_label_learning import history_dir
    from app.services.strategy_scorecard import load_daily_history, normalize_prices

    end = end or datetime.now(UTC).date().isoformat()
    frames, missing = {}, []
    for symbol in symbols:
        try:
            raw = loader(symbol) if loader else load_daily_history(symbol, history_dir(), download=True)
            frame = normalize_prices(raw, start=start, end=end)
        except Exception as exc:  # one bad symbol must not stop the run
            logger.warning("discovery history unavailable for %s: %s", symbol, exc)
            missing.append(symbol)
            if reasons is not None:
                reasons[symbol] = f"{exc.__class__.__name__}: {exc}"[:160]
            continue
        if symbol not in ETF_SYMBOLS:
            try:
                events = earnings_loader(symbol) if earnings_loader else earnings_history.load_surprises(
                    symbol, history_dir(), download=True)
            except Exception as exc:
                logger.warning("discovery earnings history unavailable for %s: %s", symbol, exc)
                events = None
            frame = earnings_history.attach_surprises(frame, events)
            try:
                tone = news_loader(symbol) if news_loader else news_history.tone_series(symbol)
            except Exception as exc:
                logger.warning("discovery news history unavailable for %s: %s", symbol, exc)
                tone = None
            frame = news_history.attach_tone(frame, tone)
        if len(frame) > rules.MAX_RULE_LOOKBACK:
            frames[symbol] = frame
        else:
            missing.append(symbol)
            if reasons is not None:
                reasons[symbol] = f"only {len(frame)} bars since {start} (needs more than {rules.MAX_RULE_LOOKBACK})"
    if sector_close:
        from app.services.sector_relative import attach_sector_closes

        frames, _method = attach_sector_closes(frames)
    return frames, missing


def report_rows(rows: list[dict], limit: int = 20) -> list[dict]:
    from app.services.yardstick import compact

    out = []
    for row in rows[:limit]:
        net, excess = row["discovery"]["net"], row["discovery"]["excess_over_drift"]
        out.append({"id": row["id"], "family": row["family"], "universe": row["universe"],
                    "description": row["description"], "tier": row["gate"]["tier"],
                    "paper_executable": paper_executable(row),
                    "failed_checks": [k for k, v in row["gate"]["checks"].items() if not v],
                    "trades": net["trades"], "win_rate": net["win_rate"], "expectancy_bps": net["expectancy_bps"],
                    "expectancy_ci_bps": net.get("expectancy_ci_bps"),
                    "excess_ci_bps": excess.get("expectancy_ci_bps"),
                    "post_period_expectancy_bps": row["post_period"]["expectancy_bps"],
                    "holdout_trades": row["holdout"]["trades"], "holdout_expectancy_bps": row["holdout"]["expectancy_bps"],
                    "sharpe_annualized": row["sharpe_annualized"], "deflated_sharpe": row["gate"]["deflated_sharpe"],
                    "dsr_trials": row["gate"].get("dsr_trials"), "family_trials": row["gate"].get("family_trials"),
                    "lineage": row.get("lineage"),
                    **({"yardstick": {"discovery": compact(row["yardstick"], "discovery"),
                                      "holdout_net_bps": compact(row["yardstick"], "holdout", "since_2015")["net_bps"],
                                      "since_2015": compact(row["yardstick"], "post_period", "since_2015")}}
                       if row.get("yardstick") else {})})
    return out


def discovery_context(frames: Mapping[str, pd.DataFrame], db: Optional[Session] = None, *, irx=None,
                      download: bool = True) -> dict:
    """What the yardstick needs per candidate: frames, SPY, T-bills, calibrated cells, measured round trips."""
    from app.services import yardstick

    return {"frames": frames, "spy": frames.get("SPY"),
            "irx": irx if irx is not None else yardstick.load_irx(download=download),
            "cells": yardstick.current_cells(db) if db is not None else [],
            "measured": yardstick.measured_costs(db) if db is not None else {}, "cache": {}}


def evolution_step(db: Session, result: dict, frames: Mapping[str, pd.DataFrame], *, groups: Mapping,
                   seen_hashes: Iterable[str], universe=None, caches: Optional[dict] = None, clock=time.monotonic,
                   context: Optional[dict] = None) -> dict:
    """The scheduled run's mutation step: settings, the stale-run pause, then ``evolve_library``.

    ``stale_runs`` counts consecutive runs without a child better than its
    parent; at ``MUTATION_STALE_RUNS`` the next run skips mutation and the
    count starts again, so a dead search costs one run in four.
    """
    from app.core.config import settings

    previous = last_run_mutation(db)
    stale = int(previous.get("stale_runs") or 0)
    if not settings.strategy_discovery_mutation_enabled:
        return {"status": "disabled", "stale_runs": stale}
    if stale >= MUTATION_STALE_RUNS:
        return {"status": "skipped", "reason": f"no child better than its parent for {stale} runs", "stale_runs": 0}
    seen = set(seen_hashes)
    from app.services.experiment_engine import release_read_transaction

    release_read_transaction(db)  # the search runs for its whole time budget
    seed = int(hashlib.sha256(f"{result['data_end']}:{len(seen)}".encode()).hexdigest()[:8], 16)
    step = evolve_library(result, frames, seen_hashes=seen, seed=seed, groups=groups, universe=universe,
                          caches=caches, budget_seconds=float(settings.strategy_discovery_mutation_budget_seconds),
                          clock=clock, context=context)
    step["stale_runs"] = 0 if step["improved"] else stale + 1
    return step


def run_strategy_discovery(db: Session, *, now: Optional[datetime] = None, loader=None,
                           earnings_loader=None) -> dict:
    """Evaluate the library on fresh history and admit/retire paper candidates (paper only)."""
    from app.core.config import settings
    from app.services.audit import write_audit_log

    now = now or datetime.now(UTC)
    base = {"paper_only": True, "live_authorized": False}
    if not settings.strategy_discovery_enabled:
        return base | {"status": "disabled"}
    injected = loader is not None   # a caller-supplied loader means no network for the T-bill history either
    symbols = sorted({s.upper() for s in settings.paper_execution_symbols})
    tradeable = set(symbols)
    universe, universe_summary = None, {"mode": "today", "symbols": len(symbols)}
    point_in_time = settings.strategy_discovery_point_in_time
    if point_in_time is None:
        point_in_time = settings.replay_point_in_time_universe
    if point_in_time:
        from app.services import point_in_time_universe

        try:  # a missing membership file falls back to today's list, never stops the run
            context = point_in_time_universe.replay_universe(symbols, loader=loader, earnings_loader=earnings_loader,
                                                             now=now)
            symbols, universe, loader, earnings_loader = (context.symbols, context.mask, context.loader,
                                                          context.earnings_loader)
            universe_summary = context.summary()
        except Exception as exc:
            logger.warning("strategy discovery: point-in-time universe unavailable, using today's list: %s", exc)
            universe_summary["fallback"] = f"{exc.__class__.__name__}: {exc}"
    frames, missing = load_frames(symbols, loader=loader, start=settings.strategy_discovery_history_start,
                                  earnings_loader=earnings_loader)
    if not frames:
        return base | {"status": "blocked", "reason": "no price history", "missing_symbols": missing}
    trials = load_trials(db)
    groups = {name: list(members) for name, members in universes(frames.keys()).items() if members}
    library = candidate_library(frames.keys())
    known = {rule_mutation.candidate_hash(candidate) for candidate in library}
    for spec in trials["retained"]:  # near-miss children of earlier runs: re-evaluated, not re-charged
        if spec.get("universe") in groups and rule_mutation.candidate_hash(spec) not in known:
            known.add(rule_mutation.candidate_hash(spec))
            library.append({**spec, "symbols": list(groups[spec["universe"]]),
                            "hypothesis": f"Retained child of {spec['lineage']['parent_id']}"})
    caches: dict = {}
    from app.services import yardstick
    from app.services.experiment_engine import release_read_transaction

    family_extra = yardstick.external_family_trials(db, exclude="discovery")
    context = discovery_context(frames, db, download=not injected)
    # The replays and the mutation search run for minutes: do not hold the
    # read transaction open through them (see release_read_transaction).
    release_read_transaction(db)
    result = run_discovery(frames, frames.keys(), universe=universe, library=library, caches=caches,
                           trials=len(trials["hashes"] | known), context=context, family_extra=family_extra)
    mutation = evolution_step(db, result, frames, groups=groups, universe=universe, caches=caches,
                              seen_hashes=trials["hashes"] | known, context=context)
    ever = trials["hashes"] | known | {row["hash"] for row in result["candidates"]}
    apply_gate(result, trials=len(ever))
    mutation["trials_cumulative"] = len(ever)
    hash_families = dict(trials.get("hash_families") or {})
    hash_families.update({rule_mutation.candidate_hash(candidate): candidate["family"] for candidate in library})
    hash_families.update({row["hash"]: row["family"] for row in result["candidates"]})
    record_trials(db, hashes=ever, retained=retained_children(result), runs=trials["runs"] + 1,
                  data_end=result["data_end"], hash_families=hash_families)
    if universe is not None:
        # The candidate was judged on every past member; the paper row it may
        # become can only trade today's list, so its scope is that subset.
        for row in result["candidates"]:
            row["evaluated_symbols"] = len(row["symbols"])
            row["symbols"] = [s for s in row["symbols"] if s in tradeable]
    admission = {"admitted": [], "retired": [], "refreshed": [], "live": None, "vetoed": []}
    if settings.strategy_discovery_admission_enabled:
        admission = admit_survivors(db, result, now=now, max_new=settings.strategy_discovery_max_new,
                                    max_live=settings.strategy_discovery_max_live,
                                    require_proven=settings.strategy_discovery_require_proven)
    summary = {"data_end": result["data_end"], "holdout_start": result["holdout_start"], "trials": result["trials"],
               "library": len(library), "proven": len(result["proven"]), "explore": len(result["explore"]),
               "missing_symbols": missing, "universe": universe_summary, "mutation": mutation,
               "require_proven": settings.strategy_discovery_require_proven, **admission,
               "yardstick": {"short_hold_veto": yardstick.VETO_RULE,
                             "short_hold_veto_enabled": bool(settings.yardstick_short_hold_veto),
                             "vetoed_candidates": sum(1 for row in result["candidates"] if short_hold_vetoed(row)),
                             "excess_passes": sum(1 for row in result["candidates"]
                                                  if ((((row.get("yardstick") or {}).get("passive") or {})
                                                       .get("windows") or {}).get("discovery") or {}).get("passes")),
                             "family_extra_trials": family_extra,
                             "tbill": "available" if context.get("irx") is not None else "unavailable"},
               "top": report_rows(result["candidates"], 10)}
    write_audit_log(db, event_type="strategy_discovery", action="run", status="complete",
                    message=(f"strategy discovery: {len(library)} candidates, {mutation.get('children_evaluated', 0)} "
                             f"children, {result['trials']} trials ever, {len(result['proven'])} proven, "
                             f"{len(result['explore'])} explore, admitted {len(admission['admitted'])}, "
                             f"retired {len(admission['retired'])}"),
                    entity_type="strategy_discovery", payload=json.loads(json.dumps(summary, default=str)))
    return base | {"status": "complete", **summary}
