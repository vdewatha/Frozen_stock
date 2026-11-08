"""Step 5 of ``reports/Retail bot edge feasibility.md``: close the pairs question with one kill test.

The pairs note's recommended lane is stock-versus-sector-ETF residual
reversal with the beta hedged out.  Its deciding question is whether the
long-only sector-relative reversal (``reports/sector-reversal-20261107.md``,
PR #42) failed because of beta noise or because no residual edge exists.  This
script hedges that run's trades with their sector ETF and reads the residual.

Pre-registered rule (the report's row, applied as written)
-----------------------------------------------------------
* Trades: the 18,469 executable trades of the default rule
  (``strategies/sector_reversal.DEFAULT_RULE``: close > SMA200 and
  ``rel_z_5 < -2``; exit when ``rel_rsi_2 > 70`` or close > SMA5, 10
  sessions, 10% stop), regenerated with PR #42's own code on the same cache
  (point-in-time S&P 500, a stock enterable only while a member, single
  stocks with a sector benchmark, published cost model).
* Per trade, the **residual gross** = the stock's gross return over the hold
  (next open to exit fill) minus beta x the sector ETF's return over the same
  hold.  The ETF is the one assigned to the stock on the decision bar
  (``sector_relative``: today's static map, else the causal statistical
  assignment), held for the whole trade; its leg runs from the entry
  session's open to the exit session's open (an intraday stop exit uses the
  ETF's close that session).  Beta is the OLS slope of the stock's daily
  returns on the assigned sector index's daily returns over the 60 sessions
  ending on the decision bar (causal; at least 40 valid days, else the trade
  has no residual and is counted as missing).
* Reported by VIX bucket (absolute level of the decision bar's close: < 15,
  15-20, 20-25, >= 25, the pairs note's crisis bucket; and the PR #42 split,
  above or below the trailing 252-session median) and by the pairs note's
  crisis sub-periods: 2015-16 (calendar years), 2018 Q4, 2020, 2022, 2025
  (calendar years), plus the rest of 2015+.
* **Proceed to the pairs note's Stage A only if** the mean 2015+ residual
  gross per trade is at least 60 bps (three times four fills at 5 bps each)
  **and** its 95% interval, clustered by entry date (CR0), is above zero.
* If it fails, one variant **without the uptrend filter** (entry
  ``rel_z_5 < -2`` alone, same exits) goes through the same rule: if it also
  fails, the market-neutral lane closes for good; if it passes, only the
  uptrend version closes and Stage A is next (not built here).

Stated limits: every default-rule trade passed a close > SMA200 filter;
entries are long only (the residual is the long leg's, the short ETF leg is
the hedge, not a second signal); 661 of the 691 stocks have a statistically
assigned sector ETF (only today's 30 use the static map).

Data ($0): the shared yfinance cache (``--data-dir``), the membership CSV under
``<data-dir>/universe``, a ``date,close`` VIX csv.  Cache only.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_pairs_kill_test.py --data-dir <dir> --vix-csv <csv> --output <json> \\
        [--tables <md>]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS, settings  # noqa: E402

START = "2015-01-01"
THRESHOLD_BPS = 60.0           # three times four fills at 5 bps each
BETA_WINDOW = 60
BETA_MIN_OBSERVATIONS = 40
Z_95 = 1.959963984540054
VIX_BUCKETS = ((None, 15.0, "<15"), (15.0, 20.0, "15-20"), (20.0, 25.0, "20-25"), (25.0, None, ">=25"))
CRISIS_PERIODS = {"2015-16": ("2015-01-01", "2017-01-01"), "2018 Q4": ("2018-10-01", "2019-01-01"),
                  "2020": ("2020-01-01", "2021-01-01"), "2022": ("2022-01-01", "2023-01-01"),
                  "2025": ("2025-01-01", "2026-01-01")}
EXPECTED_DEFAULT_TRADES = 18_469
RULE = ("Proceed to Stage A only if the mean 2015+ residual gross per trade (stock minus 60-day beta x sector ETF "
        "over the same hold) is >= 60 bps and its entry-date clustered 95% interval is above zero; on a fail, one "
        "variant without the uptrend filter goes through the same rule: both fail -> the market-neutral lane closes "
        "for good; the variant passes -> only the uptrend version closes and Stage A is next.")


# ----------------------------------------------------------------- decisions (pure, tested)

def clustered_mean(values: np.ndarray, clusters) -> dict:
    """Mean (bps), CR0 standard error clustered on ``clusters`` and the 95% interval."""
    values = np.asarray(values, dtype=float)
    clusters = np.asarray(list(clusters), dtype=object)
    keep = np.isfinite(values)
    values, clusters = values[keep], clusters[keep]
    n = len(values)
    if n < 2:
        return {"trades": n, "mean_bps": None, "se_bps": None, "ci_bps": None, "t_stat": None, "clusters": 0}
    mean = float(values.mean())
    sums = pd.Series(values - mean).groupby(clusters).sum().to_numpy(float)
    se = math.sqrt(float((sums ** 2).sum())) / n
    return {"trades": int(n), "clusters": int(len(sums)), "mean_bps": round(mean * 1e4, 2),
            "se_bps": round(se * 1e4, 2),
            "ci_bps": [round((mean - Z_95 * se) * 1e4, 2), round((mean + Z_95 * se) * 1e4, 2)],
            "t_stat": round(mean / se, 3) if se > 0 else None}


def proceed(stats: Mapping) -> dict:
    mean, ci = stats.get("mean_bps"), stats.get("ci_bps")
    mean_ok = mean is not None and float(mean) >= THRESHOLD_BPS
    ci_ok = bool(ci) and float(ci[0]) > 0
    return {"mean_ok": bool(mean_ok), "ci_ok": bool(ci_ok), "passes": bool(mean_ok and ci_ok),
            "threshold_bps": THRESHOLD_BPS}


def lane_decision(default: Mapping, variant: Optional[Mapping]) -> dict:
    """The row's branch: proceed, close the uptrend version only, or close the lane."""
    if default.get("passes"):
        return {"decision": "proceed_to_stage_a", "variant_run": False,
                "reading": "the default rule's residual clears the bar; Stage A is next"}
    if variant is None:
        raise ValueError("the default rule failed: the no-uptrend variant must be run")
    if variant.get("passes"):
        return {"decision": "close_uptrend_version_stage_a_next", "variant_run": True,
                "reading": "the uptrend version is closed; the variant without the filter clears the bar, so "
                           "Stage A is next (not built here)"}
    return {"decision": "close_market_neutral_lane", "variant_run": True,
            "reading": "both fail: the market-neutral lane closes for good"}


