"""Quantify feed and benchmark hygiene (ranked experiment 6) for the report.

Two measurements, both read-only:

1. **Gapped stops in the replays.**  Every paper strategy is replayed exactly
   as the experiment engine replays it (``gate_replay.simulate`` with the
   published cost model; ``triple_barrier.label_events`` fills a stop the open
   gapped through at ``min(stop, open)``).  For each ``stop_loss_gap`` trade
   the counterfactual "filled at the stop" return is computed, so the report
   can say how many trades the gap rule changes and by how much.
2. **IEX vs SIP on recent sessions** (``--live-feeds``, needs the Alpaca
   market-data credentials).  For the paper universe over the last
   ``--sessions`` completed sessions: the IEX vs official SIP open, the IEX vs
   SIP session high/low, and how often hypothetical stop / target levels set
   off the prior consolidated close (``--levels``, e.g. 1%, 2%, 3%) were touched
   by one feed and not the other (``feed_hygiene.compare_level``).  The paper
   fills themselves, if the database has any, are benchmarked by the
   after-close job; this script uses bars only.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/feed_hygiene_report.py --data-dir <csv dir> [--download] \\
        [--live-feeds --sessions 20] --output <json>
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS  # noqa: E402
from app.services import feed_hygiene, gate_replay  # noqa: E402
from app.services import meta_label_primary as primary  # noqa: E402
from app.services.exchange_sessions import session_bounds  # noqa: E402
from app.services.gate_cost import sessions_back  # noqa: E402
from app.services.strategy_scorecard import load_daily_history, normalize_prices  # noqa: E402

UTC = timezone.utc


def gapped_stop_impact(trades: pd.DataFrame, frame_by_symbol: dict) -> dict:
    """How many replayed trades exit on a gapped stop, and what filling them at the stop would have claimed."""
    if trades is None or trades.empty:
        return {"trades": 0, "stop_exits": 0, "gapped_stops": 0}
    stops = trades[trades["barrier"].isin(["stop_loss", "stop_loss_gap"])]
    gapped = trades[trades["barrier"] == "stop_loss_gap"]
    deltas, gaps = [], []
    for row in gapped.itertuples(index=False):
        opens = pd.to_numeric(frame_by_symbol[row.symbol]["open"], errors="coerce").to_numpy(float)
        open_ = opens[int(row.exit_idx)]
        stop_level = row.entry_price * (1 - row.stop_loss_pct)
        fill = min(stop_level, open_)
        # Costs and fees are multiplicative, so the counterfactual is exact.
        at_stop = (row.net_return + 1) * stop_level / fill - 1
        deltas.append((at_stop - row.net_return) * 10_000)
        gaps.append((stop_level - open_) / stop_level * 10_000)
    deltas_arr = np.asarray(deltas, dtype=float)
    total_net = float(trades["net_return"].sum())
    return {
        "trades": int(len(trades)), "stop_exits": int(len(stops)), "gapped_stops": int(len(gapped)),
        "gapped_share_of_stops": round(len(gapped) / len(stops), 4) if len(stops) else None,
        "gap_below_stop_bps_mean": round(float(np.mean(gaps)), 2) if gaps else None,
        "gap_below_stop_bps_median": round(float(np.median(gaps)), 2) if gaps else None,
        "gap_below_stop_bps_p90": round(float(np.percentile(gaps, 90)), 2) if gaps else None,
        "overstatement_per_gapped_trade_bps": round(float(deltas_arr.mean()), 2) if len(deltas_arr) else None,
        "overstatement_per_trade_bps": round(float(deltas_arr.sum() / len(trades)), 2),
        "expectancy_bps": round(total_net / len(trades) * 10_000, 2),
        "expectancy_if_filled_at_stop_bps": round(float((total_net + deltas_arr.sum() / 10_000) / len(trades) * 10_000), 2),
    }


def replay_impact(frames: dict) -> dict:
    spy = frames.get("SPY")
    replays = {symbol: primary.replay_signals(frame) for symbol, frame in frames.items()}
    out = {}
    all_trades = []
    for strategy in gate_replay.STRATEGIES:
        trades = gate_replay.simulate(strategy, frames, spy, {}, replays, calibration=None)
        out[strategy] = gapped_stop_impact(trades, frames)
        if not trades.empty:
            all_trades.append(trades)
        print(strategy, out[strategy], flush=True)
    out["all"] = gapped_stop_impact(pd.concat(all_trades, ignore_index=True) if all_trades else None, frames)
    return out


def live_feed_comparison(symbols: list[str], sessions: int, levels: list[float], frames: dict) -> dict:
    from app.integrations.alpaca_market_data import AlpacaBarsClient

    client = AlpacaBarsClient(max_requests=400)
    if not client.configured:
        return {"status": "unavailable", "reason": "no Alpaca market-data credentials"}
    now = datetime.now(UTC)
    last = feed_hygiene.last_sip_ready_session(now)
    days = []
    cursor = last
    while len(days) < sessions:
        if session_bounds(cursor) is not None:
            days.append(cursor)
        cursor = sessions_back(cursor, 1)
    opens, signed_opens, decision_gaps, highs, lows, ranges = [], [], [], [], [], []
    comparisons = []
    missing_iex_open = 0
    for day in sorted(days):
        bounds = session_bounds(day)
        start, end = bounds[0].astimezone(UTC), bounds[1].astimezone(UTC)
        iex = client.bars(symbols, start, end, feed="iex")
        sip = client.bars(symbols, start, end, feed="sip")
        for symbol in symbols:
            sip_open = feed_hygiene.official_open(sip.get(symbol, []), day)
            iex_open = feed_hygiene.official_open(iex.get(symbol, []), day)
            sip_x = feed_hygiene.session_extremes(sip.get(symbol, []), day)
            iex_x = feed_hygiene.session_extremes(iex.get(symbol, []), day)
            if sip_open and iex_open and iex_open["at_session_open"]:
                gap = (iex_open["price"] - sip_open["price"]) / sip_open["price"] * 10_000
                opens.append(abs(gap))
                signed_opens.append(gap)
            # The decision price proxy: the 09:39 minute close (the daily-bar entry window opens at 09:35).
            moment = start + timedelta(minutes=9, seconds=30)
            iex_minute = feed_hygiene.containing_bar(iex.get(symbol, []), moment)
            sip_minute = feed_hygiene.containing_bar(sip.get(symbol, []), moment)
            if iex_minute and sip_minute:
                decision_gaps.append((iex_minute["close"] - sip_minute["close"]) / sip_minute["close"] * 10_000)
            elif sip_open:
                missing_iex_open += 1
            if sip_x and iex_x:
                highs.append((sip_x["high"] - iex_x["high"]) / sip_x["high"] * 10_000)
                lows.append((iex_x["low"] - sip_x["low"]) / sip_x["low"] * 10_000)
                ranges.append(iex_x["bars"] / max(sip_x["bars"], 1))
            frame = frames.get(symbol)
            if frame is None:
                continue
            prior = frame[pd.to_datetime(frame["date"]).dt.date < day]
            if prior.empty:
                continue
            reference = float(prior["close"].iloc[-1])
            for pct in levels:
                for kind, price in (("stop", reference * (1 - pct)), ("target", reference * (1 + pct))):
                    comparisons.append(feed_hygiene.compare_level(
                        {"symbol": symbol, "kind": kind, "price": price, "source": f"prior_close_{pct:.0%}",
                         "pct": pct}, iex.get(symbol, []), sip.get(symbol, []), day))
        print(day, "bars fetched", flush=True)

    def stats(values):
        if not values:
            return None
        arr = np.asarray(values, dtype=float)
        return {"n": int(len(arr)), "mean": round(float(arr.mean()), 2), "median": round(float(np.median(arr)), 2),
                "p90": round(float(np.percentile(arr, 90)), 2), "max": round(float(arr.max()), 2)}

    by_level = {}
    for pct in levels:
        for kind in ("stop", "target"):
            group = [row for row in comparisons if row["pct"] == pct and row["kind"] == kind]
            counts = feed_hygiene.divergence_counts(group)
            touched_sip = counts["both"] + counts["sip_only"]
            by_level[f"{kind}_{pct:.0%}"] = {**counts, "sip_touches": touched_sip,
                                             "divergent_share_of_sip_touches":
                                                 round(counts["divergent"] / touched_sip, 4) if touched_sip else None}
    days_with_divergence = len({row["session"] for row in comparisons if row["divergent"]})
    return {"status": "ok", "sessions": [day.isoformat() for day in sorted(days)], "symbols": len(symbols),
            "iex_vs_sip_open_abs_bps": stats(opens), "iex_minus_sip_open_bps": stats(signed_opens),
            "iex_minus_sip_0939_close_bps": stats(decision_gaps),
            "iex_vs_sip_0939_close_abs_bps": stats([abs(v) for v in decision_gaps]),
            "sessions_without_iex_0930_bar": missing_iex_open,
            "sip_high_above_iex_high_bps": stats(highs), "iex_low_above_sip_low_bps": stats(lows),
            "iex_bars_per_sip_bar": stats(ranges), "levels": by_level,
            "sessions_with_a_divergence": days_with_divergence, "requests": client.requests,
            "examples": [row for row in comparisons if row["divergent"]][:12]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--live-feeds", action="store_true")
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--levels", default="0.01,0.02,0.03")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    warnings.filterwarnings("ignore")
    symbols = list(DEFAULT_PAPER_EXECUTION_SYMBOLS)
    frames = {s: normalize_prices(load_daily_history(s, args.data_dir, download=args.download),
                                  start=args.start, end=args.end) for s in symbols}
    frames = {s: f.reset_index(drop=True) for s, f in frames.items() if len(f) > 260}
    report = {"generated_at": datetime.now(UTC).isoformat(), "symbols": len(frames),
              "data_end": max(str(f["date"].iloc[-1]) for f in frames.values()), "start": args.start,
              "gapped_stops": replay_impact(frames)}
    if args.live_feeds:
        levels = [float(item) for item in args.levels.split(",") if item.strip()]
        report["live_feeds"] = live_feed_comparison(sorted(frames), args.sessions, levels, frames)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print("wrote", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
