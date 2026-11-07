"""Replay the paper *book*: every strategy's trades competing for the live position slots.

``gate_replay`` replays one strategy at a time; some protections act on the
whole book (which trades get a slot, how many new entries a session may add).
This is the book simulation of ``scripts/run_slot_allocation_backtest.py``
(``reports/slot-allocation-20261106.md``), moved into a service so the
experiment engine can run book-level experiments itself.  Nothing here places
or blocks an order.

* ``candidates``: every executable trade of every replayable strategy under
  its own setup (``gate_replay.simulate``), with its decision-day confidence
  and the live cycle order (execution symbol list, then strategy).
* ``allocate``: walks the days, fills slots under the live caps (one position
  per symbol, <= 5 per strategy, <= 3 per sector, <= 20 total), the stop-out
  cooldown and, when ``max_new_per_day`` is set, at most that many new
  entries per session (the ``max_new_entries_per_session`` protection).
* ``metrics`` / ``book_rows``: per-trade statistics and the book's equity
  curve with every position at ``1 / total cap`` of equity, in the shape the
  engine's pre-registered rule reads (``trades``, ``expectancy_bps``,
  ``total_profit_bps``, ``portfolio_max_drawdown``).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, Mapping, Optional

import numpy as np
import pandas as pd

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS
from app.services import gate_replay
from app.services import meta_label_primary as primary
from app.services.strategy_discovery import _marked_returns
from app.services.strategy_scorecard import trade_statistics
from app.services.trading_universe import sector_group

WINDOWS = gate_replay.WINDOWS
BASE_CAPS = {"total": 20, "strategy": 5, "sector": 3}
COLUMNS = ("symbol", "strategy", "entry_idx", "exit_idx", "entry_date", "exit_date", "decision_date", "barrier",
           "net_return", "confidence", "live_rank")


def _confidence(strategy: str, frames: Mapping, replays: Mapping, trades: pd.DataFrame) -> list[float]:
    out = []
    for symbol, index in zip(trades["symbol"], trades["event_idx"]):
        signal = primary.ibs_mean_reversion(frames[symbol], entry_threshold=0.1) \
            if strategy == "ibs_mean_reversion" else replays[symbol][strategy]
        out.append(float(signal["confidence"].iloc[int(index)]))
    return out


def candidates(frames: Mapping[str, pd.DataFrame], spy: Optional[pd.DataFrame], replays: Mapping[str, dict],
               setups: Mapping[str, Mapping], *, simulate: Callable = gate_replay.simulate,
               strategies: Optional[Mapping[str, Mapping]] = None, **extra) -> pd.DataFrame:
    """Every executable trade per strategy under its setup, with confidence and the live cycle order.

    ``setups`` maps strategy -> setup (``gate_replay`` keys); ``extra`` goes to
    ``simulate`` (``vix``, ``universe``, ``breadth`` ...).  Strategies that
    cannot be replayed are skipped.
    """
    out = []
    for strategy, setup in setups.items():
        if not gate_replay.replayable(strategy):
            continue
        trades = simulate(strategy, frames, spy, dict(setup or {}), replays, **extra)
        if trades is None or trades.empty:
            continue
        out.append(trades.assign(confidence=_confidence(strategy, frames, replays, trades)))
    if not out:
        return pd.DataFrame(columns=list(COLUMNS))
    rows = pd.concat(out, ignore_index=True)
    rows["entry_date"] = [frames[s]["date"].iloc[int(i)] for s, i in zip(rows["symbol"], rows["entry_idx"])]
    rows["exit_date"] = [frames[s]["date"].iloc[int(i)] for s, i in zip(rows["symbol"], rows["exit_idx"])]
    symbol_rank = {s: i for i, s in enumerate(DEFAULT_PAPER_EXECUTION_SYMBOLS)}
    strategy_rank = {s: i for i, s in enumerate(gate_replay.STRATEGIES)}
    unknown = len(symbol_rank)
    rows["live_rank"] = [symbol_rank.get(s, unknown) * 100 + strategy_rank.get(t, 99)
                         for s, t in zip(rows["symbol"], rows["strategy"])]
    return rows.sort_values(["entry_date", "live_rank", "symbol"], kind="stable").reset_index(drop=True)


def _track_records(rows: pd.DataFrame) -> dict:
    """strategy -> (sorted exit dates, cumulative sums) for a causal mean before any day."""
    out = {}
    for strategy, group in rows.groupby("strategy"):
        group = group.sort_values("exit_date")
        out[strategy] = (group["exit_date"].to_numpy(), np.cumsum(group["net_return"].to_numpy()))
    return out


def _mean_before(records: dict, strategy: str, day) -> float:
    dates, sums = records.get(strategy, (np.array([]), np.array([])))
    n = int(np.searchsorted(dates, day, side="left"))  # trades closed strictly before ``day``
    return float(sums[n - 1] / n) if n else 0.0


def allocate(rows: pd.DataFrame, frames: Mapping[str, pd.DataFrame], policy: Mapping) -> pd.DataFrame:
    """The trades the book accepts under ``policy`` (``caps``, ``order``, ``stop_cooldown``, ``max_new_per_day``)."""
    caps = policy.get("caps", BASE_CAPS)
    order = policy.get("order", "live")
    cooldown = int(policy.get("stop_cooldown", 0) or 0)
    max_new = int(policy.get("max_new_per_day", 0) or 0)
    if rows is None or rows.empty:
        return rows if rows is not None else pd.DataFrame(columns=list(COLUMNS))
    records = _track_records(rows) if order == "track_record" else {}
    session_index = {s: {d: i for i, d in enumerate(f["date"])} for s, f in frames.items()}
    blocked_until: dict[str, int] = {}
    open_rows: list[dict] = []
    keep = pd.Series(False, index=rows.index)
    for day, group in rows.groupby("entry_date", sort=True):
        if order == "confidence":
            group = group.sort_values(["confidence", "live_rank"], ascending=[False, True], kind="stable")
        elif order == "track_record":
            score = group["strategy"].map(lambda s: _mean_before(records, s, day))
            group = group.assign(_score=score).sort_values(["_score", "live_rank"], ascending=[False, True],
                                                           kind="stable")
        closing = [o for o in open_rows if o["exit_date"] <= day]
        for o in closing:
            if cooldown and o["stopped"] and o["exit_date"] in session_index.get(o["symbol"], {}):
                blocked_until[o["symbol"]] = session_index[o["symbol"]][o["exit_date"]] + cooldown
        open_rows = [o for o in open_rows if o["exit_date"] > day]
        taken = 0
        for index, row in group.iterrows():
            if max_new and taken >= max_new:
                break
            if cooldown and session_index.get(row.symbol, {}).get(day, -1) < blocked_until.get(row.symbol, -1):
                continue
            if any(o["symbol"] == row.symbol for o in open_rows):
                continue
            sector = sector_group(row.symbol)
            if (len(open_rows) >= caps["total"]
                    or sum(o["strategy"] == row.strategy for o in open_rows) >= caps["strategy"]
                    or sum(o["sector"] == sector for o in open_rows) >= caps["sector"]):
                continue
            keep[index] = True
            taken += 1
            open_rows.append({"symbol": row.symbol, "strategy": row.strategy, "sector": sector,
                              "exit_date": row.exit_date, "stopped": str(row.barrier).startswith("stop_loss")})
    return rows.loc[keep.to_numpy()]


def metrics(accepted: pd.DataFrame, frames: Mapping[str, pd.DataFrame], start, weight: float) -> dict:
    """Per-trade statistics and the book's drawdown from ``start`` with every position at ``weight`` of equity."""
    empty = {"trades": 0, "win_rate": None, "expectancy_bps": None, "expectancy_ci_bps": None,
             "total_return": 0.0, "max_drawdown": 0.0, "total_profit_bps": 0.0, "portfolio_max_drawdown": 0.0,
             "days_with_3plus_stops": 0}
    if accepted is None or accepted.empty:
        return empty
    sub = accepted if start is None else accepted.loc[pd.to_datetime(accepted["decision_date"]) >= pd.Timestamp(start)]
    if sub.empty:
        return empty
    returns: dict = defaultdict(float)
    for row in sub.itertuples(index=False):
        close = frames[row.symbol]["close"].to_numpy(float)
        dates = frames[row.symbol]["date"].to_numpy()
        marked = _marked_returns(pd.DataFrame([{"entry_idx": row.entry_idx, "exit_idx": row.exit_idx,
                                                "entry_price": row.entry_price, "exit_price": row.exit_price}]), close)
        for index in range(int(row.entry_idx), int(row.exit_idx) + 1):
            returns[dates[index]] += weight * marked[index]
    series = pd.Series(returns).sort_index()
    equity = np.concatenate([[1.0], np.cumprod(1 + series.to_numpy())]) if len(series) else np.array([1.0])
    drawdown = float(np.max(1 - equity / np.maximum.accumulate(equity)))
    months = pd.to_datetime(sub["decision_date"]).dt.to_period("M").astype(str).tolist()
    stats = trade_statistics(sub["net_return"].to_numpy(), clusters=months)
    stops = sub.loc[sub["barrier"].astype(str).str.startswith("stop_loss")]
    expectancy = stats["expectancy_bps"]
    return {"trades": stats["trades"], "win_rate": stats["win_rate"], "expectancy_bps": expectancy,
            "expectancy_ci_bps": stats.get("expectancy_ci_bps"), "total_return": round(float(equity[-1] - 1), 4),
            "max_drawdown": round(drawdown, 4),
            "total_profit_bps": round(float(stats["trades"] * expectancy), 1) if expectancy is not None else 0.0,
            "portfolio_max_drawdown": round(drawdown, 4),
            "days_with_3plus_stops": int((stops.groupby("exit_date").size() >= 3).sum())}


def book_rows(accepted: pd.DataFrame, frames: Mapping[str, pd.DataFrame], *, weight: float) -> dict:
    """``window -> metrics`` over the engine's windows (``gate_replay.WINDOWS``)."""
    return {name: metrics(accepted, frames, start, weight) for name, start in WINDOWS.items()}


def book_policy(rules: Optional[Mapping] = None, **changes) -> dict:
    """The live book policy (caps, stop cooldown and session entry cap from the protection config), overridden by ``changes``."""
    from app.services.entry_protections import protection_config

    config = protection_config(dict(rules or {}))
    policy = {"caps": dict(BASE_CAPS), "order": "live", "stop_cooldown": int(config.stop_cooldown_sessions),
              "max_new_per_day": int(config.max_new_entries_per_session)}
    policy.update(changes)
    return policy
