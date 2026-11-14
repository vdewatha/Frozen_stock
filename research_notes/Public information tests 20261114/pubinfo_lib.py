"""Shared by the three tests: the whole-market bar cache as matrices, the fixed-hold trade, the pass rule.

The prices are PR #95's cache (``~/.paper-training/dip-rules-full-market-20261112/``): Alpaca SIP daily bars,
raw and adjusted, for every (issuer, ticker) series that survived that test's identity check and duplicate
removal. Nothing here fetches anything.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

LAST_SESSION = "2026-11-11"
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 5_000_000.0
DV_WINDOW = 20
ALIVE_SESSIONS = 10
ACTING_COST = 0.0025              # a side
LENIENT_COST = 0.00125
T_EXCESS_REQUIRED = 2.0
MIN_YEAR_TRADES = 20
TOP_SHARE = 0.01
BANKRUPTCY_DAYS_BEFORE = 365
BANKRUPTCY_DAYS_AFTER = 30


@dataclass
class Market:
    calendar: list[str]
    position: dict[str, int]
    names: list[str]                  # "CIK:TICKER"
    cik: list[str]
    ticker: list[str]
    open: np.ndarray                  # adjusted, [series, session], NaN where there is no bar
    close: np.ndarray                 # adjusted
    raw_close: np.ndarray
    eligible: np.ndarray              # price and dollar-volume floor at that close; blocked years excluded
    dollar_volume: np.ndarray         # 20-session median of raw close x raw volume
    last_bar: np.ndarray              # [series, session] -> index of the latest bar at or before it, -1 if none
    first: np.ndarray                 # first bar index per series
    last: np.ndarray                  # last bar index per series
    bars: np.ndarray                  # number of bars per series
    spy_open: np.ndarray
    spy_close: np.ndarray
    by_cik: dict[str, list[int]]
    by_ticker: dict[str, list[int]]
    bankruptcies: dict[str, list[int]]    # cik -> calendar-day ordinals of Item 1.03 filings

    @property
    def sessions(self) -> int:
        return len(self.calendar)

    def delisted_since(self, s: int) -> bool:
        return int(self.last[s]) < self.sessions - ALIVE_SESSIONS


def load_market(cache: Path) -> Market:
    identity = pd.read_csv(cache / "identity.csv.gz", dtype=str, keep_default_na=False)
    kept = identity[identity["kept"] == "True"].set_index("series")
    series = pd.read_csv(cache / "series.csv", dtype=str).set_index("series")
    spy = pd.read_pickle(cache / "spy.pkl.gz")
    spy = spy[spy["date"] <= LAST_SESSION].reset_index(drop=True)
    calendar = spy["date"].tolist()
    position = {day: i for i, day in enumerate(calendar)}
    n, m = len(kept), len(calendar)
    opens = np.full((n, m), np.nan, dtype=np.float32)
    closes = np.full((n, m), np.nan, dtype=np.float32)
    raws = np.full((n, m), np.nan, dtype=np.float32)
    dvs = np.full((n, m), np.nan, dtype=np.float32)
    eligible = np.zeros((n, m), dtype=bool)
    names = list(kept.index)
    row_of = {name: i for i, name in enumerate(names)}
    by_asof = defaultdict(list)
    for name in names:
        by_asof[series.at[name, "asof"]].append(name)
    year_of = np.array([day[:4] for day in calendar])
    for asof, members in by_asof.items():
        frame = pd.read_pickle(cache / "bars" / f"{asof}.pkl.gz")
        frame = frame[frame["date"] <= LAST_SESSION]
        parts = {ticker: part for ticker, part in frame.groupby("ticker", sort=False)}
        for name in members:
            part = parts.get(name.split(":", 1)[1])
            if part is None or part.empty:
                continue
            at = part["date"].map(position)
            ok = at.notna().to_numpy()
            idx = at.to_numpy()[ok].astype(int)
            i = row_of[name]
            opens[i, idx] = part["open"].to_numpy(float)[ok]
            closes[i, idx] = part["close"].to_numpy(float)[ok]
            raw = part["raw_close"].to_numpy(float)
            dollar = pd.Series(raw * part["raw_volume"].to_numpy(float))
            median = dollar.rolling(DV_WINDOW, min_periods=DV_WINDOW).median().to_numpy()
            raws[i, idx] = raw[ok]
            dvs[i, idx] = median[ok]
            fine = np.isfinite(raw) & (raw >= MIN_PRICE) & np.isfinite(median) & (median >= MIN_DOLLAR_VOLUME)
            blocked = [y for y in kept.at[name, "blocked_years"].split(",") if y]
            row = np.zeros(m, dtype=bool)
            row[idx] = fine[ok]
            if blocked:
                row &= ~np.isin(year_of, blocked)
            eligible[i] = row
    has = np.isfinite(closes)
    index = np.where(has, np.arange(m, dtype=np.int32)[None, :], -1)
    last_bar = np.maximum.accumulate(index, axis=1).astype(np.int16)
    first = np.where(has.any(axis=1), has.argmax(axis=1), m).astype(int)
    last = last_bar[:, -1].astype(int)
    cik = [name.split(":", 1)[0] for name in names]
    ticker = [name.split(":", 1)[1] for name in names]
    by_cik, by_ticker = defaultdict(list), defaultdict(list)
    for i in range(n):
        by_cik[cik[i]].append(i)
        by_ticker[ticker[i]].append(i)
    bankrupt = defaultdict(list)
    for row in pd.read_csv(cache / "bankruptcies.csv", dtype=str).itertuples(index=False):
        bankrupt[row.cik].append(pd.Timestamp(row.filed).toordinal())
    return Market(calendar, position, names, cik, ticker, opens, closes, raws, eligible, dvs, last_bar, first, last,
                  has.sum(axis=1), spy["open"].to_numpy(float), spy["close"].to_numpy(float), dict(by_cik),
                  dict(by_ticker), dict(bankrupt))


def series_by_cik(market: Market, cik: str, session: int) -> int | None:
    """The issuer's series with a bar at ``session``; the most traded there when it has several."""
    best, best_dv = None, -1.0
    for s in market.by_cik.get(cik, ()):
        if np.isfinite(market.close[s, session]):
            dv = market.dollar_volume[s, session]
            dv = float(dv) if np.isfinite(dv) else 0.0
            if dv > best_dv:
                best, best_dv = s, dv
    return best


