"""5-minute opening-range breakout on "stocks in play" (Zarattini, Barbon, Aziz 2024), with trading costs.

The test pre-registered in PREREGISTRATION.md (same folder). Stages, each cached under <work_dir>
(outside the repo); <daily_dir> is the daily-bar cache of the dip-rules test
(~/.paper-training/dip-rules-full-market-20261112):

    <repo venv> python -I stocks_in_play_orb.py universe <work_dir> <daily_dir>
    ALPACA_KEY=... ALPACA_SECRET=... <repo venv> python -I stocks_in_play_orb.py fetch_or <work_dir>
    ALPACA_KEY=... ALPACA_SECRET=... <repo venv> python -I stocks_in_play_orb.py fetch_base <work_dir>
    <repo venv> python -I stocks_in_play_orb.py rank <work_dir>
    ALPACA_KEY=... ALPACA_SECRET=... <repo venv> python -I stocks_in_play_orb.py fetch_top <work_dir>
    <repo venv> python -I stocks_in_play_orb.py simulate <work_dir>
    <repo venv> python -I stocks_in_play_orb.py tables <work_dir> <this folder>

* ``universe``   daily filters (volume, ATR, split window) from the cached daily bars; the fetch sets.
* ``fetch_or``   the 5-minute bar stamped 09:30 ET for every stock in each session's fetch set.
* ``fetch_base`` full-session 1-minute bars of every filtered stock on the base-sample sessions.
* ``rank``       identity check of each opening bar, relative volume, each session's top 20.
* ``fetch_top``  full-session 1-minute bars of the selected stocks and SPY, every session.
* ``simulate``   stop entry, stop-loss and close exit on 1-minute bars, per order and intrabar rule.
* ``tables``     costs, variants, periods, the replication check and the verdict -> results.json, tables.md.

The fetch stages accept ``--from D --until D --reverse --interval S`` so several processes can share a download
while together staying under the plan's 200 requests a minute.

Market data only (GET https://data.alpaca.markets/v2/stocks/bars, feed=sip); never an account or an order
endpoint, never the deployed app. The keys come from the environment and are never written or printed.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import pickle
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ frozen constants (PREREGISTRATION.md)

FIRST_DAY = "2016-01-04"
LAST_SESSION = "2026-11-11"
PAPER_END = "2023-12-31"
HELD_OUT_START = "2024-01-01"
MIN_OPEN = 5.0
MIN_AVG_VOLUME = 1_000_000.0
MIN_ATR = 0.50
LOOKBACK = 14
TOP_N = 20
MIN_RELVOL = 1.0
STOP_ATR = 0.10
RISK = 0.01
COMMISSION = 0.0035                 # dollars per share per side
SLIPPAGE_CENTS = (0.0, 1.0, 2.0, 5.0)
SLIPPAGE_BPS = 5.0
PRIMARY_SLIPPAGE = "2c"
SPLIT_RANGE = (0.8, 1.25)           # day-to-day change of raw/adjusted close outside this = split
OR_TOLERANCE = 0.01                 # opening-range bar must sit inside the daily bar within 1%
BASE_SAMPLE_STEP = 5                # base strategy replicated on every 5th session of 2016-2023
DELAY_MINUTES = 15
ALIVE_SESSIONS = 10
T_REQUIRED = 2.0
TOP_SHARE = 0.01
NY = ZoneInfo("America/New_York")
URL = "https://data.alpaca.markets/v2/stocks/bars"
WORKERS = 3
REQUEST_INTERVAL = 0.32             # 3 threads sharing one gate: at most ~187 requests a minute (limit 200)
OR_BATCH = 400
MINUTE_BATCH = 25                   # 25 symbols x 390 bars stays under the 10,000-bar page
NO_BAR_RUN = 2                      # see the report: 2+ consecutive sessions without an opening bar = missing


# ------------------------------------------------------------------ daily bars -> eligibility

def load_daily(daily: Path) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Kept (identity-checked, de-duplicated) daily series of the dip-rules test, keyed 'CIK:TICKER'."""
    identity = pd.read_csv(daily / "identity.csv.gz", dtype=str)
    kept = identity[identity["kept"] == "True"].set_index("series")
    series = pd.read_csv(daily / "series.csv", dtype=str)
    by_asof = defaultdict(list)
    for row in series.itertuples(index=False):
        if row.series in kept.index:
            by_asof[row.asof].append(row)
    out = {}
    for asof, rows in by_asof.items():
        path = daily / "bars" / f"{asof}.pkl.gz"
        if not path.exists():
            continue
        frame = pd.read_pickle(path)
        frame = frame[frame["date"] <= LAST_SESSION]
        if frame.empty:
            continue
        parts = {ticker: part.reset_index(drop=True) for ticker, part in frame.groupby("ticker", sort=False)}
        for row in rows:
            part = parts.get(row.ticker)
            if part is not None and len(part):
                out[row.series] = part
    return out, identity, series


