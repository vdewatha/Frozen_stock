"""Insider cluster buys: the historical check pre-registered in PREREGISTRATION.md (same folder).

Three stages, each cached under <work_dir> so a rerun repeats nothing:

    python -I insider_cluster_check.py events  <work_dir>    # SEC quarterly zips in <work_dir>/zips -> events.csv
    python -I insider_cluster_check.py prices  <work_dir>    # yfinance bars per event ticker -> <work_dir>/bars/
    python -I insider_cluster_check.py evaluate <work_dir>   # trades.csv, results.json, printed tables

The zips are the SEC's Insider Transactions Data Sets (one per quarter). Download them yourself with a
declared User-Agent (the SEC refuses undeclared scripts); this script never contacts sec.gov or the
deployed app. Local research only.

The event, trade, statistic and rule are frozen in PREREGISTRATION.md; the constants below restate them.
"""
from __future__ import annotations

import csv
import io
import json
import math
import sys
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

MIN_PURCHASE_USD = 10_000.0
MAX_FILING_LAG_DAYS = 10
CLUSTER_WINDOW_DAYS = 30          # D minus 29 days to D
CLUSTER_INSIDERS = 3
LOCKOUT_DAYS = 90
HOLD_SESSIONS = 63
LONG_HOLD_SESSIONS = 126          # reported, not decisive
IDENTITY_RANGE = (0.7, 1.4)
MIN_HISTORY_SESSIONS = 20
MIN_PRICE = 2.0
MIN_DOLLAR_VOLUME = 500_000.0
ACTING_COST = 0.0050              # 50 bps a round trip
STRESS_COST = 0.0100
PRIMARY_START = "2015-01"
RECENT_START = "2020-01"
T_REQUIRED = 2.0
NW_LAGS = 3
BENCHMARKS = ("IWM", "SPY")
BAD_TICKERS = {"", "NONE", "N/A", "NA", "N.A.", "NOT APPLICABLE"}


# ------------------------------------------------------------------ events

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


def qualifying_filings(zip_path: Path) -> list[dict]:
    """One row per qualifying Form 4: issuer, filing date, one insider key, its purchases' value, price and last date."""
    archive = zipfile.ZipFile(zip_path)
    submissions = {}
    for row in _rows(archive, "SUBMISSION.tsv"):
        if row.get("DOCUMENT_TYPE") != "4":
            continue
        if str(row.get("AFF10B5ONE") or "").strip().lower() in {"1", "true", "y", "yes"}:
            continue
        filed = _date(row.get("FILING_DATE") or "")
        if filed is None:
            continue
        submissions[row["ACCESSION_NUMBER"]] = {
            "issuer_cik": row.get("ISSUERCIK") or "", "issuer_name": row.get("ISSUERNAME") or "",
            "ticker": (row.get("ISSUERTRADINGSYMBOL") or "").strip().upper(), "filed": filed}
    insiders: dict[str, str] = {}
    for row in _rows(archive, "REPORTINGOWNER.tsv"):
        accession = row["ACCESSION_NUMBER"]
        if accession not in submissions:
            continue
        relationship = (row.get("RPTOWNER_RELATIONSHIP") or "").lower()
        if "director" in relationship or "officer" in relationship:
            key = row.get("RPTOWNERCIK") or ""
            # Joint filers report one purchase: the filing counts as one insider (its lowest owner id).
            insiders[accession] = min(insiders.get(accession, key), key)
    purchases: dict[str, dict] = defaultdict(lambda: {"value": 0.0, "shares": 0.0, "last": None})
    for row in _rows(archive, "NONDERIV_TRANS.tsv"):
        accession = row["ACCESSION_NUMBER"]
        if accession not in insiders or row.get("TRANS_CODE") != "P" or row.get("TRANS_ACQUIRED_DISP_CD") != "A":
            continue
        shares, price, traded = _number(row.get("TRANS_SHARES")), _number(row.get("TRANS_PRICEPERSHARE")), _date(
            row.get("TRANS_DATE") or "")
        if shares <= 0 or price <= 0 or traded is None:
            continue
        item = purchases[accession]
        item["value"] += shares * price
        item["shares"] += shares
        item["last"] = traded if item["last"] is None else max(item["last"], traded)
    out = []
    for accession, item in purchases.items():
        submission = submissions[accession]
        lag = (submission["filed"] - item["last"]).days
        if item["value"] < MIN_PURCHASE_USD or lag < 0 or lag > MAX_FILING_LAG_DAYS:
            continue
        out.append({**submission, "accession": accession, "insider": insiders[accession], "value": item["value"],
                    "price": item["value"] / item["shares"], "last_purchase": item["last"]})
    return out


