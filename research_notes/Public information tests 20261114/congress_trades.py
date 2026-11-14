"""Test 2: buying after a member of Congress discloses a purchase. Frozen in PREREGISTRATION.md (same folder).

    ALPACA_KEY=... ALPACA_SECRET=... python3 -I congress_trades.py qqq <work_dir>     # the QQQ benchmark (reported only)
    <repo venv> python -I congress_trades.py evaluate <work_dir> <price cache dir>

``evaluate`` reads <work_dir>/house/transactions.csv (``house_fetch.py parse``), the Senate mirror's
<work_dir>/senate/all_daily_summaries.json and PR #95's price cache, and writes
<work_dir>/congress/{events.csv.gz, trades.csv.gz, results.json}. ``qqq`` reads daily bars of one symbol
from Alpaca's market-data API; never an account or an order endpoint.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from bisect import bisect_right
from datetime import date, datetime
from pathlib import Path

HOLD = 60
SIDE_HOLDS = (20, 120)
FIRST_FILING = "2016-01-01"
COPY_FUND_LAUNCH = "2023-02-07"
FAST_DELAY_DAYS = 15
LARGE_AMOUNT = 50_001
T_REQUIRED = 2.0
SENATE_LAST = "2021-03-10"


def qqq(work: Path) -> None:
    key, secret = os.environ["ALPACA_KEY"], os.environ["ALPACA_SECRET"]
    rows, token = [], None
    while True:
        params = {"symbols": "QQQ", "timeframe": "1Day", "start": "2016-01-01", "end": "2026-11-11",
                  "adjustment": "all", "feed": "sip", "limit": 10000}
        if token:
            params["page_token"] = token
        request = urllib.request.Request("https://data.alpaca.markets/v2/stocks/bars?" + urllib.parse.urlencode(params),
                                         headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        with urllib.request.urlopen(request, timeout=90) as response:
            body = json.load(response)
        rows += [(bar["t"][:10], bar["o"], bar["c"]) for bar in (body.get("bars") or {}).get("QQQ") or []]
        token = body.get("next_page_token")
        if not token:
            break
    (work / "congress").mkdir(exist_ok=True)
    with (work / "congress" / "qqq.csv").open("w") as out:
        out.write("date,open,close\n" + "".join(f"{d},{o},{c}\n" for d, o, c in sorted(set(rows))))
    print(f"QQQ: {len(rows)} bars")


def amount_floor(text: str) -> int:
    match = re.search(r"\$([\d,]+)", text or "")
    return int(match.group(1).replace(",", "")) if match else 0


def house_rows(work: Path) -> list[dict]:
    import csv

    out = []
    with (work / "house" / "transactions.csv").open() as handle:
        for row in csv.DictReader(handle):
            if row["asset_type"] != "ST" or not row["ticker"] or row["type"] not in ("P", "S", "S (partial)"):
                continue
            out.append({"chamber": "house", "report": row["doc_id"], "member": row["member"],
                        "public_date": row["filing_date"], "trade_date": row["trade_date"],
                        "ticker": row["ticker"].upper(), "side": "buy" if row["type"] == "P" else "sell",
                        "amount": amount_floor(row["amount"])})
    return out


def senate_rows(work: Path) -> list[dict]:
    path = work / "senate" / "all_daily_summaries.json"
    if not path.exists():
        return []
    out = []
    for report in json.loads(path.read_text()):
        try:
            public = datetime.strptime(report.get("date_recieved") or "", "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        for row in report.get("transactions") or []:
            kind = row.get("type") or ""
            if row.get("asset_type") != "Stock" or not (kind == "Purchase" or kind.startswith("Sale")):
                continue
            match = re.search(r">\s*([A-Z][A-Z0-9]{0,4}(?:[./][A-Z]{1,2})?)\s*<", row.get("ticker") or "")
            if not match:
                continue
            try:
                traded = datetime.strptime(row.get("transaction_date") or "", "%m/%d/%Y").date().isoformat()
            except ValueError:
                traded = ""
            out.append({"chamber": "senate", "report": (report.get("ptr_link") or "")[-37:-1],
                        "member": f"{report.get('first_name', '')} {report.get('last_name', '')}".strip(),
                        "public_date": public, "trade_date": traded, "ticker": match.group(1).replace("/", "."),
                        "side": "buy" if kind == "Purchase" else "sell", "amount": amount_floor(row.get("amount"))})
    return out


def evaluate(work: Path, cache: Path) -> None:
    import numpy as np
    import pandas as pd

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import pubinfo_lib as L

    market = L.load_market(cache)
    calendar = market.calendar
    out = work / "congress"
    out.mkdir(exist_ok=True)
    raw = pd.DataFrame(house_rows(work) + senate_rows(work))
    counts = {"rows": {f"{c}_{s}": int(n) for (c, s), n in raw.groupby(["chamber", "side"]).size().items()}}
    raw = raw[raw["public_date"] >= FIRST_FILING].copy()
    status = []
    series, decisions = [], []
    for row in raw.itertuples(index=False):
        d = bisect_right(calendar, row.public_date) - 1         # the last close on or before the public date
        s = L.series_by_ticker(market, row.ticker, d) if d >= 0 else None
        if d < 0 or d + 1 >= market.sessions:
            status.append("outside_data")
        elif s is None:
            status.append("no_series")
        elif row.trade_date and row.trade_date > row.public_date:
            status.append("trade_after_filing")
        elif not market.eligible[s, d]:
            status.append("not_eligible")
        else:
            status.append("event")
        series.append(-1 if s is None else s)
        decisions.append(d)
    raw["status"], raw["series"], raw["decision"] = status, series, decisions
    counts["status"] = {f"{c}_{s}_{st}": int(n) for (c, s, st), n in raw.groupby(["chamber", "side", "status"]).size().items()}
    rows = raw[raw["status"] == "event"].copy()
    delay = [(date.fromisoformat(p) - date.fromisoformat(t)).days if t else np.nan
             for p, t in zip(rows["public_date"], rows["trade_date"])]
    rows["delay"] = delay
    # One event per chamber, side, stock and entry session.
    events = rows.groupby(["chamber", "side", "series", "decision"]).agg(
        members=("member", "nunique"), rows=("member", "size"), amount=("amount", "max"), delay=("delay", "min"),
        public_date=("public_date", "max")).reset_index()
    events["ticker"] = [market.ticker[s] for s in events["series"]]
    events.to_csv(out / "events.csv.gz", index=False)

    qqq_path = out / "qqq.csv"
    qqq_open = qqq_close = None
    if qqq_path.exists():
        frame = pd.read_csv(qqq_path, dtype={"date": str}).set_index("date").reindex(calendar)
        qqq_open, qqq_close = frame["open"].to_numpy(float), frame["close"].to_numpy(float)

    def trades_of(part: pd.DataFrame, hold: int = HOLD, delay_sessions: int = 0, cost: float = L.ACTING_COST) -> pd.DataFrame:
        made = []
        for event in part.itertuples(index=False):
            trade = L.fixed_hold(market, int(event.series), int(event.decision) + delay_sessions, hold, cost)
            if trade is None or trade["outcome"] == "no_entry":
                continue
            made.append({"series": int(event.series), "decision": int(event.decision), "members": event.members,
                         "amount": event.amount, "delay": event.delay, **trade})
        frame = pd.DataFrame(L.one_position(made))
        if frame.empty:
            return frame
        frame["decision_date"] = [calendar[i] for i in frame["decision"]]
        frame["month"], frame["year"] = frame["decision_date"].str[:7], frame["decision_date"].str[:4]
        frame["ticker"] = [market.ticker[s] for s in frame["series"]]
        frame["delisted_since"] = [market.delisted_since(s) for s in frame["series"]]
        if qqq_open is not None:
            frame["qqq"] = qqq_close[frame["exit"].to_numpy()] / qqq_open[frame["entry"].to_numpy()] - 1
            frame["excess_qqq"] = frame["net"] - frame["qqq"]
        return frame

    def both(part: pd.DataFrame, **kwargs) -> dict:
        trades = trades_of(part, **kwargs)
        stats = L.summarize(trades, T_REQUIRED)
        if stats.get("trades"):
            stats["share_delisted_since"] = float(trades["delisted_since"].mean())
            if "excess_qqq" in trades:
                stats["excess_qqq_bps"] = float(trades["excess_qqq"].mean() * 1e4)
                stats["excess_qqq_t"] = L.clustered_t(trades["excess_qqq"].to_numpy(float), trades["month"].to_numpy())
        return stats

    def pick(chamber: str | None, side: str) -> pd.DataFrame:
        part = events[events["side"] == side]
        if chamber is None:                                     # both chambers: one event per stock and session
            part = part.sort_values("chamber").drop_duplicates(["series", "decision"], keep="first")
            return part
        return part[part["chamber"] == chamber]

    house_buys = pick("house", "buy")
    primary_trades = trades_of(house_buys)
    primary_trades.drop(columns=["series", "decision", "entry", "exit"]).to_csv(out / "trades.csv.gz", index=False)
    primary = L.summarize(primary_trades, T_REQUIRED)
    if primary.get("trades"):
        primary["share_delisted_since"] = float(primary_trades["delisted_since"].mean())
        if "excess_qqq" in primary_trades:
            primary["excess_qqq_bps"] = float(primary_trades["excess_qqq"].mean() * 1e4)
            primary["excess_qqq_t"] = L.clustered_t(primary_trades["excess_qqq"].to_numpy(float),
                                                    primary_trades["month"].to_numpy())
        top = primary_trades["ticker"].value_counts().head(15)
        primary["most_traded"] = {ticker: int(n) for ticker, n in top.items()}

    public = house_buys["public_date"]
    senate_window = events[events["public_date"] <= SENATE_LAST]
    both_buys = senate_window[senate_window["side"] == "buy"].sort_values("chamber") \
        .drop_duplicates(["series", "decision"], keep="first")
    reported = {
        "lenient_cost_12.5bps": both(house_buys, cost=L.LENIENT_COST),
        "hold_20": both(house_buys, hold=20),
        "hold_120": both(house_buys, hold=120),
        "entry_one_session_later": both(house_buys, delay_sessions=1),
        "before_copy_fund": both(house_buys[public < COPY_FUND_LAUNCH]),
        "since_copy_fund": both(house_buys[public >= COPY_FUND_LAUNCH]),
        "delay_15_days_or_less": both(house_buys[house_buys["delay"] <= FAST_DELAY_DAYS]),
        "delay_longer": both(house_buys[house_buys["delay"] > FAST_DELAY_DAYS]),
        "amount_50001_or_more": both(house_buys[house_buys["amount"] >= LARGE_AMOUNT]),
        "several_members_same_day": both(house_buys[house_buys["members"] >= 2]),
        "house_sales_mirror": both(pick("house", "sell")),
        "senate_buys_2016_to_2021_03": both(pick("senate", "buy")),
        "senate_sales_2016_to_2021_03": both(pick("senate", "sell")),
        "both_chambers_buys_2016_to_2021_03": both(both_buys),
    }
    results = {"data_end": calendar[-1], "counts": counts,
               "events": {f"{c}_{s}": int(n) for (c, s), n in events.groupby(["chamber", "side"]).size().items()},
               "primary_house_purchases_hold_60": primary, "reported": reported,
               "verdict": "forward paper test" if primary.get("passes") else "closed"}
    (out / "results.json").write_text(json.dumps(results, indent=1, default=str))
    print(json.dumps({"counts": counts, "events": results["events"]}, indent=1))
    keys = ("trades", "stocks", "net_bps", "net_t", "excess_bps", "excess_t", "years_positive", "years_counted",
            "trimmed_net_bps", "trimmed_excess_bps", "criteria", "passes")
    print("PRIMARY", json.dumps({k: primary.get(k) for k in keys}, default=str))
    for name, stats in reported.items():
        print(name, json.dumps({k: stats.get(k) for k in keys[:8]}, default=str))
    print("verdict:", results["verdict"])


if __name__ == "__main__":
    command, work_dir = sys.argv[1], Path(sys.argv[2])
    if command == "qqq":
        qqq(work_dir)
    elif command == "evaluate":
        evaluate(work_dir, Path(sys.argv[3]))
    else:
        raise SystemExit("commands: qqq, evaluate")
