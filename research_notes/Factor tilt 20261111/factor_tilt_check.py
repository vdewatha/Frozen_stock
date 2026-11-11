"""Factor-tilt check, 2026-11-11: would a low-turnover fundamental tilt, held as a real factor ETF in place
of VOO in the slow book's passive arm, have beaten the arm after costs?

Written for ``reports/factor-tilt-20261111.md``. It is the one family the edge-feasibility report left
untested ("low-turnover fundamental tilts ... deferred, not closed"). Factor ETFs are the only $0,
survivorship-free way to test it: a fund's history includes every stock it held, delisted or not, and its
adjusted price is already net of the fund's expense ratio.

PRE-REGISTERED (frozen before any price was fetched; do not edit after the run):

* Candidates, each replacing the whole equity leg:
    QUAL  iShares MSCI USA Quality Factor        (quality / profitability)
    SPHQ  Invesco S&P 500 Quality                 (quality, longest history)
    VLUE  iShares MSCI USA Value Factor           (value)
    RPV   Invesco S&P 500 Pure Value              (value, longest history)
    MTUM  iShares MSCI USA Momentum Factor        (momentum, a low-turnover price tilt)
    USMV  iShares MSCI USA Min Vol Factor         (low volatility)
    SPLV  Invesco S&P 500 Low Volatility          (low volatility)
    BLEND equal weights of QUAL, VLUE, MTUM, USMV, rebalanced monthly (multi-factor, netted)
* Benchmark leg: SPY (stands in for VOO, which starts in 2010; they track within a few bps).
* Arm: 20% equity leg + 80% BIL (T-bills), rebalanced to target at each month's last close, as the live
  passive arm. Cost is charged on one-way turnover at 11 and 25 bps a side, on both arms.
* Windows: W1 = from the first month-start after max(candidate inception, BIL start) to the data end;
  W2 = from max(W1 start, 2015-01-01). The data end is the last close fetched.
* Rule: a candidate replaces VOO only if ALL hold:
    (a) the arm's Sharpe (daily excess over BIL, annualized) is higher with the candidate than with SPY,
        at both costs and in both windows;
    (b) the arm's maximum drawdown is no more than 2 points worse, at both costs and in both windows;
    (c) the equity leg's monthly excess return over SPY is significant over W1: one-sided p < 0.05 after a
        Holm correction across the 8 candidates (t from Newey-West standard errors, 3 lags).
  Otherwise VOO stays.

Run (no access to the deployed app):  python -I factor_tilt_check.py <out_dir>
Writes <out_dir>/px.csv (cache, reused when present) and <out_dir>/results.json; prints the tables.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SINGLE = ["QUAL", "SPHQ", "VLUE", "RPV", "MTUM", "USMV", "SPLV"]
BLEND = ["QUAL", "VLUE", "MTUM", "USMV"]
CANDIDATES = SINGLE + ["BLEND"]
TICKERS = sorted(set(SINGLE + ["SPY", "BIL"]))
EQUITY_WEIGHT = 0.20
COSTS_BPS = (11.0, 25.0)
W2_START = pd.Timestamp("2015-01-01")
DD_TOLERANCE = 0.02
NW_LAGS = 3
ALPHA = 0.05


def load_prices(out: Path) -> pd.DataFrame:
    cache = out / "px.csv"
    if cache.exists():
        return pd.read_csv(cache, index_col=0, parse_dates=True)
    import yfinance as yf

    raw = yf.download(TICKERS, start="2005-01-01", auto_adjust=True, progress=False, threads=False)
    px = raw["Close"][TICKERS].dropna(how="all")
    px.index = pd.to_datetime(px.index).tz_localize(None)
    px.to_csv(cache)
    return px


def month_ends(index: pd.DatetimeIndex) -> set:
    s = pd.Series(index, index=index)
    return set(s.groupby([index.year, index.month]).last())


def arm_returns(ret: pd.DataFrame, legs: dict[str, float], cost_bps: float) -> pd.Series:
    """Daily returns of a monthly-rebalanced mix; weights drift between month-end rebalances."""
    cols = list(legs) + ["BIL"]
    target = np.array([legs[c] for c in legs] + [1.0 - sum(legs.values())])
    R = ret[cols].values
    rebalance = month_ends(ret.index)
    c = cost_bps / 1e4
    w = target.copy()
    out = np.zeros(len(ret))
    for j, day in enumerate(ret.index):
        gross = float((w * R[j]).sum())
        w = w * (1 + R[j])
        w = w / w.sum()
        cost = 0.0
        if day in rebalance:
            cost = float(np.abs(target - w).sum()) * c
            w = target.copy()
        out[j] = (1 + gross) * (1 - cost) - 1
    return pd.Series(out, index=ret.index)


def sharpe(r: pd.Series, rf: pd.Series) -> float:
    ex = r - rf
    return float(ex.mean() / ex.std() * math.sqrt(252))


def max_dd(r: pd.Series) -> float:
    eq = (1 + r).cumprod()
    return float((eq / eq.cummax() - 1).min())


def cagr(r: pd.Series) -> float:
    return float((1 + r).prod() ** (252 / len(r)) - 1)


def nw_t(x: np.ndarray, lags: int = NW_LAGS) -> float:
    x = np.asarray(x, float)
    n = len(x)
    d = x - x.mean()
    var = (d @ d) / n
    for k in range(1, lags + 1):
        cov = (d[k:] @ d[:-k]) / n
        var += 2 * (1 - k / (lags + 1)) * cov
    return float(x.mean() / math.sqrt(var / n))


def one_sided_p(t: float) -> float:
    return 0.5 * math.erfc(t / math.sqrt(2))


def holm(pvals: dict[str, float]) -> dict[str, float]:
    order = sorted(pvals, key=pvals.get)
    m = len(order)
    adjusted, running = {}, 0.0
    for i, key in enumerate(order):
        running = max(running, min(1.0, (m - i) * pvals[key]))
        adjusted[key] = running
    return adjusted


def main(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    px = load_prices(out)
    ret_all = px.pct_change()
    blend_start = max(px[t].first_valid_index() for t in BLEND)
    data_end = px[["SPY", "BIL"] + SINGLE].dropna().index[-1]
    results: dict = {"data_end": str(data_end.date()), "candidates": {}}
    leg_p: dict[str, float] = {}

    for cand in CANDIDATES:
        inception = blend_start if cand == "BLEND" else px[cand].first_valid_index()
        first = max(inception, px["BIL"].first_valid_index()) + pd.Timedelta(days=1)
        w1_start = (first + pd.offsets.MonthBegin(0)).normalize()
        windows = {"W1": w1_start, "W2": max(w1_start, W2_START)}
        cols = (BLEND if cand == "BLEND" else [cand]) + ["SPY", "BIL"]
        ret = ret_all.loc[w1_start:data_end, sorted(set(cols))].fillna(0.0)
        legs = {t: EQUITY_WEIGHT / len(BLEND) for t in BLEND} if cand == "BLEND" else {cand: EQUITY_WEIGHT}
        base_legs = {"SPY": EQUITY_WEIGHT}
        row: dict = {"inception": str(inception.date()), "windows": {}}
        passes = True
        for cost in COSTS_BPS:
            tilt = arm_returns(ret, legs, cost)
            base = arm_returns(ret, base_legs, cost)
            for name, start in windows.items():
                t, b, rf = tilt.loc[start:], base.loc[start:], ret["BIL"].loc[start:]
                cell = {
                    "start": str(start.date()), "years": round(len(t) / 252, 1),
                    "arm_sharpe": round(sharpe(b, rf), 3), "tilt_sharpe": round(sharpe(t, rf), 3),
                    "arm_cagr": round(cagr(b), 4), "tilt_cagr": round(cagr(t), 4),
                    "arm_dd": round(max_dd(b), 4), "tilt_dd": round(max_dd(t), 4),
                }
                cell["sharpe_ok"] = cell["tilt_sharpe"] > cell["arm_sharpe"]
                cell["dd_ok"] = cell["tilt_dd"] >= cell["arm_dd"] - DD_TOLERANCE
                passes = passes and cell["sharpe_ok"] and cell["dd_ok"]
                row["windows"][f"{name}@{int(cost)}"] = cell
        # Equity leg alone against SPY, monthly, gross of rebalancing cost (the blend's is tiny).
        leg_daily = sum(ret[t] * (1 / len(BLEND)) for t in BLEND) if cand == "BLEND" else ret[cand]
        monthly = lambda s: (1 + s).groupby([s.index.year, s.index.month]).prod() - 1  # noqa: E731
        excess = (monthly(leg_daily) - monthly(ret["SPY"])).values
        legstats = {}
        for name, start in windows.items():
            ld, sd = leg_daily.loc[start:], ret["SPY"].loc[start:]
            ex = (monthly(ld) - monthly(sd)).values
            legstats[name] = {
                "leg_cagr": round(cagr(ld), 4), "spy_cagr": round(cagr(sd), 4),
                "excess_per_year": round(float(ex.mean() * 12), 4),
                "tracking_error": round(float(ex.std(ddof=1) * math.sqrt(12)), 4),
                "info_ratio": round(float(ex.mean() / ex.std(ddof=1) * math.sqrt(12)), 3),
                "nw_t": round(nw_t(ex), 2), "months": int(len(ex)),
                "leg_dd": round(max_dd(ld), 4), "spy_dd": round(max_dd(sd), 4),
            }
        row["leg"] = legstats
        leg_p[cand] = one_sided_p(nw_t(excess))
        row["arm_rule_pass"] = passes
        results["candidates"][cand] = row

    adjusted = holm(leg_p)
    for cand, row in results["candidates"].items():
        row["leg_p_one_sided"] = round(leg_p[cand], 4)
        row["leg_p_holm"] = round(adjusted[cand], 4)
        row["significance_ok"] = adjusted[cand] < ALPHA
        row["adopt"] = row["arm_rule_pass"] and row["significance_ok"]
    (out / "results.json").write_text(json.dumps(results, indent=1))
    return results


def print_tables(results: dict) -> None:
    print(f"data end {results['data_end']}")
    print("\nEquity leg vs SPY (monthly excess)")
    print(f"{'cand':6} {'win':3} {'start':10} {'leg CAGR':>8} {'SPY':>7} {'excess/yr':>9} {'TE':>6} {'IR':>6} "
          f"{'t':>6} {'leg DD':>7} {'SPY DD':>7}")
    for cand, row in results["candidates"].items():
        for name, s in row["leg"].items():
            start = row["windows"][f"{name}@11"]["start"]
            print(f"{cand:6} {name:3} {start:10} {s['leg_cagr']:8.2%} {s['spy_cagr']:7.2%} {s['excess_per_year']:9.2%} "
                  f"{s['tracking_error']:6.2%} {s['info_ratio']:6.2f} {s['nw_t']:6.2f} {s['leg_dd']:7.1%} "
                  f"{s['spy_dd']:7.1%}")
    print("\n20/80 arm, candidate in place of SPY")
    print(f"{'cand':6} {'cell':7} {'arm Sh':>7} {'tilt Sh':>7} {'arm DD':>7} {'tilt DD':>7} ok")
    for cand, row in results["candidates"].items():
        for key, c in row["windows"].items():
            ok = "pass" if c["sharpe_ok"] and c["dd_ok"] else "FAIL"
            print(f"{cand:6} {key:7} {c['arm_sharpe']:7.3f} {c['tilt_sharpe']:7.3f} {c['arm_dd']:7.1%} "
                  f"{c['tilt_dd']:7.1%} {ok}")
    print("\nVerdict")
    for cand, row in results["candidates"].items():
        print(f"{cand:6} arm rule {'pass' if row['arm_rule_pass'] else 'FAIL'}; leg p={row['leg_p_one_sided']:.3f} "
              f"Holm={row['leg_p_holm']:.3f} -> {'ADOPT' if row['adopt'] else 'keep VOO'}")


if __name__ == "__main__":
    print_tables(main(Path(sys.argv[1])))
