"""Post-earnings drift with delisted stocks: the check pre-registered in PREREGISTRATION.md (same folder).

Four stages, each cached under <work_dir> so a rerun repeats nothing:

    python -I earnings_drift_check.py tickers  <work_dir>   # insider zips -> dated CIK-to-ticker map, insider prices
    python -I earnings_drift_check.py events   <work_dir>   # sec/submissions.zip -> 8-K Item 2.02 events, bankruptcies
    ALPACA_KEY=... ALPACA_SECRET=... python -I earnings_drift_check.py fetch <work_dir>   # bars per acceptance month
    python -I earnings_drift_check.py evaluate <work_dir>   # events.csv.gz, trades.csv.gz, results.json

Inputs the caller downloads (with a declared User-Agent; the SEC refuses undeclared scripts):
    <work_dir>/insider_zips/*_form345.zip   the SEC's Insider Transactions Data Sets, 2015 Q1 onward
    <work_dir>/sec/submissions.zip          https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip

This script never contacts sec.gov or the deployed app. The fetch stage reads market data only
(GET https://data.alpaca.markets/v2/stocks/bars); it never touches an account or an order. The keys are
read from the environment and never written anywhere. Local research only.

The event, trade, statistic and rule are frozen in PREREGISTRATION.md; the constants below restate them.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

FIRST_ACCEPTED = "2016-01-01"
MAP_WINDOW_DAYS = 365             # insider filing nearest the acceptance date, either side
MIN_HISTORY_SESSIONS = 20
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 5_000_000.0
IDENTITY_RANGE = (0.7, 1.4)
IDENTITY_CODES = ("P", "S", "F")
REACTION_CUTOFF = 0.05            # top bucket: reaction of +5.0% or more
HOLD_SESSIONS = 20                # entry at the close of R+1, exit at the close of R+21
ACTING_COST = 0.0025              # 25 bps a round trip
STRESS_COST = 0.0050
T_REQUIRED = 2.0
NW_LAGS = 2
MIN_YEAR_TRADES = 20
TOP_SHARE = 0.01
BANKRUPTCY_DAYS_BEFORE = 365
BANKRUPTCY_DAYS_AFTER = 30
ALIVE_SESSIONS = 10               # "delisted since": no bar in the last 10 sessions of the data
DAYS_BEFORE = 45                  # bars before the month: 20 sessions of history
DAYS_AFTER = 50                   # bars after the month: R+21 and a few sessions more
BATCH = 100
WORKERS = 3
REQUEST_INTERVAL = 0.32           # the free plan allows 200 requests a minute
BENCHMARK = "SPY"
EASTERN = ZoneInfo("America/New_York")
URL = "https://data.alpaca.markets/v2/stocks/bars"
BAD_TICKERS = {"", "NONE", "N/A", "NA", "N.A.", "NOT APPLICABLE"}
EXCHANGES = {"NYSE", "NASDAQ", "NASD", "AMEX", "NYSEMKT", "NYSEAMERICAN", "NYSEARCA", "ARCA", "BATS", "CBOE",
             "NASDAQGS", "NASDAQGM", "NASDAQCM", "NGS", "NGM", "NCM", "NMS", "OTC", "OTCBB", "OTCQB", "OTCQX",
             "OTCMKTS", "OB", "PK", "TSX", "MKT", "AMERICAN"}
SYMBOL = re.compile(r"[A-Z]{1,5}(\.[A-Z])?")


# ------------------------------------------------------------------ ticker map

def _date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def _rows(archive: zipfile.ZipFile, name: str):
    with archive.open(name) as handle:
        yield from csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", errors="replace"), delimiter="\t")


def _number(text: str) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def clean_symbol(text: str | None) -> str | None:
    """The issuer symbol as insiders wrote it -> a plain ticker, or None."""
    raw = (text or "").strip().upper()
    if raw in BAD_TICKERS:
        return None
    for token in re.split(r"[:,;\s()\[\]\"']+", raw):          # exchange prefixes, several symbols
        if not token or token in EXCHANGES:
            continue
        token = token.replace("-", ".").replace("/", ".")
        if SYMBOL.fullmatch(token):
            return token
        head = token.split(".")[0]                             # "ABC.ABCW": the first of several
        return head if SYMBOL.fullmatch(head) else None
    return None


def stage_tickers(work: Path) -> None:
    symbols: Counter = Counter()
    prices = []
    for path in sorted((work / "insider_zips").glob("*_form345.zip")):
        if path.name[:4] < "2015":
            continue
        archive = zipfile.ZipFile(path)
        issuer = {}
        for row in _rows(archive, "SUBMISSION.tsv"):
            cik, filed = (row.get("ISSUERCIK") or "").strip(), _date(row.get("FILING_DATE") or "")
            if not cik or filed is None:
                continue
            cik = cik.zfill(10)
            issuer[row["ACCESSION_NUMBER"]] = cik
            symbol = clean_symbol(row.get("ISSUERTRADINGSYMBOL"))
            if symbol:
                symbols[(cik, filed.isoformat(), symbol)] += 1
        for row in _rows(archive, "NONDERIV_TRANS.tsv"):
            cik = issuer.get(row["ACCESSION_NUMBER"])
            if cik is None or row.get("TRANS_CODE") not in IDENTITY_CODES:
                continue
            price, traded = _number(row.get("TRANS_PRICEPERSHARE")), _date(row.get("TRANS_DATE") or "")
            if price > 0 and traded is not None:
                prices.append((cik, traded.isoformat(), price))
        print(path.name, len(symbols), len(prices), flush=True)
    # One symbol per issuer and filing day: the most common that day (alphabetical on a tie).
    best: dict[tuple, tuple] = {}
    for (cik, day, symbol), count in symbols.items():
        key, candidate = (cik, day), (-count, symbol)
        if key not in best or candidate < best[key]:
            best[key] = candidate
    table = pd.DataFrame([(cik, day, value[1]) for (cik, day), value in best.items()], columns=["cik", "date", "ticker"])
    table.sort_values(["cik", "date"]).to_parquet(work / "ticker_map.parquet", index=False)
    pd.DataFrame(prices, columns=["cik", "date", "price"]).sort_values(["cik", "date"]) \
        .to_parquet(work / "insider_prices.parquet", index=False)
    print(f"ticker map: {len(table)} issuer-days, {table['cik'].nunique()} issuers; {len(prices)} priced transactions")


# ------------------------------------------------------------------ events

def stage_events(work: Path) -> None:
    table = pd.read_parquet(work / "ticker_map.parquet")
    by_cik: dict[str, tuple[list, list]] = {}
    for cik, part in table.groupby("cik", sort=False):
        by_cik[cik] = ([date.fromisoformat(d).toordinal() for d in part["date"]], part["ticker"].tolist())

    def ticker_on(cik: str, day: int):
        days, symbols = by_cik[cik]
        i = bisect_right(days, day)
        best = None
        if i > 0 and day - days[i - 1] <= MAP_WINDOW_DAYS:
            best = (day - days[i - 1], 0, symbols[i - 1])
        if i < len(days) and days[i] - day <= MAP_WINDOW_DAYS:
            later = (days[i] - day, 1, symbols[i])
            if best is None or later < best:
                best = later
        return best

    archive = zipfile.ZipFile(work / "sec" / "submissions.zip")
    events, bankruptcies = [], []
    filers = no_ticker = 0
    seen: set = set()
    for name in archive.namelist():
        cik = name[3:13]
        if not name.startswith("CIK") or cik not in by_cik:
            continue
        with archive.open(name) as handle:
            body = json.load(handle)
        block = body["filings"]["recent"] if "filings" in body else body
        hit = False
        for form, items, accepted, accession, filed in zip(block["form"], block["items"], block["acceptanceDateTime"],
                                                           block["accessionNumber"], block["filingDate"]):
            if form != "8-K":
                continue
            listed = {item.strip() for item in (items or "").split(",")}
            if "1.03" in listed:
                bankruptcies.append((cik, filed))
            if "2.02" not in listed or (cik, accession) in seen:
                continue
            # The field is UTC (checked against filing index pages); the rule reads Eastern time.
            local = datetime.strptime(accepted[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone(EASTERN)
            if local.date().isoformat() < FIRST_ACCEPTED:
                continue
            seen.add((cik, accession))
            match = ticker_on(cik, local.date().toordinal())
            if match is None:
                no_ticker += 1
                continue
            hit = True
            events.append({"cik": cik, "accession": accession, "accepted": local.strftime("%Y-%m-%dT%H:%M:%S"),
                           "ticker": match[2], "map_gap_days": match[0] * (1 if match[1] else -1)})
        filers += hit
    pd.DataFrame(events).sort_values(["accepted", "cik"]).to_csv(work / "events_raw.csv", index=False)
    pd.DataFrame(sorted(set(bankruptcies)), columns=["cik", "filed"]).to_csv(work / "bankruptcies.csv", index=False)
    print(f"{len(events)} Item 2.02 filings with a ticker from {filers} filers; {no_ticker} more with no ticker in "
          f"{MAP_WINDOW_DAYS} days; {len(set(bankruptcies))} Item 1.03 filings")


# ------------------------------------------------------------------ sessions

def add_sessions(events: pd.DataFrame, calendar: list[str]) -> pd.DataFrame:
    """P, R, entry (R+1) and exit (R+21) on SPY's calendar; one event per ticker and reaction session."""
    rows = []
    for event in events.to_dict("records"):
        day, clock = event["accepted"][:10], event["accepted"][11:]
        at = bisect_left(calendar, day)
        session = at < len(calendar) and calendar[at] == day
        if session and clock < "09:30:00":
            p, r = at - 1, at
        elif session and clock < "16:00:00":
            p, r = at - 1, at + 1
        else:
            p = bisect_right(calendar, day) - 1
            r = p + 1
        if p < 0 or r >= len(calendar):
            continue                                            # before the first bar, or the reaction has not happened
        entry, exit_ = r + 1, r + 1 + HOLD_SESSIONS
        rows.append({**event, "p_date": calendar[p], "r_date": calendar[r],
                     "entry_date": calendar[entry] if entry < len(calendar) else "",
                     "exit_date": calendar[exit_] if exit_ < len(calendar) else ""})
    table = pd.DataFrame(rows).sort_values(["accepted", "cik"])
    return table.drop_duplicates(["ticker", "r_date"], keep="first").reset_index(drop=True)


