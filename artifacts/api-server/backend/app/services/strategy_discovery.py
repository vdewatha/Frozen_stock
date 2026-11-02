"""Automated strategy discovery: test a library of rule candidates, admit survivors to paper.

The learning loop can select, size and filter strategies, but it can only
choose among hand-written families, and none of them has a net edge that
survives costs and selection.  This module looks for new ones:

1. ``candidate_library`` enumerates long-only daily-bar hypotheses from the
   published anomaly literature (short-term pullbacks, N-day lows, down
   streaks, turn of the month, Bollinger lows, weekly reversals, breakouts,
   gap-down recoveries, calm-market dips), each over three universes (index
   ETFs, sector ETFs, single stocks).  Every candidate is a declarative
   ``strategies.rules`` rule plus a stop / target / time stop.
2. ``evaluate_candidate`` replays it over adjusted daily history exactly as the
   backtester trades: decision on the close, fill at the next open with
   volatility-scaled costs on both sides, stop first inside a bar, gaps fill at
   the open, indicator exit at the next open, one position per symbol.
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
   qualifying is retired.

Re-running the same library on more data is not a new trial; growing the
library is, and ``trials`` grows with it.  The holdout is re-read on every
run, so it is a sanity check, not a pristine test set.  Paper only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from typing import Iterable, Mapping, Optional

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.services.backtest_statistics import deflated_sharpe_ratio, return_moments
from app.services.strategies import rules
from app.services.strategies.rules import RULE_SLUG_PREFIX
from app.services.strategy_scorecard import trade_statistics
from app.services.trading_universe import ETF_SYMBOLS
from app.services.triple_barrier import BarrierConfig, label_events

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


# ---------------------------------------------------------------- library

def _c(f: str, op: str, v: float) -> dict:
    return {"f": f, "op": op, "v": v}


TREND = [_c("dist_sma_200", ">", 0)]
FIRST_UP_CLOSE = [_c("ret_1", ">", 0)]
ABOVE_SMA5 = [_c("dist_sma_5", ">", 0)]
SHORT_EXITS = {"first_up_close": FIRST_UP_CLOSE, "close_above_sma5": ABOVE_SMA5}


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


def evaluate_candidate(candidate: Mapping, frames: Mapping[str, pd.DataFrame],
                       caches: Optional[dict] = None) -> dict:
    """Executable trades and the equal-weight daily portfolio series for one candidate."""
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
        entries = np.flatnonzero(rules.entry_signals(frame, candidate["rule"], cache))
        if not len(entries):
            continue
        labels = label_events(frame, entries, barrier, exit_signals=rules.exit_signals(frame, candidate["rule"], cache))
        if labels.empty:
            continue
        labels = _executable(labels)
        close = pd.to_numeric(frame["close"], errors="coerce").astype(float)
        drift = float(close.pct_change().mean())
        dates = pd.to_datetime(frame["date"].astype(str).str[:10]).to_numpy()
        labels = labels.assign(symbol=symbol, t0=dates[labels["event_idx"].to_numpy()],
                               excess=labels["net_return"] - labels["holding_days"].clip(lower=1) * drift)
        trades.append(labels[["symbol", "t0", "entry_idx", "exit_idx", "holding_days", "barrier", "net_return",
                              "excess", "entry_price", "exit_price"]])
        daily.append(pd.Series(_marked_returns(labels, close.to_numpy()), index=dates))
    trade_frame = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame(
        columns=["symbol", "t0", "entry_idx", "exit_idx", "holding_days", "barrier", "net_return", "excess",
                 "entry_price", "exit_price"])
    portfolio = pd.concat(daily, axis=1).mean(axis=1, skipna=True).sort_index() if daily else pd.Series(dtype=float)
    return {"trades": trade_frame, "portfolio": portfolio}


def _stats(trades: pd.DataFrame) -> dict:
    months = pd.to_datetime(trades["t0"]).dt.to_period("M").astype(str).tolist()
    net = trade_statistics(trades["net_return"].to_numpy(), clusters=months)
    excess = trade_statistics(trades["excess"].to_numpy(), clusters=months)
    return {"net": net, "excess_over_drift": {key: excess.get(key) for key in
                                               ("trades", "expectancy_bps", "expectancy_ci_bps")}}


def summarize_candidate(candidate: Mapping, evaluation: dict, holdout_start: pd.Timestamp) -> dict:
    trades = evaluation["trades"]
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
        "kurtosis": float(moments["kurtosis"]),
    }


def gate(summary: dict, *, trials: int, trial_sharpes: list[float], bar: Gate = GATE) -> dict:
    """The fixed pass/fail bar; every check is recorded, not just the verdict."""
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
            "checks": checks, "deflated_sharpe": dsr["deflated_sharpe_ratio"], "dsr_trials": trials}


def rank_key(row: dict) -> tuple:
    excess = row["discovery"]["excess_over_drift"].get("expectancy_ci_bps") or [-1e9, -1e9]
    return (TIER_ORDER[row["gate"]["tier"]], excess[0], row["gate"]["deflated_sharpe"] or 0.0)


def run_discovery(frames: Mapping[str, pd.DataFrame], symbols: Iterable[str], *,
                  data_end: Optional[str] = None, library: Optional[list[dict]] = None) -> dict:
    library = library if library is not None else candidate_library(symbols)
    end = pd.Timestamp(data_end) if data_end else max(pd.Timestamp(str(f["date"].iloc[-1])[:10]) for f in frames.values())
    holdout_start = end - pd.DateOffset(years=HOLDOUT_YEARS)
    caches: dict = {}
    summaries = [summarize_candidate(candidate, evaluate_candidate(candidate, frames, caches), holdout_start)
                 for candidate in library]
    sharpes = [row["sharpe_per_period"] for row in summaries if row["observations"] > 1]
    for row in summaries:
        row["gate"] = gate(row, trials=len(library), trial_sharpes=sharpes)
    summaries.sort(key=rank_key, reverse=True)
    return {"data_end": str(end.date()), "holdout_start": str(holdout_start.date()), "trials": len(library),
            "gate": GATE.__dict__, "proven": [row for row in summaries if row["gate"]["tier"] == "proven"],
            "explore": [row for row in summaries if row["gate"]["tier"] == "explore"],
            "candidates": summaries}


# ---------------------------------------------------------------- admission

def strategy_slug(candidate_id_: str) -> str:
    return f"{RULE_SLUG_PREFIX}{candidate_id_}"


def strategy_parameters(row: dict, *, now: datetime) -> dict:
    return {
        "rule": row["rule"], "stop_loss_pct": row["stop_loss_pct"], "take_profit_pct": row["take_profit_pct"],
        "max_holding_sessions": row["max_holding_sessions"], "symbol_scope": list(row["symbols"]),
        "discovery": {"candidate_id": row["id"], "family": row["family"], "universe": row["universe"],
                      "evaluated_at": now.isoformat(), "gate": row["gate"], "discovery": row["discovery"],
                      "post_period_expectancy_bps": row["post_period"]["expectancy_bps"],
                      "holdout": {k: row["holdout"].get(k) for k in ("trades", "win_rate", "expectancy_bps")}},
    }


def admissible(row: dict, *, require_proven: bool) -> bool:
    return row["gate"]["tier"] == "proven" or (not require_proven and row["gate"]["tier"] == "explore")


def admit_survivors(db: Session, result: dict, *, now: Optional[datetime] = None, max_new: int = 3,
                    max_live: int = 10, require_proven: bool = False) -> dict:
    """Create ``rule_<id>`` paper candidates for new admissible rows; retire candidates that stopped qualifying."""
    from app.models import Strategy
    from app.services.audit import write_audit_log

    now = now or datetime.now(UTC)
    by_id = {row["id"]: row for row in result["candidates"]}
    existing = {row.strategy_type: row for row in
                db.query(Strategy).filter(Strategy.strategy_type.like(f"{RULE_SLUG_PREFIX}%")).all()}
    live = sum(1 for row in existing.values() if row.current_status in LIVE_STATUSES)
    admitted, retired, refreshed = [], [], []
    for slug, strategy in existing.items():
        row = by_id.get(slug[len(RULE_SLUG_PREFIX):])
        if row is None:
            continue
        evidence = dict(strategy.parameters or {})
        evidence["discovery"] = strategy_parameters(row, now=now)["discovery"]
        strategy.parameters = evidence
        refreshed.append(slug)
        # Only an unpromoted candidate is retired here; a promoted strategy is
        # governed by its forward-trial evidence, not by the backtest.
        if not admissible(row, require_proven=require_proven) and strategy.current_status == DISCOVERED_STATUS:
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
                        payload={"slug": slug, "gate": row["gate"], "paper_only": True})
    db.flush()
    return {"admitted": admitted, "retired": retired, "refreshed": refreshed, "live": live}


# ---------------------------------------------------------------- scheduled run

def load_frames(symbols: Iterable[str], *, loader=None, start: str, end: Optional[str] = None) -> tuple[dict, list]:
    """Adjusted daily bars per symbol (shared cache with the meta-label retrainer)."""
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
            continue
        if len(frame) > rules.MAX_RULE_LOOKBACK:
            frames[symbol] = frame
        else:
            missing.append(symbol)
    return frames, missing


def report_rows(rows: list[dict], limit: int = 20) -> list[dict]:
    out = []
    for row in rows[:limit]:
        net, excess = row["discovery"]["net"], row["discovery"]["excess_over_drift"]
        out.append({"id": row["id"], "family": row["family"], "universe": row["universe"],
                    "description": row["description"], "tier": row["gate"]["tier"],
                    "failed_checks": [k for k, v in row["gate"]["checks"].items() if not v],
                    "trades": net["trades"], "win_rate": net["win_rate"], "expectancy_bps": net["expectancy_bps"],
                    "expectancy_ci_bps": net.get("expectancy_ci_bps"),
                    "excess_ci_bps": excess.get("expectancy_ci_bps"),
                    "post_period_expectancy_bps": row["post_period"]["expectancy_bps"],
                    "holdout_trades": row["holdout"]["trades"], "holdout_expectancy_bps": row["holdout"]["expectancy_bps"],
                    "sharpe_annualized": row["sharpe_annualized"], "deflated_sharpe": row["gate"]["deflated_sharpe"]})
    return out


def run_strategy_discovery(db: Session, *, now: Optional[datetime] = None, loader=None) -> dict:
    """Evaluate the library on fresh history and admit/retire paper candidates (paper only)."""
    from app.core.config import settings
    from app.services.audit import write_audit_log

    now = now or datetime.now(UTC)
    base = {"paper_only": True, "live_authorized": False}
    if not settings.strategy_discovery_enabled:
        return base | {"status": "disabled"}
    symbols = sorted({s.upper() for s in settings.paper_execution_symbols})
    frames, missing = load_frames(symbols, loader=loader, start=settings.strategy_discovery_history_start)
    if not frames:
        return base | {"status": "blocked", "reason": "no price history", "missing_symbols": missing}
    result = run_discovery(frames, frames.keys())
    admission = {"admitted": [], "retired": [], "refreshed": [], "live": None}
    if settings.strategy_discovery_admission_enabled:
        admission = admit_survivors(db, result, now=now, max_new=settings.strategy_discovery_max_new,
                                    max_live=settings.strategy_discovery_max_live,
                                    require_proven=settings.strategy_discovery_require_proven)
    summary = {"data_end": result["data_end"], "holdout_start": result["holdout_start"], "trials": result["trials"],
               "proven": len(result["proven"]), "explore": len(result["explore"]), "missing_symbols": missing,
               "require_proven": settings.strategy_discovery_require_proven, **admission,
               "top": report_rows(result["candidates"], 10)}
    write_audit_log(db, event_type="strategy_discovery", action="run", status="complete",
                    message=(f"strategy discovery: {result['trials']} candidates, {len(result['proven'])} proven, "
                             f"{len(result['explore'])} explore, admitted {len(admission['admitted'])}, "
                             f"retired {len(admission['retired'])}"),
                    entity_type="strategy_discovery", payload=json.loads(json.dumps(summary, default=str)))
    return base | {"status": "complete", **summary}