def series_by_ticker(market: Market, ticker: str, session: int) -> int | None:
    """The series with that ticker and a bar at ``session``; the one with the most bars when several."""
    best, best_bars = None, -1
    for s in market.by_ticker.get(ticker, ()):
        if np.isfinite(market.close[s, session]) and int(market.bars[s]) > best_bars:
            best, best_bars = s, int(market.bars[s])
    return best


def fixed_hold(market: Market, s: int, decision: int, hold: int, cost: float = ACTING_COST) -> dict | None:
    """Buy at the open after ``decision``, sell at the close of the ``hold``-th session held.

    None when the exit falls after the data (left out). ``outcome`` is ``completed``, ``stopped`` (the stock
    stopped trading: last close) or ``no_entry`` (no bar at the entry open).
    """
    entry = decision + 1
    target = entry + hold - 1
    if entry >= market.sessions:
        return None
    price_in = market.open[s, entry]
    if not (np.isfinite(price_in) and price_in > 0):
        return {"outcome": "no_entry"}
    if target >= market.sessions:
        if int(market.last[s]) >= market.sessions - ALIVE_SESSIONS:
            return None                                         # still trading and the hold is not over
        target = market.sessions - 1
    out = int(market.last_bar[s, target])
    if out < entry:
        return {"outcome": "no_entry"}
    stopped = int(market.last[s]) < target
    price_out = market.close[s, out]
    gross = float(price_out / price_in - 1)
    net = float(price_out * (1 - cost) / (price_in * (1 + cost)) - 1)
    spy = float(market.spy_close[out] / market.spy_open[entry] - 1)
    bankrupt = False
    if stopped:
        day = pd.Timestamp(market.calendar[int(market.last[s])]).toordinal()
        bankrupt = any(day - BANKRUPTCY_DAYS_BEFORE <= filed <= day + BANKRUPTCY_DAYS_AFTER
                       for filed in market.bankruptcies.get(market.cik[s], ()))
    return {"outcome": "stopped" if stopped else "completed", "entry": entry, "exit": out, "gross": gross, "net": net,
            "spy": spy, "excess": net - spy, "bankruptcy": bankrupt}