def stage_universe(work: Path, daily: Path) -> None:
    started = time.time()
    bars, identity, _series = load_daily(daily)
    spy = pd.read_pickle(daily / "spy.pkl.gz")
    spy = spy[(spy["date"] >= FIRST_DAY) & (spy["date"] <= LAST_SESSION)].reset_index(drop=True)
    calendar = spy["date"].tolist()
    position = {d: i for i, d in enumerate(calendar)}
    spy.to_pickle(work / "spy_daily.pkl")
    members = defaultdict(set)                                   # kept series -> every ticker it traded under
    for row in identity.itertuples(index=False):
        if row.kept == "True":
            members[row.series].add(row.ticker)
        elif isinstance(row.duplicate_of, str) and row.duplicate_of:
            members[row.duplicate_of].add(row.ticker)
    blocked = {row.series: set(str(row.blocked_years).split(",")) - {"", "nan"}
               for row in identity.itertuples(index=False) if row.kept == "True"}
    alive_from = calendar[-ALIVE_SESSIONS]
    rows, fetch, meta = [], defaultdict(set), {}
    for name, frame in bars.items():
        n = len(frame)
        if n < LOOKBACK + 2:
            continue
        dates = frame["date"].to_numpy(str)
        adj_close = frame["close"].to_numpy(float)
        raw_close = frame["raw_close"].to_numpy(float)
        factor = raw_close / adj_close                            # adjusted -> raw price scale of that day
        high, low = frame["high"].to_numpy(float) * factor, frame["low"].to_numpy(float) * factor
        opn = frame["open"].to_numpy(float) * factor
        # the previous close on today's scale: adjusted previous close times today's factor
        prev = np.concatenate([[np.nan], adj_close[:-1]]) * factor
        tr = np.nanmax(np.vstack([high - low, np.abs(high - prev), np.abs(low - prev)]), axis=0)
        tr[0] = high[0] - low[0]
        volume = frame["raw_volume"].to_numpy(float)
        atr = pd.Series(tr).rolling(LOOKBACK, min_periods=LOOKBACK).mean().shift(1).to_numpy()
        avg_volume = pd.Series(volume).rolling(LOOKBACK, min_periods=LOOKBACK).mean().shift(1).to_numpy()
        change = np.concatenate([[1.0], factor[1:] / factor[:-1]])
        split = (change < SPLIT_RANGE[0]) | (change > SPLIT_RANGE[1]) | ~np.isfinite(change)
        split_window = pd.Series(split.astype(float)).rolling(LOOKBACK + 1, min_periods=1).max().to_numpy() > 0
        # consecutive sessions: the 15 bars t-14..t must be 15 consecutive market sessions
        pos = np.array([position.get(d, -1) for d in dates])
        consecutive = np.zeros(n, dtype=bool)
        consecutive[LOOKBACK:] = (pos[LOOKBACK:] - pos[:-LOOKBACK] == LOOKBACK) & (pos[:-LOOKBACK] >= 0)
        years = np.array([d[:4] for d in dates])
        ok = (np.isfinite(atr) & (atr > MIN_ATR) & np.isfinite(avg_volume) & (avg_volume >= MIN_AVG_VOLUME)
              & ~split_window & consecutive & (pos >= 0))
        if blocked.get(name):
            ok &= ~np.isin(years, list(blocked[name]))
        meta[name] = {"tickers": sorted(members[name]), "delisted_since": bool(dates[-1] < alive_from),
                      "first_bar": dates[0], "last_bar": dates[-1]}
        for i in np.flatnonzero(ok):
            rows.append((dates[i], name, atr[i], avg_volume[i], raw_close[i - 1], opn[i], high[i], low[i],
                         raw_close[i], volume[i]))
            for j in range(i - LOOKBACK, i + 1):                  # the day itself and its 14 baseline days
                fetch[dates[j]].add(name)
    eligible = pd.DataFrame(rows, columns=["date", "series", "atr", "avg_volume", "prev_close", "day_open",
                                           "day_high", "day_low", "day_close", "day_volume"])
    eligible.to_pickle(work / "eligible.pkl")
    # the daily bar of every (series, day) in a fetch set, for the identity check of the opening-range bar
    daily_rows = []
    for name, frame in bars.items():
        if name not in meta:
            continue
        factor = frame["raw_close"].to_numpy(float) / frame["close"].to_numpy(float)
        sub = pd.DataFrame({"date": frame["date"], "series": name, "high": frame["high"].to_numpy(float) * factor,
                            "low": frame["low"].to_numpy(float) * factor, "volume": frame["raw_volume"]})
        keep = [name in fetch.get(d, ()) for d in sub["date"]]
        daily_rows.append(sub[keep])
    pd.concat(daily_rows, ignore_index=True).to_pickle(work / "daily_check.pkl")
    with open(work / "fetch_sets.pkl", "wb") as handle:
        pickle.dump({d: sorted(v) for d, v in fetch.items()}, handle)
    with open(work / "meta.json", "w") as handle:
        json.dump({"calendar": calendar, "series": meta}, handle)
    per_day = eligible.groupby("date").size().reindex(calendar).fillna(0)
    fetch_sizes = pd.Series({d: len(fetch.get(d, ())) for d in calendar})
    by_year = per_day.groupby(per_day.index.str[:4]).mean().round(0).astype(int).to_dict()
    print(f"{len(bars)} kept series, {len(calendar)} sessions {calendar[0]} to {calendar[-1]}; "
          f"{len(eligible)} eligible stock-days before the opening-price filter ({time.time() - started:.0f} s)")
    print("eligible per day, mean by year:", by_year)
    print(f"fetch set per day: mean {fetch_sizes.mean():.0f}, max {fetch_sizes.max():.0f}; "
          f"total stock-days {int(fetch_sizes.sum())}")
    multi = sum(1 for m in meta.values() if len(m["tickers"]) > 1)
    print(f"series with more than one ticker: {multi}")


# ------------------------------------------------------------------ Alpaca minute bars

_gate = threading.Lock()
_next_request = [0.0]


def _get(params: dict, key: str, secret: str) -> dict:
    request = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params),
                                     headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                                              "Accept-Encoding": "gzip"})
    for attempt in range(8):
        with _gate:
            wait = _next_request[0] - time.monotonic()
            _next_request[0] = max(time.monotonic(), _next_request[0]) + REQUEST_INTERVAL
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            if exc.code in (400, 422):
                raise ValueError(exc.read().decode("utf-8", "replace")[:200]) from exc
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Alpaca bars request kept failing")


def _utc(day: str, hour: int, minute: int, second: int = 0) -> str:
    local = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), hour, minute, second, tzinfo=NY)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minute_of_day(stamp: str) -> int:
    local = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(NY)
    return local.hour * 60 + local.minute


def fetch_bars(symbols: list[str], day: str, timeframe: str, start: str, end: str, key: str, secret: str) -> dict:
    """Raw SIP bars of one session for a symbol list, with ``asof`` = that session. A rejected symbol splits the batch."""
    out: dict[str, list] = {}
    token = None
    try:
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start, "end": end, "asof": day,
                      "adjustment": "raw", "feed": "sip", "limit": 10000}
            if token:
                params["page_token"] = token
            body = _get(params, key, secret)
            for symbol, rows in (body.get("bars") or {}).items():
                out.setdefault(symbol, []).extend(rows)
            token = body.get("next_page_token")
            if not token:
                return out
    except ValueError:
        if len(symbols) == 1:
            return {}
        half = len(symbols) // 2
        return {**fetch_bars(symbols[:half], day, timeframe, start, end, key, secret),
                **fetch_bars(symbols[half:], day, timeframe, start, end, key, secret)}


def _load_meta(work: Path) -> dict:
    with open(work / "meta.json") as handle:
        return json.load(handle)