# ----------------------------------------------------------------- residuals

def rolling_beta(stock: np.ndarray, sector: np.ndarray, window: int = BETA_WINDOW,
                 min_observations: int = BETA_MIN_OBSERVATIONS) -> np.ndarray:
    """Per bar t: OLS slope of stock on sector daily returns over bars t-window+1..t (NaN pairs skipped)."""
    x = pd.Series(sector, dtype=float)
    y = pd.Series(stock, dtype=float)
    valid = x.notna() & y.notna()
    x, y = x.where(valid), y.where(valid)
    count = valid.astype(float).rolling(window, min_periods=1).sum()
    mx = x.rolling(window, min_periods=min_observations).mean()
    my = y.rolling(window, min_periods=min_observations).mean()
    mxy = (x * y).rolling(window, min_periods=min_observations).mean()
    mxx = (x * x).rolling(window, min_periods=min_observations).mean()
    var = mxx - mx * mx
    beta = (mxy - mx * my) / var.where(var > 0)
    return beta.where(count >= min_observations).to_numpy(float)


def assignments(frames: Mapping[str, pd.DataFrame], method: Mapping[str, str]) -> dict:
    """Per single stock, the sector ETF assigned on each bar (``sector_relative``'s own maps)."""
    from app.services import sector_relative as sr

    closes = sr.etf_closes(frames)
    spy = sr._close_series(frames["SPY"])
    out = {}
    for symbol, how in method.items():
        if how == "none":
            continue
        frame = frames[symbol]
        dates = sr._dates(frame)
        if how == "static":
            out[symbol] = sr.static_assignment(symbol, dates, closes).reindex(dates).to_numpy(dtype=object)
        else:
            out[symbol] = sr.assign_sectors(sr._close_series(frame), closes, spy).reindex(dates).to_numpy(dtype=object)
    return out


