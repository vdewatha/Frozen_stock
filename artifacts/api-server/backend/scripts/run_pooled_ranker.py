"""One pre-registered offline run of a pooled cross-sectional ranker (step 6 of the feasibility plan).

Offline research only: never imported by the app, places no order, writes no
database row.  It answers one question from ``reports/Retail bot edge
feasibility.md`` (build-plan row "6. Run the ranker once" and the section "A
pooled ranker earns one run, after four design flaws are fixed"): does a
pooled price-and-volume model rank point-in-time S&P 500 members well enough,
after costs, to earn a zero-capital shadow?

Everything below was fixed before the first run.  The script is run once; its
numbers are reported as they come out, and nothing is tuned on them.

Design (pre-registered)
-----------------------
* **Universe.**  Point-in-time S&P 500 single stocks (``point_in_time_universe``,
  fja05680 membership file).  A stock is eligible on a weekly scoring date
  when it is a member that day, has a cached yfinance close that day and has
  at least 252 cached bars by then.  Members yfinance no longer serves are
  excluded, so every number is an **upper bound** (the missing names are
  mostly the acquired and bankrupt ones).  Coverage per date = members with a
  close / members.
* **Windows** (by scoring date): 2018-2026 primary (yearly coverage >= 80%);
  2015-2017 secondary (72-78% coverage; can veto, cannot confirm); 2022-2026
  (90%+ coverage) reported beside the primary.  Hyperparameters are frozen on
  pre-2015 data only.
* **Scoring dates.**  The last session of each calendar week.  Features use
  data through that close.
* **Label.**  The 20-session return from the next open (open t+1 to open
  t+21), cross-sectionally ranked (percentile) per date.  The one-day lag run
  uses open t+2 to open t+22.  A stock whose history ends inside the window is
  marked at its last close (cash afterwards).
* **Features** (price and volume only, yfinance, $0), each cross-sectionally
  percentile-ranked per date: returns over 1, 5, 10, 20, 60, 120 and 252
  sessions; 12-1 momentum; realised volatility over 5, 20 and 60; max daily
  return over 20; distance from the 252-session high; volume relative to its
  20-day mean and 5-day over 60-day mean volume; log median dollar volume
  (20); Amihud illiquidity (20); 252-session beta and idiosyncratic
  volatility against SPY; last overnight gap and mean gap (20); mean
  high-low range (20); sector momentum (20, 60) and the stock's 20-session
  return over its sector's.  **No earnings-date features.**  **One sector
  method for every name**: the causal statistical assignment of
  ``sector_relative.assign_sectors`` (the SPDR whose SPY-residual returns
  tracked the stock best over the trailing year, refreshed monthly), applied
  to today's names too.
* **Models** (the four trials of one family, ``cross_sectional_ranker``):
  1. ``ranker``: pooled ``HistGradientBoostingRegressor`` (squared error on
     the ranked label), small trees, ``max_features=0.5`` so the seed matters,
     no early stopping, predictions averaged over seeds 0-4.
  2. ``ridge``: ridge regression on the same ranked features (missing = 0.5).
  3. ``momentum``: the 12-1 momentum rank.
  4. ``composite``: the mean of the ranks of 12-1 momentum, low 60-session
     volatility and closeness to the 252-session high.
* **Hyperparameters**, frozen on pre-2015 data: train on scoring dates from
  2000 whose label ends before 2010 minus the embargo, validate on 2010-2014
  dates whose label ends before 2015; pick the grid point with the highest
  mean weekly rank IC.  Grid: ranker ``max_leaf_nodes`` {7, 15} x
  ``max_iter`` {100, 300}, ``learning_rate`` 0.05, ``min_samples_leaf`` 500,
  ``l2_regularization`` 1.0; ridge ``alpha`` {0.1, 10, 1000}.
* **Training.**  Retrained every January on an expanding window from 2000.
  Purge plus embargo: a training row's label window must end more than 20
  sessions before the first session of the test year (scoring index
  <= first test index - 21 - 20 - 1).
* **Portfolio.**  Long-only top 25, enter at rank <= 25, exit when the rank
  falls past 75 or the name leaves the eligible set.  Weekly: decided on the
  scoring close, traded at the next open (lag run: the open after).  Entrants
  get 1/25 of the book; kept names keep their drifted relative weights,
  scaled to fill the rest.  Benchmark: the equal-weight eligible universe,
  re-equalised at the same opens, no cost.  Costs are charged a round trip
  (half a side) at 5, 10 and 25 bps.  Turnover is one-sided (buys) as a
  multiple of equity a year.
* **Statistics.**  Rank IC = Spearman correlation of score and the label's raw
  return per date; Newey-West t with 4 lags (the label overlaps the next
  three weekly dates; ``backtest_statistics.newey_west_t``), 8 lags shown as a
  sensitivity.  Active return per rebalance interval = book net minus
  benchmark (compounded within the interval); annualised mean x 52, IR =
  mean / sd x sqrt(52).  Deflated Sharpe of the weekly net active return at
  10 bps against the cumulative trial count (the discovery library's 422 on
  the host as of 2026-11-07, plus every hand-run research trial in
  ``yardstick.RESEARCH_TRIALS`` including these four), with the four
  models' Sharpes as the trial dispersion.  Reported, not a gate.

Pass rule (all must hold, for the ranker)
-----------------------------------------
1. Mean weekly rank IC > 0 with Newey-West t >= 3 in 2018-2026.
2. One-day execution lag: Newey-West t >= 2 in 2018-2026.
3. Mean rank IC > 0 in 2015-2017.
4. Net active return against the equal-weight universe > 0 at 25 bps, 2018-2026.
5. Information ratio >= 0.5 at 10 bps, 2018-2026.
6. Beats ridge and momentum after costs: higher net active return than each,
   at 10 and at 25 bps, 2018-2026.
7. Turnover <= 8 times a year, 2018-2026.
8. Net active return (10 bps) > 0 in at least two of 2018-2019, 2020-2022,
   2023-2026.
9. Step-3 marginal rule, 2018-2026: a 20% slice of the book (net), taken from
   the T-bill leg of the 20% SPY / 80% T-bill arm (monthly rebalanced),
   raises the arm's Sharpe (daily excess over T-bills) with no more than 2
   extra points of maximum drawdown, at 10 and at 25 bps.  The 50% SPY / 50%
   T-bill arm is shown, not decisive.

Every baseline is scored by rules 1-5 and 7-9 too.  Outcomes: all nine pass
-> document a 13-week zero-capital shadow plan (not built); a baseline passes
but the ranker does not beat it -> that baseline is the one that would ship;
only rules 1-3 pass -> the score could serve as a zero-turnover tie-breaker
(not built); otherwise a documented negative, no architecture iteration.

Data: ``--data-dir`` holds the shared yfinance cache (``<YAHOO>.csv``), the
membership file under ``universe/`` and ``IRX_close.csv``.  Cache only, no
network.

Usage (from artifacts/api-server/backend):

    PYTHONPATH=. python scripts/run_pooled_ranker.py --data-dir <dir> --output <json> [--log <file>]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# ------------------------------------------------------------ pre-registered constants
DATA_END = "2026-11-07"            # exclusive: bars through 2026-11-06
CALENDAR_START = "1998-01-01"
FIRST_TRAIN = "2000-01-01"
TUNE_SPLIT = "2010-01-01"
OOS_START_YEAR = 2015
LABEL_H = 20
EMBARGO = 20
MIN_BARS = 252
SEEDS = (0, 1, 2, 3, 4)
HGB_FIXED = {"learning_rate": 0.05, "min_samples_leaf": 500, "l2_regularization": 1.0, "max_features": 0.5,
             "early_stopping": False}
HGB_GRID = [{"max_leaf_nodes": leaves, "max_iter": iters} for leaves in (7, 15) for iters in (100, 300)]
RIDGE_GRID = (0.1, 10.0, 1000.0)
TOP_N, ENTER_RANK, EXIT_RANK = 25, 25, 75
COSTS_BPS = (5.0, 10.0, 25.0)
NW_LAGS, NW_LAGS_SENSITIVITY = 4, 8
LIBRARY_TRIALS = 422               # discovery library hashes on the host, reports/yardstick-20261107.md
MODELS = ("ranker", "ridge", "momentum", "composite")
WINDOWS = {"2015-2017": ("2015-01-01", "2018-01-01"), "2018-2026": ("2018-01-01", DATA_END),
           "2022-2026": ("2022-01-01", DATA_END)}
SUBPERIODS = {"2018-2019": ("2018-01-01", "2020-01-01"), "2020-2022": ("2020-01-01", "2023-01-01"),
              "2023-2026": ("2023-01-01", DATA_END)}
SLICE = 0.20
ARMS = {"20/80": 0.20, "50/50": 0.50}
FEATURES = ["ret_1", "ret_5", "ret_10", "ret_20", "ret_60", "ret_120", "ret_252", "mom_12_1",
            "vol_5", "vol_20", "vol_60", "max_ret_20", "dist_high_252", "vlm_rel_20", "vlm_rel_5_60",
            "log_dollar_vol_20", "amihud_20", "beta_252", "idio_vol_252", "gap_1", "gap_mean_20",
            "range_20", "sector_ret_20", "sector_ret_60", "rel_sector_20"]

LOG = {"handle": None, "t0": time.time()}


def log(message: str) -> None:
    line = f"[{time.time() - LOG['t0']:7.1f}s] {message}"
    print(line, flush=True)
    if LOG["handle"] is not None:
        LOG["handle"].write(line + "\n")
        LOG["handle"].flush()


# ------------------------------------------------------------------------- data

def read_frame(path: Path) -> pd.DataFrame:
    from app.services.strategy_scorecard import normalize_prices

    return normalize_prices(pd.read_csv(path), end=DATA_END)


def load_data(data_dir: Path) -> dict:
    from app.services.point_in_time_universe import ALWAYS_MEMBERS, load_membership, yahoo_symbol
    from app.services.sector_relative import SECTOR_ETFS

    membership = load_membership(download=False, path=data_dir / "universe" / "sp500_membership.csv")
    tickers = [t for t in membership.tickers(since=FIRST_TRAIN) if t not in ALWAYS_MEMBERS]
    spy = read_frame(data_dir / "SPY.csv")
    calendar = pd.DatetimeIndex(pd.to_datetime(spy["date"]))
    calendar = calendar[calendar >= pd.Timestamp(CALENDAR_START)]
    frames, missing = {}, []
    for ticker in tickers:
        path = data_dir / f"{yahoo_symbol(ticker)}.csv"
        if not path.exists():
            missing.append(ticker)
            continue
        frame = read_frame(path)
        if len(frame) < 60:
            missing.append(ticker)
            continue
        frames[ticker] = frame
    etfs = {etf: read_frame(data_dir / f"{etf}.csv") for etf in SECTOR_ETFS if (data_dir / f"{etf}.csv").exists()}
    irx = pd.read_csv(data_dir / "IRX_close.csv")
    log(f"membership tickers since {FIRST_TRAIN}: {len(tickers)}; with history: {len(frames)}; "
        f"without: {len(missing)}; sector ETFs: {len(etfs)}")
    return {"membership": membership, "tickers": tickers, "frames": frames, "missing": missing, "spy": spy,
            "etfs": etfs, "irx": irx, "calendar": calendar}


def wide(frames: dict, column: str, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    out = {}
    for symbol, frame in frames.items():
        series = pd.Series(pd.to_numeric(frame[column], errors="coerce").to_numpy(float),
                           index=pd.to_datetime(frame["date"]))
        out[symbol] = series[~series.index.duplicated(keep="last")].reindex(calendar)
    return pd.DataFrame(out, index=calendar)


# --------------------------------------------------------------------- features

def sector_features(frames: dict, etfs: dict, spy: pd.DataFrame, calendar: pd.DatetimeIndex) -> dict:
    """Sector momentum (20, 60) and the stock's 20-session return over its sector's, statistical map for all."""
    from app.services.sector_relative import _close_series, assign_sectors, chained_index

    closes = {etf: _close_series(frame) for etf, frame in etfs.items()}
    spy_close = _close_series(spy)
    s20, s60, rel = {}, {}, {}
    assigned = 0
    for k, (symbol, frame) in enumerate(frames.items()):
        close = _close_series(frame)
        close = close[close.index >= pd.Timestamp(CALENDAR_START) - pd.Timedelta(days=550)]
        assignment = assign_sectors(close, closes, spy_close)
        level = pd.Series(chained_index(close.index, assignment, closes), index=close.index)
        if np.isfinite(level.to_numpy()).any():
            assigned += 1
        r20 = level / level.shift(20) - 1
        s20[symbol] = r20.reindex(calendar)
        s60[symbol] = (level / level.shift(60) - 1).reindex(calendar)
        rel[symbol] = (close / close.shift(20) - 1 - r20).reindex(calendar)
        if (k + 1) % 150 == 0:
            log(f"  sector assignment {k + 1}/{len(frames)}")
    log(f"sector assignment done: {assigned}/{len(frames)} stocks with an assigned sector at some date")
    return {"sector_ret_20": pd.DataFrame(s20, index=calendar), "sector_ret_60": pd.DataFrame(s60, index=calendar),
            "rel_sector_20": pd.DataFrame(rel, index=calendar)}


def feature_panels(data: dict) -> dict:
    """Every feature as a weekly-date x symbol frame (raw, unranked), plus prices for labels and the book."""
    calendar, frames = data["calendar"], data["frames"]
    O, H, L, C, V = (wide(frames, column, calendar) for column in ("open", "high", "low", "close", "volume"))
    valid = C.notna()
    bars = valid.cumsum()
    C_ff = C.ffill()
    O_f = O.where(O.notna(), C_ff.shift(1))           # a missing open (or after delisting) = previous close
    O_f = O_f.where(C_ff.shift(1).notna() | O.notna())
    spy = data["spy"]
    spy_close = pd.Series(spy["close"].to_numpy(float), index=pd.to_datetime(spy["date"])).reindex(calendar)
    m = spy_close.pct_change()

    weekly_mask = pd.Series(calendar.isocalendar().week.to_numpy() * 10_000 + calendar.isocalendar().year.to_numpy(),
                            index=calendar)
    is_week_end = weekly_mask.ne(weekly_mask.shift(-1)).to_numpy()
    is_week_end[-1] = True
    weekly_idx = np.flatnonzero(is_week_end & (calendar >= pd.Timestamp(FIRST_TRAIN)))

    def sample(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.iloc[weekly_idx]

    r = C_ff.pct_change(fill_method=None).where(valid)
    feats: dict[str, pd.DataFrame] = {}
    for k in (1, 5, 10, 20, 60, 120, 252):
        feats[f"ret_{k}"] = sample(C_ff / C_ff.shift(k) - 1)
    feats["mom_12_1"] = sample(C_ff.shift(21) / C_ff.shift(252) - 1)
    for k in (5, 20, 60):
        feats[f"vol_{k}"] = sample(r.rolling(k, min_periods=max(3, int(0.8 * k))).std())
    feats["max_ret_20"] = sample(r.rolling(20, min_periods=16).max())
    feats["dist_high_252"] = sample(C_ff / C.rolling(252, min_periods=200).max() - 1)
    vol = V.where(V > 0)
    feats["vlm_rel_20"] = sample(vol / vol.rolling(20, min_periods=16).mean())
    feats["vlm_rel_5_60"] = sample(vol.rolling(5, min_periods=3).mean() / vol.rolling(60, min_periods=48).mean())
    dollar = C * vol
    feats["log_dollar_vol_20"] = sample(np.log(dollar.rolling(20, min_periods=16).median()))
    feats["amihud_20"] = sample((r.abs() / dollar).rolling(20, min_periods=16).mean())
    log("  return, volatility and volume features done")
    w = 252
    mp = 200
    mean_r = r.rolling(w, min_periods=mp).mean()
    mean_m = m.rolling(w, min_periods=mp).mean()
    var_m = m.rolling(w, min_periods=mp).var(ddof=0)
    cov = r.mul(m, axis=0).rolling(w, min_periods=mp).mean() - mean_r.mul(mean_m, axis=0)
    beta = cov.div(var_m, axis=0)
    var_r = r.rolling(w, min_periods=mp).var(ddof=0)
    idio = (var_r - beta.pow(2).mul(var_m, axis=0)).clip(lower=0).pow(0.5)
    feats["beta_252"], feats["idio_vol_252"] = sample(beta), sample(idio)
    del mean_r, cov, beta, var_r, idio
    gap = (O / C_ff.shift(1) - 1).where(valid)
    feats["gap_1"] = sample(gap)
    feats["gap_mean_20"] = sample(gap.rolling(20, min_periods=16).mean())
    feats["range_20"] = sample(((H - L) / C).rolling(20, min_periods=16).mean())
    log("  beta, gap and range features done")
    for name, frame in sector_features(frames, data["etfs"], spy, calendar).items():
        feats[name] = sample(frame)
    for name, frame in feats.items():
        assert list(frame.columns) == list(C.columns), name
    member = pd.DataFrame({s: data["membership"].mask(s, calendar[weekly_idx]) for s in C.columns},
                          index=calendar[weekly_idx])
    eligible = member & sample(valid) & (sample(bars) >= MIN_BARS)
    members_all = np.array([len(data["membership"].members_on(d)) for d in calendar[weekly_idx]])
    with_close = (member & sample(valid)).sum(axis=1).to_numpy()
    return {"features": feats, "eligible": eligible, "weekly_idx": weekly_idx, "calendar": calendar,
            "O_f": O_f, "C_ff": C_ff, "symbols": list(C.columns), "members": members_all,
            "with_close": with_close, "spy_close": spy_close}


def long_panel(p: dict) -> pd.DataFrame:
    """One row per (weekly date, eligible stock): ranked features, labels and raw forward returns."""
    weekly_idx, eligible = p["weekly_idx"], p["eligible"]
    O = p["O_f"].to_numpy(float)
    n_days = O.shape[0]
    rows = []
    feats = {name: frame.to_numpy(float) for name, frame in p["features"].items()}
    elig = eligible.to_numpy(bool)
    for k, i in enumerate(weekly_idx):
        cols = np.flatnonzero(elig[k])
        if cols.size == 0:
            continue
        block = {"week": np.full(cols.size, k), "day": np.full(cols.size, i), "sym": cols}
        for name in FEATURES:
            block[name] = feats[name][k, cols]

        def fwd(a: int, b: int) -> np.ndarray:
            if i + b >= n_days:
                return np.full(cols.size, np.nan)
            return O[i + b, cols] / O[i + a, cols] - 1

        block["fwd0"] = fwd(1, 1 + LABEL_H)
        block["fwd1"] = fwd(2, 2 + LABEL_H)
        block["fwd5"] = fwd(1, 6)
        rows.append(pd.DataFrame(block))
    panel = pd.concat(rows, ignore_index=True)
    grouped = panel.groupby("week", sort=False)
    for name in FEATURES:
        panel[name] = grouped[name].rank(pct=True)
    panel["y"] = grouped["fwd0"].rank(pct=True)
    panel["date"] = p["calendar"][panel["day"].to_numpy()]
    return panel


# ----------------------------------------------------------------------- models

def baseline_scores(panel: pd.DataFrame) -> dict:
    mom = panel["mom_12_1"].fillna(0.5).to_numpy()
    composite = np.nanmean(np.column_stack([panel["mom_12_1"], 1 - panel["vol_60"], panel["dist_high_252"]]), axis=1)
    return {"momentum": mom, "composite": np.nan_to_num(composite, nan=0.5)}


def ridge_matrix(panel: pd.DataFrame) -> np.ndarray:
    return panel[FEATURES].fillna(0.5).to_numpy(float) - 0.5


def fit_hgb(X, y, params: dict, seed: int):
    from sklearn.ensemble import HistGradientBoostingRegressor

    model = HistGradientBoostingRegressor(loss="squared_error", random_state=seed, **HGB_FIXED, **params)
    return model.fit(X, y)


def fit_ridge(X, y, alpha: float):
    from sklearn.linear_model import Ridge

    return Ridge(alpha=alpha).fit(X, y - 0.5)


def weekly_ic(panel: pd.DataFrame, score: np.ndarray, label: str = "fwd0") -> pd.Series:
    """Spearman rank IC per scoring date (dates with at least 30 labelled names)."""
    frame = pd.DataFrame({"week": panel["week"].to_numpy(), "date": panel["date"].to_numpy(), "s": score,
                          "f": panel[label].to_numpy()}).dropna()
    frame = frame[frame.groupby("week")["f"].transform("size") >= 30]
    g = frame.groupby("week")
    frame["rs"], frame["rf"] = g["s"].rank(), g["f"].rank()
    ic = frame.groupby("week").apply(lambda d: np.corrcoef(d["rs"], d["rf"])[0, 1], include_groups=False)
    dates = frame.groupby("week")["date"].first()
    return pd.Series(ic.to_numpy(), index=pd.DatetimeIndex(dates.loc[ic.index].to_numpy()))


def train_mask(panel: pd.DataFrame, calendar: pd.DatetimeIndex, before: str) -> np.ndarray:
    """Rows from FIRST_TRAIN whose label window ends more than EMBARGO sessions before ``before``."""
    first = int(np.searchsorted(calendar, pd.Timestamp(before)))
    day = panel["day"].to_numpy()
    return (panel["date"].to_numpy() >= np.datetime64(FIRST_TRAIN)) & (day + 1 + LABEL_H < first - EMBARGO) \
        & panel["y"].notna().to_numpy()


def tune(panel: pd.DataFrame, calendar: pd.DatetimeIndex) -> dict:
    """Pre-2015 only: train before 2010 (purged), validate 2010-2014 on labels ending before 2015."""
    train = train_mask(panel, calendar, TUNE_SPLIT)
    end_2015 = int(np.searchsorted(calendar, pd.Timestamp(f"{OOS_START_YEAR}-01-01")))
    day = panel["day"].to_numpy()
    valid = (panel["date"].to_numpy() >= np.datetime64(TUNE_SPLIT)) & (day + 1 + LABEL_H < end_2015) \
        & panel["fwd0"].notna().to_numpy()
    X = panel[FEATURES].to_numpy(float)
    y = panel["y"].to_numpy(float)
    vpanel = panel[valid].reset_index(drop=True)
    log(f"tuning: {int(train.sum())} training rows, {int(valid.sum())} validation rows "
        f"({vpanel['week'].nunique()} dates)")
    results = {"hgb": [], "ridge": []}
    for params in HGB_GRID:
        preds = np.mean([fit_hgb(X[train], y[train], params, seed).predict(X[valid]) for seed in SEEDS], axis=0)
        ic = weekly_ic(vpanel, preds)
        results["hgb"].append({"params": params, "mean_ic": float(ic.mean()), "dates": int(ic.size)})
        log(f"  hgb {params}: validation mean IC {ic.mean():+.4f}")
    XR = ridge_matrix(panel)
    for alpha in RIDGE_GRID:
        preds = fit_ridge(XR[train], y[train], alpha).predict(XR[valid])
        ic = weekly_ic(vpanel, preds)
        results["ridge"].append({"alpha": alpha, "mean_ic": float(ic.mean()), "dates": int(ic.size)})
        log(f"  ridge alpha={alpha}: validation mean IC {ic.mean():+.4f}")
    best_hgb = max(results["hgb"], key=lambda row: row["mean_ic"])["params"]
    best_alpha = max(results["ridge"], key=lambda row: row["mean_ic"])["alpha"]
    for name, scores in baseline_scores(vpanel).items():
        results[f"{name}_validation_ic"] = float(weekly_ic(vpanel, scores).mean())
    results.update({"chosen_hgb": best_hgb, "chosen_ridge_alpha": best_alpha,
                    "train_rows": int(train.sum()), "validation_rows": int(valid.sum())})
    log(f"frozen: hgb {best_hgb}, ridge alpha {best_alpha}")
    return results


def out_of_sample(panel: pd.DataFrame, calendar: pd.DatetimeIndex, tuned: dict) -> tuple[pd.DataFrame, dict]:
    """Annual expanding retrain; scores for every OOS row (seed-averaged ranker, ridge, baselines)."""
    X = panel[FEATURES].to_numpy(float)
    XR = ridge_matrix(panel)
    y = panel["y"].to_numpy(float)
    years = panel["date"].dt.year.to_numpy()
    last_year = int(years.max())
    oos = years >= OOS_START_YEAR
    ranker = np.full(len(panel), np.nan)
    per_seed = np.full((len(SEEDS), len(panel)), np.nan)
    ridge = np.full(len(panel), np.nan)
    fits = []
    for year in range(OOS_START_YEAR, last_year + 1):
        test = years == year
        train = train_mask(panel, calendar, f"{year}-01-01")
        t_fit = time.time()
        for s, seed in enumerate(SEEDS):
            per_seed[s, test] = fit_hgb(X[train], y[train], tuned["chosen_hgb"], seed).predict(X[test])
        ranker[test] = per_seed[:, test].mean(axis=0)
        ridge[test] = fit_ridge(XR[train], y[train], tuned["chosen_ridge_alpha"]).predict(XR[test])
        last_train = panel["date"][train].max()
        fits.append({"test_year": year, "train_rows": int(train.sum()), "test_rows": int(test.sum()),
                     "last_training_date": str(pd.Timestamp(last_train).date())})
        log(f"  retrain for {year}: {int(train.sum())} rows through {pd.Timestamp(last_train).date()}, "
            f"{int(test.sum())} test rows, {time.time() - t_fit:.0f}s")
    out = panel[oos].reset_index(drop=True)
    base = baseline_scores(out)
    scores = {"ranker": ranker[oos], "ridge": ridge[oos], "momentum": base["momentum"], "composite": base["composite"]}
    for name, values in scores.items():
        out[f"score_{name}"] = values
    for s in range(len(SEEDS)):
        out[f"seed_{s}"] = per_seed[s, oos]
    return out, {"fits": fits}


# -------------------------------------------------------------------- portfolio

def simulate(out: pd.DataFrame, p: dict, score_col: str | None, lag: int) -> dict:
    """Daily book (band) or equal-weight benchmark (``score_col`` None) from the first OOS trade."""
    O = p["O_f"].to_numpy(float)
    C = p["C_ff"].to_numpy(float)
    n_days, n_sym = C.shape
    targets: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for day, group in out.groupby("day", sort=True):
        trade_day = int(day) + 1 + lag
        if trade_day >= n_days:
            continue
        syms = group["sym"].to_numpy()
        if score_col is None:
            targets[trade_day] = (syms, None)
        else:
            order = np.argsort(-group[score_col].to_numpy(float), kind="stable")
            ranks = np.empty(len(order), dtype=int)
            ranks[order] = np.arange(1, len(order) + 1)
            targets[trade_day] = (syms, ranks)
    first = min(targets)
    w = np.zeros(n_sym)
    gross = np.zeros(n_days)
    traded = np.zeros(n_days)
    buys = np.zeros(n_days)
    holdings = np.zeros(n_days)
    for d in range(first, n_days):
        if d in targets:
            r_on = np.nan_to_num(O[d] / C[d - 1] - 1)
            g_on = float(w @ r_on)
            if w.any():
                w = w * (1 + r_on) / (1 + g_on)
            syms, ranks = targets[d]
            new = np.zeros(n_sym)
            if ranks is None:
                new[syms] = 1.0 / len(syms)
            else:
                rank_of = np.full(n_sym, np.iinfo(np.int64).max)
                rank_of[syms] = ranks
                held = np.flatnonzero(w > 0)
                keep = held[rank_of[held] <= EXIT_RANK]
                need = TOP_N - keep.size
                candidates = syms[np.argsort(ranks)]
                candidates = candidates[(rank_of[candidates] <= ENTER_RANK) & ~np.isin(candidates, keep)][:max(need, 0)]
                k = candidates.size
                new[candidates] = 1.0 / TOP_N
                kept_total = float(w[keep].sum())
                if keep.size:
                    new[keep] = w[keep] / kept_total * (1 - k / TOP_N) if kept_total > 0 else (1 - k / TOP_N) / keep.size
            delta = new - w
            traded[d] = float(np.abs(delta).sum())
            buys[d] = float(np.clip(delta, 0, None).sum())
            w = new
            r_day = np.nan_to_num(C[d] / O[d] - 1)
            g_day = float(w @ r_day)
            w = w * (1 + r_day) / (1 + g_day)
            gross[d] = (1 + g_on) * (1 + g_day) - 1
        else:
            r = np.nan_to_num(C[d] / C[d - 1] - 1)
            g = float(w @ r)
            w = w * (1 + r) / (1 + g)
            gross[d] = g
        holdings[d] = int((w > 1e-12).sum())
    dates = p["calendar"]
    return {"dates": dates[first:], "gross": gross[first:], "traded": traded[first:], "buys": buys[first:],
            "holdings": holdings[first:], "trade_days": np.array(sorted(targets)) - first}


def net_returns(book: dict, cost_bps: float) -> np.ndarray:
    return (1 + book["gross"]) * (1 - book["traded"] * cost_bps / 2 / 10_000) - 1


def interval_returns(daily: np.ndarray, trade_days: np.ndarray, dates: pd.DatetimeIndex) -> pd.Series:
    starts = trade_days
    ends = np.append(trade_days[1:], len(daily))
    values = [float(np.prod(1 + daily[a:b]) - 1) for a, b in zip(starts, ends)]
    return pd.Series(values, index=dates[starts])


def window(series: pd.Series, bounds: tuple[str, str]) -> pd.Series:
    return series[(series.index >= pd.Timestamp(bounds[0])) & (series.index < pd.Timestamp(bounds[1]))]


def max_drawdown(daily: np.ndarray) -> float:
    equity = np.cumprod(1 + daily)
    return float((equity / np.maximum.accumulate(equity) - 1).min()) if equity.size else 0.0


def book_stats(book: dict, bench: dict, cost_bps: float) -> dict:
    """Per window: annualised net active return, IR, t, turnover, book and benchmark CAGR, max DD."""
    from app.services.backtest_statistics import newey_west_t

    dates = book["dates"]
    assert (bench["dates"] == dates).all() and (bench["trade_days"] == book["trade_days"]).all()
    net = net_returns(book, cost_bps)
    weekly_book = interval_returns(net, book["trade_days"], dates)
    weekly_bench = interval_returns(bench["gross"], bench["trade_days"], dates)
    active = weekly_book - weekly_bench
    daily_net = pd.Series(net, index=dates)
    daily_bench = pd.Series(bench["gross"], index=dates)
    buys = pd.Series(book["buys"], index=dates)
    out = {}
    for name, bounds in {**WINDOWS, **SUBPERIODS}.items():
        a = window(active, bounds)
        dn, db = window(daily_net, bounds), window(daily_bench, bounds)
        years = len(dn) / 252
        if a.size < 10 or years <= 0:
            continue
        sd = float(a.std(ddof=1))
        out[name] = {
            "weeks": int(a.size),
            "active_ann": float(a.mean() * 52),
            "ir": float(a.mean() / sd * math.sqrt(52)) if sd > 0 else None,
            "active_t": newey_west_t(a.to_numpy(), lags=0)["t_stat"],
            "turnover_per_year": float(window(buys, bounds).sum() / years),
            "book_cagr": float(np.prod(1 + dn.to_numpy()) ** (1 / years) - 1),
            "bench_cagr": float(np.prod(1 + db.to_numpy()) ** (1 / years) - 1),
            "book_max_dd": max_drawdown(dn.to_numpy()),
            "bench_max_dd": max_drawdown(db.to_numpy()),
            "weekly_sharpe_active": float(a.mean() / sd) if sd > 0 else None,
        }
    return out


def monthly_mix(streams: list[np.ndarray], weights: list[float], dates: pd.DatetimeIndex) -> np.ndarray:
    """Monthly-rebalanced mix (weights reset after each month's last session), as in blend_check.py."""
    month = dates.to_period("M")
    month_end = np.append(month[1:] != month[:-1], True)
    R = np.column_stack(streams)
    wt = np.array(weights, float)
    out = np.zeros(len(dates))
    for j in range(len(dates)):
        g = float(wt @ R[j])
        out[j] = g
        wt = wt * (1 + R[j])
        wt = wt / wt.sum()
        if month_end[j]:
            wt = np.array(weights, float)
    return out


def marginal_rule(book: dict, cost_bps: float, p: dict, irx: pd.DataFrame, bounds: tuple[str, str]) -> dict:
    from app.services.yardstick import tbill_daily

    dates = book["dates"]
    keep = (dates >= pd.Timestamp(bounds[0])) & (dates < pd.Timestamp(bounds[1]))
    dates_w = dates[keep]
    sleeve = net_returns(book, cost_bps)[keep]
    spy = p["spy_close"].pct_change().reindex(dates_w).fillna(0.0).to_numpy(float)
    tbill, known = tbill_daily(irx, dates_w)

    def stats(daily: np.ndarray) -> dict:
        excess = daily - tbill
        sd = float(excess.std(ddof=1))
        return {"sharpe": float(excess.mean() / sd * math.sqrt(252)) if sd > 0 else None,
                "max_dd": max_drawdown(daily), "cagr": float(np.prod(1 + daily) ** (252 / len(daily)) - 1)}

    out = {"tbill_known": bool(known)}
    for arm, equity in ARMS.items():
        base = stats(monthly_mix([spy, tbill], [equity, 1 - equity], dates_w))
        with_slice = stats(monthly_mix([spy, tbill, sleeve], [equity, 1 - equity - SLICE, SLICE], dates_w))
        passes = (base["sharpe"] is not None and with_slice["sharpe"] is not None
                  and with_slice["sharpe"] > base["sharpe"] and with_slice["max_dd"] >= base["max_dd"] - 0.02)
        out[arm] = {"base": base, "with_slice": with_slice, "passes": bool(passes)}
    return out


# ------------------------------------------------------------------------ rules

def ic_stats(ic: pd.Series) -> dict:
    from app.services.backtest_statistics import newey_west_t

    out = {}
    for name, bounds in WINDOWS.items():
        series = window(ic, bounds)
        nw = newey_west_t(series.to_numpy(), NW_LAGS)
        nw8 = newey_west_t(series.to_numpy(), NW_LAGS_SENSITIVITY)
        out[name] = {"dates": int(series.size), "mean_ic": nw["mean"], "nw_t": nw["t_stat"], "nw_t_8": nw8["t_stat"],
                     "share_positive": float((series > 0).mean()) if series.size else None,
                     "sd": float(series.std(ddof=1)) if series.size > 1 else None}
    return out


def evaluate_rules(name: str, r: dict, all_results: dict) -> dict:
    ic, lag_ic = r["ic"], r["ic_lag1"]
    primary = r["book"]["10"]["2018-2026"]
    rules = {}
    rules["1_ic_t_ge_3"] = {"value": ic["2018-2026"]["nw_t"], "mean_ic": ic["2018-2026"]["mean_ic"],
                            "pass": bool(ic["2018-2026"]["mean_ic"] > 0 and (ic["2018-2026"]["nw_t"] or 0) >= 3)}
    rules["2_lag_ic_t_ge_2"] = {"value": lag_ic["2018-2026"]["nw_t"],
                                "pass": bool(lag_ic["2018-2026"]["mean_ic"] > 0 and (lag_ic["2018-2026"]["nw_t"] or 0) >= 2)}
    rules["3_ic_2015_2017_positive"] = {"value": ic["2015-2017"]["mean_ic"], "pass": bool(ic["2015-2017"]["mean_ic"] > 0)}
    a25 = r["book"]["25"]["2018-2026"]["active_ann"]
    rules["4_active_25bps_positive"] = {"value": a25, "pass": bool(a25 > 0)}
    rules["5_ir_10bps_ge_0_5"] = {"value": primary["ir"], "pass": bool((primary["ir"] or -9) >= 0.5)}
    if name == "ranker":
        diffs = {f"{other}_{c}bps": r["book"][c]["2018-2026"]["active_ann"] - all_results[other]["book"][c]["2018-2026"]["active_ann"]
                 for other in ("ridge", "momentum") for c in ("10", "25")}
        rules["6_beats_ridge_and_momentum"] = {"value": diffs, "pass": bool(all(v > 0 for v in diffs.values()))}
    rules["7_turnover_le_8"] = {"value": primary["turnover_per_year"], "pass": bool(primary["turnover_per_year"] <= 8)}
    subs = {k: r["book"]["10"][k]["active_ann"] for k in SUBPERIODS}
    rules["8_two_of_three_subperiods"] = {"value": subs, "pass": bool(sum(v > 0 for v in subs.values()) >= 2)}
    m10, m25 = r["marginal"]["10"]["20/80"], r["marginal"]["25"]["20/80"]
    rules["9_marginal_rule_20_80"] = {"value": {"10bps": m10, "25bps": m25}, "pass": bool(m10["passes"] and m25["passes"])}
    return rules


def dsr(results: dict) -> dict:
    from app.services.backtest_statistics import deflated_sharpe_ratio, return_moments
    from app.services.yardstick import research_family_counts

    research = sum(research_family_counts().values())
    trials = LIBRARY_TRIALS + research
    sharpes = [results[m]["weekly_active_10"]["sharpe_per_period"] for m in MODELS]
    out = {"trials": trials, "library_trials": LIBRARY_TRIALS, "research_trials": research,
           "note": "experiment-engine candidates on the host are not counted offline; they can only lower these"}
    for m in MODELS:
        mom = results[m]["weekly_active_10"]
        out[m] = deflated_sharpe_ratio(mom["sharpe_per_period"], mom["observations"], trials=trials,
                                       trial_sharpes=sharpes, skewness=mom["skewness"], kurtosis=mom["kurtosis"])
        out[m]["family_only"] = deflated_sharpe_ratio(mom["sharpe_per_period"], mom["observations"], trials=4,
                                                      trial_sharpes=sharpes, skewness=mom["skewness"],
                                                      kurtosis=mom["kurtosis"])["deflated_sharpe_ratio"]
    return out


def verdict(results: dict) -> dict:
    ranker_rules = results["ranker"]["rules"]
    ranker_pass = all(rule["pass"] for rule in ranker_rules.values())
    baselines_passing = [m for m in ("ridge", "momentum", "composite")
                         if all(rule["pass"] for rule in results[m]["rules"].values())]
    ic_only = all(ranker_rules[k]["pass"] for k in ("1_ic_t_ge_3", "2_lag_ic_t_ge_2", "3_ic_2015_2017_positive"))
    def beaten(baseline: str) -> bool:
        return all(results["ranker"]["book"][c]["2018-2026"]["active_ann"]
                   > results[baseline]["book"][c]["2018-2026"]["active_ann"] for c in ("10", "25"))

    unbeaten = [m for m in baselines_passing if not beaten(m)]
    if ranker_pass and not unbeaten:
        outcome = "pass"
    elif baselines_passing:
        outcome = "baseline_passes"
    elif ic_only:
        outcome = "ic_only"
    else:
        outcome = "fail"
    return {"outcome": outcome, "ranker_passes": ranker_pass, "baselines_passing": baselines_passing,
            "ranker_ic_rules_pass": ic_only, "passing_baselines_not_beaten": unbeaten,
            "failed_ranker_rules": [k for k, rule in ranker_rules.items() if not rule["pass"]]}


# ------------------------------------------------------------------------- main

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--smoke", type=int, default=0,
                        help="debug only: first N tickers, one seed, one grid point (never the reported run)")
    args = parser.parse_args(argv)
    if args.smoke:
        global SEEDS, HGB_GRID, RIDGE_GRID
        SEEDS, HGB_GRID, RIDGE_GRID = (0,), HGB_GRID[:1], RIDGE_GRID[:1]
    if args.log:
        LOG["handle"] = args.log.open("w")
    from app.services.backtest_statistics import return_moments

    data = load_data(args.data_dir)
    if args.smoke:
        data["frames"] = dict(list(data["frames"].items())[:args.smoke])
    p = feature_panels(data)
    log(f"features: {len(FEATURES)} on {len(p['weekly_idx'])} weekly dates x {len(p['symbols'])} symbols")
    panel = long_panel(p)
    log(f"panel: {len(panel)} rows, {panel['week'].nunique()} dates")
    coverage = pd.DataFrame({"date": p["calendar"][p["weekly_idx"]], "members": p["members"],
                             "with_close": p["with_close"], "eligible": p["eligible"].sum(axis=1).to_numpy()})
    coverage["coverage"] = coverage["with_close"] / coverage["members"]
    coverage["eligible_share"] = coverage["eligible"] / coverage["members"]
    cov_by_window = {}
    for name, bounds in {**WINDOWS, "2000-2014": (FIRST_TRAIN, "2015-01-01")}.items():
        c = coverage[(coverage["date"] >= pd.Timestamp(bounds[0])) & (coverage["date"] < pd.Timestamp(bounds[1]))]
        cov_by_window[name] = {"dates": int(len(c)), "coverage_mean": float(c["coverage"].mean()),
                               "coverage_min": float(c["coverage"].min()),
                               "eligible_share_mean": float(c["eligible_share"].mean()),
                               "eligible_mean": float(c["eligible"].mean()), "members_mean": float(c["members"].mean())}
    log(f"coverage: {json.dumps(cov_by_window)}")

    tuned = tune(panel, p["calendar"])
    out, fit_info = out_of_sample(panel, p["calendar"], tuned)
    log(f"OOS rows: {len(out)} over {out['week'].nunique()} dates")

    results: dict = {}
    bench = {lag: simulate(out, p, None, lag) for lag in (0, 1)}
    for m in MODELS:
        col = f"score_{m}"
        r = {"ic": ic_stats(weekly_ic(out, out[col].to_numpy(), "fwd0")),
             "ic_lag1": ic_stats(weekly_ic(out, out[col].to_numpy(), "fwd1")),
             "ic_5session": ic_stats(weekly_ic(out, out[col].to_numpy(), "fwd5"))}
        r["ic_series"] = {str(k.date()): round(float(v), 5) for k, v in weekly_ic(out, out[col].to_numpy(), "fwd0").items()}
        book = simulate(out, p, col, 0)
        book_lag = simulate(out, p, col, 1)
        r["book"] = {f"{c:g}": book_stats(book, bench[0], c) for c in COSTS_BPS}
        r["book_lag1"] = {f"{c:g}": book_stats(book_lag, bench[1], c) for c in COSTS_BPS}
        r["marginal"] = {f"{c:g}": marginal_rule(book, c, p, data["irx"], WINDOWS["2018-2026"]) for c in (10.0, 25.0)}
        hold = pd.Series(book["holdings"], index=book["dates"])
        r["mean_holdings_2018_2026"] = float(window(hold, WINDOWS["2018-2026"]).mean())
        net10 = net_returns(book, 10.0)
        weekly_active = window(interval_returns(net10, book["trade_days"], book["dates"])
                               - interval_returns(bench[0]["gross"], bench[0]["trade_days"], bench[0]["dates"]),
                               WINDOWS["2018-2026"])
        r["weekly_active_10"] = return_moments(weekly_active.to_numpy())
        results[m] = r
        log(f"{m}: IC 2018-2026 {r['ic']['2018-2026']['mean_ic']:+.4f} (NW t {r['ic']['2018-2026']['nw_t']:.2f}), "
            f"active@10 {r['book']['10']['2018-2026']['active_ann']:+.4f}, IR@10 {r['book']['10']['2018-2026']['ir']:.2f}, "
            f"turnover {r['book']['10']['2018-2026']['turnover_per_year']:.2f}")
    seeds = {}
    for s in range(len(SEEDS)):
        seeds[str(SEEDS[s])] = ic_stats(weekly_ic(out, out[f"seed_{s}"].to_numpy(), "fwd0"))["2018-2026"]
    # How much of the ranker is momentum / composite: mean weekly cross-sectional Spearman between scores.
    overlap = {}
    for other in ("ridge", "momentum", "composite"):
        frame = out[["week", "date", "score_ranker", f"score_{other}"]].rename(columns={f"score_{other}": "fwd_other"})
        frame = frame.assign(fwd0=frame["fwd_other"])
        overlap[other] = float(window(weekly_ic(frame, frame["score_ranker"].to_numpy(), "fwd0"), WINDOWS["2018-2026"]).mean())
    for m in MODELS:
        results[m]["rules"] = evaluate_rules(m, results[m], results)
    deflated = dsr(results)
    decision = verdict(results)
    log(f"verdict: {json.dumps(decision)}")
    for m in MODELS:
        results[m]["weekly_active_10"] = {k: (float(v) if isinstance(v, (int, float, np.floating)) else v)
                                          for k, v in results[m]["weekly_active_10"].items()}
    payload = {
        "run": "pooled-ranker", "date": "2026-11-07", "paper_only": True, "offline": True,
        "family": "cross_sectional_ranker", "trials_charged": len(MODELS), "models": list(MODELS),
        "upper_bound": True,
        "upper_bound_reason": "members without cached yfinance history are excluded (mostly acquired or bankrupt)",
        "data": {"data_end_exclusive": DATA_END, "membership_source": data["membership"].source,
                 "tickers_since_2000": len(data["tickers"]), "with_history": len(data["frames"]),
                 "without_history": len(data["missing"]), "coverage": cov_by_window},
        "design": {"features": FEATURES, "label": f"{LABEL_H}-session open-to-open return from the next open, ranked",
                   "embargo": EMBARGO, "seeds": list(SEEDS), "hgb_fixed": HGB_FIXED, "hgb_grid": HGB_GRID,
                   "ridge_grid": list(RIDGE_GRID), "top_n": TOP_N, "enter_rank": ENTER_RANK, "exit_rank": EXIT_RANK,
                   "costs_bps_round_trip": list(COSTS_BPS), "nw_lags": NW_LAGS, "slice": SLICE},
        "tuning": tuned, "fits": fit_info["fits"], "seed_ic_2018_2026": seeds, "score_overlap_2018_2026": overlap,
        "results": results, "deflated_sharpe": deflated, "verdict": decision,
        "runtime_seconds": round(time.time() - LOG["t0"], 1),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=1, default=str))
    log(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
