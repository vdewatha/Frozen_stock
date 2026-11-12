"""Dip rules and 50-day breakouts on the whole market, delisted stocks included.

The test pre-registered in PREREGISTRATION.md (same folder). Stages, each cached under <work_dir>:

    <venv with pyarrow>  python -I dip_rules_check.py inputs   <work_dir> <earnings_work_dir>
    <repo venv>          python -I dip_rules_check.py series   <work_dir>
    ALPACA_KEY=... ALPACA_SECRET=... <repo venv> python -I dip_rules_check.py fetch <work_dir>
    <repo venv>          python -I dip_rules_check.py vix      <work_dir>
    <repo venv>          python -I dip_rules_check.py evaluate <work_dir> [--workers 8]
    <repo venv>          python -I dip_rules_check.py tables   <work_dir> <this folder>

* ``inputs`` copies the earnings test's SEC-derived files (``ticker_map.parquet``, ``insider_prices.parquet``,
  ``bankruptcies.csv``) into <work_dir> as CSV, because the repo venv has no parquet engine.
* ``series`` builds one candidate series per (issuer CIK, ticker) pair, with its ``asof`` date.
* ``fetch`` reads Alpaca daily bars (GET https://data.alpaca.markets/v2/stocks/bars, ``feed=sip``), raw and
  ``adjustment=all``, one request group per ``asof`` date. Market data only; never an account or an order
  endpoint. The keys come from the environment and are never written or printed.
* ``vix`` reads ``^VIX`` daily closes from yfinance.
* ``evaluate`` runs the identity check, removes duplicates, replays the three hypotheses with the app's own
  labeler (``triple_barrier.label_events``) and writes trades and results.
* ``tables`` renders results.json as tables.md.

This script never contacts sec.gov or the deployed app. The rules, execution, universe and pass rule are frozen in
PREREGISTRATION.md; the constants below restate them.
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1] / "artifacts" / "api-server" / "backend"

FIRST_DAY = "2016-01-01"
# The bars were fetched during the session of 2026-11-12, whose bar was still forming (15-minute delayed SIP).
# Everything is cut at the last complete session.
LAST_SESSION = "2026-11-11"
ASOF_FLOOR = "2016-01-15"
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 5_000_000.0
DV_WINDOW = 20
IDENTITY_RANGE = (0.7, 1.4)
IDENTITY_YEAR_MIN = 3
MIN_FILING_DAYS_UNVERIFIED = 3
DUP_STEP = 20                      # duplicate check on every 20th session
DUP_MATCHES = 3
ALIVE_SESSIONS = 10                # "delisted since": no bar in the last 10 sessions of the data
BANKRUPTCY_DAYS = 365
ACTING_COST_BPS = 25.0             # per side
REPORTED_COST_BPS = 11.0
T_REQUIRED = 2.0
T_REQUIRED_BREAKOUT = 2.50         # Bonferroni over the four breakout settings
MIN_YEAR_TRADES = 20
TOP_SHARE = 0.01
TOP_N = 500
VERDICT_SAMPLE = {"H2": 800, "H5": 800, "HB": 800}
VIX_LEVEL = 20.0
VIX_FFILL = 5
BATCH = 100
WORKERS = 3
REQUEST_INTERVAL = 0.32
BENCHMARK = "SPY"
URL = "https://data.alpaca.markets/v2/stocks/bars"

REVERSION = {"stop_loss_pct": 0.10, "take_profit_pct": 0.0}
HYPOTHESES = [
    {"key": "H2", "name": "Candidate 2 (rule_1cd460d679)", "gate": "none",
     "rule": {"entry": [{"f": "dist_sma_200", "op": ">", "v": 0}, {"f": "ret_5", "op": "<", "v": -0.08}],
              "exit": [{"f": "ret_1", "op": ">", "v": 0}]}, "hold": 10},
    {"key": "H5", "name": "Candidate 5 (rule_7df4090f31)", "gate": "vix_above_20",
     "rule": {"entry": [{"f": "down_streak", "op": ">=", "v": 4}], "exit": [{"f": "dist_sma_5", "op": ">", "v": 0}]},
     "hold": 10},
    *[{"key": "HB", "name": f"Breakout 94d4afa886 ({gate})", "gate": gate,
       "rule": {"entry": [{"f": "at_high_50", "op": "==", "v": 1}], "exit": [{"f": "dist_sma_50", "op": "<", "v": 0}]},
       "hold": 40} for gate in ("none", "adx_not_downtrend", "spy_sma10", "spy_sma50")],
]
OPEN_EXITS = {"stop_loss_gap", "vertical", "indicator", "take_profit_gap"}


def variant_id(h: dict) -> str:
    return f"{h['key']}:{h['gate']}"


# ------------------------------------------------------------------ inputs and series

def stage_inputs(work: Path, earnings: Path) -> None:
    pd.read_parquet(earnings / "ticker_map.parquet").to_csv(work / "ticker_map.csv.gz", index=False)
    pd.read_parquet(earnings / "insider_prices.parquet").to_csv(work / "insider_prices.csv.gz", index=False)
    pd.read_csv(earnings / "bankruptcies.csv", dtype=str).to_csv(work / "bankruptcies.csv", index=False)
    print("inputs copied")


def stage_series(work: Path) -> None:
    table = pd.read_csv(work / "ticker_map.csv.gz", dtype=str)
    rows = []
    for (cik, ticker), part in table.groupby(["cik", "ticker"], sort=True):
        dates = sorted(part["date"])
        if dates[-1] < FIRST_DAY:
            continue
        median = dates[(len(dates) - 1) // 2]
        rows.append({"series": f"{cik}:{ticker}", "cik": cik, "ticker": ticker, "first_filing": dates[0],
                     "last_filing": dates[-1], "filing_days": len(dates), "asof": max(median, ASOF_FLOOR)})
    frame = pd.DataFrame(rows)
    frame.to_csv(work / "series.csv", index=False)
    print(f"{len(frame)} (CIK, ticker) series from {frame['cik'].nunique()} issuers; "
          f"{frame['asof'].nunique()} distinct asof dates")


# ------------------------------------------------------------------ Alpaca bars (the earnings test's method)

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
            if exc.code in (400, 422):
                raise ValueError(exc.read().decode("utf-8", "replace")[:200]) from exc
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Alpaca bars request kept failing")


def fetch_bars(symbols: list[str], start: str, end: str | None, asof: str, adjustment: str, key: str,
               secret: str) -> dict:
    out: dict[str, list] = {}
    token = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "asof": asof,
                  "adjustment": adjustment, "feed": "sip", "limit": 10000}
        if end:
            params["end"] = end
        if token:
            params["page_token"] = token
        body = _get(params, key, secret)
        for symbol, rows in (body.get("bars") or {}).items():
            out.setdefault(symbol, []).extend(rows)
        token = body.get("next_page_token")
        if not token:
            return out


def fetch_group(symbols: list[str], *args) -> dict:
    try:
        return fetch_bars(symbols, *args)
    except ValueError:
        if len(symbols) == 1:
            return {}
        half = len(symbols) // 2
        return {**fetch_group(symbols[:half], *args), **fetch_group(symbols[half:], *args)}


def _bars_frame(raw: dict, total: dict) -> pd.DataFrame:
    records = []
    for symbol, rows in total.items():
        unadjusted = {row["t"][:10]: (row["c"], row["v"]) for row in raw.get(symbol) or []}
        for row in rows:
            day = row["t"][:10]
            rc, rv = unadjusted.get(day, (np.nan, np.nan))
            records.append((symbol, day, row["o"], row["h"], row["l"], row["c"], row["v"], rc, rv))
    frame = pd.DataFrame(records, columns=["ticker", "date", "open", "high", "low", "close", "volume",
                                           "raw_close", "raw_volume"])
    return frame.drop_duplicates(["ticker", "date"]).sort_values(["ticker", "date"])


def stage_fetch(work: Path) -> None:
    key, secret = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
    target = work / "bars"
    target.mkdir(exist_ok=True)
    spy_path = work / "spy.pkl.gz"
    if not spy_path.exists():
        today = (pd.Timestamp.now(tz="UTC").tz_localize(None).normalize() - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        raw = fetch_bars([BENCHMARK], FIRST_DAY, None, today, "raw", key, secret)
        total = fetch_bars([BENCHMARK], FIRST_DAY, None, today, "all", key, secret)
        _bars_frame(raw, total).to_pickle(spy_path)
    spy = pd.read_pickle(spy_path)
    data_end = spy["date"].max()
    series = pd.read_csv(work / "series.csv", dtype=str)
    groups = [(asof, sorted(set(part["ticker"]))) for asof, part in series.groupby("asof")]
    todo = [(asof, symbols) for asof, symbols in groups if not (target / f"{asof}.pkl.gz").exists()]
    print(f"SPY {spy['date'].min()} to {data_end}; {len(groups)} asof groups, {len(todo)} to fetch", flush=True)

    def one(item):
        asof, symbols = item
        asof = min(asof, data_end)
        raw, total = {}, {}
        for i in range(0, len(symbols), BATCH):
            batch = symbols[i:i + BATCH]
            raw.update(fetch_group(batch, FIRST_DAY, None, asof, "raw", key, secret))
            total.update(fetch_group(batch, FIRST_DAY, None, asof, "all", key, secret))
        frame = _bars_frame(raw, total)
        frame.to_pickle(target / f"{item[0]}.pkl.gz")
        return item[0], len(symbols), frame["ticker"].nunique() if len(frame) else 0, len(frame)

    started, done = time.time(), 0
    with ThreadPoolExecutor(WORKERS) as pool:
        for asof, asked, got, rows in pool.map(one, todo):
            done += 1
            if done % 50 == 0 or done == len(todo):
                print(f"{done}/{len(todo)} groups ({time.time() - started:.0f} s); last {asof}: {got}/{asked} "
                      f"symbols, {rows} bars", flush=True)


def stage_vix(work: Path) -> None:
    import yfinance as yf
    frame = yf.Ticker("^VIX").history(start="2015-06-01", auto_adjust=False)
    out = pd.DataFrame({"date": frame.index.tz_localize(None).strftime("%Y-%m-%d"), "close": frame["Close"].to_numpy()})
    out.to_csv(work / "vix.csv", index=False)
    print(f"VIX {out['date'].min()} to {out['date'].max()}, {len(out)} closes")


# ------------------------------------------------------------------ evaluation helpers

def clustered_t(values: np.ndarray, clusters: np.ndarray) -> float | None:
    """t of the mean with month-clustered standard errors (the sweep's ``_clustered_t``)."""
    if len(values) < 10:
        return None
    frame = pd.DataFrame({"v": values, "c": clusters})
    mean = float(values.mean())
    sums = frame.groupby("c")["v"].apply(lambda s: float((s - mean).sum()))
    g = len(sums)
    if g < 2:
        return None
    variance = float((sums ** 2).sum()) * g / (g - 1) / len(values) ** 2
    return mean / math.sqrt(variance) if variance > 0 else None


def load_series(work: Path) -> tuple[dict, pd.DataFrame]:
    """Every fetched series keyed by 'CIK:TICKER', as a frame of bars."""
    series = pd.read_csv(work / "series.csv", dtype=str)
    by_asof = defaultdict(list)
    for row in series.itertuples(index=False):
        by_asof[row.asof].append(row)
    out = {}
    for asof, rows in by_asof.items():
        path = work / "bars" / f"{asof}.pkl.gz"
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
    return out, series


def identity_check(bars: dict, series: pd.DataFrame, work: Path) -> tuple[dict, pd.DataFrame]:
    """Insider prices against the raw close; returns kept series and a per-series status table."""
    prices = pd.read_csv(work / "insider_prices.csv.gz", dtype={"cik": str, "date": str})
    by_cik = {cik: part for cik, part in prices.groupby("cik", sort=False)}
    meta = series.set_index("series")
    kept, rows = {}, []
    for name, frame in bars.items():
        cik = meta.at[name, "cik"]
        status, ratio, blocked_years = "verified", None, []
        part = by_cik.get(cik)
        merged = None
        if part is not None:
            merged = part.merge(frame[["date", "raw_close"]], on="date", how="inner")
            merged = merged[merged["raw_close"] > 0]
        if merged is not None and len(merged):
            r = merged["price"].to_numpy(float) / merged["raw_close"].to_numpy(float)
            ratio = float(np.median(r))
            if not IDENTITY_RANGE[0] <= ratio <= IDENTITY_RANGE[1]:
                status = "identity_mismatch"
            else:
                merged = merged.assign(ratio=r, year=merged["date"].str[:4])
                for year, g in merged.groupby("year"):
                    if len(g) >= IDENTITY_YEAR_MIN and not IDENTITY_RANGE[0] <= float(g["ratio"].median()) <= IDENTITY_RANGE[1]:
                        blocked_years.append(year)
        else:
            status = "unverified" if int(meta.at[name, "filing_days"]) >= MIN_FILING_DAYS_UNVERIFIED else "unverified_thin"
        rows.append({"series": name, "cik": cik, "ticker": meta.at[name, "ticker"], "status": status,
                     "ratio": ratio, "blocked_years": ",".join(blocked_years), "bars": len(frame),
                     "first_bar": frame["date"].iloc[0], "last_bar": frame["date"].iloc[-1]})
        if status in ("verified", "unverified"):
            kept[name] = (frame, set(blocked_years))
    return kept, pd.DataFrame(rows)


def remove_duplicates(kept: dict, calendar: list[str]) -> tuple[dict, dict]:
    """Series sharing raw close and volume on >= 3 sampled sessions are one stock; keep the longest."""
    sample = set(calendar[::DUP_STEP])
    index = defaultdict(list)
    for name, (frame, _blocked) in kept.items():
        rows = frame[frame["date"].isin(sample)]
        for d, c, v in zip(rows["date"], rows["raw_close"], rows["raw_volume"]):
            if np.isfinite(c) and np.isfinite(v) and v > 0:
                index[(d, round(float(c), 4), int(v))].append(name)
    pairs = defaultdict(int)
    for names in index.values():
        if 1 < len(names) < 20:
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    pairs[tuple(sorted((names[i], names[j])))] += 1
    parent = {name: name for name in kept}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), count in pairs.items():
        if count >= DUP_MATCHES:
            parent[find(a)] = find(b)
    groups = defaultdict(list)
    for name in kept:
        groups[find(name)].append(name)
    out, dropped = {}, {}
    for members in groups.values():
        best = max(members, key=lambda n: (len(kept[n][0]), n))
        out[best] = kept[best]
        for other in members:
            if other != best:
                dropped[other] = best
    return out, dropped