def hedge(trades: pd.DataFrame, frames: Mapping[str, pd.DataFrame], assigned: Mapping[str, np.ndarray]) -> pd.DataFrame:
    """``trades`` with ``etf``, ``beta``, ``etf_return`` and ``residual`` (gross) per trade."""
    from app.services.sector_relative import SECTOR_CLOSE_COLUMN

    rows = trades.reset_index(drop=True).copy()
    n = len(rows)
    etf_name = np.full(n, None, dtype=object)
    beta = np.full(n, np.nan)
    etf_ret = np.full(n, np.nan)
    etf_frames = {}
    for etf in {e for a in assigned.values() for e in a if e}:
        frame = frames.get(etf)
        if frame is not None:
            idx = pd.DatetimeIndex(pd.to_datetime(frame["date"].astype(str).str[:10]))
            etf_frames[etf] = (pd.Series(pd.to_numeric(frame["open"], errors="coerce").to_numpy(float), index=idx),
                               pd.Series(pd.to_numeric(frame["close"], errors="coerce").to_numpy(float), index=idx))
    for symbol, group in rows.groupby("symbol"):
        frame = frames[symbol]
        names = assigned.get(symbol)
        if names is None:
            continue
        close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(float)
        sector = pd.to_numeric(frame[SECTOR_CLOSE_COLUMN], errors="coerce").to_numpy(float)
        stock_ret = np.r_[np.nan, close[1:] / close[:-1] - 1]
        sector_ret = np.r_[np.nan, sector[1:] / sector[:-1] - 1]
        betas = rolling_beta(stock_ret, sector_ret)
        dates = pd.DatetimeIndex(pd.to_datetime(frame["date"].astype(str).str[:10]))
        for k in group.index:
            t, e, x = int(rows.at[k, "event_idx"]), int(rows.at[k, "entry_idx"]), int(rows.at[k, "exit_idx"])
            etf = names[t]
            if not etf or etf not in etf_frames:
                continue
            opens, closes = etf_frames[etf]
            entry_px = opens.get(dates[e], np.nan)
            intraday = str(rows.at[k, "barrier"]) in ("stop_loss", "take_profit")
            exit_px = (closes if intraday else opens).get(dates[x], np.nan)
            etf_name[k] = etf
            beta[k] = betas[t]
            etf_ret[k] = exit_px / entry_px - 1 if entry_px and np.isfinite(entry_px) else np.nan
    rows["etf"], rows["beta"], rows["etf_return"] = etf_name, beta, etf_ret
    rows["residual"] = rows["gross_return"] - rows["beta"] * rows["etf_return"]
    return rows


# ----------------------------------------------------------------- tables

def vix_levels(vix: Optional[pd.DataFrame], dates) -> np.ndarray:
    if vix is None or vix.empty:
        return np.full(len(dates), np.nan)
    close = pd.Series(pd.to_numeric(vix["close"], errors="coerce").to_numpy(float),
                      index=pd.DatetimeIndex(pd.to_datetime(vix["date"].astype(str).str[:10]))).sort_index()
    close = close[~close.index.duplicated(keep="last")]
    index = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates).astype(str).str[:10]))
    return close.reindex(index, method="ffill").to_numpy(float)


def bucket_of(level: float) -> Optional[str]:
    if not np.isfinite(level):
        return None
    for low, high, name in VIX_BUCKETS:
        if (low is None or level >= low) and (high is None or level < high):
            return name
    return None


def summary(rows: pd.DataFrame, mask: np.ndarray) -> dict:
    sub = rows.loc[mask]
    resid = sub["residual"].to_numpy(float)
    stats = clustered_mean(resid, sub["entry_date"].astype(str).str[:10])
    gross = sub["gross_return"].to_numpy(float)
    return stats | {
        "all_trades": int(mask.sum()),
        "missing_residual": int((~np.isfinite(resid)).sum()),
        "gross_bps": round(float(np.nanmean(gross)) * 1e4, 2) if len(gross) else None,
        "etf_leg_bps": round(float(np.nanmean(sub["etf_return"])) * 1e4, 2) if len(sub) else None,
        "mean_beta": round(float(np.nanmean(sub["beta"])), 3) if len(sub) else None,
        "net_after_4_fills_5bps": None if stats["mean_bps"] is None else round(stats["mean_bps"] - 20.0, 2),
        "win_rate_residual": round(float(np.nanmean(resid > 0)), 4) if np.isfinite(resid).any() else None}


