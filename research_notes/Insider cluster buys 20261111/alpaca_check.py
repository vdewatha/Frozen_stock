"""Insider cluster buys: the second check of ADDENDUM.md, on Alpaca bars that include delisted stocks.

    ALPACA_KEY=... ALPACA_SECRET=... python -I alpaca_check.py fetch    <work_dir>   # bars per filing month
    python -I alpaca_check.py evaluate <work_dir>                                   # alpaca_trades.csv, alpaca_results.json

Needs <work_dir>/events.csv from ``insider_cluster_check.py events``. Market data only (GET
https://data.alpaca.markets/v2/stocks/bars); it never touches an account, an order or the deployed app.
The keys are read from the environment and never written anywhere.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import insider_cluster_check as base  # noqa: E402

FIRST_MONTH = "2016-01"
PRIMARY_START = "2016-03"
RECENT_START = "2020-01"
DAYS_BEFORE = 60        # 20 sessions of history and the purchase date
DAYS_AFTER = 215        # 126 sessions after the last event of the month
BATCH = 40
URL = "https://data.alpaca.markets/v2/stocks/bars"


def alpaca_symbol(ticker: str) -> str:
    return ticker.replace("-", ".")


def _get(params: dict, key: str, secret: str) -> dict:
    request = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params),
                                     headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            if exc.code in (400, 422):       # a symbol the API rejects: the caller retries one by one
                raise ValueError(exc.read().decode("utf-8", "replace")[:200]) from exc
            raise
        except (urllib.error.URLError, TimeoutError):
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
        time.sleep(0.35)                      # the free plan allows 200 requests a minute
        if not token:
            return out


def fetch_group(symbols: list[str], *args) -> dict:
    """One batch; when the API rejects it, each symbol on its own so one bad ticker costs only itself."""
    try:
        return fetch_bars(symbols, *args)
    except ValueError:
        out = {}
        for symbol in symbols:
            try:
                out.update(fetch_bars([symbol], *args))
            except ValueError:
                continue
        return out


def stage_fetch(work: Path) -> None:
    key, secret = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
    events = pd.read_csv(work / "events.csv", dtype={"ticker": str})
    events = events[events["event_date"].str[:7] >= FIRST_MONTH]
    today = pd.Timestamp.utcnow().tz_localize(None).normalize() - pd.Timedelta(days=1)   # SIP history is delayed
    target = work / "alpaca"
    target.mkdir(exist_ok=True)
    for month, group in events.groupby(events["event_date"].str[:7]):
        path = target / f"{month}.json"
        if path.exists():
            continue
        first = pd.Timestamp(month + "-01")
        last = first + pd.offsets.MonthEnd(0)
        start = (first - pd.Timedelta(days=DAYS_BEFORE)).strftime("%Y-%m-%d")
        end = min(last + pd.Timedelta(days=DAYS_AFTER), today).strftime("%Y-%m-%d")
        asof = min(first + pd.Timedelta(days=14), today).strftime("%Y-%m-%d")
        symbols = sorted({alpaca_symbol(t) for t in group["ticker"]} | set(base.BENCHMARKS))
        payload = {"month": month, "start": start, "end": end, "asof": asof, "raw": {}, "all": {}}
        for adjustment in ("raw", "all"):
            for i in range(0, len(symbols), BATCH):
                payload[adjustment].update(fetch_group(symbols[i:i + BATCH], start, end, asof, adjustment, key, secret))
        path.write_text(json.dumps(payload))
        print(f"{month}: {len(symbols)} symbols requested, {len(payload['raw'])} with bars", flush=True)


def month_frames(path: Path) -> dict[str, pd.DataFrame]:
    """Per symbol: the yfinance-shaped frame ``insider_cluster_check.evaluate_event`` reads."""
    payload = json.loads(path.read_text())
    frames = {}
    for symbol, rows in payload["raw"].items():
        adjusted = {row["t"][:10]: row for row in payload["all"].get(symbol) or []}
        records = []
        for row in rows:
            day = row["t"][:10]
            total = adjusted.get(day)
            if total is None or row["o"] <= 0 or row["c"] <= 0 or total["c"] <= 0:
                continue
            records.append({"date": day, "open": row["o"], "close": row["c"], "adj_close": total["c"], "volume": row["v"],
                            "close_then": row["c"], "factor": total["c"] / row["c"]})
        if records:
            frames[symbol] = pd.DataFrame(records).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return frames


def stage_evaluate(work: Path) -> dict:
    events = pd.read_csv(work / "events.csv", dtype={"issuer_cik": str, "ticker": str})
    events = events[events["event_date"].str[:7] >= FIRST_MONTH]
    rows = []
    for month, group in events.groupby(events["event_date"].str[:7]):
        path = work / "alpaca" / f"{month}.json"
        if not path.exists():
            continue
        frames = month_frames(path)
        if "IWM" not in frames or "SPY" not in frames:
            continue
        benchmarks = {name: (frames[name], frames[name]["date"].to_numpy(str)) for name in base.BENCHMARKS}
        last_bar = benchmarks["IWM"][1][-1]             # the last session this month's window covers
        for event in group.to_dict("records"):
            row = base.evaluate_event(event, frames.get(alpaca_symbol(event["ticker"])), benchmarks, last_bar)
            if row["status"] == "traded":
                frame = frames[alpaca_symbol(event["ticker"])]
                row["stopped_trading"] = bool(frame["date"].iat[-1] < last_bar and row["exit_date"] == frame["date"].iat[-1])
            rows.append(row)
    table = pd.DataFrame(rows)
    table.to_csv(work / "alpaca_trades.csv", index=False)
    table["year"] = table["event_date"].str[:4]
    coverage = table.groupby("year")["status"].value_counts().unstack(fill_value=0)
    traded = table[table["status"] == "traded"].copy()

    def window(start: str) -> pd.DataFrame:
        return traded[traded["entry_date"].str[:7] >= start]

    primary, recent = window(PRIMARY_START), window(RECENT_START)
    stopped = primary[primary["stopped_trading"]]
    cutoff = primary["excess_iwm"].quantile(0.99)
    results = {
        "source": "alpaca sip daily bars (delisted included)", "events": int(len(table)),
        "status": table["status"].value_counts().to_dict(),
        "primary_2016_03": {"net_50_iwm": base.summarise(primary, "excess_iwm", base.ACTING_COST),
                            "gross_iwm": base.summarise(primary, "excess_iwm", 0.0),
                            "net_100_iwm": base.summarise(primary, "excess_iwm", base.STRESS_COST),
                            "net_50_spy": base.summarise(primary, "excess_spy", base.ACTING_COST),
                            "net_50_iwm_126": base.summarise(primary, "excess_iwm_long", base.ACTING_COST),
                            "net_50_iwm_without_top_1pct": base.summarise(primary[primary["excess_iwm"] < cutoff],
                                                                          "excess_iwm", base.ACTING_COST),
                            "raw_return_gross": base.summarise(primary, "return", 0.0)},
        "recent_2020": {"net_50_iwm": base.summarise(recent, "excess_iwm", base.ACTING_COST),
                        "net_50_spy": base.summarise(recent, "excess_spy", base.ACTING_COST)},
        "stopped_trading_during_hold": {"events": int(len(stopped)),
                                        "share": round(float(len(stopped) / max(len(primary), 1)), 4),
                                        "mean_return": round(float(stopped["return"].mean()), 4) if len(stopped) else None,
                                        "median_return": round(float(stopped["return"].median()), 4) if len(stopped) else None},
    }
    if len(primary) >= 30:
        terciles = pd.qcut(primary["dollar_volume"], 3, labels=["low", "mid", "high"])
        results["liquidity_terciles"] = {
            str(label): {**base.summarise(primary[terciles == label], "excess_iwm", base.ACTING_COST),
                         "median_dollar_volume": round(float(primary.loc[terciles == label, "dollar_volume"].median()))}
            for label in ("low", "mid", "high")}
    first, second = results["primary_2016_03"]["net_50_iwm"], results["recent_2020"]["net_50_iwm"]
    passes_primary = bool(first.get("events") and first["monthly_mean"] > 0 and (first["t"] or 0) >= base.T_REQUIRED)
    passes_recent = bool(second.get("events") and second["monthly_mean"] > 0)
    results["rule"] = {"primary_mean_positive_t_ge_2": passes_primary, "recent_2020_mean_positive": passes_recent,
                       "verdict": "forward paper test" if passes_primary and passes_recent else "closed"}
    by_year = traded.assign(net=traded["excess_iwm"] - base.ACTING_COST, net_spy=traded["excess_spy"] - base.ACTING_COST) \
        .groupby("year").agg(events=("net", "size"), mean_vs_iwm=("net", "mean"), median_vs_iwm=("net", "median"),
                             mean_vs_spy=("net_spy", "mean"), stopped=("stopped_trading", "sum"))
    results["by_year"] = {year: {k: (int(v) if k in ("events", "stopped") else round(float(v), 4)) for k, v in row.items()}
                          for year, row in by_year.iterrows()}
    results["coverage_by_year"] = {year: {k: int(v) for k, v in row.items()} for year, row in coverage.iterrows()}
    (work / "alpaca_results.json").write_text(json.dumps(results, indent=1))
    print(json.dumps({k: v for k, v in results.items() if k not in ("coverage_by_year", "by_year")}, indent=1))
    print(by_year.round(4).to_string())
    print(coverage.to_string())
    return results


if __name__ == "__main__":
    stage, work_dir = sys.argv[1], Path(sys.argv[2])
    {"fetch": stage_fetch, "evaluate": stage_evaluate}[stage](work_dir)
