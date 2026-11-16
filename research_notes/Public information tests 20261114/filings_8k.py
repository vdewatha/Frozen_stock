"""Test 1: drift after 8-K event filings, by item. The rule is frozen in PREREGISTRATION.md (same folder).

    <repo venv> python -I filings_8k.py <work_dir> <price cache dir>

Reads <work_dir>/sec/filings_8k.csv and <work_dir>/sec/acceptance.csv (``sec_8k_fetch.py``) and PR #95's
price cache. Writes <work_dir>/filings_8k/{events.csv.gz, discovery.json, results.json, trades_*.csv.gz}.

Order of work, as registered: discovery (decisions to 2021-12-31) for all 75 candidates; the held-out period
is computed only for the candidates discovery selects, and for the avoid-list candidates.
"""
from __future__ import annotations

import json
import sys
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pubinfo_lib as L  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
FIRST_ACCEPTED = "2016-01-01"
DISCOVERY_END = "2021-12-31"
ITEMS = ["1.01", "1.02", "1.03", "1.04", "1.05", "2.01", "2.03", "2.04", "2.05", "2.06", "3.01", "3.02", "3.03",
         "4.01", "4.02", "5.01", "5.02", "5.03", "5.04", "5.05", "5.06", "5.07", "5.08", "7.01", "8.01"]
SIGNALS = ("any", "up", "down")
REACTION = 0.05
HOLD = 20
SIDE_HOLDS = (5, 60)
MIN_TRADES = 300
T_SELECT = 3.2
T_HELD_OUT = 2.0


def sessions_of(accepted: str, calendar: list[str]) -> tuple[int, int] | None:
    """(P, R) as PR #92: the last close before the filing and the first close that reacts to all of it."""
    day, clock = accepted[:10], accepted[11:]
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
        return None
    return p, r