# ------------------------------------------------------------------ per-series replay (worker)

STATE: dict = {}


def _init(state: dict) -> None:
    sys.path.insert(0, str(BACKEND))
    STATE.update(state)


def replay(item) -> dict:
    """Trades of every hypothesis variant on one series, at both costs."""
    from app.services.strategies import rules
    from app.services.strategies.regime import REGIME_GATES, wilder_adx
    from app.services.strategy_discovery import _executable
    from app.services.triple_barrier import BarrierConfig, label_events

    name, frame, blocked = item
    frame = frame.reset_index(drop=True)
    n = len(frame)
    dates = frame["date"].to_numpy(str)
    close = frame["close"].to_numpy(float)
    raw_close = frame["raw_close"].to_numpy(float)
    dollar = pd.Series(raw_close * frame["raw_volume"].to_numpy(float))
    median_dv = dollar.rolling(DV_WINDOW, min_periods=DV_WINDOW).median().to_numpy()
    eligible = np.isfinite(raw_close) & (raw_close >= MIN_PRICE) & np.isfinite(median_dv) & (median_dv >= MIN_DOLLAR_VOLUME)
    if blocked:
        eligible &= ~np.isin(np.array([d[:4] for d in dates]), list(blocked))
    out = {"series": name, "eligible_days": {}, "dv_by_year": {}, "trades": []}
    years = np.array([d[:4] for d in dates])
    for year in np.unique(years):
        mask = years == year
        out["eligible_days"][year] = int(eligible[mask].sum())
        dv = dollar.to_numpy()[mask]
        dv = dv[np.isfinite(dv)]
        out["dv_by_year"][year] = float(np.median(dv)) if len(dv) else 0.0
    if n < 25 or not eligible.any():
        return out
    alive = STATE["alive"]
    delisted = dates[-1] < STATE["alive_from"]
    pct = np.diff(close) / close[:-1]
    drift = np.concatenate([[np.nan], np.cumsum(pct) / np.arange(1, n)])
    spy_open, spy_close = STATE["spy_open"], STATE["spy_close"]
    gates = {"none": np.ones(n, dtype=bool)}
    adx = wilder_adx(frame, 14)
    a, p, m = adx["adx"].to_numpy(), adx["plus_di"].to_numpy(), adx["minus_di"].to_numpy()
    ok = np.isfinite(a) & np.isfinite(p) & np.isfinite(m)
    regime = np.full(n, "neutral", dtype=object)
    regime[ok & (a >= 25) & (p >= m)] = "trend_up"
    regime[ok & (a >= 25) & (p < m)] = "trend_down"
    regime[ok & (a < 20)] = "range"
    gates["adx_not_downtrend"] = np.isin(regime, list(REGIME_GATES["not_downtrend"]))
    for window in (10, 50):
        gates[f"spy_sma{window}"] = np.array([STATE[f"spy_sma{window}"].get(d, False) for d in dates], dtype=bool)
    vix = np.array([STATE["vix"].get(d, np.nan) for d in dates], dtype=float)
    gates["vix_above_20"] = ~np.isfinite(vix) | (vix >= VIX_LEVEL)
    cache: dict = {}
    for h in HYPOTHESES:
        entry = np.asarray(rules.entry_signals(frame, h["rule"], cache), dtype=bool)
        exits = np.asarray(rules.exit_signals(frame, h["rule"], cache), dtype=bool)
        events = np.flatnonzero(entry & gates[h["gate"]] & eligible)
        if not len(events):
            continue
        for cost in (ACTING_COST_BPS, REPORTED_COST_BPS):
            config = BarrierConfig(take_profit_pct=0.0, stop_loss_pct=REVERSION["stop_loss_pct"],
                                   max_holding_days=h["hold"], cost_model="flat", slippage_bps=cost, fees_bps=0.0)
            labels = label_events(frame, events, config, exit_signals=exits)
            labels = labels.assign(barrier=labels["barrier"].astype(object)) if len(labels) else labels
            extra = []
            if delisted:
                resolved = set(labels["event_idx"].tolist()) if len(labels) else set()
                for t in events:
                    t = int(t)
                    entry_idx = t + 1
                    if t in resolved or entry_idx >= n:
                        continue
                    open_entry = float(frame.at[entry_idx, "open"])
                    if not (math.isfinite(open_entry) and open_entry > 0):
                        continue
                    entry_price = open_entry * (1 + cost / 1e4)
                    exit_raw = close[n - 1]
                    exit_price = exit_raw * (1 - cost / 1e4)
                    net = exit_price / entry_price - 1
                    extra.append({"event_idx": t, "entry_idx": entry_idx, "exit_idx": n - 1, "decision_date": dates[t],
                                  "entry_date": dates[entry_idx], "exit_date": dates[n - 1], "entry_price": entry_price,
                                  "exit_price": exit_price, "barrier": "delisted_last_close",
                                  "holding_days": n - 1 - entry_idx, "gross_return": exit_raw / open_entry - 1,
                                  "net_return": net})
            if extra:
                labels = pd.concat([labels, pd.DataFrame(extra)], ignore_index=True) if len(labels) else pd.DataFrame(extra)
            if not len(labels):
                continue
            labels = _executable(labels.sort_values("entry_idx").reset_index(drop=True))
            for row in labels.itertuples(index=False):
                entry_date, exit_date = row.entry_date, row.exit_date
                spy_in = spy_open.get(entry_date)
                spy_out = (spy_open if row.barrier in OPEN_EXITS else spy_close).get(exit_date)
                spy_ret = spy_out / spy_in - 1 if spy_in and spy_out else np.nan
                hold = max(int(row.holding_days), 1)
                d = drift[int(row.event_idx)]
                out["trades"].append({
                    "variant": variant_id(h), "cost_bps": cost, "series": name, "decision_date": row.decision_date,
                    "entry_date": entry_date, "exit_date": exit_date, "barrier": row.barrier,
                    "holding_days": int(row.holding_days), "gross_return": float(row.gross_return),
                    "net_return": float(row.net_return), "spy_return": spy_ret,
                    "excess_spy": float(row.net_return) - spy_ret if np.isfinite(spy_ret) else np.nan,
                    "excess_drift": float(row.net_return) - hold * (0.0 if not np.isfinite(d) else float(d)),
                    "raw_close_decision": float(raw_close[int(row.event_idx)]),
                    "dollar_volume_decision": float(median_dv[int(row.event_idx)]),
                    "delisted_since": bool(delisted)})
    return out


