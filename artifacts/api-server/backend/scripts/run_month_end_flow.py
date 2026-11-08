"""Step 7 of the build plan: replicate the month-end rebalancing flow on SPY minus IEF (pre-registered).

``reports/Retail bot edge feasibility.md``, build-plan row 7, run exactly as
written.  Everything below was fixed before the first run.

Pre-registration
----------------
* **Data.**  yfinance adjusted daily closes (``auto_adjust=True``, so
  distributions are in the return) for SPY and IEF, 2002-2026.  IEF starts on
  2002-07-30 in the yfinance data, so the joint sample starts there.  The current, incomplete month
  is excluded (a month is used only when the first session of the next month is
  in the data).
* **Daily spread.**  ``d_t = r_SPY,t - r_IEF,t`` (simple close-to-close returns).
* **Signal.**  On each of the last three sessions ``t`` of a month, the
  month-to-date spread ``M_t = (SPY_t / SPY_m0 - 1) - (IEF_t / IEF_m0 - 1)``,
  where ``m0`` is the prior month's last close, scaled by its trailing standard
  deviation: ``z_t = M_t / (s_t * sqrt(n_t))``.  ``s_t`` is the sample standard
  deviation of ``d`` over the **63 sessions ending at t** (the trailing window,
  chosen before running: one quarter, short enough that the 2002 months enter
  the sample, and using only closes known at t), and ``n_t`` is the number of
  sessions in the month through ``t``, so ``s_t * sqrt(n_t)`` is the standard
  deviation of a month-to-date spread of that length.  A signal needs all 63
  returns; the first signals are in October 2002.
* **Outcome.**  ``y_{t+1} = d_{t+1}``, the next session's SPY-minus-IEF return,
  in basis points.  For the month's last session that is the new month's first.
* **Model.**  Pooled OLS ``y = a + b * z`` over the stacked signal days.
  Standard errors: **Newey-West HAC with 2 lags** (Bartlett kernel) on the
  stacked series in date order, so the three overlapping signals of a month may
  share errors.  Date-clustered errors would equal plain heteroskedasticity-
  robust ones here, since every observation is a different date.
* **Variants (two only).**  ``all``: every month-end.  ``quarter``: the month-
  ends of March, June, September and December.  Same signal (month to date)
  in both.
* **Windows.**  ``full``: 2002-2026 (every signal day).  ``post2015``: signal
  days from 2015-01-01.
* **Pass rule, per variant.**  ``b_full < 0`` with ``t_full <= -2``, and
  ``b_post2015 < 0`` with ``|b_post2015| >= 0.5 * |b_full|``.
* **Decision.**  If a variant passes, the slow book gets it only as an overlay
  (delay that week's VOO tranche by one to three sessions when ``z > +1``; never
  add a trade); if both pass, the ``all`` variant is used (it contains the
  quarter-ends).  If neither passes, the idea is dropped and no further
  calendar variant is tested.

Diagnostics printed but not part of the rule: plain OLS t, the number of
signal days with ``z > +1`` and their mean next-session spread.

Data is $0 (yfinance).  ``--data-dir`` caches ``SPY.csv`` / ``IEF.csv``; with
``--download`` they are refreshed, otherwise the cache is used.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_month_end_flow.py --data-dir <dir> [--download] [--output <json>]
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

TICKERS = ("SPY", "IEF")
START = "2002-01-01"
TRAILING_SESSIONS = 63
LAST_SESSIONS = 3
HAC_LAGS = 2
POST_START = "2015-01-01"
QUARTER_MONTHS = (3, 6, 9, 12)
T_MAX = -2.0
POST_SHARE = 0.5
SIGNAL_THRESHOLD = 1.0


def download(ticker: str) -> pd.DataFrame:
    import yfinance as yf

    frame = yf.download(ticker, start=START, auto_adjust=True, progress=False, threads=False)
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame = frame.reset_index()
    frame.columns = [str(column).lower() for column in frame.columns]
    return frame[["date", "close"]]


def load(ticker: str, data_dir: Path, refresh: bool) -> pd.Series:
    path = data_dir / f"{ticker}.csv"
    if refresh or not path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        download(ticker).to_csv(path, index=False)
    frame = pd.read_csv(path)
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    return frame.dropna(subset=["close"]).set_index("date")["close"].astype(float).sort_index()


def signal_frame(spy: pd.Series, ief: pd.Series, *, trailing: int = TRAILING_SESSIONS,
                 last_sessions: int = LAST_SESSIONS) -> pd.DataFrame:
    """One row per signal day (the last ``last_sessions`` sessions of each complete month): z and next-day spread."""
    prices = pd.concat({"spy": spy, "ief": ief}, axis=1, join="inner").dropna()
    returns = prices.pct_change()
    spread = returns["spy"] - returns["ief"]
    month = prices.index.to_period("M")
    month_end_close = prices.groupby(month).tail(1)
    # The prior month's last close for every row (m0), aligned by month.
    prior = month_end_close.copy()
    prior.index = month_end_close.index.to_period("M") + 1
    base = prior.reindex(month)
    base.index = prices.index
    mtd = (prices["spy"] / base["spy"] - 1.0) - (prices["ief"] / base["ief"] - 1.0)
    sessions_in_month = prices.groupby(month).cumcount() + 1
    from_end = prices.groupby(month).cumcount(ascending=False)  # 0 = last session of the month
    sd = spread.rolling(trailing, min_periods=trailing).std(ddof=1)
    z = mtd / (sd * np.sqrt(sessions_in_month))
    outcome = spread.shift(-1) * 1e4
    frame = pd.DataFrame({
        "mtd": mtd, "sessions": sessions_in_month, "from_end": from_end, "sd": sd, "z": z,
        "next_spread_bps": outcome, "month": month.month, "period": month.astype(str),
    })
    complete = set(month.unique()[:-1])  # the last month in the data may be incomplete
    frame = frame[(frame["from_end"] < last_sessions) & frame["period"].map(lambda p: pd.Period(p) in complete)]
    return frame.dropna(subset=["z", "next_spread_bps", "mtd"])


def newey_west(y: np.ndarray, x: np.ndarray, lags: int = HAC_LAGS) -> dict:
    """OLS ``y = a + b x`` with Newey-West (Bartlett) HAC standard errors."""
    n = len(y)
    design = np.column_stack([np.ones(n), x])
    xtx_inv = np.linalg.inv(design.T @ design)
    beta = xtx_inv @ design.T @ y
    resid = y - design @ beta
    scores = design * resid[:, None]
    meat = scores.T @ scores
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        gamma = scores[lag:].T @ scores[:-lag]
        meat += weight * (gamma + gamma.T)
    hac = xtx_inv @ meat @ xtx_inv
    sigma2 = float(resid @ resid) / (n - 2)
    ols = sigma2 * xtx_inv
    se, se_ols = math.sqrt(hac[1, 1]), math.sqrt(ols[1, 1])
    return {"n": n, "intercept_bps": float(beta[0]), "slope_bps_per_sd": float(beta[1]),
            "se_hac": se, "t_hac": float(beta[1] / se), "t_ols": float(beta[1] / se_ols)}


def fit(frame: pd.DataFrame) -> dict:
    out = newey_west(frame["next_spread_bps"].to_numpy(float), frame["z"].to_numpy(float))
    high = frame[frame["z"] > SIGNAL_THRESHOLD]
    out.update({
        "months": int(frame["period"].nunique()), "first": str(frame.index.min().date()),
        "last": str(frame.index.max().date()), "z_sd": float(frame["z"].std(ddof=1)),
        "z_above_1": int(len(high)),
        "z_above_1_mean_next_bps": float(high["next_spread_bps"].mean()) if len(high) else None,
    })
    return out


def verdict(full: dict, post: dict) -> dict:
    checks = {
        "full_negative": full["slope_bps_per_sd"] < 0,
        "full_t_at_most_minus_2": full["t_hac"] <= T_MAX,
        "post2015_negative": post["slope_bps_per_sd"] < 0,
        "post2015_at_least_half": abs(post["slope_bps_per_sd"]) >= POST_SHARE * abs(full["slope_bps_per_sd"]),
    }
    return {"checks": checks, "passes": all(checks.values())}


def run(spy: pd.Series, ief: pd.Series) -> dict:
    signals = signal_frame(spy, ief)
    variants = {"all": signals, "quarter": signals[signals["month"].isin(QUARTER_MONTHS)]}
    results = {}
    for name, frame in variants.items():
        full = fit(frame)
        post = fit(frame[frame.index >= pd.Timestamp(POST_START)])
        results[name] = {"full": full, "post2015": post, **verdict(full, post)}
    passing = [name for name in ("all", "quarter") if results[name]["passes"]]
    return {
        "pre_registration": {
            "tickers": list(TICKERS), "trailing_sessions": TRAILING_SESSIONS, "last_sessions": LAST_SESSIONS,
            "errors": f"Newey-West HAC, {HAC_LAGS} lags (Bartlett)", "post_start": POST_START,
            "pass_rule": "slope < 0 with t_hac <= -2 over the full sample; post-2015 slope < 0 and at least "
                         "half the full-sample size",
        },
        "data": {"first_common_date": str(pd.concat([spy, ief], axis=1, join="inner").index.min().date()),
                 "last_date": str(min(spy.index.max(), ief.index.max()).date())},
        "variants": results,
        "outcome": "pass" if passing else "fail",
        "adopted_variant": passing[0] if passing else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    spy, ief = (load(ticker, args.data_dir, args.download) for ticker in TICKERS)
    result = run(spy, ief)
    for name, variant in result["variants"].items():
        for window in ("full", "post2015"):
            r = variant[window]
            print(f"{name:8s} {window:9s} n={r['n']:4d} months={r['months']:3d} {r['first']}..{r['last']} "
                  f"slope={r['slope_bps_per_sd']:+.2f} bps/SD t_hac={r['t_hac']:+.2f} t_ols={r['t_ols']:+.2f} "
                  f"z>1: {r['z_above_1']} mean next {r['z_above_1_mean_next_bps'] if r['z_above_1_mean_next_bps'] is None else round(r['z_above_1_mean_next_bps'], 1)} bps")
        print(f"{name:8s} checks {variant['checks']} -> {'PASS' if variant['passes'] else 'FAIL'}")
    print(f"outcome: {result['outcome']} (variant {result['adopted_variant']})")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