def _span(days: list[str], argv: list[str]) -> list[str]:
    """``--from D`` / ``--until D`` / ``--reverse``: lets several processes share one download by date range."""
    if "--from" in argv:
        days = [d for d in days if d >= argv[argv.index("--from") + 1]]
    if "--until" in argv:
        days = [d for d in days if d < argv[argv.index("--until") + 1]]
    return days[::-1] if "--reverse" in argv else days


def stage_fetch_or(work: Path, argv: list[str]) -> None:
    """The 5-minute bar stamped 09:30 ET of every stock in each session's fetch set."""
    key, secret = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
    meta = _load_meta(work)
    with open(work / "fetch_sets.pkl", "rb") as handle:
        fetch_sets = pickle.load(handle)
    target = work / "or"
    target.mkdir(exist_ok=True)
    days = [d for d in meta["calendar"] if d in fetch_sets and not (target / f"{d}.pkl").exists()]
    days = _span(days, argv)
    print(f"{len(meta['calendar'])} sessions, {len(days)} to fetch", flush=True)

    def one(day: str):
        if (target / f"{day}.pkl").exists():
            return day, 0, 0
        symbols = sorted({t for name in fetch_sets[day] for t in meta["series"][name]["tickers"]})
        start, end = _utc(day, 9, 30), _utc(day, 9, 34, 59)
        bars = {}
        for i in range(0, len(symbols), OR_BATCH):
            got = fetch_bars(symbols[i:i + OR_BATCH], day, "5Min", start, end, key, secret)
            for symbol, rows in got.items():
                rows = [r for r in rows if _minute_of_day(r["t"]) == 570]
                if rows:
                    r = rows[0]
                    bars[symbol] = (r["o"], r["h"], r["l"], r["c"], r["v"])
        tmp = target / f"{day}.{os.getpid()}.tmp"
        with open(tmp, "wb") as handle:
            pickle.dump(bars, handle)
        tmp.rename(target / f"{day}.pkl")
        return day, len(symbols), len(bars)

    started, done = time.time(), 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for day, asked, got in pool.map(one, days):
            done += 1
            if done % 25 == 0 or done == len(days):
                print(f"{done}/{len(days)} sessions ({time.time() - started:.0f} s); {day}: {got}/{asked} symbols",
                      flush=True)


# ------------------------------------------------------------------ full-session minute bars

def _minute_arrays(rows: list) -> np.ndarray:
    """Rows of one symbol -> array [minute of day ET, open, high, low, close, volume], sorted by time."""
    out = np.array([(_minute_of_day(r["t"]), r["o"], r["h"], r["l"], r["c"], r["v"]) for r in rows], dtype=float)
    return out[np.argsort(out[:, 0], kind="stable")]


def _fetch_minutes(work: Path, folder: str, wanted: dict, argv: list[str]) -> None:
    key, secret = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
    target = work / folder
    target.mkdir(exist_ok=True)
    days = _span(sorted(d for d in wanted if not (target / f"{d}.pkl.gz").exists()), argv)
    print(f"{folder}: {len(wanted)} sessions, {len(days)} to fetch", flush=True)

    def one(day: str):
        if (target / f"{day}.pkl.gz").exists():
            return day, 0, 0
        symbols = sorted(wanted[day])
        start, end = _utc(day, 9, 30), _utc(day, 15, 59, 59)
        bars = {}
        for i in range(0, len(symbols), MINUTE_BATCH):
            got = fetch_bars(symbols[i:i + MINUTE_BATCH], day, "1Min", start, end, key, secret)
            for symbol, rows in got.items():
                if rows:
                    bars[symbol] = _minute_arrays(rows)
        tmp = target / f"{day}.{os.getpid()}.tmp"
        with gzip.open(tmp, "wb", compresslevel=4) as handle:
            pickle.dump(bars, handle)
        tmp.rename(target / f"{day}.pkl.gz")
        return day, len(symbols), len(bars)

    started, done = time.time(), 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for day, asked, got in pool.map(one, days):
            done += 1
            if done % 25 == 0 or done == len(days):
                print(f"{done}/{len(days)} sessions ({time.time() - started:.0f} s); {day}: {got}/{asked} symbols",
                      flush=True)


def base_sample_days(calendar: list[str]) -> list[str]:
    return [d for i, d in enumerate(calendar) if i % BASE_SAMPLE_STEP == 0 and d <= PAPER_END]


def stage_fetch_base(work: Path, argv: list[str]) -> None:
    """Full-session bars of every stock that passes the daily filters, on the base-sample sessions.

    Needs only the daily filters, so it can run while the opening-range download is still going. Every ticker a
    series ever carried is requested; the simulation keeps the one whose opening-range bar passed the identity check.
    """
    meta = _load_meta(work)
    eligible = pd.read_pickle(work / "eligible.pkl")
    sample = set(base_sample_days(meta["calendar"]))
    eligible = eligible[eligible["date"].isin(sample)]
    wanted = defaultdict(set)
    for day, name in zip(eligible["date"], eligible["series"]):
        wanted[day].update(meta["series"][name]["tickers"])
    _fetch_minutes(work, "minutes_base", wanted, argv)


def stage_fetch_top(work: Path, argv: list[str]) -> None:
    """Full-session bars of each session's selected stocks, and of SPY (half-day detection)."""
    orders = pd.read_pickle(work / "orders.pkl")
    top = orders[orders["top20"]]
    wanted = defaultdict(set)
    for day in _load_meta(work)["calendar"]:
        wanted[day].add("SPY")
    for day, ticker in zip(top["date"], top["ticker"]):
        wanted[day].add(ticker)
    _fetch_minutes(work, "minutes_top", wanted, argv)


# ------------------------------------------------------------------ ranking