def cluster_events(filings: list[dict]) -> list[dict]:
    """The first filing date on which an issuer has 3 distinct insiders in 30 days, at most once per 90 days."""
    by_issuer: dict[str, list[dict]] = defaultdict(list)
    for filing in filings:
        by_issuer[filing["issuer_cik"]].append(filing)
    events = []
    for issuer, rows in by_issuer.items():
        rows.sort(key=lambda r: (r["filed"], r["accession"]))
        last_event: date | None = None
        for index, row in enumerate(rows):
            day = row["filed"]
            if last_event is not None and (day - last_event).days <= LOCKOUT_DAYS:
                continue
            window = [r for r in rows[:index + 1] if (day - r["filed"]).days < CLUSTER_WINDOW_DAYS]
            if len({r["insider"] for r in window}) < CLUSTER_INSIDERS:
                continue
            ticker = row["ticker"].replace(".", "-").replace("/", "-")
            last_event = day
            if ticker in BAD_TICKERS or not ticker.replace("-", "").isalnum():
                continue
            events.append({"issuer_cik": issuer, "issuer_name": row["issuer_name"], "ticker": ticker,
                           "event_date": day.isoformat(), "insiders": len({r["insider"] for r in window}),
                           "cluster_value": round(sum(r["value"] for r in window), 2),
                           # Identity check inputs: the filing that completed the cluster.
                           "purchase_price": round(row["price"], 6), "last_purchase": row["last_purchase"].isoformat()})
    events.sort(key=lambda e: (e["event_date"], e["ticker"]))
    return events


def stage_events(work: Path) -> None:
    filings = []
    zips = sorted((work / "zips").glob("*_form345.zip"))
    for path in zips:
        found = qualifying_filings(path)
        filings.extend(found)
        print(f"{path.name}: {len(found)} qualifying purchase filings", flush=True)
    # A filing can appear in two quarterly files; keep one.
    unique = {f["accession"]: f for f in filings}
    events = cluster_events(list(unique.values()))
    pd.DataFrame(events).to_csv(work / "events.csv", index=False)
    by_year = pd.Series([e["event_date"][:4] for e in events]).value_counts().sort_index()
    print(f"{len(zips)} quarters, {len(unique)} qualifying filings, {len(events)} cluster events, "
          f"{len({e['ticker'] for e in events})} tickers")
    print(by_year.to_string())


# ------------------------------------------------------------------ prices

def _download(ticker: str, bars: Path) -> str:
    import yfinance as yf

    path = bars / f"{ticker}.csv"
    if path.exists():
        return "cached"
    try:
        frame = yf.download(ticker, start="2005-06-01", auto_adjust=False, actions=True, progress=False, threads=False)
    except Exception as exc:  # a failed ticker is recorded as empty, never retried in the same run
        frame = None
        note = f"{exc.__class__.__name__}"
    else:
        note = "ok"
    if frame is None or len(frame) == 0:
        path.write_text("date\n")
        return "empty" if note == "ok" else note
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame = frame.reset_index()
    frame.columns = [str(c).lower().replace(" ", "_") for c in frame.columns]
    frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
    frame.to_csv(path, index=False)
    return "ok"


def stage_prices(work: Path) -> None:
    import logging

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    bars = work / "bars"
    bars.mkdir(exist_ok=True)
    tickers = sorted(set(pd.read_csv(work / "events.csv")["ticker"].astype(str)) | set(BENCHMARKS))
    counts: dict[str, int] = defaultdict(int)
    started = time.time()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for done, status in enumerate(pool.map(lambda t: _download(t, bars), tickers), 1):
            counts[status] += 1
            if done % 250 == 0:
                print(f"{done}/{len(tickers)} {dict(counts)} {round(time.time() - started)}s", flush=True)
    print(f"done {len(tickers)} tickers: {dict(counts)}")


# ---------------------------------------------------------------- evaluate