def one_position(rows: list[dict]) -> list[dict]:
    """Rows of one rule, each with ``series``, ``decision`` and ``exit``: a signal while a position is open is skipped."""
    busy: dict[int, int] = {}
    taken = []
    for row in sorted(rows, key=lambda r: (r["decision"], r["series"])):
        if row["decision"] <= busy.get(row["series"], -1):
            continue
        busy[row["series"]] = row["exit"]
        taken.append(row)
    return taken


def clustered_t(values: np.ndarray, clusters: np.ndarray) -> float | None:
    """t of the mean with errors clustered by month (PR #95's ``clustered_t``)."""
    values = np.asarray(values, float)
    if len(values) < 10:
        return None
    mean = float(values.mean())
    sums = pd.Series(values - mean).groupby(np.asarray(clusters)).sum()
    g = len(sums)
    if g < 2:
        return None
    variance = float((sums ** 2).sum()) * g / (g - 1) / len(values) ** 2
    return mean / math.sqrt(variance) if variance > 0 else None


def summarize(trades: pd.DataFrame, t_required: float) -> dict:
    """The pass rule on one set of trades (columns net, excess, month, year, series)."""
    if trades.empty:
        return {"trades": 0, "passes": False}
    net, exc = trades["net"].to_numpy(float), trades["excess"].to_numpy(float)
    months = trades["month"].to_numpy()
    t_net, t_exc = clustered_t(net, months), clustered_t(exc, months)
    by_year = trades.groupby("year").agg(trades=("net", "size"), net=("net", "mean"), excess=("excess", "mean"))
    counted = by_year[by_year["trades"] >= MIN_YEAR_TRADES]
    years_positive = int((counted["excess"] > 0).sum())
    k = int(math.floor(len(trades) * TOP_SHARE))
    trimmed = trades.sort_values("excess", ascending=False).iloc[k:]
    criteria = {
        "1_net_positive_t": bool(net.mean() > 0 and t_net is not None and t_net >= t_required),
        "2_beats_spy_t": bool(exc.mean() > 0 and t_exc is not None and t_exc >= T_EXCESS_REQUIRED),
        "3_most_years": bool(len(counted) > 0 and years_positive > len(counted) / 2),
        "4_without_top_1pct": bool(len(trimmed) > 0 and trimmed["net"].mean() > 0 and trimmed["excess"].mean() > 0),
    }
    out = {
        "trades": int(len(trades)), "stocks": int(trades["series"].nunique()),
        "net_bps": float(net.mean() * 1e4), "net_t": t_net, "median_net_bps": float(np.median(net) * 1e4),
        "win_rate": float((net > 0).mean()), "excess_bps": float(exc.mean() * 1e4), "excess_t": t_exc,
        "years_counted": int(len(counted)), "years_positive": years_positive,
        "trimmed_net_bps": float(trimmed["net"].mean() * 1e4), "trimmed_excess_bps": float(trimmed["excess"].mean() * 1e4),
        "criteria": criteria, "passes": all(criteria.values()),
        "by_year": {str(y): {"trades": int(r.trades), "net_bps": float(r.net * 1e4), "excess_bps": float(r.excess * 1e4)}
                    for y, r in by_year.iterrows()},
    }
    if "gross" in trades:
        out["gross_bps"] = float(trades["gross"].mean() * 1e4)
    return out


def fmt(value, digits: int = 1, signed: bool = True) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"