def stage_rank(work: Path) -> None:
    """Identity-check each opening-range bar, compute relative volume, pick each session's top 20."""
    started = time.time()
    meta = _load_meta(work)
    calendar = meta["calendar"]
    position = {d: i for i, d in enumerate(calendar)}
    with open(work / "fetch_sets.pkl", "rb") as handle:
        fetch_sets = pickle.load(handle)
    check = pd.read_pickle(work / "daily_check.pkl")
    names = sorted(meta["series"])
    row_of = {name: i for i, name in enumerate(names)}
    n_days = len(calendar)
    volume = np.full((len(names), n_days), np.nan)            # NaN = not fetched or discarded; 0 = no bar
    status = np.zeros((len(names), n_days), dtype=np.int8)     # 0 not fetched, 1 ok, 2 no bar, 3 discarded
    bar = {}                                                   # (row, day index) -> (ticker, o, h, l, c, v)
    counts = defaultdict(int)
    for day, part in check.groupby("date", sort=True):
        j = position.get(day)
        path = work / "or" / f"{day}.pkl"
        if j is None or not path.exists():
            continue
        with open(path, "rb") as handle:
            bars = pickle.load(handle)
        wanted = set(fetch_sets.get(day, ()))
        for name, high, low, vol in zip(part["series"], part["high"], part["low"], part["volume"]):
            if name not in wanted:
                continue
            i = row_of[name]
            found = [(t, bars[t]) for t in meta["series"][name]["tickers"] if t in bars]
            good = [(t, b) for t, b in found
                    if b[1] <= high * (1 + OR_TOLERANCE) and b[2] >= low * (1 - OR_TOLERANCE)
                    and b[4] <= vol * (1 + OR_TOLERANCE)]
            if good:
                ticker, b = good[0]
                volume[i, j], status[i, j] = b[4], 1
                bar[(i, j)] = (ticker, *b)
                counts["ok"] += 1
            elif found:
                status[i, j] = 3
                counts["discarded"] += 1
            else:
                volume[i, j], status[i, j] = 0.0, 2
                counts["no_bar"] += 1
    # runs of consecutive no-bar sessions per stock (a halt is one session; an unknown ticker is many)
    runs = defaultdict(int)
    for i in range(len(names)):
        none = status[i] == 2
        if not none.any():
            continue
        edges = np.diff(np.concatenate([[0], none.astype(np.int8), [0]]))
        for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
            runs[min(b - a, 10)] += 1
            if b - a >= NO_BAR_RUN:
                volume[i, a:b] = np.nan                        # identity unresolved: missing, not zero
                counts["no_bar_run_missing"] += b - a
    baseline = pd.DataFrame(volume.T).rolling(LOOKBACK, min_periods=LOOKBACK).mean().shift(1).to_numpy().T
    eligible = pd.read_pickle(work / "eligible.pkl")
    sample = set(base_sample_days(calendar))
    rows = []
    for day, name, atr in zip(eligible["date"], eligible["series"], eligible["atr"]):
        i, j = row_of[name], position[day]
        counts["eligible_daily"] += 1
        b = bar.get((i, j))
        if b is None:
            counts["eligible_no_or_bar"] += 1
            continue
        ticker, o, h, l, c, v = b
        if not o > MIN_OPEN:
            counts["eligible_open_at_most_5"] += 1
            continue
        base = baseline[i, j]
        rv = v / base if np.isfinite(base) and base > 0 else np.nan
        rows.append((day, name, ticker, atr, o, h, l, c, v, rv))
    orders = pd.DataFrame(rows, columns=["date", "series", "ticker", "atr", "or_open", "or_high", "or_low",
                                         "or_close", "or_volume", "rv"])
    orders["side"] = np.sign(orders["or_close"] - orders["or_open"]).astype(int)
    ranked = orders[orders["rv"] >= MIN_RELVOL].sort_values(["date", "rv", "series"], ascending=[True, False, True])
    orders["rank"] = ranked.groupby("date").cumcount() + 1
    orders["top20"] = orders["rank"] <= TOP_N
    orders["base_sample"] = orders["date"].isin(sample)
    orders["delisted_since"] = orders["series"].map(lambda n: meta["series"][n]["delisted_since"])
    keep = orders[orders["top20"] | orders["base_sample"]].reset_index(drop=True)
    keep.to_pickle(work / "orders.pkl")
    per_day = orders.groupby("date").agg(candidates=("series", "size"), ranked=("rank", "count"),
                                         rv_missing=("rv", lambda s: int(s.isna().sum())))
    cut = orders[orders["rank"] == TOP_N].set_index("date")["rv"]
    per_day["rv_20th"] = cut
    per_day = per_day.reindex(calendar)
    per_day.to_csv(work / "rank_by_day.csv")
    coverage = {**{k: int(v) for k, v in counts.items()},
                "no_bar_run_lengths": {str(k): int(v) for k, v in sorted(runs.items())},
                "sessions": n_days, "sessions_with_or_file": int(sum((work / "or" / f"{d}.pkl").exists() for d in calendar)),
                "candidates": int(len(orders)), "candidates_rv_missing": int(orders["rv"].isna().sum()),
                "sessions_under_20_ranked": int((per_day["ranked"].fillna(0) < TOP_N).sum()),
                "median_rv_20th": float(cut.median()), "top20_orders": int(orders["top20"].sum()),
                "top20_long": int((orders["top20"] & (orders["side"] > 0)).sum()),
                "top20_short": int((orders["top20"] & (orders["side"] < 0)).sum()),
                "top20_doji": int((orders["top20"] & (orders["side"] == 0)).sum()),
                "base_sample_sessions": len(sample), "base_sample_orders": int(orders["base_sample"].sum())}
    with open(work / "coverage.json", "w") as handle:
        json.dump(coverage, handle, indent=1)
    print(json.dumps(coverage, indent=1))
    print(f"rank done ({time.time() - started:.0f} s)")


# ------------------------------------------------------------------ simulation

OPEN_MINUTE, ENTRY_MINUTE, CLOSE_MINUTE, HALF_DAY_MINUTE = 570, 575, 960, 780


def session_end(spy: np.ndarray | None) -> int:
    """16:00 ET, or 13:00 ET when SPY's volume from 13:05 on is under 5% of its volume before 13:00."""
    if spy is None or not len(spy):
        return CLOSE_MINUTE
    minute, volume = spy[:, 0], spy[:, 5]
    morning = volume[(minute >= OPEN_MINUTE) & (minute < HALF_DAY_MINUTE)].sum()
    afternoon = volume[(minute >= HALF_DAY_MINUTE + 5) & (minute < CLOSE_MINUTE)].sum()
    return HALF_DAY_MINUTE if morning > 0 and afternoon < 0.05 * morning else CLOSE_MINUTE