def load_bars(path: Path) -> pd.DataFrame | None:
    if not path.exists() or path.stat().st_size < 64:
        return None
    frame = pd.read_csv(path)
    needed = {"date", "open", "close", "adj_close", "volume"}
    if not needed <= set(frame.columns):
        return None
    frame = frame.dropna(subset=["open", "close", "adj_close"])
    frame = frame[(frame["open"] > 0) & (frame["close"] > 0) & (frame["adj_close"] > 0)].reset_index(drop=True)
    if frame.empty:
        return None
    splits = frame["stock_splits"].fillna(0.0).to_numpy(float) if "stock_splits" in frame else np.zeros(len(frame))
    ratio = np.where(splits > 0, splits, 1.0)
    # Yahoo's close is adjusted for every split to date; a split on day s applies to the bars before s.
    later = np.concatenate([np.cumprod(ratio[::-1])[::-1][1:], [1.0]])
    frame["close_then"] = frame["close"] * later
    frame["factor"] = frame["adj_close"] / frame["close"]          # total-return adjustment
    return frame


def _total_return(frame: pd.DataFrame, entry: int, exit_: int) -> float:
    entry_open = frame["open"].iat[entry] * frame["factor"].iat[entry]
    return float(frame["adj_close"].iat[exit_] / entry_open - 1.0)


def _benchmark_return(frame: pd.DataFrame, dates: np.ndarray, entry_date: str, exit_date: str) -> float | None:
    i = int(np.searchsorted(dates, entry_date))
    j = int(np.searchsorted(dates, exit_date, side="right")) - 1
    if i >= len(dates) or j < i:
        return None
    return _total_return(frame, i, j)


def evaluate_event(event: dict, frame: pd.DataFrame | None, benchmarks: dict, last_bar: str) -> dict:
    out = {**event, "status": "unpriced"}
    if frame is None:
        return out
    dates = frame["date"].to_numpy(str)
    at_d = int(np.searchsorted(dates, event["event_date"], side="right")) - 1
    if at_d + 1 < MIN_HISTORY_SESSIONS:
        return out
    at_purchase = int(np.searchsorted(dates, event["last_purchase"], side="right")) - 1
    if at_purchase < 0:
        return out
    ratio = float(event["purchase_price"]) / float(frame["close_then"].iat[at_purchase])
    out["identity_ratio"] = round(ratio, 4)
    if not IDENTITY_RANGE[0] <= ratio <= IDENTITY_RANGE[1]:
        return {**out, "status": "identity_mismatch"}
    window = frame.iloc[at_d + 1 - MIN_HISTORY_SESSIONS:at_d + 1]
    dollar_volume = float((window["close"] * window["volume"]).median())
    out.update({"price_at_event": round(float(frame["close_then"].iat[at_d]), 4), "dollar_volume": round(dollar_volume, 0)})
    if frame["close_then"].iat[at_d] < MIN_PRICE or dollar_volume < MIN_DOLLAR_VOLUME:
        return {**out, "status": "ineligible"}
    entry = at_d + 1
    if entry >= len(dates):
        return {**out, "status": "ended_before_entry" if dates[-1] < last_bar else "open"}
    ended = dates[-1] < last_bar                      # the history stops before the data does: delisted
    for label, sessions in (("", HOLD_SESSIONS), ("_long", LONG_HOLD_SESSIONS)):
        exit_ = entry + sessions - 1
        if exit_ >= len(dates):
            if not ended:
                continue                              # the hold has not finished yet
            exit_ = len(dates) - 1
        stock = _total_return(frame, entry, exit_)
        out[f"return{label}"] = round(stock, 6)
        out[f"exit_date{label}"] = dates[exit_]
        for name, (bench, bench_dates) in benchmarks.items():
            bench_return = _benchmark_return(bench, bench_dates, dates[entry], dates[exit_])
            if bench_return is not None:
                out[f"excess_{name.lower()}{label}"] = round(stock - bench_return, 6)
    out["entry_date"] = dates[entry]
    out["status"] = "traded" if "return" in out and "excess_iwm" in out else "open"
    return out


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
    """The pre-registered statistic on one slice: mean of monthly means of net excess, Newey-West t."""
    if trades.empty or column not in trades:
        return {"events": 0}
    net = trades[column].dropna() - cost
    if net.empty:
        return {"events": 0}
    months = trades.loc[net.index, "entry_date"].str[:7]
    monthly = net.groupby(months).mean().sort_index()
    t_stat = newey_west_t(monthly.to_numpy())
    return {"events": int(len(net)), "months": int(len(monthly)),
            "monthly_mean": round(float(monthly.mean()), 5), "t": None if t_stat is None else round(t_stat, 2),
            "event_mean": round(float(net.mean()), 5), "event_median": round(float(net.median()), 5),
            "share_positive": round(float((net > 0).mean()), 4)}