# ------------------------------------------------------------------ Alpaca bars

_gate = threading.Lock()
_next_request = [0.0]


def _get(params: dict, key: str, secret: str) -> dict:
    request = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params),
                                     headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    for attempt in range(6):
        with _gate:
            wait = _next_request[0] - time.monotonic()
            _next_request[0] = max(time.monotonic(), _next_request[0]) + REQUEST_INTERVAL
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            if exc.code in (400, 422):       # a symbol the API rejects: the caller splits the batch
                raise ValueError(exc.read().decode("utf-8", "replace")[:200]) from exc
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Alpaca bars request kept failing")


def fetch_bars(symbols: list[str], start: str, end: str, asof: str, adjustment: str, key: str, secret: str) -> dict:
    out: dict[str, list] = {}
    token = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end, "asof": asof,
                  "adjustment": adjustment, "feed": "sip", "limit": 10000}
        if token:
            params["page_token"] = token
        body = _get(params, key, secret)
        for symbol, rows in (body.get("bars") or {}).items():
            out.setdefault(symbol, []).extend(rows)
        token = body.get("next_page_token")
        if not token:
            return out


def fetch_group(symbols: list[str], *args) -> dict:
    """One batch; when the API rejects it, halves of it, so one bad ticker costs only itself."""
    try:
        return fetch_bars(symbols, *args)
    except ValueError:
        if len(symbols) == 1:
            return {}
        half = len(symbols) // 2
        return {**fetch_group(symbols[:half], *args), **fetch_group(symbols[half:], *args)}