def simulate_order(bars: np.ndarray, side: int, level: float, atr: float, start: int, end: int,
                   optimistic: bool) -> tuple | None:
    """One stop entry with a 10%-of-ATR stop-loss and an exit at the close, on 1-minute bars.

    Returns (entry minute, entry price, exit minute, exit price, reason, same_bar) or None when never triggered.
    ``same_bar`` says the entry bar's range also reached the stop-loss; the worst-case rule stops the trade there.
    """
    window = bars[(bars[:, 0] >= start) & (bars[:, 0] < end)]
    if not len(window):
        return None
    minute, opn, high, low, close = window[:, 0], window[:, 1], window[:, 2], window[:, 3], window[:, 4]
    hit = np.flatnonzero(high >= level) if side > 0 else np.flatnonzero(low <= level)
    if not len(hit):
        return None
    i = int(hit[0])
    entry = max(level, opn[i]) if side > 0 else min(level, opn[i])
    stop = entry - side * STOP_ATR * atr
    same = bool(low[i] <= stop) if side > 0 else bool(high[i] >= stop)
    if same and not optimistic:
        return int(minute[i]), entry, int(minute[i]), stop, "stop_same_bar", True
    later = np.flatnonzero(low[i + 1:] <= stop) if side > 0 else np.flatnonzero(high[i + 1:] >= stop)
    if len(later):
        j = i + 1 + int(later[0])
        price = min(stop, opn[j]) if side > 0 else max(stop, opn[j])
        return int(minute[i]), entry, int(minute[j]), price, "stop", same
    return int(minute[i]), entry, int(minute[-1]), close[-1], "close", same


MODES = {"worst": (ENTRY_MINUTE, False), "optimistic": (ENTRY_MINUTE, True),
         "delay15": (ENTRY_MINUTE + DELAY_MINUTES, False)}


def _load_minutes(path: Path) -> dict:
    if not path.exists():
        return {}
    with gzip.open(path, "rb") as handle:
        return pickle.load(handle)


def stage_simulate(work: Path) -> None:
    started = time.time()
    meta = _load_meta(work)
    orders = pd.read_pickle(work / "orders.pkl")
    ends, rows, missing = {}, [], defaultdict(int)
    for day, part in orders.groupby("date", sort=True):
        top = _load_minutes(work / "minutes_top" / f"{day}.pkl.gz")
        end = ends[day] = session_end(top.get("SPY"))
        base = _load_minutes(work / "minutes_base" / f"{day}.pkl.gz") if part["base_sample"].any() else {}
        for order in part.itertuples(index=False):
            if order.side == 0:
                continue
            level = order.or_high if order.side > 0 else order.or_low
            for group, flag, source, modes in (("top20", order.top20, top, ("worst", "optimistic", "delay15")),
                                               ("base", order.base_sample, base, ("worst", "optimistic"))):
                if not flag:
                    continue
                bars = source.get(order.ticker)
                if bars is None:
                    missing[group] += 1
                    continue
                for mode in modes:
                    start, optimistic = MODES[mode]
                    result = simulate_order(bars, order.side, level, order.atr, start, end, optimistic)
                    if result is None:
                        continue
                    rows.append((group, mode, day, order.series, order.ticker, order.side, order.rank, order.rv,
                                 order.atr, order.or_open, level, *result, order.delisted_since))
    trades = pd.DataFrame(rows, columns=["group", "mode", "date", "series", "ticker", "side", "rank", "rv", "atr",
                                         "or_open", "level", "entry_minute", "entry", "exit_minute", "exit",
                                         "reason", "same_bar", "delisted_since"])
    trades.to_pickle(work / "trades.pkl")
    with open(work / "sessions.json", "w") as handle:
        json.dump({"session_end": ends, "orders_without_minute_bars": dict(missing),
                   "half_days": sorted(d for d, e in ends.items() if e == HALF_DAY_MINUTE)}, handle)
    print(f"{len(trades)} simulated trades; orders without minute bars {dict(missing)}; "
          f"{sum(1 for e in ends.values() if e == HALF_DAY_MINUTE)} half days ({time.time() - started:.0f} s)")
    print(trades.groupby(["group", "mode"]).size().to_string())


# ------------------------------------------------------------------ statistics

COSTS = {"0c": ("cents", 0.0), "1c": ("cents", 1.0), "2c": ("cents", 2.0), "5c": ("cents", 5.0),
         "5bps": ("bps", SLIPPAGE_BPS)}
VARIANTS = {"A": ("both", 4.0), "B": ("both", 1.0), "C": ("long", 1.0)}
PERIODS = {"replication": (FIRST_DAY, "2023-12-29"), "held_out": (HELD_OUT_START, LAST_SESSION)}


def cost_per_share(trades: pd.DataFrame, kind: str, value: float) -> np.ndarray:
    """Round-trip commission plus slippage, dollars per share."""
    if kind == "cents":
        return np.full(len(trades), 2 * (COMMISSION + value / 100.0))
    return 2 * COMMISSION + value / 1e4 * (trades["entry"].to_numpy(float) + trades["exit"].to_numpy(float))


def with_returns(trades: pd.DataFrame, kind: str, value: float) -> pd.DataFrame:
    entry, exit_ = trades["entry"].to_numpy(float), trades["exit"].to_numpy(float)
    side, atr = trades["side"].to_numpy(float), trades["atr"].to_numpy(float)
    move = side * (exit_ - entry)
    cost = cost_per_share(trades, kind, value)
    return trades.assign(gross=move / entry, net=(move - cost) / entry, r=(move - cost) / (STOP_ATR * atr),
                         leverage=RISK * entry / (STOP_ATR * atr))


def clustered_t(values: np.ndarray, clusters: np.ndarray) -> float | None:
    """t of the mean with standard errors clustered by session."""
    if len(values) < 10:
        return None
    mean = float(values.mean())
    sums = pd.Series(values - mean).groupby(clusters).sum().to_numpy()
    g = len(sums)
    if g < 2:
        return None
    variance = float((sums ** 2).sum()) * g / (g - 1) / len(values) ** 2
    return mean / math.sqrt(variance) if variance > 0 else None


def daily_returns(trades: pd.DataFrame, weights: np.ndarray, days: list[str]) -> pd.Series:
    contribution = pd.Series(weights * trades["net"].to_numpy(float)).groupby(trades["date"].to_numpy()).sum()
    return contribution.reindex(days).fillna(0.0)