def stage_evaluate(work: Path) -> dict:
    events = pd.read_csv(work / "events.csv", dtype={"issuer_cik": str, "ticker": str}).to_dict("records")
    bars = work / "bars"
    benchmarks = {}
    for name in BENCHMARKS:
        frame = load_bars(bars / f"{name}.csv")
        if frame is None:
            raise SystemExit(f"benchmark {name} has no bars")
        benchmarks[name] = (frame, frame["date"].to_numpy(str))
    last_bar = benchmarks["IWM"][1][-1]
    cache: dict[str, pd.DataFrame | None] = {}
    rows = []
    for event in events:
        ticker = event["ticker"]
        if ticker not in cache:
            cache[ticker] = load_bars(bars / f"{ticker}.csv")
        rows.append(evaluate_event(event, cache[ticker], benchmarks, last_bar))
    table = pd.DataFrame(rows)
    table.to_csv(work / "trades.csv", index=False)
    table["year"] = table["event_date"].str[:4]
    coverage = table.groupby("year")["status"].value_counts().unstack(fill_value=0)
    traded = table[table["status"] == "traded"].copy()

    def window(start: str | None, end: str | None = None) -> pd.DataFrame:
        part = traded
        if start:
            part = part[part["entry_date"].str[:7] >= start]
        if end:
            part = part[part["entry_date"].str[:7] < end]
        return part

    primary, recent, early = window(PRIMARY_START), window(RECENT_START), window(None, PRIMARY_START)
    results = {
        "data_end": last_bar, "events": int(len(table)), "status": table["status"].value_counts().to_dict(),
        "primary_2015": {"net_50_iwm": summarise(primary, "excess_iwm", ACTING_COST),
                         "gross_iwm": summarise(primary, "excess_iwm", 0.0),
                         "net_100_iwm": summarise(primary, "excess_iwm", STRESS_COST),
                         "net_50_spy": summarise(primary, "excess_spy", ACTING_COST),
                         "net_50_iwm_126": summarise(primary, "excess_iwm_long", ACTING_COST),
                         "raw_return_gross": summarise(primary, "return", 0.0)},
        "recent_2020": {"net_50_iwm": summarise(recent, "excess_iwm", ACTING_COST)},
        "early_2006_2014": {"net_50_iwm": summarise(early, "excess_iwm", ACTING_COST)},
    }
    if len(primary) >= 30:
        terciles = pd.qcut(primary["dollar_volume"], 3, labels=["low", "mid", "high"])
        results["liquidity_terciles_2015"] = {
            str(label): {**summarise(primary[terciles == label], "excess_iwm", ACTING_COST),
                         "median_dollar_volume": round(float(primary.loc[terciles == label, "dollar_volume"].median()))}
            for label in ("low", "mid", "high")}
    first, second = results["primary_2015"]["net_50_iwm"], results["recent_2020"]["net_50_iwm"]
    passes_primary = bool(first.get("events") and first["monthly_mean"] > 0 and (first["t"] or 0) >= T_REQUIRED)
    passes_recent = bool(second.get("events") and second["monthly_mean"] > 0)
    results["rule"] = {"primary_2015_mean_positive_t_ge_2": passes_primary, "recent_2020_mean_positive": passes_recent,
                       "verdict": "forward paper test" if passes_primary and passes_recent else "closed"}
    results["coverage_by_year"] = {year: {k: int(v) for k, v in row.items()} for year, row in coverage.iterrows()}
    (work / "results.json").write_text(json.dumps(results, indent=1))
    print(json.dumps({k: v for k, v in results.items() if k != "coverage_by_year"}, indent=1))
    print(coverage.to_string())
    return results


if __name__ == "__main__":
    stage, work_dir = sys.argv[1], Path(sys.argv[2])
    {"events": stage_events, "prices": stage_prices, "evaluate": stage_evaluate}[stage](work_dir)