def build_events(work: Path, market: L.Market) -> pd.DataFrame:
    """One row per (filing without Item 2.02, mapped and eligible at R): series, P, R, reaction, items."""
    filings = pd.read_csv(work / "sec" / "filings_8k.csv", dtype=str, keep_default_na=False)
    accepted = pd.read_csv(work / "sec" / "acceptance.csv", dtype=str).drop_duplicates("accession")
    stamp = dict(zip(accepted["accession"], accepted["accepted_utc"]))
    counts = {"filings": len(filings), "with_item_202": 0, "no_time": 0, "before_2016": 0, "no_session": 0,
              "no_series": 0, "no_bar_at_p": 0, "not_eligible": 0, "no_listed_item": 0, "events": 0}
    rows = []
    for filing in filings.itertuples(index=False):
        items = filing.items.split()
        if "2.02" in items:
            counts["with_item_202"] += 1
            continue
        listed = [item for item in items if item in ITEMS]
        if not listed:
            counts["no_listed_item"] += 1
            continue
        utc = stamp.get(filing.accession)
        if utc is None:
            counts["no_time"] += 1
            continue
        local = datetime.strptime(utc[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone(EASTERN)
        text = local.strftime("%Y-%m-%dT%H:%M:%S")
        if text[:10] < FIRST_ACCEPTED:
            counts["before_2016"] += 1
            continue
        pr = sessions_of(text, market.calendar)
        if pr is None:
            counts["no_session"] += 1
            continue
        p, r = pr
        s = L.series_by_cik(market, filing.cik, r)
        if s is None:
            counts["no_series"] += 1
            continue
        if not np.isfinite(market.close[s, p]):
            counts["no_bar_at_p"] += 1
            continue
        if not market.eligible[s, r]:
            counts["not_eligible"] += 1
            continue
        reaction = float(market.close[s, r] / market.close[s, p] - 1
                         - (market.spy_close[r] / market.spy_close[p] - 1))
        rows.append({"cik": filing.cik, "accession": filing.accession, "accepted": text, "series": s, "p": p, "r": r,
                     "reaction": reaction, "items": " ".join(listed)})
    counts["events"] = len(rows)
    events = pd.DataFrame(rows)
    events["decision_date"] = [market.calendar[r] for r in events["r"]]
    return events, counts


def candidate_rows(events: pd.DataFrame, item: str, signal: str) -> pd.DataFrame:
    has = events["items"].str.split().map(lambda listed: item in listed)
    part = events[has]
    if signal == "up":
        part = part[part["reaction"] >= REACTION]
    elif signal == "down":
        part = part[part["reaction"] <= -REACTION]
    return part.drop_duplicates(["series", "r"], keep="first")


def trades_for(market: L.Market, part: pd.DataFrame, hold: int, cost: float = L.ACTING_COST) -> pd.DataFrame:
    rows = []
    for event in part.itertuples(index=False):
        trade = L.fixed_hold(market, int(event.series), int(event.r), hold, cost)
        if trade is None or trade["outcome"] == "no_entry":
            continue
        rows.append({"series": int(event.series), "decision": int(event.r), "accession": event.accession,
                     "reaction": event.reaction, **trade})
    taken = L.one_position(rows)
    frame = pd.DataFrame(taken)
    if frame.empty:
        return frame
    frame["decision_date"] = [market.calendar[i] for i in frame["decision"]]
    frame["month"] = frame["decision_date"].str[:7]
    frame["year"] = frame["decision_date"].str[:4]
    frame["ticker"] = [market.ticker[s] for s in frame["series"]]
    frame["delisted_since"] = [market.delisted_since(s) for s in frame["series"]]
    return frame


def describe(market: L.Market, trades: pd.DataFrame, t_required: float) -> dict:
    stats = L.summarize(trades, t_required)
    if not stats.get("trades"):
        return stats
    stats["share_delisted_since"] = float(trades["delisted_since"].mean())
    stats["stopped"] = int((trades["outcome"] == "stopped").sum())
    harsh = trades.copy()
    hit = harsh["bankruptcy"].astype(bool)
    stats["bankruptcy_tagged"] = int(hit.sum())
    if hit.any():
        harsh.loc[hit, "net"] = -1.0
        harsh.loc[hit, "excess"] = -1.0 - harsh.loc[hit, "spy"]
        again = L.summarize(harsh, t_required)
        stats["bankruptcy_minus_100"] = {key: again[key] for key in ("net_bps", "net_t", "excess_bps", "excess_t")}
    return stats


def main(work: Path, cache: Path) -> None:
    out = work / "filings_8k"
    out.mkdir(exist_ok=True)
    market = L.load_market(cache)
    events, counts = build_events(work, market)
    print(json.dumps(counts), flush=True)
    clock = events["accepted"].str[11:]
    timing = {"before_0930": float((clock < "09:30:00").mean()),
              "session": float(((clock >= "09:30:00") & (clock < "16:00:00")).mean()),
              "after_1600": float((clock >= "16:00:00").mean())}
    events.assign(ticker=[market.ticker[s] for s in events["series"]]).drop(columns=["series", "p", "r"]) \
        .to_csv(out / "events.csv.gz", index=False)

    # ---- discovery: decisions up to 2021-12-31, every candidate
    early = events[events["decision_date"] <= DISCOVERY_END]
    discovery, selected, avoid = {}, [], []
    for item in ITEMS:
        for signal in SIGNALS:
            name = f"{item}:{signal}"
            part = candidate_rows(early, item, signal)
            trades = trades_for(market, part, HOLD)
            stats = describe(market, trades, T_SELECT)
            stats["signals"] = int(len(part))
            n, exc_t = stats.get("trades", 0), stats.get("excess_t")
            stats["selected"] = bool(n >= MIN_TRADES and stats["net_bps"] > 0 and stats["excess_bps"] > 0
                                     and exc_t is not None and exc_t >= T_SELECT)
            stats["avoid_candidate"] = bool(n >= MIN_TRADES and stats["excess_bps"] < 0
                                            and exc_t is not None and exc_t <= -T_SELECT)
            discovery[name] = stats
            if stats["selected"]:
                selected.append(name)
            if stats["avoid_candidate"]:
                avoid.append(name)
            if n:
                print(f"{name:10s} trades {n:6d} net {stats['net_bps']:+7.1f} bps  vs SPY {stats['excess_bps']:+7.1f} "
                      f"(t {L.fmt(exc_t, 2)}){'  SELECTED' if stats['selected'] else ''}"
                      f"{'  AVOID' if stats['avoid_candidate'] else ''}", flush=True)
    (out / "discovery.json").write_text(json.dumps({"counts": counts, "accepted_clock": timing, "candidates": discovery,
                                                     "selected": selected, "avoid": avoid}, indent=1, default=str))
    print(f"selected: {selected or 'none'}; avoid-list candidates: {avoid or 'none'}", flush=True)

    # ---- held-out: only what discovery selected, once
    late = events[events["decision_date"] > DISCOVERY_END]
    held = {}
    for name in selected + avoid:
        item, signal = name.split(":")
        part = candidate_rows(late, item, signal)
        trades = trades_for(market, part, HOLD)
        stats = describe(market, trades, T_HELD_OUT)
        stats["signals"] = int(len(part))
        if name in selected:
            stats["passes_held_out"] = bool(stats.get("passes"))
            stats["side_holds"] = {}
            for hold in SIDE_HOLDS:
                side = L.summarize(trades_for(market, part, hold), T_HELD_OUT)
                stats["side_holds"][str(hold)] = {k: side.get(k) for k in ("trades", "net_bps", "net_t", "excess_bps",
                                                                         "excess_t")}
            lenient = L.summarize(trades_for(market, part, HOLD, L.LENIENT_COST), T_HELD_OUT)
            stats["lenient_cost"] = {k: lenient.get(k) for k in ("net_bps", "net_t", "excess_bps", "excess_t", "passes")}
        else:
            exc_t = stats.get("excess_t")
            stats["avoid_confirmed"] = bool(stats.get("trades") and stats["excess_bps"] < 0
                                            and exc_t is not None and exc_t <= -T_HELD_OUT)
        held[name] = stats
        if len(trades):
            trades.drop(columns=["series", "decision", "entry", "exit"]).to_csv(
                out / f"trades_heldout_{name.replace(':', '_')}.csv.gz", index=False)
        print(f"held-out {name}: {json.dumps({k: stats.get(k) for k in ('trades', 'net_bps', 'net_t', 'excess_bps', 'excess_t', 'criteria', 'passes_held_out', 'avoid_confirmed')}, default=str)}",
              flush=True)
    passed = [name for name in selected if held[name].get("passes_held_out")]
    results = {"data_end": market.calendar[-1], "counts": counts, "accepted_clock": timing,
               "candidates_tested": len(ITEMS) * len(SIGNALS), "selected": selected, "avoid": avoid, "held_out": held,
               "passed": passed, "verdict": "forward paper test" if passed else "closed"}
    (out / "results.json").write_text(json.dumps(results, indent=1, default=str))
    print(json.dumps({"selected": selected, "avoid": avoid, "passed": passed, "verdict": results["verdict"]}))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