def breakdown(rows: pd.DataFrame, vix: Optional[pd.DataFrame], states: pd.Series) -> dict:
    dates = pd.to_datetime(rows["decision_date"].astype(str).str[:10])
    since = (dates >= pd.Timestamp(START)).to_numpy()
    levels = vix_levels(vix, rows["decision_date"])
    buckets = np.array([bucket_of(v) for v in levels], dtype=object)
    median_state = _state_of(rows["decision_date"], states)
    out = {"all": summary(rows, np.ones(len(rows), dtype=bool)), "2015+": summary(rows, since),
           "vix_bucket": {}, "vix_vs_median": {}, "crisis": {}}
    for _, _, name in VIX_BUCKETS:
        out["vix_bucket"][name] = {"all": summary(rows, buckets == name), "2015+": summary(rows, since & (buckets == name))}
    for state in ("high", "low"):
        out["vix_vs_median"][state] = {"all": summary(rows, median_state == state),
                                       "2015+": summary(rows, since & (median_state == state))}
    in_crisis = np.zeros(len(rows), dtype=bool)
    for name, (start, end) in CRISIS_PERIODS.items():
        mask = ((dates >= pd.Timestamp(start)) & (dates < pd.Timestamp(end))).to_numpy()
        in_crisis |= mask
        out["crisis"][name] = summary(rows, mask)
    out["crisis"]["rest of 2015+"] = summary(rows, since & ~in_crisis)
    return out


def _state_of(dates, states: pd.Series) -> np.ndarray:
    if states is None or states.empty:
        return np.full(len(dates), None, dtype=object)
    index = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates).astype(str).str[:10]))
    return states.reindex(index, method="ffill").to_numpy(dtype=object)


# ----------------------------------------------------------------- run