# ------------------------------------------------------------------ statistics

def summarize(trades: pd.DataFrame, t_required: float) -> dict:
    if trades.empty:
        return {"trades": 0, "passes": False}
    net = trades["net_return"].to_numpy(float)
    exc = trades["excess_spy"].to_numpy(float)
    drift = trades["excess_drift"].to_numpy(float)
    months = trades["decision_date"].str[:7].to_numpy()
    t_net = clustered_t(net, months)
    by_year = trades.assign(year=trades["decision_date"].str[:4]).groupby("year").agg(
        trades=("net_return", "size"), net=("net_return", "mean"), excess_spy=("excess_spy", "mean"))
    counted = by_year[by_year["trades"] >= MIN_YEAR_TRADES]
    years_positive = int((counted["excess_spy"] > 0).sum())
    k = int(math.floor(len(trades) * TOP_SHARE))        # the 1% of trades with the highest excess over SPY
    trimmed = trades.sort_values("excess_spy", ascending=False).iloc[k:]
    criteria = {
        "1_net_positive_t": bool(net.mean() > 0 and t_net is not None and t_net >= t_required),
        "2_beats_spy": bool(np.nanmean(exc) > 0),
        "3_most_years": bool(len(counted) > 0 and years_positive > len(counted) / 2),
        "4_without_top_1pct": bool(len(trimmed) > 0 and trimmed["net_return"].mean() > 0
                                   and trimmed["excess_spy"].mean() > 0),
    }
    return {
        "trades": int(len(trades)), "stocks": int(trades["series"].nunique()),
        "net_bps": float(net.mean() * 1e4), "net_t": t_net,
        "median_net_bps": float(np.median(net) * 1e4), "win_rate": float((net > 0).mean()),
        "gross_bps": float(trades["gross_return"].mean() * 1e4),
        "excess_spy_bps": float(np.nanmean(exc) * 1e4), "excess_spy_t": clustered_t(np.nan_to_num(exc), months),
        "excess_drift_bps": float(drift.mean() * 1e4), "excess_drift_t": clustered_t(drift, months),
        "mean_hold": float(trades["holding_days"].mean()),
        "years_counted": int(len(counted)), "years_positive_vs_spy": years_positive,
        "trimmed_net_bps": float(trimmed["net_return"].mean() * 1e4) if len(trimmed) else None,
        "trimmed_excess_spy_bps": float(trimmed["excess_spy"].mean() * 1e4) if len(trimmed) else None,
        "criteria": criteria, "passes": all(criteria.values()),
        "by_year": {y: {"trades": int(r.trades), "net_bps": float(r.net * 1e4), "excess_spy_bps": float(r.excess_spy * 1e4)}
                    for y, r in by_year.iterrows()},
    }