def fetch_many(symbols: list[str], *args) -> dict:
    batches = [symbols[i:i + BATCH] for i in range(0, len(symbols), BATCH)]
    out: dict[str, list] = {}
    with ThreadPoolExecutor(WORKERS) as pool:
        for part in pool.map(lambda batch: fetch_group(batch, *args), batches):
            out.update(part)
    return out


def _day_after(day: pd.Timestamp) -> str:
    """A date-only `end` is midnight UTC, before that day's bar: ask for the next day to include it."""
    return (day + pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def stage_fetch(work: Path) -> None:
    key, secret = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
    today = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize() - pd.Timedelta(days=1)   # SIP history is delayed
    calendar_path = work / "spy_calendar.csv"
    if not calendar_path.exists():
        bars = fetch_bars([BENCHMARK], "2015-12-01", _day_after(today), today.strftime("%Y-%m-%d"), "all",
                          key, secret)[BENCHMARK]
        pd.DataFrame({"date": sorted({row["t"][:10] for row in bars})}).to_csv(calendar_path, index=False)
    calendar = pd.read_csv(calendar_path)["date"].tolist()
    data_end = calendar[-1]
    events_path = work / "events_sessions.csv"
    if not events_path.exists():
        add_sessions(pd.read_csv(work / "events_raw.csv", dtype=str), calendar).to_csv(events_path, index=False)
    events = pd.read_csv(events_path, dtype=str, keep_default_na=False)
    print(f"{len(events)} events after one-per-ticker-and-reaction-session; data ends {data_end}", flush=True)
    target = work / "alpaca"
    target.mkdir(exist_ok=True)
    for month, group in events.groupby(events["accepted"].str[:7]):
        path = target / f"{month}.parquet"
        if path.exists():
            continue
        first = pd.Timestamp(month + "-01")
        last = first + pd.offsets.MonthEnd(0)
        start = (first - pd.Timedelta(days=DAYS_BEFORE)).strftime("%Y-%m-%d")
        end = _day_after(min(last + pd.Timedelta(days=DAYS_AFTER), pd.Timestamp(data_end)))
        asof = min(first + pd.Timedelta(days=14), pd.Timestamp(data_end)).strftime("%Y-%m-%d")
        symbols = sorted(set(group["ticker"]) | {BENCHMARK})
        raw = fetch_many(symbols, start, end, asof, "raw", key, secret)
        total = fetch_many(symbols, start, end, asof, "all", key, secret)
        alive = fetch_many(symbols, calendar[-ALIVE_SESSIONS], _day_after(pd.Timestamp(data_end)), asof, "raw", key, secret)
        records = []
        for symbol, rows in raw.items():
            adjusted = {row["t"][:10]: row["c"] for row in total.get(symbol) or []}
            for row in rows:
                day = row["t"][:10]
                if row["c"] > 0 and adjusted.get(day, 0) > 0:
                    records.append((symbol, day, row["c"], row["v"], adjusted[day]))
        frame = pd.DataFrame(records, columns=["symbol", "date", "close", "volume", "adj_close"])
        frame = frame.drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"])
        (target / f"{month}.alive.json").write_text(json.dumps(sorted(s for s, rows in alive.items() if rows)))
        frame.to_parquet(path, index=False)
        print(f"{month}: {len(symbols)} symbols requested, {frame['symbol'].nunique()} with bars, "
              f"{len(alive)} still trading, asof {asof}", flush=True)


# ------------------------------------------------------------------ evaluation

def load_month(path: Path) -> dict[str, tuple]:
    frame = pd.read_parquet(path)
    return {symbol: (part["date"].to_numpy(str), part["close"].to_numpy(float), part["volume"].to_numpy(float),
                     part["adj_close"].to_numpy(float))
            for symbol, part in frame.groupby("symbol", sort=False)}


def _index(dates: np.ndarray, day: str) -> int:
    i = int(np.searchsorted(dates, day))
    return i if i < len(dates) and dates[i] == day else -1


def evaluate_event(event: dict, series: tuple | None, spy: dict[str, float], window_end: str,
                   insider: tuple | None, bankruptcies: list[int]) -> dict:
    """One event against its own month's bars: status, reaction, and the trade it would make."""
    row = dict(event)
    if series is None:
        return {**row, "status": "no_bars"}
    dates, close, volume, adj = series
    verified = False
    if insider is not None:                                     # identity: insider prices against the raw close
        days, prices = insider
        lo, hi = np.searchsorted(days, dates[0]), np.searchsorted(days, dates[-1], side="right")
        ratios = [prices[k] / close[i] for k in range(lo, hi) if (i := _index(dates, days[k])) >= 0]
        if ratios:
            verified = True
            row["identity_ratio"] = round(float(np.median(ratios)), 4)
            if not IDENTITY_RANGE[0] <= row["identity_ratio"] <= IDENTITY_RANGE[1]:
                return {**row, "status": "identity_mismatch"}
    row["verified"] = verified
    p = _index(dates, event["p_date"])
    if p < 0:
        return {**row, "status": "no_bar_at_p"}
    if p + 1 < MIN_HISTORY_SESSIONS:
        return {**row, "status": "short_history"}
    row["price"] = float(close[p])
    row["dollar_volume"] = float(np.median(close[p - MIN_HISTORY_SESSIONS + 1:p + 1] * volume[p - MIN_HISTORY_SESSIONS + 1:p + 1]))
    if row["price"] < MIN_PRICE:
        return {**row, "status": "below_5"}
    if row["dollar_volume"] < MIN_DOLLAR_VOLUME:
        return {**row, "status": "illiquid"}
    r = _index(dates, event["r_date"])
    if r < 0:
        return {**row, "status": "no_bar_at_r"}
    row["status"] = "eligible"
    row["reaction"] = float(adj[r] / adj[p] - 1 - (spy[event["r_date"]] / spy[event["p_date"]] - 1))
    if not event["entry_date"]:
        return {**row, "outcome": "open"}
    entry = _index(dates, event["entry_date"])
    if entry < 0:
        return {**row, "outcome": "no_bar_at_entry"}
    last = len(dates) - 1
    target = event["exit_date"]
    if target:
        at = _index(dates, target)
        if at >= 0:
            outcome, out = "completed", at
        elif dates[last] > target:                              # no bar that day but trades later: last close before it
            outcome, out = "completed", max(int(np.searchsorted(dates, target)) - 1, entry)
        else:
            outcome, out = "stopped", last
    elif dates[last] < window_end:
        outcome, out = "stopped", last
    else:
        return {**row, "outcome": "open"}
    spy_end = dates[out]
    row["return"] = float(adj[out] / adj[entry] - 1)
    if outcome == "stopped":
        day = date.fromisoformat(dates[last]).toordinal()
        if any(day - BANKRUPTCY_DAYS_BEFORE <= filed <= day + BANKRUPTCY_DAYS_AFTER for filed in bankruptcies):
            outcome, row["return"], spy_end = "stopped_full_loss", -1.0, target or window_end
        else:
            outcome = "stopped_last_price"
    row["outcome"], row["exit_actual"] = outcome, spy_end
    row["spy_return"] = float(spy[spy_end] / spy[event["entry_date"]] - 1)
    row["excess"] = row["return"] - row["spy_return"]
    return row


def newey_west_t(values: np.ndarray, lags: int = NW_LAGS) -> float | None:
    values = np.asarray(values, float)
    n = len(values)
    if n < 12:
        return None
    centred = values - values.mean()
    variance = centred @ centred / n
    for lag in range(1, min(lags, n - 1) + 1):
        variance += 2 * (1 - lag / (lags + 1)) * (centred[lag:] @ centred[:-lag]) / n
    if variance <= 0:
        return None
    return float(values.mean() / math.sqrt(variance / n))


def summarise(trades: pd.DataFrame, column: str, cost: float) -> dict:
    """The pre-registered statistic on one slice: mean of entry-month means net of cost, Newey-West t."""
    if trades.empty:
        return {"trades": 0}
    net = trades[column] - cost
    monthly = net.groupby(trades["entry_date"].str[:7]).mean().sort_index()
    t_stat = newey_west_t(monthly.to_numpy())
    return {"trades": int(len(net)), "months": int(len(monthly)), "monthly_mean": round(float(monthly.mean()), 5),
            "t": None if t_stat is None else round(t_stat, 2), "trade_mean": round(float(net.mean()), 5),
            "trade_median": round(float(net.median()), 5), "share_positive": round(float((net > 0).mean()), 4)}


def both(trades: pd.DataFrame, cost: float = ACTING_COST) -> dict:
    return {"net_return": summarise(trades, "return", cost), "net_excess_spy": summarise(trades, "excess", cost)}


def stage_evaluate(work: Path) -> dict:
    events = pd.read_csv(work / "events_sessions.csv", dtype=str, keep_default_na=False)
    data_end = pd.read_csv(work / "spy_calendar.csv")["date"].iloc[-1]
    insider = {cik: (part["date"].to_numpy(str), part["price"].to_numpy(float))
               for cik, part in pd.read_parquet(work / "insider_prices.parquet").groupby("cik", sort=False)}
    bankrupt: dict[str, list[int]] = defaultdict(list)
    for item in pd.read_csv(work / "bankruptcies.csv", dtype=str).to_dict("records"):
        bankrupt[item["cik"]].append(date.fromisoformat(item["filed"]).toordinal())
    rows = []
    for month, group in events.groupby(events["accepted"].str[:7]):
        path = work / "alpaca" / f"{month}.parquet"
        if not path.exists():
            raise SystemExit(f"bars for {month} are missing: run the fetch stage")
        bars = load_month(path)
        alive = set(json.loads((work / "alpaca" / f"{month}.alive.json").read_text()))
        spy_dates, _, _, spy_adj = bars[BENCHMARK]
        spy = dict(zip(spy_dates.tolist(), spy_adj.tolist()))
        for event in group.to_dict("records"):
            row = evaluate_event(event, bars.get(event["ticker"]), spy, spy_dates[-1], insider.get(event["cik"]),
                                 bankrupt.get(event["cik"], []))
            row["delisted_since"] = event["ticker"] not in alive
            rows.append(row)
    table = pd.DataFrame(rows).sort_values(["r_date", "accepted", "ticker"]).reset_index(drop=True)

    # One position per stock: an event whose R is on or before an earlier trade's exit session is skipped.
    signal = (table["status"] == "eligible") & (table["reaction"] >= REACTION_CUTOFF)
    table["trade"] = ""
    busy_until: dict[str, str] = {}
    for i in table.index[signal]:
        ticker, outcome = table.at[i, "ticker"], table.at[i, "outcome"]
        if ticker in busy_until and table.at[i, "r_date"] <= busy_until[ticker]:
            table.at[i, "trade"] = "skipped_position_open"
        elif outcome == "no_bar_at_entry":
            table.at[i, "trade"] = "no_bar_at_entry"
        elif outcome == "open":
            table.at[i, "trade"], busy_until[ticker] = "open", "9999-12-31"
        else:
            table.at[i, "trade"] = "taken"
            busy_until[ticker] = table.at[i, "exit_date"] if outcome == "stopped_full_loss" and table.at[i, "exit_date"] \
                else table.at[i, "exit_actual"]

    table["year"] = table["accepted"].str[:4]
    columns = ["cik", "accession", "accepted", "ticker", "map_gap_days", "p_date", "r_date", "entry_date", "exit_date",
               "status", "verified", "identity_ratio", "price", "dollar_volume", "reaction", "outcome", "exit_actual",
               "return", "spy_return", "excess", "delisted_since", "trade"]
    out = table[columns].copy()
    for column in ("identity_ratio", "reaction", "return", "spy_return", "excess"):
        out[column] = out[column].round(5)
    out["price"], out["dollar_volume"] = out["price"].round(2), out["dollar_volume"].round(0)
    out.to_csv(work / "events.csv.gz", index=False)
    trades = table[table["trade"] == "taken"].copy()
    out.loc[trades.index].to_csv(work / "trades.csv.gz", index=False)

    done = table[(table["status"] == "eligible") & table["outcome"].isin(
        ["completed", "stopped_last_price", "stopped_full_loss"])]
    cutoff = trades["excess"].quantile(1 - TOP_SHARE)
    trimmed = trades[trades["excess"] < cutoff]
    primary, without_top = both(trades), both(trimmed)
    trades["entry_year"] = trades["entry_date"].str[:4]
    years = {}
    for year, part in trades.groupby("entry_year"):
        stats = both(part)
        years[year] = {"trades": int(len(part)), "net_return": stats["net_return"]["monthly_mean"],
                       "net_excess_spy": stats["net_excess_spy"]["monthly_mean"],
                       "trade_mean_excess": stats["net_excess_spy"]["trade_mean"],
                       "stopped": int(part["outcome"].str.startswith("stopped").sum())}
    counted = {year: value for year, value in years.items() if value["trades"] >= MIN_YEAR_TRADES}
    positive_years = sum(value["net_excess_spy"] > 0 for value in counted.values())
    rule = {
        "1_net_return_positive_t_ge_2": bool(primary["net_return"]["monthly_mean"] > 0
                                             and (primary["net_return"]["t"] or 0) >= T_REQUIRED),
        "2_net_excess_spy_positive": bool(primary["net_excess_spy"]["monthly_mean"] > 0),
        "3_most_years_positive": bool(positive_years > len(counted) / 2),
        "4_survives_without_top_1pct": bool(without_top["net_return"]["monthly_mean"] > 0
                                            and without_top["net_excess_spy"]["monthly_mean"] > 0),
    }
    rule["verdict"] = "forward paper test" if all(rule.values()) else "closed"
    stopped = trades[trades["outcome"].str.startswith("stopped")]
    buckets = {"reaction_ge_10pct": trades[trades["reaction"] >= 0.10],
               "reaction_5_to_10pct": trades[trades["reaction"] < 0.10],
               "between_minus5_and_5pct_all_events": done[(done["reaction"] > -0.05) & (done["reaction"] < 0.05)],
               "reaction_le_minus5pct_all_events": done[done["reaction"] <= -0.05],
               "all_eligible_events": done}
    eligible = table[table["status"] == "eligible"]
    results = {
        "source": "SEC 8-K Item 2.02 filings, Alpaca sip daily bars (delisted included)", "data_end": data_end,
        "events": int(len(table)), "status": table["status"].value_counts().to_dict(),
        "eligible_events": {"events": int(len(eligible)),
                            "verified_share": round(float(eligible["verified"].astype(bool).mean()), 4),
                            "delisted_since_share": round(float(eligible["delisted_since"].mean()), 4),
                            "signal_share": round(float((eligible["reaction"] >= REACTION_CUTOFF).mean()), 4)},
        "signals": table.loc[signal, "trade"].value_counts().to_dict(),
        "primary": primary, "without_top_1pct": without_top,
        "years_counted": len(counted), "years_positive": int(positive_years), "by_entry_year": years, "rule": rule,
        "gross": both(trades, 0.0), "net_50bps": both(trades, STRESS_COST),
        "buckets": {name: both(part) for name, part in buckets.items()},
        "verified_only": both(trades[trades["verified"].astype(bool)]),
        "delisted_since": {"trades": int(trades["delisted_since"].sum()),
                           "share": round(float(trades["delisted_since"].mean()), 4),
                           "those": both(trades[trades["delisted_since"]]),
                           "still_listed": both(trades[~trades["delisted_since"]])},
        "stopped_during_hold": {"trades": int(len(stopped)), "kinds": stopped["outcome"].value_counts().to_dict(),
                                "mean_return": round(float(stopped["return"].mean()), 4) if len(stopped) else None,
                                "median_return": round(float(stopped["return"].median()), 4) if len(stopped) else None},
    }
    if len(trades) >= 30:
        terciles = pd.qcut(trades["dollar_volume"], 3, labels=["low", "mid", "high"])
        results["liquidity_terciles"] = {
            str(label): {**both(trades[terciles == label]),
                         "median_dollar_volume": round(float(trades.loc[terciles == label, "dollar_volume"].median()))}
            for label in ("low", "mid", "high")}
    coverage = table.groupby("year")["status"].value_counts().unstack(fill_value=0)
    results["coverage_by_year"] = {year: {k: int(v) for k, v in row.items()} for year, row in coverage.iterrows()}
    (work / "results.json").write_text(json.dumps(results, indent=1))
    print(json.dumps({k: v for k, v in results.items() if k != "coverage_by_year"}, indent=1))
    print(coverage.to_string())
    return results


if __name__ == "__main__":
    stage, work_dir = sys.argv[1], Path(sys.argv[2])
    {"tickers": stage_tickers, "events": stage_events, "fetch": stage_fetch, "evaluate": stage_evaluate}[stage](work_dir)