def _sector_script():
    path = Path(__file__).resolve().parent / "run_sector_reversal_backtest.py"
    spec = importlib.util.spec_from_file_location("run_sector_reversal_backtest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def no_uptrend(rule: Mapping) -> dict:
    """The variant: the default rule's entry without its close > SMA200 condition, same exits."""
    return {"entry": [c for c in rule["entry"] if c.get("f") != "dist_sma_200"], "exit": list(rule["exit"])}


def run(args) -> dict:
    settings.meta_label_history_dir = str(args.data_dir)
    from app.services import point_in_time_universe as pit
    from app.services import strategy_discovery as discovery
    from app.services.earnings_history import load_surprises
    from app.services.meta_label_learning import history_dir
    from app.services.sector_relative import attach_sector_closes
    from app.services.strategy_scorecard import load_daily_history
    from app.services.trading_universe import ETF_SYMBOLS

    sector_script = _sector_script()
    warnings.simplefilter("ignore", RuntimeWarning)
    started = time.time()
    now = datetime.now(timezone.utc)
    membership = pit.load_membership(download=False, now=now)

    def loader(ticker: str) -> pd.DataFrame:
        return load_daily_history(pit.yahoo_symbol(ticker), history_dir(), download=False)

    coverage = pit.coverage(membership, loader=loader, now=now, start=args.start, cache_path=pit.coverage_path())
    base = sorted({s.upper() for s in DEFAULT_PAPER_EXECUTION_SYMBOLS})
    symbols = pit.replay_symbols(base, coverage)
    frames, missing = discovery.load_frames(
        symbols, loader=loader, start=args.start, end=args.end, sector_close=False,
        earnings_loader=lambda s: load_surprises(s, history_dir(), download=False), news_loader=lambda s: None)
    frames, method = attach_sector_closes(frames)
    singles = sorted(s for s in frames if s not in ETF_SYMBOLS and method.get(s, "none") != "none")
    assigned = assignments(frames, {s: method[s] for s in singles})
    load_seconds = time.time() - started
    vix = pd.read_csv(args.vix_csv) if args.vix_csv and Path(args.vix_csv).exists() else None
    states = sector_script.vix_states(vix)

    name, rule, plan = discovery.sector_reversal_variants()[0]
    caches: dict = {}
    out: dict = {}
    for label, entry_rule in (("default", rule), ("no_uptrend", no_uptrend(rule))):
        trades = sector_script.variant_trades(entry_rule, plan, frames, singles, membership.mask, caches, None)
        hedged = hedge(trades, frames, assigned)
        if args.trades_dir:
            Path(args.trades_dir).mkdir(parents=True, exist_ok=True)
            hedged.to_csv(Path(args.trades_dir) / f"pairs_{label}_trades.csv", index=False)
        tables = breakdown(hedged, vix, states)
        out[label] = {"rule": entry_rule, "plan": plan, "trades": int(len(hedged)), "tables": tables,
                      "decision": proceed(tables["2015+"]),
                      "etf_counts": {k: int(v) for k, v in pd.Series(hedged["etf"]).value_counts().items()}}
        if label == "default" and out[label]["decision"]["passes"]:
            break   # the row runs the variant only on a fail
    lane = lane_decision(out["default"]["decision"], out.get("no_uptrend", {}).get("decision"))
    by_method = {how: sum(1 for s in singles if method.get(s) == how) for how in ("static", "statistical")}
    end = max(pd.Timestamp(str(f["date"].iloc[-1])[:10]) for f in frames.values())
    return {"ran_at": now.isoformat(), "paper_only": True, "rule": RULE, "start": args.start, "end": str(end.date()),
            "default_variant": name, "expected_default_trades": EXPECTED_DEFAULT_TRADES,
            "reproduces_pr42": out["default"]["trades"] == EXPECTED_DEFAULT_TRADES,
            "universe": {"symbols": len(frames), "single_stocks_with_sector": len(singles), "sector_method": by_method,
                         "missing_history": len(missing), "coverage_since_2010": coverage.get("coverage_since_2010")},
            "runs": out, "lane": lane,
            "timings": {"load_seconds": round(load_seconds, 1), "total_seconds": round(time.time() - started, 1)}}


def _b(value):
    return "n/a" if value is None else f"{float(value):+.1f}"


def _row(label: str, s: Mapping) -> str:
    ci = s.get("ci_bps")
    return (f"| {label} | {s['all_trades']} | {s['trades']} | {_b(s['gross_bps'])} | {_b(s['etf_leg_bps'])} | "
            f"{s['mean_beta'] if s['mean_beta'] is not None else 'n/a'} | {_b(s['mean_bps'])}"
            + (f" [{ci[0]:+.1f}, {ci[1]:+.1f}]" if ci else "") + f" | {s['t_stat']} | {_b(s['net_after_4_fills_5bps'])} |")


def render_tables(result: dict) -> str:
    head = ("| Slice | Trades | With residual | Stock gross | ETF leg | Mean beta | Residual gross [95% CI, "
            "entry-date clustered] | t | Residual - 4 x 5 bps |")
    rule = "|---|---|---|---|---|---|---|---|---|"
    lines = []
    for label, run_ in result["runs"].items():
        t = run_["tables"]
        d = run_["decision"]
        lines += [f"### {label} ({run_['trades']} trades)", "",
                  f"Rule on 2015+: mean {_b(t['2015+']['mean_bps'])} bps (need >= {d['threshold_bps']:.0f}: "
                  f"{'pass' if d['mean_ok'] else 'fail'}), interval {t['2015+']['ci_bps']} "
                  f"({'above zero' if d['ci_ok'] else 'not above zero'}) -> **{'pass' if d['passes'] else 'fail'}**.",
                  "", head, rule, _row("all history", t["all"]), _row("2015+", t["2015+"])]
        for name, cell in t["vix_bucket"].items():
            lines.append(_row(f"VIX {name}, all", cell["all"]))
            lines.append(_row(f"VIX {name}, 2015+", cell["2015+"]))
        for name, cell in t["vix_vs_median"].items():
            lines.append(_row(f"VIX {name} vs 252d median, 2015+", cell["2015+"]))
        for name, cell in t["crisis"].items():
            lines.append(_row(name, cell))
        lines.append("")
    lines += [f"Lane: **{result['lane']['decision']}** ({result['lane']['reading']})."]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--vix-csv", type=Path, default=None)
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=None, help="exclusive; default today")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tables", type=Path, default=None)
    parser.add_argument("--trades-dir", type=Path, default=None, help="write each run's hedged trades as CSV")
    args = parser.parse_args()
    result = run(args)
    args.output.write_text(json.dumps(result, indent=2, default=str))
    if args.tables:
        args.tables.write_text(render_tables(result))
    print(json.dumps({"reproduces_pr42": result["reproduces_pr42"], "lane": result["lane"],
                      "decisions": {k: v["decision"] for k, v in result["runs"].items()},
                      "trades": {k: v["trades"] for k, v in result["runs"].items()},
                      "timings": result["timings"]}, indent=2, default=str))


if __name__ == "__main__":
    main()
