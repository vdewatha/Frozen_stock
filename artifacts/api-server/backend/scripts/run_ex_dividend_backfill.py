"""Step 8's ex-dividend backfill and its one date-clustered test (pre-registered).

``reports/Retail bot edge feasibility.md``, build-plan row 8: log the
five-session pre-ex-dividend market-adjusted return for point-in-time S&P 500
payers, backfilled over 2015-2026 from free dividend history, members without
history flagged and the result labelled an upper bound; run one date-clustered
test, admitted only as a zero-turnover tie-breaker if t >= 2.5.

Pre-registration (fixed before the first run)
---------------------------------------------
* **Universe.**  Every ticker that was an S&P 500 member at any time from
  2015-01-01 (``point_in_time_universe``, the fja05680/sp500 membership file).
  An event counts only if the ticker was a member on its ex-date.
* **Data ($0).**  yfinance adjusted daily closes and the ``Dividends`` action
  column (``auto_adjust=True, actions=True``) from 2014-09-01, plus SPY.  A
  dividend > 0 on a bar is an ex-date.
* **Event window.**  From the close six sessions before the ex-date to the
  close one session before it: five sessions, all cum-dividend, so the stock's
  price ratio is the same on adjusted and raw closes and the dividend itself is
  never in the return.  Sessions are SPY's trading days.
* **Outcome.**  Market-adjusted return = stock return minus SPY's (adjusted,
  so total-return) return over the same two closes, in basis points.
* **Sample.**  Ex-dates 2015-01-01 to the last completed session in the data.
* **The one test.**  Mean market-adjusted return with standard errors
  clustered by ex-date (CR1).  Passes if the mean is positive and t >= 2.5.
  A pass admits it only as a zero-turnover tie-breaker, documented and not
  wired into entries.
* **Flags.**  Members with no yfinance history at all are listed and counted
  by year; their events cannot be scored.  Delisted and acquired names are the
  ones Yahoo drops, so the scored sample over-represents survivors and the
  mean is labelled an **upper bound**.

Reported but not deciding: the iid t, a t clustered by calendar week (windows
of nearby ex-dates overlap), the mean by year and the intra-date correlation
the earnings log uses as its clustering prior until it has its own.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_ex_dividend_backfill.py --data-dir <dir> --membership-csv <csv> \\
        [--download] --output <json> [--summary-module app/services/event_log_backfill.py]
"""
from __future__ import annotations

import argparse
import json
import pprint
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import event_logs  # noqa: E402
from app.services.point_in_time_universe import load_membership  # noqa: E402

START = date(2015, 1, 1)
DATA_START = date(2014, 9, 1)
MARKET = "SPY"


def _cache_path(data_dir: Path, symbol: str) -> Path:
    return data_dir / f"{event_logs.yahoo_symbol(symbol)}.csv"


def load_frames(symbols: list[str], data_dir: Path, *, download: bool, end: date) -> tuple[dict, list[str]]:
    data_dir.mkdir(parents=True, exist_ok=True)
    missing_path = data_dir / "_missing.json"
    known_missing = set(json.loads(missing_path.read_text())) if missing_path.exists() else set()
    need = [s for s in symbols if not _cache_path(data_dir, s).exists() and (download or s not in known_missing)]
    if download and need:
        got = event_logs.download_prices(need, DATA_START, end, actions=True)
        for symbol, frame in got.items():
            frame.rename_axis("date").reset_index().to_csv(_cache_path(data_dir, symbol), index=False)
        known_missing |= {s for s in need if s not in got}
        known_missing -= set(got)
        missing_path.write_text(json.dumps(sorted(known_missing)))
    frames, missing = {}, []
    for symbol in symbols:
        path = _cache_path(data_dir, symbol)
        if not path.exists():
            missing.append(symbol)
            continue
        frame = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
        if "dividends" not in frame:
            frame["dividends"] = 0.0
        frames[symbol] = frame
    return frames, missing


def by_year_flags(membership, tickers: list[str], missing: set[str], last: date) -> dict:
    out = {}
    for year in range(START.year, last.year + 1):
        start, end = pd.Timestamp(year, 1, 1), pd.Timestamp(min(date(year, 12, 31), last))
        members = [t for t in tickers if any(s <= end and (e is None or e > start)
                                             for s, e in membership.intervals.get(t, ()))]
        lacking = [t for t in members if t in missing]
        out[str(year)] = {"members": len(members), "without_history": len(lacking),
                          "share_without_history": round(len(lacking) / len(members), 4) if members else None}
    return out