def stage_evaluate(work: Path, workers: int) -> None:
    from multiprocessing import Pool

    sys.path.insert(0, str(BACKEND))
    from app.services.market_gate import above_sma

    started = time.time()
    spy = pd.read_pickle(work / "spy.pkl.gz")
    spy = spy[spy["date"] <= LAST_SESSION].reset_index(drop=True)
    calendar = spy["date"].tolist()
    data_end = calendar[-1]
    vix = pd.read_csv(work / "vix.csv", dtype={"date": str})
    vix_series = pd.Series(vix["close"].to_numpy(float), index=vix["date"]).reindex(calendar).ffill(limit=VIX_FFILL)
    state = {
        "spy_open": dict(zip(spy["date"], spy["open"].astype(float))),
        "spy_close": dict(zip(spy["date"], spy["close"].astype(float))),
        "spy_sma10": dict(zip(spy["date"], above_sma(spy["close"], 10))),
        "spy_sma50": dict(zip(spy["date"], above_sma(spy["close"], 50))),
        "vix": {d: float(v) for d, v in vix_series.items() if np.isfinite(v)},
        "alive_from": calendar[-ALIVE_SESSIONS], "alive": True,
    }
    bars, series = load_series(work)
    print(f"{len(series)} series requested, {len(bars)} with bars ({time.time() - started:.0f} s)", flush=True)
    kept, identity = identity_check(bars, series, work)
    kept, dropped = remove_duplicates(kept, calendar)
    identity["duplicate_of"] = identity["series"].map(dropped)
    identity["kept"] = identity["series"].isin(list(kept))
    identity.to_csv(work / "identity.csv.gz", index=False)
    print(identity["status"].value_counts().to_dict(), f"duplicates dropped {len(dropped)}, kept {len(kept)}", flush=True)

    items = [(name, frame, blocked) for name, (frame, blocked) in kept.items()]
    results = []
    with Pool(workers, initializer=_init, initargs=(state,)) as pool:
        for i, result in enumerate(pool.imap_unordered(replay, items, chunksize=8)):
            results.append(result)
            if (i + 1) % 1000 == 0:
                print(f"{i + 1}/{len(items)} series replayed ({time.time() - started:.0f} s)", flush=True)
    trades = pd.DataFrame([t for r in results for t in r["trades"]])
    meta = series.set_index("series")
    trades["cik"] = trades["series"].map(meta["cik"])
    trades["ticker"] = trades["series"].map(meta["ticker"])
    trades["verified"] = trades["series"].map(identity.set_index("series")["status"]) == "verified"
    bankrupt = pd.read_csv(work / "bankruptcies.csv", dtype=str)
    bankrupt_days = defaultdict(list)
    for cik, filed in zip(bankrupt["cik"], bankrupt["filed"]):
        bankrupt_days[cik].append(pd.Timestamp(filed))
    last_bar = identity.set_index("series")["last_bar"]

    def bankruptcy_tag(row) -> bool:
        if row["barrier"] != "delisted_last_close":
            return False
        end = pd.Timestamp(last_bar[row["series"]])
        return any(end - pd.Timedelta(days=BANKRUPTCY_DAYS) <= day <= end + pd.Timedelta(days=30)
                   for day in bankrupt_days.get(row["cik"], []))

    trades["bankruptcy_8k"] = trades.apply(bankruptcy_tag, axis=1)
    trades = trades.sort_values(["variant", "cost_bps", "decision_date", "series"]).reset_index(drop=True)
    trades.to_csv(work / "trades.csv.gz", index=False)

    # Universe census and the top-500 by previous-year dollar volume.
    eligible_days = defaultdict(int)
    stocks_eligible = defaultdict(int)
    dv_rows = []
    for r in results:
        for year, count in r["eligible_days"].items():
            eligible_days[year] += count
            stocks_eligible[year] += int(count > 0)
        for year, dv in r["dv_by_year"].items():
            dv_rows.append((r["series"], year, dv))
    dv = pd.DataFrame(dv_rows, columns=["series", "year", "dv"])
    top = {}
    for year in range(2017, int(data_end[:4]) + 1):
        prev = dv[dv["year"] == str(year - 1)].sort_values("dv", ascending=False).head(TOP_N)
        top[str(year)] = set(prev["series"])
    sessions_by_year = pd.Series([d[:4] for d in calendar]).value_counts().to_dict()

    results_json = {"data_end": data_end, "data_start": calendar[0], "series_requested": int(len(series)),
                    "series_with_bars": int(len(bars)), "identity": identity["status"].value_counts().to_dict(),
                    "duplicates_dropped": len(dropped), "series_kept": len(kept),
                    "series_kept_delisted_since": int(sum(1 for f, _ in kept.values() if f["date"].iloc[-1] < state["alive_from"])),
                    "eligible_stock_days_by_year": dict(sorted(eligible_days.items())),
                    "stocks_eligible_by_year": dict(sorted(stocks_eligible.items())),
                    "variants": {}}
    for h in HYPOTHESES:
        vid = variant_id(h)
        t_req = T_REQUIRED_BREAKOUT if h["key"] == "HB" else T_REQUIRED
        entry = {"name": h["name"], "t_required": t_req}
        for cost in (ACTING_COST_BPS, REPORTED_COST_BPS):
            part = trades[(trades["variant"] == vid) & (trades["cost_bps"] == cost)]
            label = f"{cost:g}bps"
            entry[label] = summarize(part, t_req)
            if cost != ACTING_COST_BPS:
                continue
            entry["delisted_since"] = summarize(part[part["delisted_since"]], t_req)
            entry["survivors_only"] = summarize(part[~part["delisted_since"]], t_req)
            entry["verified_only"] = summarize(part[part["verified"]], t_req)
            entry["share_delisted_since"] = float(part["delisted_since"].mean()) if len(part) else None
            stopped = part[part["barrier"] == "delisted_last_close"]
            entry["stopped_trading_in_hold"] = {
                "count": int(len(stopped)), "bankruptcy_tagged": int(stopped["bankruptcy_8k"].sum()),
                "mean_net_bps": float(stopped["net_return"].mean() * 1e4) if len(stopped) else None}
            harsh = part.copy()
            hit = harsh["bankruptcy_8k"]
            harsh.loc[hit, "net_return"] = -1.0
            harsh.loc[hit, "excess_spy"] = -1.0 - harsh.loc[hit, "spy_return"]
            entry["bankruptcy_minus_100"] = summarize(harsh, t_req) if hit.any() else None
            entry["barriers"] = part["barrier"].value_counts().to_dict()
            rate = {}
            for year, members in top.items():
                inside = part[(part["decision_date"].str[:4] == year) & part["series"].isin(members)]
                scale = 252 / sessions_by_year.get(year, 252)
                rate[year] = {"top500": round(len(inside) * scale, 1),
                              "all": round(int((part["decision_date"].str[:4] == year).sum()) * scale, 1)}
            full = [y for y in rate if y < data_end[:4]]
            mean_top = float(np.mean([rate[y]["top500"] for y in full])) if full else None
            mean_all = float(np.mean([rate[y]["all"] for y in full])) if full else None
            entry["trades_per_year"] = rate
            entry["mean_trades_per_year_top500"] = mean_top
            entry["mean_trades_per_year_all"] = mean_all
            sample = VERDICT_SAMPLE[h["key"]]
            entry["verdict_sample"] = sample
            entry["years_to_verdict_top500"] = sample / mean_top if mean_top else None
            entry["years_to_verdict_all"] = sample / mean_all if mean_all else None
        results_json["variants"][vid] = entry
    hb = [results_json["variants"][variant_id(h)]["25bps"].get("passes", False) for h in HYPOTHESES if h["key"] == "HB"]
    results_json["verdict"] = {"H2": results_json["variants"]["H2:none"]["25bps"].get("passes", False),
                               "H5": results_json["variants"]["H5:vix_above_20"]["25bps"].get("passes", False),
                               "HB": any(hb)}
    (work / "results.json").write_text(json.dumps(results_json, indent=1, default=str))
    print(json.dumps(results_json["verdict"]), f"({time.time() - started:.0f} s)")


