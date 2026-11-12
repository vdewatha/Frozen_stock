"""SPY intraday momentum (noise band) with a held-out period.

The test pre-registered in PREREGISTRATION.md (same folder). Stages, each reading <work_dir> (outside the repo):

    ALPACA_KEY=... ALPACA_SECRET=... <repo venv> python -I spy_intraday_momentum.py fetch     <work_dir>
    <repo venv> python -I spy_intraday_momentum.py replicate <work_dir>
    <repo venv> python -I spy_intraday_momentum.py heldout   <work_dir>
    <repo venv> python -I spy_intraday_momentum.py iex       <work_dir>

* ``fetch`` reads SPY minute bars from Alpaca (GET https://data.alpaca.markets/v2/stocks/bars, ``1Min``,
  ``adjustment=all``) on ``feed=sip`` and ``feed=iex``, and SPY daily bars (adjusted and raw). Market data only;
  never an account or an order endpoint. The keys come from the environment and are never written or printed.
* ``replicate`` reads bars up to 2024-04-30 ONLY and writes the replication check (replication.json / .md).
* ``heldout`` runs every variant once on the whole window and writes results.json, tables.md and
  daily_returns.csv.gz. Run it once, after the replication check is settled.
* ``iex`` recomputes the signals from IEX bars alone and compares them with the consolidated-feed signals.

This script never contacts the deployed app. The rule, costs, periods and pass rule are frozen in
PREREGISTRATION.md; the constants below restate them.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent

SYMBOL = "SPY"
FIRST_DAY = "2016-01-01"
LAST_SESSION = "2026-11-11"          # last complete session before the fetch (run on 2026-11-12)
REPLICATION_END = "2024-04-30"       # the end of the paper's sample
HELDOUT_START = "2024-05-01"
UNREPORTED_START = "2025-02-01"      # after the last month the authors' FAQ prints

LOOKBACK = 14                        # sessions in sigma (paper)
VOL_MULT = 1.0                       # volatility multiplier (paper)
DECISION_EVERY = 30                  # minutes (paper)
FULL_LEN, HALF_LEN = 390, 210        # regular bars: 09:30-15:59, or 09:30-12:59 on an early-close day
MIN_BARS_FULL, MIN_BARS_HALF = 300, 150
TARGET_VOL, MAX_LEVERAGE = 0.02, 4.0  # variant (d) only: the paper's volatility-target sizing
PAPER_COST_PER_SHARE = 0.0035 + 0.001

COSTS_BPS = [0.0, 0.5, 1.0, 2.0, 5.0]
PRIMARY_COST = 1.0
PRIMARY = "c"
VARIANTS = {
    "a": "Long-short, opposite-band stop (replication only)",
    "a2": "Long-short, current band + VWAP stop (replication only)",
    "d": "Long-short, band + VWAP, volatility-target sizing up to 4x (replication only)",
    "b": "Long-only, opposite-band stop",
    "c": "Long-only, current band + VWAP stop (PRIMARY)",
}
RULE_OF = {"a": "a", "a2": "a2", "d": "a2", "b": "b", "c": "c"}

EARLY_CLOSES = {
    "2016-11-25", "2017-07-03", "2017-11-24", "2018-07-03", "2018-11-23", "2018-12-24", "2019-07-03",
    "2019-11-29", "2019-12-24", "2020-11-27", "2020-12-24", "2021-11-26", "2022-11-25", "2023-07-03",
    "2023-11-24", "2024-07-03", "2024-11-29", "2024-12-24", "2025-07-03", "2025-11-28", "2025-12-24",
}

# The paper's monthly returns of its headline rule, in percent (FAQ Q4 and Q24, 3 February 2025 version).
PAPER_MONTHLY = {
    2016: [-1.0, -1.9, 1.3, -1.4, 0.4, -7.3, -3.1, -5.2, 8.4, -5.0, 1.4, 0.7],
    2017: [-3.3, 0.9, 1.7, -3.3, -2.4, 0.4, -1.8, 3.6, -1.1, 2.9, -2.2, -2.3],
    2018: [3.5, 8.9, 7.2, 3.5, -4.7, 2.0, 6.2, -1.7, -3.5, 13.4, 3.0, 12.5],
    2019: [0.9, -0.7, 2.9, 0.9, -5.1, -0.5, 1.4, 6.8, -1.4, 2.6, -1.7, 1.0],
    2020: [5.3, 1.9, -0.5, -1.8, -0.2, 8.0, 0.1, -1.8, 14.3, 2.8, -1.1, -1.9],
    2021: [7.8, 3.1, 0.7, 6.1, 2.6, -1.0, 2.8, 1.7, 2.8, 3.3, -3.2, 4.1],
    2022: [-5.5, 2.8, 0.2, 9.0, 2.1, 0.5, 0.2, 6.3, -1.0, 5.8, 1.6, 0.7],
    2023: [2.9, -1.3, 7.8, 1.8, 2.9, 4.1, 2.2, 6.0, 2.9, -1.1, 0.5, 3.8],
    2024: [8.8, -1.5, -0.4, 5.8, -4.3, 1.6, 8.2, -2.8, 4.1, 6.7, -2.6, 5.7],
    2025: [-1.2],
}
PAPER_YEARLY = {2017: -6.9, 2018: 61.1, 2019: 6.9, 2020: 26.8, 2021: 34.8, 2022: 24.4, 2023: 37.2}
R1_MIN_CORR, R1_MAX_MAD, R1_MIN_YEARS = 0.85, 1.5, 6

API = "https://data.alpaca.markets/v2/stocks/bars"
WORKERS, SPACING = 3, 0.32
_lock = threading.Lock()
_last_call = [0.0]


# ----------------------------------------------------------------------------------------------- fetch

def _get(params: dict, key: str, secret: str) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(8):
        with _lock:
            wait = _last_call[0] + SPACING - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        request = urllib.request.Request(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                                                       "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504):
                time.sleep(2.0 * (attempt + 1))
                continue
            raise ValueError(f"HTTP {error.code}: {error.read()[:200]!r}") from None
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError("Alpaca did not answer after 8 attempts")


def _bars(timeframe: str, start: str, end: str, feed: str, adjustment: str, key: str, secret: str) -> list:
    rows, token = [], None
    while True:
        params = {"symbols": SYMBOL, "timeframe": timeframe, "start": start, "end": end, "feed": feed,
                  "adjustment": adjustment, "limit": 10000}
        if token:
            params["page_token"] = token
        body = _get(params, key, secret)
        rows.extend((body.get("bars") or {}).get(SYMBOL, []))
        token = body.get("next_page_token")
        if not token:
            return rows


def _months() -> list[tuple[str, str, str]]:
    out = []
    for period in pd.period_range(FIRST_DAY[:7], LAST_SESSION[:7], freq="M"):
        last = min(period.end_time.strftime("%Y-%m-%d"), LAST_SESSION)
        out.append((str(period), f"{period.start_time:%Y-%m-%d}T00:00:00Z", f"{last}T23:59:59Z"))
    return out


def _write_csv(path: Path, rows: list) -> None:
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as handle:
        handle.write("t,o,h,l,c,v,n,vw\n")
        for r in rows:
            handle.write(f"{r['t']},{r['o']},{r['h']},{r['l']},{r['c']},{r['v']},{r.get('n', '')},{r.get('vw', '')}\n")
    tmp.rename(path)


def stage_fetch(work: Path) -> None:
    key, secret = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
    for adjustment in ("all", "raw"):
        path = work / f"daily_{adjustment}.csv.gz"
        if not path.exists():
            _write_csv(path, _bars("1Day", "2015-12-01T00:00:00Z", f"{LAST_SESSION}T23:59:59Z", "sip", adjustment,
                                   key, secret))
    for feed in ("sip", "iex"):
        folder = work / f"minute_{feed}"
        folder.mkdir(parents=True, exist_ok=True)
        todo = [m for m in _months() if not (folder / f"{m[0]}.csv.gz").exists()]

        def one(month, feed=feed, folder=folder):
            rows = _bars("1Min", month[1], month[2], feed, "all", key, secret)
            _write_csv(folder / f"{month[0]}.csv.gz", rows)
            return month[0], len(rows)

        with ThreadPoolExecutor(WORKERS) as pool:
            for name, count in pool.map(one, todo):
                print(f"{feed} {name}: {count} bars", flush=True)
    print("fetch done")


# ------------------------------------------------------------------------------------------------ load

def load_minutes(work: Path, feed: str, upto: str) -> pd.DataFrame:
    """Regular-session minute bars up to and including `upto`. Files after that month are not opened."""
    frames = []
    for path in sorted((work / f"minute_{feed}").glob("*.csv.gz")):
        if path.name[:7] > upto[:7]:
            continue
        frame = pd.read_csv(path, usecols=["t", "o", "h", "l", "c", "v"])
        if len(frame):
            frames.append(frame)
    df = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(df["t"], utc=True).dt.tz_convert("America/New_York")
    df["day"] = ts.dt.strftime("%Y-%m-%d")
    df["m"] = ts.dt.hour * 60 + ts.dt.minute - 570
    df = df[(df["m"] >= 0) & (df["m"] < FULL_LEN) & (df["day"] <= upto)]
    return df.drop(columns="t").reset_index(drop=True)


def load_daily(work: Path, upto: str) -> pd.DataFrame:
    out = {}
    for adjustment in ("all", "raw"):
        frame = pd.read_csv(work / f"daily_{adjustment}.csv.gz", usecols=["t", "c"])
        frame["day"] = frame["t"].str[:10]
        out[adjustment] = frame.set_index("day")["c"]
    daily = pd.DataFrame({"adj": out["all"], "raw": out["raw"]})
    daily = daily[daily.index <= upto]
    daily["ret"] = daily["adj"].pct_change()
    daily["factor"] = daily["adj"] / daily["raw"]
    return daily


class Grid:
    """Sessions x 390 minute matrices for one feed, on a given session calendar."""

    def __init__(self, df: pd.DataFrame, days: list[str], length: np.ndarray):
        n = len(days)
        index = {d: i for i, d in enumerate(days)}
        row = df["day"].map(index)
        keep = row.notna().to_numpy()
        rows, cols = row[keep].astype(int).to_numpy(), df["m"].to_numpy()[keep]
        raw = {}
        for name in "ohlcv":
            matrix = np.full((n, FULL_LEN), np.nan)
            matrix[rows, cols] = df[name].to_numpy()[keep]
            matrix[np.arange(FULL_LEN)[None, :] >= length[:, None]] = np.nan     # after an early close
            raw[name] = matrix
        self.days, self.length = days, length
        self.has = ~np.isnan(raw["c"])
        self.bars = self.has.sum(axis=1)
        self.o = raw["o"]                                            # NaN where the minute has no bar
        self.cf = pd.DataFrame(raw["c"]).ffill(axis=1).to_numpy()    # last close carried forward
        self.cf[np.arange(FULL_LEN)[None, :] >= length[:, None]] = np.nan
        high = np.where(self.has, raw["h"], self.cf)
        low = np.where(self.has, raw["l"], self.cf)
        volume = np.where(self.has, raw["v"], 0.0)
        typical = np.where(self.has, (high + low + self.cf) / 3.0, 0.0)
        cum_volume = np.cumsum(volume, axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            self.vwap = np.where(cum_volume > 0, np.cumsum(volume * typical, axis=1) / cum_volume, np.nan)
        self.volume = volume
        first = np.where(self.has.any(axis=1), self.has.argmax(axis=1), 0)
        self.open = np.where(self.has.any(axis=1), raw["o"][np.arange(n), first], np.nan)
        self.close = self.cf[np.arange(n), length - 1]
        self.prev_close = np.concatenate([[np.nan], self.close[:-1]])
        with np.errstate(invalid="ignore"):
            move = np.abs(self.cf / self.open[:, None] - 1.0)
        self.sigma = np.full((n, FULL_LEN), np.nan)
        for m in range(FULL_LEN):
            column = pd.Series(move[:, m]).dropna()
            self.sigma[column.index.to_numpy(), m] = column.rolling(LOOKBACK).mean().shift(1).to_numpy()


def calendar(sip: pd.DataFrame) -> tuple[list[str], np.ndarray, list[str]]:
    """Sessions and their regular length, from the consolidated bars; the early-close list is checked.

    The pre-registered check (13:00-15:59 volume under 5% of the day's) was wrong: SPY keeps trading after an
    early close, so those hours hold 6% to 34% of the day's volume. The check used instead: every listed day
    must have fewer than 180 bars from 13:00 (after-hours trading is patchy), and every other session with
    fewer than 180 is returned by name so it can be looked at (a halt or a gap in the vendor's history).
    The list itself is unchanged.
    """
    days = sorted(sip["day"].unique())
    afternoon = sip[sip["m"] >= HALF_LEN].groupby("day").size().reindex(days).fillna(0)
    listed = {d for d in EARLY_CLOSES if days[0] <= d <= days[-1]}
    full_afternoon = sorted(d for d in listed if afternoon[d] >= FULL_LEN - HALF_LEN)
    if full_afternoon or not listed <= set(days):
        raise SystemExit(f"early-close list disagrees with the data: {full_afternoon or sorted(listed - set(days))}")
    gaps = sorted(set(afternoon[afternoon < FULL_LEN - HALF_LEN].index) - listed)
    length = np.array([HALF_LEN if d in EARLY_CLOSES else FULL_LEN for d in days])
    return days, length, gaps


# ------------------------------------------------------------------------------------------------- rule

def decision_bars(length: int) -> range:
    """Index of the bar each half-hour decision reads: 29 (ends 10:00), 59, ... up to the 15:30 decision."""
    return range(DECISION_EVERY - 1, length - 1, DECISION_EVERY)


def run_rule(signal: Grid, price: Grid, rule: str, tradable: np.ndarray, fill: str = "next_open") -> dict:
    """One pass of one position rule. Signals come from `signal`, fills and sizing from `price`."""
    n = len(signal.days)
    gross, turnover, sides = np.zeros(n), np.zeros(n), np.zeros(n)
    entries, wins = np.zeros(n, dtype=int), np.zeros(n, dtype=int)
    first_slot, first_dir = np.full(n, -1), np.zeros(n, dtype=int)
    path = np.zeros((n, 12), dtype=np.int8)
    use_vwap = rule in ("a2", "c")
    for d in range(n):
        if not tradable[d]:
            continue
        open_s, prev_s, open_p = signal.open[d], signal.prev_close[d], price.open[d]
        if math.isnan(open_s) or math.isnan(prev_s) or math.isnan(open_p):
            continue
        top, bottom = max(open_s, prev_s), min(open_s, prev_s)
        position, entry_price, pnl, traded, n_sides = 0, math.nan, 0.0, 0.0, 0
        for slot, k in enumerate(decision_bars(int(signal.length[d]))):
            close, sigma, vwap = signal.cf[d, k], signal.sigma[d, k], signal.vwap[d, k]
            target = position
            if not (math.isnan(close) or math.isnan(sigma) or (use_vwap and math.isnan(vwap))):
                upper, lower = top * (1.0 + VOL_MULT * sigma), bottom * (1.0 - VOL_MULT * sigma)
                if use_vwap:
                    upper, lower = max(upper, vwap), min(lower, vwap)
                above, below = close > upper, close < lower
                if rule == "a":
                    target = 1 if above else (-1 if below else position)
                elif rule == "a2":
                    target = 1 if above else (-1 if below else 0)
                elif rule == "b":
                    target = 1 if above else (0 if below else position)
                else:
                    target = 1 if above else 0
            if target != position:
                fill_price = price.o[d, k + 1] if fill == "next_open" else math.nan
                if math.isnan(fill_price):
                    fill_price = price.cf[d, k]
                if not math.isnan(fill_price):
                    if position != 0:
                        leg = position * (fill_price - entry_price)
                        pnl += leg
                        wins[d] += leg > 0
                    change = abs(target - position)
                    traded += change * fill_price
                    n_sides += change
                    if target != 0:
                        entry_price = fill_price
                        entries[d] += 1
                        if first_slot[d] < 0:
                            first_slot[d], first_dir[d] = slot, target
                    position = target
            path[d, slot] = position
        if position != 0:
            last = price.close[d]
            leg = position * (last - entry_price)
            pnl += leg
            wins[d] += leg > 0
            traded += abs(position) * last
            n_sides += abs(position)
        gross[d], turnover[d], sides[d] = pnl / open_p, traded / open_p, n_sides
    return {"gross": gross, "turnover": turnover, "sides": sides, "entries": entries, "wins": wins,
            "first_slot": first_slot, "first_dir": first_dir, "path": path}


def vol_target_size(grid: Grid) -> np.ndarray:
    """min(4, 2% / s), s = sample std of SPY's previous 14 daily returns (paper's formula). NaN before that."""
    returns = pd.Series(grid.close).pct_change()
    s = returns.rolling(LOOKBACK).std(ddof=1).shift(1)
    return np.minimum(MAX_LEVERAGE, TARGET_VOL / s).to_numpy()


def net_returns(run: dict, cost_bps: float, size: np.ndarray | None = None) -> np.ndarray:
    net = run["gross"] - run["turnover"] * cost_bps / 1e4
    return net if size is None else np.where(np.isnan(size), 0.0, size * net)


def paper_cost_returns(run: dict, size: np.ndarray, raw_open: np.ndarray) -> np.ndarray:
    """Variant (d) with the paper's cost: $0.0045 a share per side, on the raw share price."""
    net = run["gross"] - run["sides"] * PAPER_COST_PER_SHARE / raw_open
    return np.where(np.isnan(size) | np.isnan(net), 0.0, size * net)


# ------------------------------------------------------------------------------------------------ stats

def max_drawdown(returns: np.ndarray) -> float:
    equity = np.cumprod(1.0 + returns)
    peak = np.maximum.accumulate(np.concatenate([[1.0], equity]))[1:]
    return float(np.max(1.0 - equity / peak)) if len(equity) else 0.0


def sharpe(returns: np.ndarray) -> float:
    sd = returns.std(ddof=1)
    return float(returns.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0


def summary(returns: np.ndarray, spy: np.ndarray) -> dict:
    n = len(returns)
    years = n / 252.0
    total = float(np.prod(1.0 + returns) - 1.0)
    sr = sharpe(returns)
    sd = returns.std(ddof=1)
    beta = float(np.cov(returns, spy)[0, 1] / spy.var(ddof=1)) if sd > 0 else 0.0
    return {
        "sessions": n, "total_return": total,
        "annual_return": float((1.0 + total) ** (1.0 / years) - 1.0) if total > -1 else -1.0,
        "annual_vol": float(sd * math.sqrt(252)), "sharpe": sr,
        "sharpe_se": float(math.sqrt((1.0 + 0.5 * sr * sr) / years)),
        "t_mean": float(returns.mean() / (sd / math.sqrt(n))) if sd > 0 else 0.0,
        "max_drawdown": max_drawdown(returns),
        "corr_spy": float(np.corrcoef(returns, spy)[0, 1]) if sd > 0 else 0.0, "beta_spy": beta,
        "alpha_annual": float((returns.mean() - beta * spy.mean()) * 252),
        "best_day": float(returns.max()), "worst_day": float(returns.min()),
    }


def activity(run: dict, mask: np.ndarray, returns: np.ndarray) -> dict:
    traded = run["entries"][mask] > 0
    trips = int(run["entries"][mask].sum())
    return {
        "share_days_traded": float(traded.mean()), "round_trips": trips,
        "round_trips_per_day": float(trips / mask.sum()),
        "win_rate_days_traded": float((returns[traded] > 0).mean()) if traded.any() else 0.0,
        "win_rate_round_trips": float(run["wins"][mask].sum() / trips) if trips else 0.0,
        "gross_bps_per_side": float(run["gross"][mask].sum() / run["sides"][mask].sum() * 1e4)
        if run["sides"][mask].sum() else 0.0,
    }


def break_even(run: dict, mask: np.ndarray) -> float | None:
    """Cost per side (bps) at which the compounded net return is zero; None when it loses before costs."""
    gross, turnover = run["gross"][mask], run["turnover"][mask]

    def total(cost):
        return np.prod(1.0 + gross - turnover * cost / 1e4) - 1.0

    if total(0.0) <= 0:
        return None
    low, high = 0.0, 100.0
    if total(high) > 0:
        return high
    for _ in range(60):
        mid = (low + high) / 2
        low, high = (mid, high) if total(mid) > 0 else (low, mid)
    return float((low + high) / 2)


def by_year(days: list[str], returns: np.ndarray, mask: np.ndarray) -> dict:
    series = pd.Series(returns[mask], index=[d[:4] for d, m in zip(days, mask) if m])
    return {year: float(np.prod(1.0 + group.to_numpy()) - 1.0) for year, group in series.groupby(level=0)}


def by_month(days: list[str], returns: np.ndarray, mask: np.ndarray) -> dict:
    series = pd.Series(returns[mask], index=[d[:7] for d, m in zip(days, mask) if m])
    return {month: float(np.prod(1.0 + group.to_numpy()) - 1.0) for month, group in series.groupby(level=0)}


def criteria(returns: np.ndarray, spy: np.ndarray) -> dict:
    overlay = spy + returns
    out = {
        "net_return": float(np.prod(1.0 + returns) - 1.0), "sharpe": sharpe(returns),
        "corr_spy": float(np.corrcoef(returns, spy)[0, 1]) if returns.std() > 0 else 0.0,
        "spy_sharpe": sharpe(spy), "overlay_sharpe": sharpe(overlay),
        "spy_max_drawdown": max_drawdown(spy), "overlay_max_drawdown": max_drawdown(overlay),
        "spy_total_return": float(np.prod(1.0 + spy) - 1.0),
        "overlay_total_return": float(np.prod(1.0 + overlay) - 1.0),
    }
    out["pass_1_profit"] = out["net_return"] > 0
    out["pass_2_sharpe"] = out["sharpe"] >= 0.5
    out["pass_3_corr"] = out["corr_spy"] < 0.3
    out["pass_4_overlay"] = (out["overlay_sharpe"] > out["spy_sharpe"]
                             and out["overlay_max_drawdown"] < out["spy_max_drawdown"])
    out["pass_all"] = all(out[k] for k in ("pass_1_profit", "pass_2_sharpe", "pass_3_corr", "pass_4_overlay"))
    return out


# ------------------------------------------------------------------------------------------- pipeline

def prepare(work: Path, upto: str):
    sip = load_minutes(work, "sip", upto)
    days, length, afternoon_gaps = calendar(sip)
    grid = Grid(sip, days, length)
    minimum = np.where(length == HALF_LEN, MIN_BARS_HALF, MIN_BARS_FULL)
    thin = grid.bars < minimum
    tradable = ~thin
    tradable[0] = False
    daily = load_daily(work, upto).reindex(days)
    if daily["adj"].isna().any():
        raise SystemExit(f"sessions with no daily bar: {list(daily.index[daily['adj'].isna()])[:5]}")
    info = {"first_session": days[0], "last_session": days[-1], "sessions": len(days),
            "early_closes": int((length == HALF_LEN).sum()), "thin_sessions": [days[i] for i in np.where(thin)[0]],
            "unlisted_sessions_short_of_afternoon_bars": afternoon_gaps,
            "regular_minutes_without_bar": float(1.0 - grid.has.sum() / length.sum()),
            "first_session_with_sigma": days[int(np.argmax(~np.isnan(grid.sigma[:, 29])))]}
    return grid, days, tradable, daily, info


def run_all(grid: Grid, tradable: np.ndarray) -> dict:
    return {rule: run_rule(grid, grid, rule, tradable) for rule in ("a", "a2", "b", "c")}


def fmt_pct(x, digits=1):
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def day_detail(grid: Grid, run: dict, day: str) -> dict:
    d = grid.days.index(day)
    slots = [f"{(570 + k + 1) // 60:02d}:{(570 + k + 1) % 60:02d}" for k in decision_bars(int(grid.length[d]))]
    return {"positions": dict(zip(slots, [int(x) for x in run["path"][d][:len(slots)]])),
            "first_entry": slots[run["first_slot"][d]] if run["first_slot"][d] >= 0 else None,
            "first_dir": int(run["first_dir"][d]), "gross": float(run["gross"][d]), "sides": int(run["sides"][d])}


def stage_replicate(work: Path) -> None:
    grid, days, tradable, daily, info = prepare(work, REPLICATION_END)
    runs = run_all(grid, tradable)
    spy = daily["ret"].fillna(0.0).to_numpy()
    raw_open = grid.open / daily["factor"].to_numpy()
    size = vol_target_size(grid)
    d_returns = paper_cost_returns(runs["a2"], size, raw_open)
    everything = np.ones(len(days), dtype=bool)

    # R1: monthly table of the headline rule, February 2016 to April 2024
    mine = by_month(days, d_returns, everything)
    rows = []
    for year, values in PAPER_MONTHLY.items():
        for i, value in enumerate(values):
            month = f"{year}-{i + 1:02d}"
            if "2016-02" <= month <= REPLICATION_END[:7] and month in mine:
                rows.append((month, value, mine[month] * 100))
    paper = np.array([r[1] for r in rows])
    ours = np.array([r[2] for r in rows])
    yearly = by_year(days, d_returns, everything)
    same_sign = {str(y): bool(np.sign(yearly[str(y)]) == np.sign(v)) for y, v in PAPER_YEARLY.items()}
    r1 = {"months": len(rows), "correlation": float(np.corrcoef(paper, ours)[0, 1]),
          "mean_abs_diff_pp": float(np.abs(paper - ours).mean()),
          "years_same_sign": int(sum(same_sign.values())), "same_sign": same_sign,
          "my_yearly_pct": {y: yearly[str(y)] * 100 for y in PAPER_YEARLY}, "paper_yearly_pct": PAPER_YEARLY,
          "paper_sum_pct": float(paper.sum()), "my_sum_pct": float(ours.sum())}
    r1["pass"] = bool(r1["correlation"] >= R1_MIN_CORR and r1["mean_abs_diff_pp"] <= R1_MAX_MAD
                      and r1["years_same_sign"] >= R1_MIN_YEARS)

    # R2: the paper's dated examples (returns with the paper's cost, unlevered)
    def unlevered(rule, day):
        d = days.index(day)
        return float(runs[rule]["gross"][d] - runs[rule]["sides"][d] * PAPER_COST_PER_SHARE / raw_open[d])

    jan20_a, jan20_a2 = day_detail(grid, runs["a"], "2022-01-20"), day_detail(grid, runs["a2"], "2022-01-20")
    jan31, apr29 = day_detail(grid, runs["a"], "2022-01-31"), day_detail(grid, runs["a"], "2022-04-29")
    a2_positions = jan20_a2["positions"]
    checks = {
        "2022-01-20 (a): long from 10:00, day -1.9% to -2.5%": bool(
            jan20_a["first_entry"] == "10:00" and jan20_a["first_dir"] == 1
            and -0.025 <= unlevered("a", "2022-01-20") <= -0.019),
        "2022-01-20 (a2): long closed at 13:00, day within 0.4% of zero": bool(
            a2_positions.get("12:30") == 1 and a2_positions.get("13:00") == 0
            and abs(unlevered("a2", "2022-01-20")) <= 0.004),
        "2022-01-31 (a): first position long at 10:30": bool(jan31["first_entry"] == "10:30" and jan31["first_dir"] == 1),
        "2022-04-29 (a): first position short at 10:30": bool(apr29["first_entry"] == "10:30" and apr29["first_dir"] == -1),
    }
    r2 = {"checks": checks, "held": int(sum(checks.values())), "pass": bool(sum(checks.values()) >= 3),
          "detail": {"2022-01-20 a": {**jan20_a, "net": unlevered("a", "2022-01-20")},
                     "2022-01-20 a2": {**jan20_a2, "net": unlevered("a2", "2022-01-20")},
                     "2022-01-31 a": jan31, "2022-04-29 a": apr29}}

    # R3: unlevered long-short variants against the paper's whole-sample figures (reported)
    mask = np.array([d >= info["first_session_with_sigma"] for d in days])
    r3 = {}
    for rule in ("a", "a2"):
        returns = runs[rule]["gross"] - runs[rule]["sides"] * PAPER_COST_PER_SHARE / raw_open
        returns = np.where(np.isnan(returns), 0.0, returns)
        r3[rule] = {**summary(returns[mask], spy[mask]), **activity(runs[rule], mask, returns[mask])}
    r3["a_vol_in_range"] = bool(0.07 <= r3["a"]["annual_vol"] <= 0.125)
    r3["a_beta_in_range"] = bool(-0.20 <= r3["a"]["beta_spy"] <= 0.10)

    out = {"info": info, "R1": r1, "R2": r2, "R3": r3, "trusted": bool(r1["pass"] and r2["pass"]),
           "monthly": [{"month": m, "paper_pct": p, "mine_pct": round(o, 2)} for m, p, o in rows]}
    (HERE / "replication.json").write_text(json.dumps(out, indent=1))

    lines = ["# Replication check (bars up to 2024-04-30 only)", "",
             f"Sessions {info['first_session']} to {info['last_session']}: {info['sessions']} "
             f"({info['early_closes']} early closes). First session with a sigma: {info['first_session_with_sigma']}. "
             f"Thin sessions not traded: {info['thin_sessions'] or 'none'}. Sessions not on the early-close list with "
             f"afternoon minutes missing: {info['unlisted_sessions_short_of_afternoon_bars'] or 'none'}. "
             f"Regular minutes with no consolidated bar: {info['regular_minutes_without_bar'] * 100:.3f}%.", "",
             f"**Pipeline trusted: {'YES' if out['trusted'] else 'NO'}** (R1 {'pass' if r1['pass'] else 'FAIL'}, "
             f"R2 {'pass' if r2['pass'] else 'FAIL'})", "",
             "## R1. Monthly returns of the headline rule (d), February 2016 to April 2024", "",
             "| Measure | Mine against the paper | Needed | |", "|---|---|---|---|",
             f"| Correlation of monthly returns ({r1['months']} months) | {r1['correlation']:.3f} | at least {R1_MIN_CORR} | "
             f"{'pass' if r1['correlation'] >= R1_MIN_CORR else 'FAIL'} |",
             f"| Mean absolute monthly difference | {r1['mean_abs_diff_pp']:.2f} pp | at most {R1_MAX_MAD} pp | "
             f"{'pass' if r1['mean_abs_diff_pp'] <= R1_MAX_MAD else 'FAIL'} |",
             f"| Full years 2017-2023 with the paper's sign | {r1['years_same_sign']} of 7 | at least {R1_MIN_YEARS} | "
             f"{'pass' if r1['years_same_sign'] >= R1_MIN_YEARS else 'FAIL'} |", "",
             f"Sum of the {r1['months']} monthly returns: paper {r1['paper_sum_pct']:.1f}%, mine {r1['my_sum_pct']:.1f}%.", "",
             "| Year | Paper | Mine |", "|---|---|---|"]
    lines += [f"| {y} | {v:.1f}% | {r1['my_yearly_pct'][y]:.1f}% |" for y, v in PAPER_YEARLY.items()]
    lines += ["", "## R2. The paper's dated examples", "", "| Check | Held |", "|---|---|"]
    lines += [f"| {name} | {'yes' if ok else 'NO'} |" for name, ok in checks.items()]
    for name, detail in r2["detail"].items():
        lines.append("")
        lines.append(f"* {name}: first entry {detail['first_entry']} dir {detail['first_dir']}, gross "
                     f"{detail['gross'] * 100:.2f}%" + (f", net {detail['net'] * 100:.2f}%" if "net" in detail else "")
                     + f", positions {detail['positions']}")
    lines += ["", "## R3. Unlevered long-short variants on this window, next to the paper's 2007-2024 figures (reported)",
              "", "| Variant | Annual return | Volatility | Sharpe | Beta to SPY | Max drawdown | Days traded | Round trips |",
              "|---|---|---|---|---|---|---|---|"]
    paper_rows = {"a": "6.2% / 10.9% / 0.61 / -0.05 / 21%", "a2": "9.7% / 7.7% / 1.24 / -0.03 / 12%"}
    for rule in ("a", "a2"):
        s = r3[rule]
        lines.append(f"| ({rule}) mine | {fmt_pct(s['annual_return'])} | {fmt_pct(s['annual_vol'])} | {s['sharpe']:.2f} | "
                     f"{s['beta_spy']:.2f} | {fmt_pct(s['max_drawdown'])} | {fmt_pct(s['share_days_traded'])} | "
                     f"{s['round_trips']} |")
        lines.append(f"| ({rule}) paper, 2007-2024 | {paper_rows[rule]} (return / vol / Sharpe / beta / drawdown) | | | | | | |")
    lines += ["", f"(a) volatility within 7% to 12.5%: {'yes' if r3['a_vol_in_range'] else 'NO'}; "
                  f"(a) beta within -0.20 to +0.10: {'yes' if r3['a_beta_in_range'] else 'NO'}.", "",
              "## Month by month", "", "| Month | Paper | Mine |", "|---|---|---|"]
    lines += [f"| {m} | {p:.1f}% | {o:.1f}% |" for m, p, o in rows]
    (HERE / "replication.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:40]))


def stage_heldout(work: Path) -> None:
    replication = json.loads((HERE / "replication.json").read_text())
    grid, days, tradable, daily, info = prepare(work, LAST_SESSION)
    runs = run_all(grid, tradable)
    runs["c_close_fill"] = run_rule(grid, grid, "c", tradable, fill="decision_close")
    spy = daily["ret"].fillna(0.0).to_numpy()
    raw_open = grid.open / daily["factor"].to_numpy()
    size = vol_target_size(grid)
    day_array = np.array(days)
    periods = {
        "replication": (day_array >= replication["info"]["first_session_with_sigma"]) & (day_array <= REPLICATION_END),
        "heldout": day_array >= HELDOUT_START,
        "unreported": day_array >= UNREPORTED_START,
    }

    def variant_returns(variant, cost):
        run = runs[RULE_OF[variant]]
        return net_returns(run, cost, size if variant == "d" else None)

    results = {"info": info, "replication_trusted": replication["trusted"], "periods": {}, "variants": VARIANTS}
    for name, mask in periods.items():
        block = {"first": str(day_array[mask][0]), "last": str(day_array[mask][-1]), "sessions": int(mask.sum()),
                 "spy": summary(spy[mask], spy[mask]), "variants": {}}
        for variant in VARIANTS:
            run = runs[RULE_OF[variant]]
            entry = {"activity": activity(run, mask, variant_returns(variant, PRIMARY_COST)[mask]),
                     "break_even_bps": break_even(run, mask) if variant != "d" else None, "costs": {}}
            for cost in COSTS_BPS:
                returns = variant_returns(variant, cost)[mask]
                entry["costs"][str(cost)] = {**summary(returns, spy[mask]), "criteria": criteria(returns, spy[mask])}
            entry["by_year"] = by_year(days, variant_returns(variant, PRIMARY_COST), mask)
            block["variants"][variant] = entry
        block["spy_by_year"] = by_year(days, spy, mask)
        results["periods"][name] = block

    held = periods["heldout"]
    close_fill = net_returns(runs["c_close_fill"], PRIMARY_COST)[held]
    results["c_close_fill_heldout"] = {**summary(close_fill, spy[held]), "criteria": criteria(close_fill, spy[held]),
                                       "break_even_bps": break_even(runs["c_close_fill"], held)}
    d_paper_cost = paper_cost_returns(runs["a2"], size, raw_open)
    mine = by_month(days, d_paper_cost, held)
    results["d_heldout_months_vs_faq"] = [
        {"month": f"{year}-{i + 1:02d}", "paper_pct": value, "mine_pct": round(mine[f"{year}-{i + 1:02d}"] * 100, 2)}
        for year, values in PAPER_MONTHLY.items() for i, value in enumerate(values)
        if "2024-05" <= f"{year}-{i + 1:02d}" <= "2025-01"]
    results["d_heldout_paper_cost"] = summary(d_paper_cost[held], spy[held])

    primary = results["periods"]["heldout"]["variants"][PRIMARY]["costs"][str(PRIMARY_COST)]["criteria"]
    results["verdict"] = "PASS" if (primary["pass_all"] and replication["trusted"]) else "FAIL"
    (HERE / "results.json").write_text(json.dumps(results, indent=1))

    table = pd.DataFrame({"day": days, "spy_ret": np.round(spy, 8), "sigma_1000": np.round(grid.sigma[:, 29], 8)})
    for rule in ("a", "a2", "b", "c"):
        table[f"{rule}_gross"] = np.round(runs[rule]["gross"], 8)
        table[f"{rule}_turnover"] = np.round(runs[rule]["turnover"], 6)
        table[f"{rule}_sides"] = runs[rule]["sides"].astype(int)
        table[f"{rule}_first_slot"] = runs[rule]["first_slot"]
        table[f"{rule}_net_1bp"] = np.round(net_returns(runs[rule], PRIMARY_COST), 8)
    table["d_size"] = np.round(size, 4)
    table["d_net_paper_cost"] = np.round(d_paper_cost, 8)
    table.to_csv(HERE / "daily_returns.csv.gz", index=False)
    write_tables(results)
    print(f"verdict: {results['verdict']}")


def write_tables(results: dict) -> None:
    lines = ["# SPY intraday momentum: tables", "",
             f"Verdict on the held-out period, primary variant (c) at {PRIMARY_COST:g} bp a side: "
             f"**{results['verdict']}** (pipeline trusted: {'yes' if results['replication_trusted'] else 'NO'}).", ""]
    labels = {"replication": "Replication period", "heldout": "Held-out period",
              "unreported": "Unreported stretch (2025-02-01 onward)"}
    for name, block in results["periods"].items():
        lines += [f"## {labels[name]}: {block['first']} to {block['last']} ({block['sessions']} sessions)", ""]
        s = block["spy"]
        lines += [f"SPY held throughout: total {fmt_pct(s['total_return'])}, {fmt_pct(s['annual_return'])} a year, "
                  f"volatility {fmt_pct(s['annual_vol'])}, Sharpe {s['sharpe']:.2f}, max drawdown "
                  f"{fmt_pct(s['max_drawdown'])}.", "",
                  "| Variant | Cost/side | Total | A year | Vol | Sharpe (SE) | Max DD | Corr SPY | Beta | Worst day | Best day |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for variant, entry in block["variants"].items():
            for cost in COSTS_BPS:
                c = entry["costs"][str(cost)]
                lines.append(f"| ({variant}) | {cost:g} bp | {fmt_pct(c['total_return'])} | {fmt_pct(c['annual_return'])} | "
                             f"{fmt_pct(c['annual_vol'])} | {c['sharpe']:.2f} ({c['sharpe_se']:.2f}) | "
                             f"{fmt_pct(c['max_drawdown'])} | {c['corr_spy']:.2f} | {c['beta_spy']:.2f} | "
                             f"{fmt_pct(c['worst_day'], 2)} | {fmt_pct(c['best_day'], 2)} |")
        lines += ["", "| Variant | Days with a trade | Round trips | Per day | Winning days (of traded) | Winning round trips | "
                      "Gross per side | Break-even cost/side |", "|---|---|---|---|---|---|---|---|"]
        for variant, entry in block["variants"].items():
            a = entry["activity"]
            be = entry["break_even_bps"]
            be_text = ("n/a (levered)" if variant == "d" else
                       "none: loses before costs" if be is None else f"{be:.2f} bp")
            lines.append(f"| ({variant}) | {fmt_pct(a['share_days_traded'])} | {a['round_trips']} | "
                         f"{a['round_trips_per_day']:.2f} | {fmt_pct(a['win_rate_days_traded'])} | "
                         f"{fmt_pct(a['win_rate_round_trips'])} | {a['gross_bps_per_side']:.2f} bp | {be_text} |")
        lines += ["", f"Pass criteria at each cost ({labels[name].lower()}; decisive only for (c), held-out, "
                      f"{PRIMARY_COST:g} bp):", "",
                  "| Variant | Cost/side | 1. Net return > 0 | 2. Sharpe >= 0.5 | 3. Corr < 0.3 | 4. Overlay Sharpe / max DD "
                  "against SPY alone | All four |", "|---|---|---|---|---|---|---|"]
        for variant in ("c", "b"):
            for cost in COSTS_BPS:
                k = block["variants"][variant]["costs"][str(cost)]["criteria"]
                mark = lambda ok: "pass" if ok else "FAIL"   # noqa: E731
                lines.append(
                    f"| ({variant}) | {cost:g} bp | {fmt_pct(k['net_return'])} {mark(k['pass_1_profit'])} | "
                    f"{k['sharpe']:.2f} {mark(k['pass_2_sharpe'])} | {k['corr_spy']:.2f} {mark(k['pass_3_corr'])} | "
                    f"{k['overlay_sharpe']:.2f} vs {k['spy_sharpe']:.2f}; {fmt_pct(k['overlay_max_drawdown'])} vs "
                    f"{fmt_pct(k['spy_max_drawdown'])} {mark(k['pass_4_overlay'])} | {mark(k['pass_all'])} |")
        years = sorted(block["spy_by_year"])
        lines += ["", f"Year by year, net at {PRIMARY_COST:g} bp a side:", "",
                  "| Year | SPY | " + " | ".join(f"({v})" for v in VARIANTS) + " |", "|---|---|" + "---|" * len(VARIANTS)]
        for year in years:
            lines.append(f"| {year} | {fmt_pct(block['spy_by_year'][year])} | " + " | ".join(
                fmt_pct(block["variants"][v]["by_year"].get(year)) for v in VARIANTS) + " |")
        lines.append("")
    c = results["c_close_fill_heldout"]
    be = c["break_even_bps"]
    lines += ["## Reported extras", "",
              f"* (c) held-out with fills at the close the decision read (the authors' code convention), {PRIMARY_COST:g} bp: "
              f"total {fmt_pct(c['total_return'])}, Sharpe {c['sharpe']:.2f}, break-even "
              f"{'none' if be is None else f'{be:.2f} bp'}, all four criteria: "
              f"{'pass' if c['criteria']['pass_all'] else 'FAIL'}.",
              f"* (d) held-out with the paper's own cost ($0.0045 a share): total "
              f"{fmt_pct(results['d_heldout_paper_cost']['total_return'])}, Sharpe "
              f"{results['d_heldout_paper_cost']['sharpe']:.2f}, max drawdown "
              f"{fmt_pct(results['d_heldout_paper_cost']['max_drawdown'])}.", "",
              "(d) month by month against the authors' FAQ table, May 2024 to January 2025:", "",
              "| Month | Authors | Mine |", "|---|---|---|"]
    lines += [f"| {r['month']} | {r['paper_pct']:.1f}% | {r['mine_pct']:.1f}% |" for r in results["d_heldout_months_vs_faq"]]
    (HERE / "tables.md").write_text("\n".join(lines) + "\n")


def stage_iex(work: Path) -> None:
    grid, days, tradable, daily, info = prepare(work, LAST_SESSION)
    iex_bars = load_minutes(work, "iex", LAST_SESSION)
    iex = Grid(iex_bars, days, grid.length)
    spy = daily["ret"].fillna(0.0).to_numpy()
    day_array = np.array(days)
    has_sigma = ~np.isnan(iex.sigma[:, 29])
    first_iex = days[int(np.argmax(iex.bars > 0))]
    out = {"first_iex_session": first_iex, "first_iex_session_with_sigma": days[int(np.argmax(has_sigma))],
           "sessions_without_iex_bars": int(((iex.bars == 0) & (day_array >= first_iex)).sum()), "periods": {}}
    periods = {"all": has_sigma & tradable, "heldout": has_sigma & tradable & (day_array >= HELDOUT_START)}
    slots_per_day = np.array([len(decision_bars(int(x))) for x in grid.length])
    slot_mask = np.arange(12)[None, :] < slots_per_day[:, None]
    for name, mask in periods.items():
        block = {"sessions": int(mask.sum()), "first": str(day_array[mask][0]),
                 "regular_minutes_without_iex_bar": float(1.0 - iex.has[mask].sum() / grid.length[mask].sum()),
                 "decision_minutes_without_iex_bar": float(np.mean(
                     [not iex.has[d, k] for d in np.where(mask)[0] for k in decision_bars(int(grid.length[d]))])),
                 "median_abs_open_diff_bps": float(np.nanmedian(np.abs(iex.open[mask] / grid.open[mask] - 1)) * 1e4),
                 "median_abs_close_diff_bps": float(np.nanmedian(np.abs(iex.close[mask] / grid.close[mask] - 1)) * 1e4),
                 "variants": {}}
        for rule in ("c", "b", "a"):
            sip_run = run_rule(grid, grid, rule, tradable)
            iex_run = run_rule(iex, grid, rule, tradable)          # IEX signals, consolidated fills
            sip_traded, iex_traded = sip_run["entries"][mask] > 0, iex_run["entries"][mask] > 0
            both = sip_traded & iex_traded
            same_dir = sip_run["first_dir"][mask] == iex_run["first_dir"][mask]
            gap = np.abs(sip_run["first_slot"][mask] - iex_run["first_slot"][mask])
            same_path = (sip_run["path"][mask] == iex_run["path"][mask])[slot_mask[mask]]
            full_day = (sip_run["path"][mask] == iex_run["path"][mask]) | ~slot_mask[mask]
            sip_net, iex_net = net_returns(sip_run, PRIMARY_COST)[mask], net_returns(iex_run, PRIMARY_COST)[mask]
            block["variants"][rule] = {
                "sip_days_traded": int(sip_traded.sum()), "iex_days_traded": int(iex_traded.sum()),
                "trade_or_not_agree": float((sip_traded == iex_traded).mean()),
                "sip_traded_and_iex_traded": float(both.sum() / max(sip_traded.sum(), 1)),
                "iex_traded_but_sip_not": int((iex_traded & ~sip_traded).sum()),
                "same_first_direction_of_both": float(same_dir[both].mean()) if both.any() else 0.0,
                "same_first_entry_decision_of_both": float(((gap == 0) & same_dir)[both].mean()) if both.any() else 0.0,
                "within_one_decision_of_both": float(((gap <= 1) & same_dir)[both].mean()) if both.any() else 0.0,
                "full_agreement_of_sip_traded": float((both & same_dir & (gap == 0)).sum() / max(sip_traded.sum(), 1)),
                "decisions_same_position": float(same_path.mean()),
                "sessions_identical_all_decisions": float(full_day.all(axis=1).mean()),
                "sip_signals": summary(sip_net, spy[mask]), "iex_signals_sip_fills": summary(iex_net, spy[mask]),
                "daily_return_correlation": float(np.corrcoef(sip_net, iex_net)[0, 1]),
            }
        out["periods"][name] = block
    (HERE / "iex_feasibility.json").write_text(json.dumps(out, indent=1))
    lines = ["# Feasibility on the free live feed (IEX only): tables", "",
             f"First session with IEX bars: {out['first_iex_session']}; first with a 14-session IEX sigma: "
             f"{out['first_iex_session_with_sigma']}. Sessions since then with no IEX bar at all: "
             f"{out['sessions_without_iex_bars']}. Not part of the verdict.", ""]
    for name, block in out["periods"].items():
        lines += [f"## {'Every session with IEX signals' if name == 'all' else 'Held-out period'}: from {block['first']}, "
                  f"{block['sessions']} sessions", "",
                  f"Regular minutes with no IEX bar: {fmt_pct(block['regular_minutes_without_iex_bar'], 2)}; decision "
                  f"minutes with no IEX bar: {fmt_pct(block['decision_minutes_without_iex_bar'], 2)}. Median gap between "
                  f"the IEX and consolidated open: {block['median_abs_open_diff_bps']:.2f} bp; close: "
                  f"{block['median_abs_close_diff_bps']:.2f} bp.", "",
                  "| Variant | Days traded (consolidated / IEX) | Trade-or-not agrees | Of consolidated trade days: IEX also "
                  "trades | Same first direction | Same first entry decision | Within one decision | Same day, direction and "
                  "entry time (of consolidated trade days) | Decisions with the same position | Sessions identical at every "
                  "decision |", "|---|---|---|---|---|---|---|---|---|---|"]
        for rule, v in block["variants"].items():
            lines.append(f"| ({rule}) | {v['sip_days_traded']} / {v['iex_days_traded']} | {fmt_pct(v['trade_or_not_agree'])} | "
                         f"{fmt_pct(v['sip_traded_and_iex_traded'])} | {fmt_pct(v['same_first_direction_of_both'])} | "
                         f"{fmt_pct(v['same_first_entry_decision_of_both'])} | {fmt_pct(v['within_one_decision_of_both'])} | "
                         f"{fmt_pct(v['full_agreement_of_sip_traded'])} | {fmt_pct(v['decisions_same_position'])} | "
                         f"{fmt_pct(v['sessions_identical_all_decisions'])} |")
        lines += ["", f"Net at {PRIMARY_COST:g} bp a side, consolidated fills in both columns:", "",
                  "| Variant | Consolidated signals: total / Sharpe | IEX signals: total / Sharpe | Correlation of daily returns |",
                  "|---|---|---|---|"]
        for rule, v in block["variants"].items():
            lines.append(f"| ({rule}) | {fmt_pct(v['sip_signals']['total_return'])} / {v['sip_signals']['sharpe']:.2f} | "
                         f"{fmt_pct(v['iex_signals_sip_fills']['total_return'])} / "
                         f"{v['iex_signals_sip_fills']['sharpe']:.2f} | {v['daily_return_correlation']:.3f} |")
        lines.append("")
    (HERE / "iex_feasibility.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def main() -> None:
    stage, work = sys.argv[1], Path(sys.argv[2]).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    {"fetch": stage_fetch, "replicate": stage_replicate, "heldout": stage_heldout, "iex": stage_iex}[stage](work)


if __name__ == "__main__":
    main()