def run(frames: dict, missing: list[str], tickers: list[str], membership, *, until: date) -> dict:
    market = frames[MARKET]["close"]
    events = []
    payers = set()
    for symbol in tickers:
        frame = frames.get(symbol)
        if frame is None:
            continue
        found = event_logs.ex_dividend_events(symbol, frame, market, membership=membership, since=START, until=until)
        if found:
            payers.add(symbol)
        events.extend(found)
    frame = pd.DataFrame(events)
    frame["bps"] = frame["market_adjusted"] * 1e4
    frame["week"] = pd.to_datetime(frame["ex_date"]).dt.to_period("W").astype(str)
    frame["year"] = frame["ex_date"].str[:4]
    test = event_logs.clustered_mean(frame["bps"], frame["ex_date"])
    week = event_logs.clustered_mean(frame["bps"], frame["week"])
    rho = event_logs.intra_date_correlation(frame["market_adjusted"], frame["ex_date"])
    passes = bool(test["mean"] is not None and test["mean"] > 0 and test["t"] is not None
                  and test["t"] >= event_logs.ADMISSION_T)
    by_year = {year: {"events": int(len(g)), "mean_bps": round(float(g["bps"].mean()), 2),
                      "t_date_clustered": round(event_logs.clustered_mean(g["bps"], g["ex_date"])["t"] or 0.0, 2)}
               for year, g in frame.groupby("year")}
    flags = by_year_flags(membership, tickers, set(missing), until)
    return {
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": "close 6 sessions before the ex-date to the close 1 session before it, minus SPY",
        "sample": {"first": frame["ex_date"].min(), "last": frame["ex_date"].max()},
        "events": int(len(frame)), "ex_dates": int(frame["ex_date"].nunique()), "payers": len(payers),
        "members_since_2015": len(tickers), "members_without_history": len(missing),
        "members_without_history_list": sorted(missing),
        "mean_bps": round(test["mean"], 3), "median_bps": round(float(frame["bps"].median()), 3),
        "sd_bps": round(float(frame["bps"].std(ddof=1)), 2), "win_rate": round(float((frame["bps"] > 0).mean()), 4),
        "se_bps": round(test["se"], 3), "t_date_clustered": round(test["t"], 3),
        "t_iid": round(test["iid_t"], 3), "t_week_clustered": round(week["t"], 3),
        "intra_date_correlation": None if rho is None else round(rho, 4),
        "admission_t": event_logs.ADMISSION_T, "passes": passes,
        "outcome": ("admitted as a zero-turnover tie-breaker (documented, not wired into entries)" if passes
                    else "not admitted: below the t >= 2.5 bar"),
        "label": "upper bound: members without yfinance history (mostly delisted or acquired) are not scored",
        "by_year": by_year, "flags_by_year": flags,
    }


def write_module(path: Path, summary: dict) -> None:
    compact = {key: value for key, value in summary.items() if key != "members_without_history_list"}
    compact["members_without_history_sample"] = summary["members_without_history_list"][:40]
    text = ('"""Stored summary of the ex-dividend backfill (written by scripts/run_ex_dividend_backfill.py).\n\n'
            "See reports/event-logs-20261107.md.  Read by event_logs.event_log_status and as the earnings\n"
            'log\'s clustering prior; nothing trades on it.\n"""\n\n'
            f"EX_DIVIDEND_BACKFILL = {pprint.pformat(compact, width=110, sort_dicts=False)}\n")
    path.write_text(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--membership-csv", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-module", type=Path)
    args = parser.parse_args()
    membership = load_membership(download=False, path=args.membership_csv)
    tickers = membership.tickers(since=START.isoformat())
    today = date.today()
    frames, missing = load_frames(sorted(set(tickers) | {MARKET}), args.data_dir, download=args.download, end=today)
    if MARKET not in frames:
        raise SystemExit("no SPY history")
    last = frames[MARKET].index.max().date()
    summary = run(frames, [m for m in missing if m != MARKET], tickers, membership, until=last)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({k: v for k, v in summary.items() if k not in ("members_without_history_list", "flags_by_year",
                                                                     "by_year")}, indent=2, default=str))
    for year, row in summary["by_year"].items():
        print(year, row, summary["flags_by_year"].get(year))
    if args.summary_module:
        write_module(args.summary_module, summary)


if __name__ == "__main__":
    main()