def portfolio_stats(daily: pd.Series, spy: pd.Series) -> dict:
    equity = (1 + daily).cumprod()
    n = len(daily)
    annual = float(equity.iloc[-1] ** (252 / n) - 1) if equity.iloc[-1] > 0 else -1.0
    vol = float(daily.std(ddof=1) * math.sqrt(252))
    drawdown = float((equity / equity.cummax() - 1).min())
    traded = daily[daily != 0]
    return {"annual_return": annual, "total_return": float(equity.iloc[-1] - 1), "volatility": vol,
            "sharpe": float(daily.mean() / daily.std(ddof=1) * math.sqrt(252)) if daily.std(ddof=1) > 0 else None,
            "max_drawdown": drawdown, "worst_day": float(daily.min()),
            "corr_spy": float(np.corrcoef(daily.to_numpy(), spy.reindex(daily.index).to_numpy())[0, 1])
            if daily.std() > 0 else None,
            "days": n, "days_traded": int(len(traded)),
            "days_positive_share": float((traded > 0).mean()) if len(traded) else None}


def trade_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0}
    net = trades["net"].to_numpy(float)
    k = int(math.floor(len(net) * TOP_SHARE))
    trimmed = np.sort(net)[:len(net) - k] if k else net
    return {"trades": int(len(net)), "stocks": int(trades["series"].nunique()),
            "mean_net_bps": float(net.mean() * 1e4), "t_day_clustered": clustered_t(net, trades["date"].to_numpy()),
            "median_net_bps": float(np.median(net) * 1e4), "win_rate": float((net > 0).mean()),
            "mean_gross_bps": float(trades["gross"].mean() * 1e4), "mean_r": float(trades["r"].mean()),
            "trimmed_mean_net_bps": float(trimmed.mean() * 1e4),
            "mean_entry_price": float(trades["entry"].mean()), "median_entry_price": float(trades["entry"].median()),
            "stopped_share": float(trades["reason"].isin(["stop", "stop_same_bar"]).mean()),
            "same_bar_share": float(trades["same_bar"].mean()),
            "delisted_share": float(trades["delisted_since"].mean())}


def spy_returns(spy: pd.DataFrame) -> pd.Series:
    close = spy["close"].to_numpy(float)
    previous = np.concatenate([[float(spy["open"].iloc[0])], close[:-1]])
    return pd.Series(close / previous - 1, index=spy["date"].to_numpy())


def select(trades: pd.DataFrame, variant: str) -> tuple[pd.DataFrame, float]:
    sides, cap = VARIANTS[variant]
    return (trades[trades["side"] > 0] if sides == "long" else trades), cap


def evaluate(trades: pd.DataFrame, variant: str, days: list[str], spy: pd.Series, slots: pd.Series | None) -> dict:
    """Per-trade and portfolio statistics of one variant over one period at one cost (trades carry returns)."""
    part, cap = select(trades, variant)
    part = part[(part["date"] >= days[0]) & (part["date"] <= days[-1])]
    out = trade_stats(part)
    slot = (1.0 / TOP_N) if slots is None else (1.0 / part["date"].map(slots).to_numpy(float))
    weights = np.minimum(part["leverage"].to_numpy(float), cap) * slot
    daily = daily_returns(part, weights, days)
    out.update(portfolio_stats(daily, spy))
    out["mean_gross_exposure"] = float(pd.Series(weights).groupby(part["date"].to_numpy()).sum().reindex(days)
                                       .fillna(0).mean())
    return out


def break_even(trades: pd.DataFrame, variant: str, days: list[str], spy: pd.Series, spy_annual: float) -> dict:
    part, _cap = select(trades, variant)
    part = part[(part["date"] >= days[0]) & (part["date"] <= days[-1])]
    entry, exit_, side = (part[c].to_numpy(float) for c in ("entry", "exit", "side"))
    gross = side * (exit_ - entry) / entry
    inverse = (1 / entry).mean()
    per_trade = float(gross.mean() / (2 * inverse) - COMMISSION) * 100            # cents per share per side
    grid = np.round(np.arange(0.0, 20.01, 0.1), 1)
    annual = [evaluate(with_returns(part, "cents", c), variant, days, spy, None)["annual_return"] for c in grid]

    def crossing(target: float):
        if annual[0] <= target:
            return None
        for a, b, x, y in zip(annual, annual[1:], grid, grid[1:]):
            if a > target >= b:
                return float(x + (a - target) / (a - b) * (y - x))
        return float(grid[-1])
    return {"per_trade_cents": per_trade, "annual_zero_cents": crossing(0.0), "annual_spy_cents": crossing(spy_annual),
            "annual_at_zero_slippage": float(annual[0])}


def bucket_table(trades: pd.DataFrame, column: str, edges: list[float], labels: list[str]) -> list[dict]:
    out = []
    groups = pd.cut(trades[column], edges, labels=labels, right=False)
    for label in labels:
        part = trades[groups == label]
        if len(part):
            out.append({"bucket": label, **{k: v for k, v in trade_stats(part).items()
                                            if k in ("trades", "mean_net_bps", "t_day_clustered", "win_rate",
                                                     "mean_r", "mean_gross_bps", "median_entry_price")}})
    return out