def _f(value, digits=1, signed=True):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"


def stage_tables(work: Path, out: Path) -> None:
    """results.json -> tables.md (the report's tables), and copies of the result files beside this script."""
    res = json.loads((work / "results.json").read_text())
    lines = [f"# Tables: dip rules and breakouts on the whole market ({res['data_start']} to {res['data_end']})", ""]
    lines += ["## Pass rule at 25 bps a side", "",
              "| Variant | Trades | Stocks | Net bps | t (net) | t needed | vs SPY bps | t (vs SPY) | Years + / counted | "
              "Without top 1%: net / vs SPY | vs own drift bps (t) | 1 | 2 | 3 | 4 | Pass |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for vid, v in res["variants"].items():
        s = v["25bps"]
        if not s.get("trades"):
            lines.append(f"| {vid} | 0 | | | | | | | | | | | | | | fail |")
            continue
        c = s["criteria"]
        mark = lambda ok: "yes" if ok else "**no**"
        lines.append(f"| {vid} | {s['trades']:,} | {s['stocks']:,} | {_f(s['net_bps'])} | {_f(s['net_t'], 2)} | "
                     f"{v['t_required']:.2f} | {_f(s['excess_spy_bps'])} | {_f(s['excess_spy_t'], 2)} | "
                     f"{s['years_positive_vs_spy']} / {s['years_counted']} | {_f(s['trimmed_net_bps'])} / "
                     f"{_f(s['trimmed_excess_spy_bps'])} | {_f(s['excess_drift_bps'])} ({_f(s['excess_drift_t'], 2)}) | "
                     f"{mark(c['1_net_positive_t'])} | {mark(c['2_beats_spy'])} | {mark(c['3_most_years'])} | "
                     f"{mark(c['4_without_top_1pct'])} | {'**pass**' if s['passes'] else 'fail'} |")
    lines += ["", "## Reported, not decisive", "",
              "| Variant | Gross bps | Net @11 bps (t) | Win rate | Mean hold | Delisted-since share | Delisted since: net / vs SPY (n) | "
              "Survivors only: net (t) / vs SPY (n) | Verified only: net (t) / vs SPY (n) |",
              "|---|---|---|---|---|---|---|---|---|"]
    for vid, v in res["variants"].items():
        s, e = v["25bps"], v["11bps"]
        if not s.get("trades"):
            continue
        d, sv, vf = v["delisted_since"], v["survivors_only"], v["verified_only"]
        lines.append(f"| {vid} | {_f(s['gross_bps'])} | {_f(e['net_bps'])} ({_f(e['net_t'], 2)}) | {s['win_rate']:.1%} | "
                     f"{s['mean_hold']:.1f} | {v['share_delisted_since']:.1%} | "
                     f"{_f(d.get('net_bps'))} / {_f(d.get('excess_spy_bps'))} ({d.get('trades', 0):,}) | "
                     f"{_f(sv.get('net_bps'))} ({_f(sv.get('net_t'), 2)}) / {_f(sv.get('excess_spy_bps'))} ({sv.get('trades', 0):,}) | "
                     f"{_f(vf.get('net_bps'))} ({_f(vf.get('net_t'), 2)}) / {_f(vf.get('excess_spy_bps'))} ({vf.get('trades', 0):,}) |")
    lines += ["", "## Stocks that stopped trading during a hold (25 bps)", "",
              "| Variant | Count | Bankruptcy 8-K within a year | Mean net bps | Whole rule with those at −100%: net bps (t), pass |",
              "|---|---|---|---|---|"]
    for vid, v in res["variants"].items():
        st = v.get("stopped_trading_in_hold")
        if not st:
            continue
        harsh = v.get("bankruptcy_minus_100")
        h = f"{_f(harsh['net_bps'])} ({_f(harsh['net_t'], 2)}), {'pass' if harsh['passes'] else 'fail'}" if harsh else "no case"
        lines.append(f"| {vid} | {st['count']} | {st['bankruptcy_tagged']} | {_f(st['mean_net_bps'])} | {h} |")
    lines += ["", "## Each calendar year at 25 bps (trades / net bps / vs SPY bps)", ""]
    years = sorted({y for v in res["variants"].values() for y in v["25bps"].get("by_year", {})})
    lines += ["| Variant | " + " | ".join(years) + " |", "|---|" + "---|" * len(years)]
    for vid, v in res["variants"].items():
        by = v["25bps"].get("by_year", {})
        cells = [f"{by[y]['trades']} / {by[y]['net_bps']:+.0f} / {by[y]['excess_spy_bps']:+.0f}" if y in by else "" for y in years]
        lines.append(f"| {vid} | " + " | ".join(cells) + " |")
    lines += ["", "## Trade rate (completed trades a year; 2026 scaled to a full year)", "",
              "Top 500 = the 500 stocks with the highest median daily dollar volume over the previous calendar year.", ""]
    rate_years = sorted({y for v in res["variants"].values() for y in v.get("trades_per_year", {})})
    lines += ["| Variant | " + " | ".join(rate_years) + " | Mean 2017-2025 | Verdict sample | Years to verdict |",
              "|---|" + "---|" * (len(rate_years) + 3)]
    for vid, v in res["variants"].items():
        rate = v.get("trades_per_year", {})
        cells = [f"{rate[y]['top500']:.0f} ({rate[y]['all']:.0f})" if y in rate else "" for y in rate_years]
        lines.append(f"| {vid} | " + " | ".join(cells) + f" | {_f(v.get('mean_trades_per_year_top500'), 0, False)} "
                     f"({_f(v.get('mean_trades_per_year_all'), 0, False)}) | {v.get('verdict_sample')} | "
                     f"{_f(v.get('years_to_verdict_top500'), 2, False)} ({_f(v.get('years_to_verdict_all'), 2, False)}) |")
    lines += ["", "Cells: top 500 (whole eligible universe).", "", "## Coverage", "",
              f"* (CIK, ticker) series requested: {res['series_requested']:,}; with bars: {res['series_with_bars']:,}",
              f"* Identity: {json.dumps(res['identity'])}",
              f"* Duplicates dropped: {res['duplicates_dropped']:,}; series kept: {res['series_kept']:,}, of which delisted "
              f"since: {res['series_kept_delisted_since']:,}",
              "* Stocks eligible on at least one day, by year: " + ", ".join(f"{y}: {n:,}" for y, n in res["stocks_eligible_by_year"].items()),
              "* Eligible stock-days by year: " + ", ".join(f"{y}: {n:,}" for y, n in res["eligible_stock_days_by_year"].items())]
    out.mkdir(parents=True, exist_ok=True)
    (out / "tables.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {out / 'tables.md'}")


def main() -> None:
    stage, work = sys.argv[1], Path(sys.argv[2]).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    if stage == "inputs":
        stage_inputs(work, Path(sys.argv[3]).expanduser())
    elif stage == "series":
        stage_series(work)
    elif stage == "fetch":
        stage_fetch(work)
    elif stage == "vix":
        stage_vix(work)
    elif stage == "evaluate":
        workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 8
        stage_evaluate(work, workers)
    elif stage == "tables":
        stage_tables(work, Path(sys.argv[3]).expanduser())
    else:
        raise SystemExit(f"unknown stage {stage}")


if __name__ == "__main__":
    main()
