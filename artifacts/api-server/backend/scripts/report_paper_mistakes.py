"""Rank paper-trading mistakes by dollar cost from read-only paper evidence.

Sources (any combination; all are read-only):

* ``--from-alpaca``: GET-only pull of the Alpaca *paper* account (account,
  orders, activities, daily portfolio history) and SPY daily bars, using
  ``PAPER_ALPACA_API_KEY``/``PAPER_ALPACA_API_SECRET``.  Live keys are never
  read.  No order is placed, modified or cancelled.
* ``--snapshot FILE``: a JSON file with the same shape the Alpaca pull saves
  (``--save-snapshot``), so a report can be re-run offline.
* ``--legacy-sqlite FILE``: closed rows of the retired ``paper_trades``
  simulator table (opened with ``mode=ro``).
* ``--comparison-sqlite FILE``: isolated crypto paper-comparison databases;
  the latest step is summarised per strategy (fees versus net change).

Fills are matched FIFO per symbol into round trips.  Strategy attribution uses
``--strategy-map`` (client_order_id -> strategy JSON) when given, otherwise
the client_order_id conventions: ``sp-`` stock-paper ledger, ``qa-`` broker
round-trip probes, ``lb-`` live broker (must never appear on paper).

Outputs Markdown (default) or JSON (``--json``).  Every number is computed from
the supplied evidence; nothing here authorises trading.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PAPER_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
OPEN_WINDOW_MINUTES = 15
CHURN_MINUTES = 30
DEFAULT_STOP_PCT = 0.03  # legacy simulator STOP_LOSS_PCT


# ----------------------------------------------------------------- loading

def _env(path: str | None) -> dict:
    values = dict(os.environ)
    if path:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return values


def _get(base: str, path: str, headers: dict, params: dict | None = None):
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def pull_alpaca_paper(env_file: str | None, benchmark: str = "SPY") -> dict:
    """GET-only snapshot of the paper account.  Uses PAPER_* keys only."""
    env = _env(env_file)
    key, secret = env.get("PAPER_ALPACA_API_KEY"), env.get("PAPER_ALPACA_API_SECRET")
    if not key or not secret:
        raise SystemExit("PAPER_ALPACA_API_KEY/PAPER_ALPACA_API_SECRET are required")
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    snapshot = {"account": _get(PAPER_BASE, "/v2/account", headers),
                "positions": _get(PAPER_BASE, "/v2/positions", headers)}
    orders, until = [], None
    while True:
        params = {"status": "all", "limit": 500, "direction": "desc", "nested": "true"}
        if until:
            params["until"] = until
        page = _get(PAPER_BASE, "/v2/orders", headers, params)
        orders += page
        if len(page) < 500:
            break
        until = page[-1]["submitted_at"]
    activities, token = [], None
    while True:
        params = {"page_size": 100, "direction": "desc"}
        if token:
            params["page_token"] = token
        page = _get(PAPER_BASE, "/v2/account/activities", headers, params)
        activities += page
        if len(page) < 100:
            break
        token = page[-1]["id"]
    snapshot["orders"], snapshot["activities"] = orders, activities
    snapshot["history"] = _get(PAPER_BASE, "/v2/account/portfolio/history", headers,
                               {"period": "1A", "timeframe": "1D"})
    start = (snapshot["account"].get("created_at") or "")[:10] or None
    if start:
        bars = _get(DATA_BASE, f"/v2/stocks/{benchmark}/bars", headers,
                    {"timeframe": "1Day", "start": start, "feed": "iex", "limit": 1000,
                     "adjustment": "all"})
        snapshot["benchmark"] = {"symbol": benchmark, "bars": bars.get("bars") or []}
    return snapshot


def load_legacy_trades(path: str) -> list[dict]:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        strategies = {row["id"]: row["strategy_type"] for row in connection.execute(
            "SELECT id, strategy_type FROM strategies")}
        rows = connection.execute(
            "SELECT * FROM paper_trades WHERE status='closed' ORDER BY entry_time").fetchall()
    trips = []
    for row in rows:
        quantity = float(row["quantity"] or 0)
        entry, exit_ = float(row["entry_price"] or 0), float(row["exit_price"] or 0)
        features = json.loads(row["features_at_entry"] or "{}")
        decision = (features.get("market_regime_features") or {}).get("latest_close")
        trips.append({
            "source": "legacy_simulator", "symbol": row["symbol"],
            "strategy": strategies.get(row["strategy_id"], f"strategy-{row['strategy_id']}"),
            "quantity": quantity, "entry_price": entry, "exit_price": exit_,
            "entry_time": _ts(row["entry_time"]), "exit_time": _ts(row["exit_time"]),
            "pnl": float(row["profit_loss"] or 0), "fees": 0.0,
            "decision_price": float(decision) if decision else None,
            "exit_reason": row["reason_exited"] or "",
        })
    return trips


def load_comparison(path: str) -> dict:
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        last = connection.execute(
            "SELECT report FROM comparison_steps ORDER BY bar_close DESC LIMIT 1").fetchone()
    report = json.loads(last[0]) if last else {"accounts": []}
    rows = {}
    for account in report.get("accounts", []):
        state = account["state"]
        rows[account["strategy"]] = {
            "net_change": float(account["equity"]) - 10000.0,
            "fees": float(state["fees_paid"]), "fills": int(state["fills"]),
        }
    return {"path": str(path), "bar_close": report.get("bar_close"), "strategies": rows}


# ---------------------------------------------------------------- analysis

def _ts(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).replace("Z", "+00:00").replace(" ", "T")
        parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def strategy_for(client_order_id: str | None, mapping: dict | None = None) -> str:
    cid = client_order_id or ""
    if mapping and cid in mapping:
        return str(mapping[cid])
    if cid.startswith("sp-"):
        return "stock_paper_ledger_unattributed"
    if cid.startswith("qa-"):
        return "qa_roundtrip_probe"
    if cid.startswith("lb-"):
        return "LIVE_BROKER_ID_ON_PAPER"
    return "manual_or_external"


def round_trips(orders: list[dict], activities: list[dict], mapping: dict | None = None) -> list[dict]:
    """FIFO-match FILL activities per symbol into long round trips."""
    by_order = {row["id"]: row for row in orders}
    fills = sorted((row for row in activities if row.get("activity_type") == "FILL"),
                   key=lambda row: row["transaction_time"])
    fees_by_day: dict[str, float] = defaultdict(float)
    for row in activities:
        if row.get("activity_type") in {"FEE", "CFEE"} and row.get("net_amount") is not None:
            fees_by_day[str(row.get("date"))[:10]] += -float(row["net_amount"])
    notional_by_day: dict[str, float] = defaultdict(float)
    for row in fills:
        notional_by_day[row["transaction_time"][:10]] += float(row["qty"]) * float(row["price"])
    lots: dict[str, deque] = defaultdict(deque)
    trips = []

    def fee_share(row, quantity):
        day = row["transaction_time"][:10]
        total = notional_by_day[day]
        return fees_by_day.get(day, 0.0) * (quantity * float(row["price"]) / total) if total else 0.0

    for row in fills:
        order = by_order.get(row.get("order_id"), {})
        quantity, price = float(row["qty"]), float(row["price"])
        decision = order.get("limit_price")
        if row["side"] == "buy":
            lots[row["symbol"]].append({
                "qty": quantity, "price": price, "time": _ts(row["transaction_time"]),
                "strategy": strategy_for(order.get("client_order_id"), mapping),
                "decision": float(decision) if decision else None,
                "fee_per_share": fee_share(row, quantity) / quantity if quantity else 0.0,
                "stacked": bool(lots[row["symbol"]]),
            })
            continue
        remaining = quantity
        sell_fee = fee_share(row, quantity) / quantity if quantity else 0.0
        while remaining > 1e-12 and lots[row["symbol"]]:
            lot = lots[row["symbol"]][0]
            take = min(lot["qty"], remaining)
            trips.append({
                "source": "alpaca_paper", "symbol": row["symbol"], "strategy": lot["strategy"],
                "quantity": take, "entry_price": lot["price"], "exit_price": price,
                "entry_time": lot["time"], "exit_time": _ts(row["transaction_time"]),
                "pnl": (price - lot["price"]) * take,
                "fees": (lot["fee_per_share"] + sell_fee) * take,
                "decision_price": lot["decision"], "exit_reason": order.get("client_order_id") or "",
                "stacked": lot["stacked"],
            })
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 1e-12:
                lots[row["symbol"]].popleft()
    return trips


def _bucket(trips: list[dict], key: str) -> dict:
    out: dict[str, dict] = {}
    for trip in trips:
        row = out.setdefault(trip[key], {"trips": 0, "gross_pnl": 0.0, "fees": 0.0, "net_pnl": 0.0, "wins": 0})
        net = trip["pnl"] - trip["fees"]
        row["trips"] += 1
        row["gross_pnl"] += trip["pnl"]
        row["fees"] += trip["fees"]
        row["net_pnl"] += net
        row["wins"] += net > 0
    return out


def summarize(trips: list[dict]) -> dict:
    nets = [trip["pnl"] - trip["fees"] for trip in trips]
    wins, losses = [n for n in nets if n > 0], [n for n in nets if n <= 0]
    holds = [(t["exit_time"] - t["entry_time"]).total_seconds() / 3600
             for t in trips if t["entry_time"] and t["exit_time"]]
    slippage = [((t["entry_price"] - t["decision_price"]) / t["decision_price"]) * 1e4
                for t in trips if t.get("decision_price")]
    return {
        "round_trips": len(trips),
        "gross_pnl": sum(t["pnl"] for t in trips),
        "fees": sum(t["fees"] for t in trips),
        "net_pnl": sum(nets),
        "win_rate": len(wins) / len(nets) if nets else None,
        "payoff_ratio": (statistics.mean(wins) / abs(statistics.mean(losses)))
        if wins and losses and statistics.mean(losses) else None,
        "median_hold_hours": statistics.median(holds) if holds else None,
        "entry_slippage_bps_vs_decision": statistics.mean(slippage) if slippage else None,
        "by_symbol": _bucket(trips, "symbol"),
        "by_strategy": _bucket(trips, "strategy"),
    }


def _near_open(moment: datetime | None) -> bool:
    if not moment:
        return False
    local = moment.astimezone(ET)
    opening = datetime.combine(local.date(), time(9, 30), ET)
    return opening <= local < opening + timedelta(minutes=OPEN_WINDOW_MINUTES)


def rank_mistakes(trips: list[dict], *, stop_pct: float = DEFAULT_STOP_PCT) -> list[dict]:
    """Return mistake categories sorted by dollar cost (largest first).

    Categories overlap (one losing lot can be both stacked and oversized);
    each row states its own cost basis and is not additive with the others.
    """
    found: dict[str, dict] = {}

    def add(name, cost, trip, basis):
        row = found.setdefault(name, {"mistake": name, "cost": 0.0, "count": 0, "basis": basis, "examples": []})
        row["cost"] += cost
        row["count"] += 1
        if len(row["examples"]) < 3:
            row["examples"].append(f"{trip['symbol']} {trip['strategy']} {trip['entry_time']:%Y-%m-%d %H:%M} net {trip['pnl'] - trip['fees']:+.2f}")

    entries: dict[tuple, list] = defaultdict(list)
    for trip in trips:
        net = trip["pnl"] - trip["fees"]
        notional = trip["entry_price"] * trip["quantity"]
        if trip["fees"] > 0:
            add("fee_drag", trip["fees"], trip, "reported regulatory/account fees allocated by notional")
        loss_pct = -trip["pnl"] / notional if notional else 0
        if loss_pct > stop_pct * 1.5:
            add("oversized_loser_beyond_stop", (loss_pct - stop_pct) * notional, trip,
                f"loss beyond the {stop_pct:.0%} stop budget (late / gapped exit)")
        if trip["entry_time"] and trip["exit_time"] and \
                trip["exit_time"] - trip["entry_time"] < timedelta(minutes=CHURN_MINUTES) and net <= 0:
            add("churn_round_trip", -net, trip, f"net loss of round trips held < {CHURN_MINUTES} min")
        if trip.get("stacked") and net < 0:
            add("stacked_entry_while_long", -net, trip, "net loss on lots added while the symbol was already held")
        if _near_open(trip["entry_time"]) and net < 0:
            add("entry_in_opening_minutes", -net, trip, f"net loss of entries in first {OPEN_WINDOW_MINUTES} min")
        if trip.get("decision_price") and trip["entry_price"] > 0:
            gap = abs(trip["entry_price"] - trip["decision_price"]) / trip["decision_price"]
            if gap > 0.02:
                add("fill_far_from_decision_price", max(0.0, -net), trip,
                    "net loss where entry price differs >2% from the price the decision saw")
        entries[(trip["source"], trip["symbol"])].append(trip)
    # Same symbol held concurrently by different strategies.
    for group in entries.values():
        for later in group:
            overlap = [t for t in group if t is not later and t["strategy"] != later["strategy"]
                       and t["entry_time"] and later["entry_time"] and t["entry_time"] < later["entry_time"]
                       and (t["exit_time"] is None or t["exit_time"] > later["entry_time"])]
            net = later["pnl"] - later["fees"]
            if overlap and net < 0 and not later.get("stacked"):
                add("stacked_entry_while_long", -net, later,
                    "net loss on lots added while another strategy held the symbol")
    by_strategy = _bucket(trips, "strategy")
    for name, row in by_strategy.items():
        if row["net_pnl"] < 0 and row["trips"] >= 2:
            found[f"losing_strategy:{name}"] = {
                "mistake": f"losing_strategy:{name}", "cost": -row["net_pnl"], "count": row["trips"],
                "basis": "strategy net P&L across all its round trips", "examples": []}
    return sorted(found.values(), key=lambda row: row["cost"], reverse=True)


def benchmark_comparison(history: dict | None, bars: list[dict] | None) -> dict | None:
    if not history or not bars:
        return None
    points = [(datetime.fromtimestamp(ts, timezone.utc).date(), equity)
              for ts, equity in zip(history.get("timestamp", []), history.get("equity", [])) if equity]
    if not points:
        return None
    start_day, start_equity = points[0]
    end_day, end_equity = points[-1]
    closes = {datetime.fromisoformat(b["t"].replace("Z", "+00:00")).date(): float(b["c"]) for b in bars}
    days = sorted(day for day in closes if start_day <= day <= end_day + timedelta(days=3))
    if len(days) < 2:
        return None
    spy_return = closes[days[-1]] / closes[days[0]] - 1
    account_return = end_equity / start_equity - 1
    return {
        "start": start_day.isoformat(), "end": days[-1].isoformat(),
        "account_return": account_return, "benchmark_return": spy_return,
        "account_change": end_equity - start_equity,
        "benchmark_change_on_same_capital": start_equity * spy_return,
        "opportunity_cost": start_equity * spy_return - (end_equity - start_equity),
    }


# ------------------------------------------------------------------ report

def build_report(snapshot: dict | None, legacy: list[dict], comparisons: list[dict],
                 mapping: dict | None = None, stop_pct: float = DEFAULT_STOP_PCT) -> dict:
    report: dict = {"generated_at": datetime.now(timezone.utc).isoformat(), "paper_only": True}
    if snapshot:
        trips = round_trips(snapshot.get("orders", []), snapshot.get("activities", []), mapping)
        orders = snapshot.get("orders", [])
        prefixes = defaultdict(int)
        for order in orders:
            prefixes[strategy_for(order.get("client_order_id"), mapping)] += 1
        account = snapshot.get("account", {})
        report["alpaca_paper"] = {
            "equity": float(account.get("equity") or 0), "cash": float(account.get("cash") or 0),
            "open_positions": len(snapshot.get("positions", [])),
            "orders": len(orders), "orders_by_origin": dict(prefixes),
            "unrealized_pnl": sum(float(p.get("unrealized_pl") or 0) for p in snapshot.get("positions", [])),
            "summary": summarize(trips),
            "mistakes": rank_mistakes(trips, stop_pct=stop_pct),
            "benchmark": benchmark_comparison(snapshot.get("history"),
                                              (snapshot.get("benchmark") or {}).get("bars")),
        }
    if legacy:
        report["legacy_simulator"] = {"summary": summarize(legacy),
                                      "mistakes": rank_mistakes(legacy, stop_pct=stop_pct)}
    if comparisons:
        report["crypto_comparison"] = comparisons
    return report


def _money(value) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _num(value, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def _jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(type(value))


def render_markdown(report: dict) -> str:
    lines = [f"# Paper mistake ranking ({report['generated_at'][:10]})", ""]
    for key, title in (("alpaca_paper", "Alpaca paper account"), ("legacy_simulator", "Legacy local simulator")):
        section = report.get(key)
        if not section:
            continue
        summary = section["summary"]
        lines += [f"## {title}", ""]
        if key == "alpaca_paper":
            lines.append(f"Equity {_money(section['equity'])}, open positions {section['open_positions']}, "
                         f"unrealized {_money(section['unrealized_pnl'])}, orders {section['orders']} "
                         f"by origin {section['orders_by_origin']}.")
            bench = section.get("benchmark")
            if bench:
                lines.append(f"Account {_pct(bench['account_return'])} vs SPY {_pct(bench['benchmark_return'])} "
                             f"({bench['start']} to {bench['end']}); SPY on the same capital "
                             f"{_money(bench['benchmark_change_on_same_capital'])}, opportunity cost "
                             f"{_money(bench['opportunity_cost'])}.")
        lines += ["", f"Round trips {summary['round_trips']}; gross {_money(summary['gross_pnl'])}; "
                  f"fees {_money(summary['fees'])}; net {_money(summary['net_pnl'])}; win rate "
                  f"{_pct(summary['win_rate'])}; payoff {_num(summary['payoff_ratio'])}; median hold "
                  f"{_num(summary['median_hold_hours'], 4)} h; entry slippage vs decision "
                  f"{_num(summary['entry_slippage_bps_vs_decision'], 1)} bps.", "",
                  "| Group | Trips | Gross | Fees | Net | Wins |", "|---|---:|---:|---:|---:|---:|"]
        for label in ("by_strategy", "by_symbol"):
            for name, row in sorted(summary[label].items()):
                lines.append(f"| {label[3:]}: {name} | {row['trips']} | {_money(row['gross_pnl'])} | "
                             f"{_money(row['fees'])} | {_money(row['net_pnl'])} | {row['wins']} |")
        lines += ["", "| Rank | Mistake | Cost | Count | Basis |", "|---:|---|---:|---:|---|"]
        for rank, row in enumerate(section["mistakes"], 1):
            lines.append(f"| {rank} | {row['mistake']} | {_money(row['cost'])} | {row['count']} | {row['basis']} |")
        lines.append("")
    for comparison in report.get("crypto_comparison", []):
        lines += [f"## Crypto comparison {comparison['path']} (to {comparison['bar_close']})", "",
                  "| Strategy | Fills | Fees | Net change | Fees share of loss |", "|---|---:|---:|---:|---:|"]
        for name, row in comparison["strategies"].items():
            share = row["fees"] / -row["net_change"] if row["net_change"] < 0 else None
            lines.append(f"| {name} | {row['fills']} | {_money(row['fees'])} | {_money(row['net_change'])} | {_pct(share)} |")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-alpaca", action="store_true", help="GET-only pull from the Alpaca paper API")
    parser.add_argument("--env-file", help="dotenv file with PAPER_ALPACA_API_KEY/SECRET")
    parser.add_argument("--snapshot", type=Path, help="previously saved snapshot JSON")
    parser.add_argument("--save-snapshot", type=Path, help="write the pulled snapshot here")
    parser.add_argument("--legacy-sqlite", type=Path)
    parser.add_argument("--comparison-sqlite", type=Path, action="append", default=[])
    parser.add_argument("--strategy-map", type=Path, help="JSON client_order_id -> strategy")
    parser.add_argument("--stop-pct", type=float, default=DEFAULT_STOP_PCT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    snapshot = None
    if args.from_alpaca:
        snapshot = pull_alpaca_paper(args.env_file)
        if args.save_snapshot:
            args.save_snapshot.write_text(json.dumps(snapshot, indent=1))
    elif args.snapshot:
        snapshot = json.loads(args.snapshot.read_text())
    mapping = json.loads(args.strategy_map.read_text()) if args.strategy_map else None
    legacy = load_legacy_trades(str(args.legacy_sqlite)) if args.legacy_sqlite else []
    comparisons = [load_comparison(str(path)) for path in args.comparison_sqlite]
    report = build_report(snapshot, legacy, comparisons, mapping, args.stop_pct)
    print(json.dumps(report, default=_jsonable, indent=1) if args.json else render_markdown(report))


if __name__ == "__main__":
    main()