def stage_tables(work: Path, folder: Path) -> None:
    meta = _load_meta(work)
    calendar = meta["calendar"]
    spy_frame = pd.read_pickle(work / "spy_daily.pkl")
    spy = spy_returns(spy_frame)
    trades = pd.read_pickle(work / "trades.pkl")
    orders = pd.read_pickle(work / "orders.pkl")
    with open(work / "coverage.json") as handle:
        coverage = json.load(handle)
    with open(work / "sessions.json") as handle:
        sessions = json.load(handle)
    period_days = {name: [d for d in calendar if a <= d <= b] for name, (a, b) in PERIODS.items()}
    sample = base_sample_days(calendar)
    base_orders = orders[orders["base_sample"] & (orders["side"] != 0)]
    slots = base_orders.groupby("date").size()
    results: dict = {"coverage": coverage, "sessions": {k: v for k, v in sessions.items() if k != "session_end"},
                     "periods": {k: {"first": v[0], "last": v[-1], "sessions": len(v)} for k, v in period_days.items()}}
    spy_annual = {}
    for name, days in period_days.items():
        r = spy.reindex(days)
        spy_annual[name] = float((1 + r).prod() ** (252 / len(days)) - 1)
        results["periods"][name]["spy"] = portfolio_stats(r, spy)
    # every variant, period, cost, mode on the top 20
    top = trades[trades["group"] == "top20"]
    table = {}
    for mode in MODES:
        part_mode = top[top["mode"] == mode]
        for cost, (kind, value) in COSTS.items():
            priced = with_returns(part_mode, kind, value)
            for variant in VARIANTS:
                for period, days in period_days.items():
                    table[f"{mode}|{variant}|{period}|{cost}"] = evaluate(priced, variant, days, spy, None)
    results["top20"] = table
    # base strategy on the sample sessions of the paper's period, paper's sizing (4x cap, equal split of the orders)
    base = trades[trades["group"] == "base"]
    base_table = {}
    for mode in ("worst", "optimistic"):
        for cost in ("0c", PRIMARY_SLIPPAGE):
            priced = with_returns(base[base["mode"] == mode], *COSTS[cost])
            stats = evaluate(priced, "A", sample, spy, slots)
            stats["orders"] = int(len(base_orders))
            stats["by_relative_volume"] = bucket_table(priced, "rv", [0, 1, 2, 5, 10, 30, np.inf],
                                                       ["under 1x", "1-2x", "2-5x", "5-10x", "10-30x", "over 30x"])
            base_table[f"{mode}|{cost}"] = stats
            # the top 20 on the same sample sessions, for a like-for-like comparison
            priced_top = with_returns(top[(top["mode"] == mode) & top["date"].isin(sample)], *COSTS[cost])
            base_table[f"{mode}|{cost}|top20_same_sessions"] = evaluate(priced_top, "A", sample, spy, None)
    results["base"] = base_table
    # replication check
    a_worst = table["worst|A|replication|0c"]["annual_return"] * 100
    a_opt = table["optimistic|A|replication|0c"]["annual_return"] * 100
    b_worst = base_table["worst|0c"]["annual_return"] * 100
    b_opt = base_table["optimistic|0c"]["annual_return"] * 100
    results["replication_check"] = {
        "paper_top20": 41.6, "top20_worst": a_worst, "top20_optimistic": a_opt,
        "top20_in_bracket": bool(a_worst - 10 <= 41.6 <= a_opt + 10),
        "paper_base": 3.2, "base_worst": b_worst, "base_optimistic": b_opt,
        "base_in_bracket": bool(b_worst - 10 <= 3.2 <= b_opt + 10),
        "top20_above_base": bool(base_table["worst|0c|top20_same_sessions"]["annual_return"] * 100 > b_worst
                                 and base_table["optimistic|0c|top20_same_sessions"]["annual_return"] * 100 > b_opt)}
    results["replication_check"]["reproduced"] = bool(
        results["replication_check"]["top20_in_bracket"] and results["replication_check"]["base_in_bracket"]
        and results["replication_check"]["top20_above_base"])
    # verdict: C, held-out, primary cost, worst case
    primary = table[f"worst|C|held_out|{PRIMARY_SLIPPAGE}"]
    criteria = {
        "1_profit_per_trade": bool(primary["trades"] and primary["mean_net_bps"] > 0
                                   and (primary["t_day_clustered"] or 0) >= T_REQUIRED),
        "2_beats_spy": bool(primary["annual_return"] > spy_annual["held_out"]),
        "3_without_top_1pct": bool(primary["trades"] and primary["trimmed_mean_net_bps"] > 0)}
    results["verdict"] = {"variant": "C", "period": "held_out", "cost": PRIMARY_SLIPPAGE, "criteria": criteria,
                          "passes": all(criteria.values()), "spy_annual": spy_annual["held_out"]}
    worst = top[top["mode"] == "worst"]
    results["break_even"] = {f"{variant}|{period}": break_even(worst, variant, days, spy, spy_annual[period])
                             for variant in VARIANTS for period, days in period_days.items()}
    results["break_even_optimistic"] = {
        f"C|{period}": break_even(top[top["mode"] == "optimistic"], "C", days, spy, spy_annual[period])
        for period, days in period_days.items()}
    # buckets for C (worst case) at zero and at the primary cost, each period
    buckets = {}
    for cost in ("0c", PRIMARY_SLIPPAGE):
        priced = with_returns(worst[worst["side"] > 0], *COSTS[cost])
        priced = priced.assign(atr_share=priced["atr"] / priced["or_open"], year=priced["date"].str[:4])
        for period, days in period_days.items():
            part = priced[(priced["date"] >= days[0]) & (priced["date"] <= days[-1])]
            buckets[f"{period}|{cost}"] = {
                "price": bucket_table(part, "or_open", [5, 10, 20, 50, 100, np.inf],
                                      ["$5-10", "$10-20", "$20-50", "$50-100", "over $100"]),
                "atr_share": bucket_table(part, "atr_share", [0, 0.03, 0.05, 0.08, np.inf],
                                          ["under 3%", "3-5%", "5-8%", "over 8%"]),
                "relative_volume": bucket_table(part, "rv", [1, 2, 5, 10, np.inf], ["1-2x", "2-5x", "5-10x", "over 10x"]),
                "delisted": [{"bucket": label, **{k: v for k, v in trade_stats(part[part["delisted_since"] == flag]).items()
                                                  if k in ("trades", "mean_net_bps", "t_day_clustered", "win_rate", "mean_r")}}
                             for label, flag in (("still listed", False), ("delisted since", True))
                             if (part["delisted_since"] == flag).any()]}
        years = {}
        for variant in VARIANTS:
            priced_v = with_returns(select(worst, variant)[0], *COSTS[cost])
            for year, part in priced_v.groupby(priced_v["date"].str[:4]):
                days = [d for d in calendar if d[:4] == year]
                stats = evaluate(part, variant, days, spy, None)
                r = spy.reindex(days)
                years[f"{variant}|{year}"] = {k: stats.get(k) for k in ("trades", "mean_net_bps", "t_day_clustered",
                                                                       "mean_r", "annual_return", "total_return")}
                years[f"{variant}|{year}"]["spy_return"] = float((1 + r).prod() - 1)
        buckets[f"by_year|{cost}"] = years
    results["buckets_C"] = buckets
    top_orders = orders[orders["top20"]]
    placed = top_orders[top_orders["side"] != 0]
    results["orders"] = {
        "top20_orders_placed": int(len(placed)),
        "trigger_rate_worst": float(len(worst) / len(placed)) if len(placed) else None,
        "trigger_rate_delay15": float((top["mode"] == "delay15").sum() / len(placed)) if len(placed) else None,
        "median_rv_selected": float(top_orders["rv"].median()), "median_atr_share": float((top_orders["atr"] / top_orders["or_open"]).median()),
        "median_stop_cents": float((STOP_ATR * top_orders["atr"]).median() * 100),
        "median_price": float(top_orders["or_open"].median()),
        "delisted_share_orders": float(top_orders["delisted_since"].mean())}
    with open(folder / "results.json", "w") as handle:
        json.dump(results, handle, indent=1, default=float)
    columns = ["mode", "date", "series", "ticker", "side", "rank", "rv", "atr", "or_open", "level", "entry_minute",
               "entry", "exit_minute", "exit", "reason", "same_bar", "delisted_since"]
    top[columns].round(6).to_csv(folder / "trades_top20.csv.gz", index=False)
    write_tables(results, folder / "tables.md")
    print(json.dumps(results["replication_check"], indent=1))
    print(json.dumps(results["verdict"], indent=1))
    print(json.dumps(results["break_even"], indent=1))


def _fmt(value, kind: str = "") -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    if kind == "pct":
        return f"{value * 100:+.1f}%"
    if kind == "bps":
        return f"{value:+.1f}"
    if kind == "t":
        return f"{value:+.2f}"
    if kind == "r":
        return f"{value:+.3f}"
    if kind == "n":
        return f"{int(value):,}"
    return f"{value:.2f}"


ROW = [("trades", "n"), ("win_rate", "pct"), ("mean_net_bps", "bps"), ("t_day_clustered", "t"), ("mean_r", "r"),
       ("trimmed_mean_net_bps", "bps"), ("annual_return", "pct"), ("volatility", "pct"), ("sharpe", ""),
       ("max_drawdown", "pct"), ("worst_day", "pct"), ("corr_spy", ""), ("days_positive_share", "pct")]
HEAD = ("| Variant | Cost | Trades | Win rate | Net bps/trade | t (by day) | Net R/trade | Net bps w/o top 1% | "
        "Annual return | Volatility | Sharpe | Max drawdown | Worst day | Corr. SPY | Days up |\n" + "|---" * 15 + "|\n")


def write_tables(results: dict, path: Path) -> None:
    lines = ["# Stocks in play breakout: tables (generated by stocks_in_play_orb.py tables)\n"]
    for mode, title in (("worst", "Worst-case intrabar rule (used for the verdict)"),
                        ("optimistic", "Optimistic intrabar rule (replication and side result)"),
                        ("delay15", "Entries delayed 15 minutes (side result; worst-case intrabar rule)")):
        for period in PERIODS:
            info = results["periods"][period]
            lines.append(f"\n## {title}: {period.replace('_', '-')} {info['first']} to {info['last']} "
                         f"({info['sessions']} sessions; SPY {_fmt(info['spy']['annual_return'], 'pct')} a year)\n\n")
            lines.append(HEAD)
            for variant in VARIANTS:
                for cost in COSTS:
                    row = results["top20"][f"{mode}|{variant}|{period}|{cost}"]
                    lines.append(f"| {variant} | {cost} | " + " | ".join(_fmt(row.get(k), kind) for k, kind in ROW) + " |\n")
    lines.append("\n## Base strategy (every filtered stock) on the sample sessions, 2016-2023, paper sizing\n\n" + HEAD)
    for key, row in results["base"].items():
        mode, cost, *rest = key.split("|")
        label = "top 20, same sessions" if rest else "base"
        lines.append(f"| {label} ({mode}) | {cost} | " + " | ".join(_fmt(row.get(k), kind) for k, kind in ROW) + " |\n")
    lines.append("\n### Base strategy by relative volume (net R per trade, commission only)\n\n")
    lines.append("| Intrabar rule | Bucket | Trades | Net R/trade | Net bps/trade | Win rate |\n|---|---|---|---|---|---|\n")
    for mode in ("worst", "optimistic"):
        for b in results["base"][f"{mode}|0c"]["by_relative_volume"]:
            lines.append(f"| {mode} | {b['bucket']} | {b['trades']:,} | {_fmt(b['mean_r'], 'r')} | "
                         f"{_fmt(b['mean_net_bps'], 'bps')} | {_fmt(b['win_rate'], 'pct')} |\n")
    for key, groups in results["buckets_C"].items():
        if key.startswith("by_year"):
            lines.append(f"\n## By year, worst case, cost {key.split('|')[1]}\n\n| Variant | Year | Trades | Net bps/trade | "
                         "t | Net R/trade | Year return | SPY |\n|---|---|---|---|---|---|---|---|\n")
            for name, row in groups.items():
                variant, year = name.split("|")
                lines.append(f"| {variant} | {year} | {_fmt(row['trades'], 'n')} | {_fmt(row['mean_net_bps'], 'bps')} | "
                             f"{_fmt(row['t_day_clustered'], 't')} | {_fmt(row['mean_r'], 'r')} | "
                             f"{_fmt(row['total_return'], 'pct')} | {_fmt(row['spy_return'], 'pct')} |\n")
            continue
        period, cost = key.split("|")
        lines.append(f"\n## Variant C buckets, worst case, {period.replace('_', '-')}, cost {cost}\n\n"
                     "| Split | Bucket | Trades | Net bps/trade | t | Net R/trade | Win rate |\n|---|---|---|---|---|---|---|\n")
        for split, rows in groups.items():
            for b in rows:
                lines.append(f"| {split} | {b['bucket']} | {b['trades']:,} | {_fmt(b['mean_net_bps'], 'bps')} | "
                             f"{_fmt(b.get('t_day_clustered'), 't')} | {_fmt(b['mean_r'], 'r')} | {_fmt(b['win_rate'], 'pct')} |\n")
    path.write_text("".join(lines))


def main() -> None:
    global REQUEST_INTERVAL
    if "--interval" in sys.argv:                     # seconds between requests of this process
        REQUEST_INTERVAL = float(sys.argv[sys.argv.index("--interval") + 1])
    stage = sys.argv[1]
    work = Path(sys.argv[2]).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    if stage == "universe":
        stage_universe(work, Path(sys.argv[3]).expanduser())
    elif stage == "fetch_or":
        stage_fetch_or(work, sys.argv)
    elif stage == "fetch_base":
        stage_fetch_base(work, sys.argv)
    elif stage == "rank":
        stage_rank(work)
    elif stage == "fetch_top":
        stage_fetch_top(work, sys.argv)
    elif stage == "simulate":
        stage_simulate(work)
    elif stage == "tables":
        stage_tables(work, Path(sys.argv[3]).expanduser())
    else:
        raise SystemExit(f"unknown stage {stage}")


if __name__ == "__main__":
    main()
