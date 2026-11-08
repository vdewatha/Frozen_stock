# Pooled ranker: one pre-registered run (2026-11-07)

Step 6 of `reports/Retail bot edge feasibility.md` ("Run the ranker once"). This is one offline run of a pooled
price-and-volume ranker on the point-in-time S&P 500, plus three baselines, judged by the build plan's pass rule. It
places no orders, changes no app behaviour and needs no migration. It is paper research only.

**Verdict: fail. This is a documented negative.** The ranker fails five of the nine rules:

- the information-coefficient (IC) t-statistic (1.63 against the bar of 3);
- the one-day-lag t (1.47 against 2);
- the 2015-2017 veto (mean IC -0.013);
- turnover (20.2 times a year against a cap of 8);
- the step-3 marginal rule.

None of the three baselines passes either. No IC rule passes, so the "zero-turnover tie-breaker" fallback does not
apply. Under the plan, the architecture is not iterated, and the price-and-volume branch of direction 6 is closed.
The low-turnover fundamental tilt stays deferred, not closed.

Every number below is an **upper bound**: members that yfinance no longer serves (392 of the 1,096 tickers that were
members at some point since 2000, mostly acquired or bankrupt names) are excluded.

## What was run

- **Script:** `artifacts/api-server/backend/scripts/run_pooled_ranker.py`. It is offline only and imported by
  nothing in the app.
- **Pre-registration:** the design, the grid and the pass rule sit in the script's docstring and constants. They were
  committed (`962c233`) before the full run, and nothing was changed after seeing results.
- **Output:** the full result is `reports/pooled-ranker-20261107.json`. It holds per-window statistics, the weekly IC
  series of every model, the tuning grid and the fits.
- **Runtime:** 9.9 minutes on a laptop (10 cores), with five seeds and the full grid. No reduction was needed.

| Item | Setting |
|---|---|
| Data | yfinance adjusted daily OHLCV, $0 cost, through 2026-11-06 (the cache used for the yardstick, 704 past and present members with history), plus 11 SPDR sector ETFs, SPY and `^IRX` |
| Universe | Point-in-time S&P 500 (fja05680 membership file). A name is eligible on a scoring date if it is a member, has a close that day and has at least 252 cached bars |
| Coverage (members with a close / members, per weekly date) | 2018-2026: mean 90.9%, min 80.8%. 2022-2026: mean 95.3%, min 90.1%. 2015-2017: mean 76.7%, min 72.3%. Pre-2015 (training only): mean 59.0% |
| Eligible names per date | 457 on average in 2018-2026, 478 in 2022-2026, 385 in 2015-2017 |
| Scoring | Last session of each week, using features through that close. 457 IC dates in 2018-2026 |
| Label | 20-session return from the next open (open t+1 to open t+21), percentile-ranked per date. Lag run: open t+2 to open t+22 |
| Features (25, all cross-sectionally ranked) | Returns over 1, 5, 10, 20, 60, 120 and 252 sessions; 12-1 momentum; volatility over 5, 20 and 60; max daily return over 20; distance from the 252-session high; two relative-volume measures; log dollar volume; Amihud illiquidity; 252-session beta and idiosyncratic volatility against SPY; overnight gap (last and 20-day mean); 20-day high-low range; sector momentum (20, 60); return relative to the sector (20) |
| Earnings | No earnings-date features |
| Sector | One method for all 704 names: the causal statistical map (`sector_relative.assign_sectors`, the SPDR whose SPY-residual returns tracked the stock best over the trailing year, refreshed monthly). 697 of the 704 names have a sector on some date |
| Ranker | sklearn `HistGradientBoostingRegressor` (squared error on the ranked label), `learning_rate` 0.05, `min_samples_leaf` 500, `l2` 1.0, `max_features` 0.5, no early stopping. Seeds 0-4 averaged |
| Baselines | Ridge on the same features; 12-1 momentum rank; composite (mean rank of 12-1 momentum, low 60-day volatility and closeness to the 52-week high) |
| Hyperparameters | Frozen on pre-2015 data only. Trained on 2000-2009 (purged and embargoed), validated on 2010-2014 dates whose label ends before 2015. Ranker grid: leaves {7, 15} x iterations {100, 300}, chose 15 leaves and 300 iterations (validation IC +0.0064). Ridge grid: alpha {0.1, 10, 1000}, chose 1000 (+0.0016). For reference, momentum scored +0.0200 and the composite +0.0117 on the same validation set |
| Training | Retrained each January on an expanding window from 2000. Purge plus a 20-session embargo: the last training date is about nine weeks before the test year (for example 2014-10-31 for 2015). 225k to 473k training rows |
| Portfolio | Long-only top 25: enter at rank 25 or better, exit past rank 75 or on leaving the eligible set. Decided on the weekly close and traded at the next open (lag run: the open after). Entrants get 1/25 of the book and kept names keep their drifted relative weights |
| Benchmark | Equal-weight eligible universe, re-equalised at the same opens, no cost |
| Costs | 5, 10 and 25 bps a round trip (half a side). Turnover is one-sided, as a multiple of equity a year |
| Statistics | Newey-West t on weekly rank IC with 4 lags (8 shown as a sensitivity), from `backtest_statistics.newey_west_t`. Active return is per rebalance interval, annualised as mean x 52; IR = mean / sd x sqrt(52) |

## Pass rule (ranker)

| # | Condition | Value | Result |
|---|---|---|---|
| 1 | Mean weekly rank IC > 0 with Newey-West t >= 3, 2018-2026 | IC +0.0153, t 1.63 | **fail** |
| 2 | One-day execution lag: t >= 2, 2018-2026 | IC +0.0134, t 1.47 | **fail** |
| 3 | Mean IC > 0 in 2015-2017 | -0.0128 (t -1.03) | **fail** (veto) |
| 4 | Net active return > 0 at 25 bps, 2018-2026 | +2.98%/yr | pass |
| 5 | Information ratio >= 0.5 at 10 bps, 2018-2026 | 0.52 | pass (barely) |
| 6 | Beats ridge and momentum after costs (10 and 25 bps) | vs ridge +5.2 / +4.9 pts; vs momentum +2.8 / +0.1 pts | pass |
| 7 | Turnover <= 8x a year, 2018-2026 | 20.2x | **fail** |
| 8 | Net active (10 bps) > 0 in at least 2 of 3 sub-periods | 2018-19 -2.3%, 2020-22 +7.0%, 2023-26 +9.6% | pass |
| 9 | Step-3 marginal rule, 20/80 arm, at 10 and 25 bps | Sharpe 0.635 -> 0.654 (10 bps), 0.583 (25 bps); max DD -7.3% -> -17.6% | **fail** |

Outcome: **fail**. Rules 4 and 5 pass on a book that fails rules 1, 2, 3 and 7, so the portfolio numbers are not
backed by a statistically real ranking signal.

## Rank IC by window

Mean weekly Spearman IC on the 20-session label. Newey-West t is shown with 4 lags (8 lags in brackets). "Share > 0"
is the share of dates with a positive IC.

| Model | 2015-2017 IC (t) | 2018-2026 IC | t (4 lags) | t (8 lags) | Share > 0 | 2022-2026 IC (t) |
|---|---|---|---|---|---|---|
| Ranker | -0.0128 (-1.03) | +0.0153 | 1.63 | 1.69 | 51.4% | +0.0094 (0.68) |
| Ridge | -0.0310 (-2.00) | +0.0109 | 0.79 | 0.78 | 51.9% | +0.0102 (0.54) |
| 12-1 momentum | -0.0009 (-0.03) | +0.0008 | 0.05 | 0.05 | 51.6% | +0.0270 (1.36) |
| Composite | +0.0047 (0.14) | -0.0088 | -0.46 | -0.45 | 51.9% | +0.0099 (0.43) |

**One-day execution lag** (open t+2 to t+22), 2018-2026:

| Model | IC | t |
|---|---|---|
| Ranker | +0.0134 | 1.47 |
| Ridge | +0.0095 | 0.69 |
| Momentum | +0.0026 | 0.16 |
| Composite | -0.0058 | -0.31 |

**Ranker IC by calendar year:**

| 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| -0.017 | +0.008 | -0.029 | +0.023 | +0.012 | +0.012 | +0.042 | +0.018 | -0.002 | +0.004 | +0.013 | +0.017 |

The five seeds agree. Their 2018-2026 IC runs from +0.0143 to +0.0152, with t from 1.57 to 1.66, so seed noise is not
the issue.

The secondary 5-session label was reported, not gated, and not used for selection, as the ML note specified. On it the
ranker's 2018-2026 IC is +0.0177 with t 3.08 (3.61 at 8 lags), but +0.0032 (t 0.39) in 2015-2017. That is the
short-horizon reversal and volatility signal that the 20-session book cannot harvest at 20 times turnover. It is not a
result: it was not the pre-registered label. Under the plan it does not reopen the branch.

## Net active return, IR and turnover, 2018-2026 (primary)

Annualised active return against the equal-weight universe. IR is per week times sqrt(52). The t column is the plain
weekly t.

| Model | Cost (bps RT) | Active %/yr | IR | t | Turnover x/yr | Book CAGR | EW CAGR | Book max DD | EW max DD |
|---|---|---|---|---|---|---|---|---|---|
| Ranker | 5 | +7.01 | 0.60 | 1.80 | 20.2 | 18.3% | 11.9% | -48.3% | -40.0% |
| Ranker | 10 | +6.00 | 0.52 | 1.55 | 20.2 | 17.1% | 11.9% | -48.4% | -40.0% |
| Ranker | 25 | +2.98 | 0.26 | 0.77 | 20.2 | 13.6% | 11.9% | -48.5% | -40.0% |
| Ridge | 10 | +0.78 | 0.05 | 0.16 | 18.2 | 10.2% | 11.9% | -58.4% | -40.0% |
| Ridge | 25 | -1.94 | -0.14 | -0.41 | 18.2 | 7.2% | 11.9% | -58.5% | -40.0% |
| Momentum | 10 | +3.19 | 0.15 | 0.46 | 1.9 | 13.2% | 11.9% | -37.6% | -40.0% |
| Momentum | 25 | +2.90 | 0.14 | 0.41 | 1.9 | 12.8% | 11.9% | -38.0% | -40.0% |
| Composite | 10 | -4.21 | -0.31 | -0.93 | 3.7 | 7.8% | 11.9% | -34.9% | -40.0% |
| Composite | 25 | -4.75 | -0.35 | -1.05 | 3.7 | 7.2% | 11.9% | -35.0% | -40.0% |

What this table shows:

- **The ranker's book is a high-risk tilt, not a clean ranking edge.** Its maximum drawdown is 8 points deeper than
  the benchmark's (the 2020 crash), and its weekly active t is 1.55 at 10 bps.
- **The 0.52 IR rests on 2020-2026.** In 2018-2019 the active return was negative.
- **The enter-25/exit-75 band does not hold turnover down.** The model's scores re-rank too much week to week, and
  even ridge turns over 18 times a year.
- **The costs, in drag:** 20 times turnover costs about 2.0 points a year at 10 bps and 5.1 points at 25 bps.

**One-day execution lag, 2018-2026:**

| Model | Active at 10 bps | IR | Active at 25 bps |
|---|---|---|---|
| Ranker | +3.76% | 0.35 | +0.74% |
| Ridge | -2.28% | -0.17 | -4.99% |
| Momentum | +3.76% | 0.17 | +3.47% |
| Composite | -3.72% | -0.29 | -4.27% |

The ranker loses about 2.2 points a year from a one-day delay. Momentum loses nothing.

**2022-2026 (90%+ coverage), 10 bps:**

| Model | Active | IR | Turnover |
|---|---|---|---|
| Ranker | +9.13% | 0.80 | 20.0 |
| Ridge | -0.22% | -0.02 | 18.9 |
| Momentum | +11.67% | 0.49 | 1.8 |
| Composite | -4.18% | -0.34 | 3.2 |

The ranker's IC in this window is only +0.0094 (t 0.68).

**2015-2017 (secondary), 10 bps:**

| Model | Active | IR |
|---|---|---|
| Ranker | -5.94% | -0.74 |
| Ridge | -6.39% | -0.89 |
| Momentum | -1.31% | -0.14 |
| Composite | +0.56% | +0.07 |

## Sub-periods (net active %/yr at 10 bps)

| Model | 2018-2019 | 2020-2022 | 2023-2026 | Positive in at least 2 of 3 |
|---|---|---|---|---|
| Ranker | -2.33 | +7.02 | +9.57 | yes |
| Ridge | -1.99 | +0.38 | +2.53 | yes |
| Momentum | -5.22 | -0.94 | +10.79 | no |
| Composite | -0.14 | -3.57 | -6.83 | no |

## Baselines under the same rule

| Rule | Ridge | Momentum | Composite |
|---|---|---|---|
| 1 IC t >= 3 | fail (0.79) | fail (0.05) | fail (-0.46) |
| 2 Lag t >= 2 | fail (0.69) | fail (0.16) | fail (-0.31) |
| 3 2015-2017 IC > 0 | fail (-0.031) | fail (-0.001) | pass (+0.005) |
| 4 Active at 25 bps > 0 | fail (-1.94%) | pass (+2.90%) | fail (-4.75%) |
| 5 IR at 10 bps >= 0.5 | fail (0.05) | fail (0.15) | fail (-0.31) |
| 7 Turnover <= 8 | fail (18.2) | pass (1.9) | pass (3.7) |
| 8 Sub-periods | pass | fail | fail |
| 9 Marginal rule | fail | fail | fail |

No baseline passes, so none would ship in the ranker's place.

## Step-3 marginal rule, 2018-2026

A 20% slice of the book (net of its own costs) is taken from the T-bill leg of the passive arm, and the arm is
rebalanced monthly as in `blend_check.py`. Monthly rebalancing flows are not charged. Sharpe is the daily excess over
T-bills (`^IRX`), annualised. The rule passes if the Sharpe rises and the maximum drawdown is no more than 2 points
deeper. The 20/80 arm decides; the 50/50 arm is shown only.

| Sleeve | Cost | 20/80 Sharpe (base 0.635) | 20/80 max DD (base -7.3%) | 20/80 rule | 50/50 Sharpe (base 0.636) | 50/50 max DD (base -17.7%) |
|---|---|---|---|---|---|---|
| Ranker | 10 | 0.654 | -17.6% | fail (DD) | 0.654 | -27.4% |
| Ranker | 25 | 0.583 | -17.6% | fail | 0.611 | -27.4% |
| Ridge | 10 | 0.514 | -19.8% | fail | 0.570 | -29.5% |
| Momentum | 10 | 0.559 | -15.0% | fail | 0.605 | -24.9% |
| Composite | 10 | 0.552 | -14.5% | fail | 0.601 | -24.5% |

A long-only equity sleeve funded from T-bills roughly doubles the 20/80 arm's equity exposure. That alone deepened its
drawdown by 7-13 points here, so rule 9 is close to unpassable for any beta-one stock book. The ETF sleeves it was written
for are lower-volatility. This is an observation about the rule, not a change to it: the rule was applied as written.
At 10 bps the ranker's slice did raise the Sharpe (0.635 to 0.654), but at 25 bps it lowered it.

## Deflated Sharpe (reported, not a gate)

This is the weekly net active return at 10 bps over 2018-2026 (462 weeks). The trial count is 507 cumulative trials:
the discovery library's 422 on the host, plus the 85 hand-run research trials in `yardstick.RESEARCH_TRIALS`,
including these four. The four models' Sharpes set the trial dispersion. Experiment-engine candidates on the host are
not counted offline; adding them could only lower these numbers.

| Model | Weekly Sharpe | DSR at 507 trials | DSR at the family's 4 |
|---|---|---|---|
| Ranker | 0.072 | 0.054 | 0.688 |
| Ridge | 0.008 | 0.002 | 0.182 |
| Momentum | 0.021 | 0.004 | 0.272 |
| Composite | -0.043 | 0.000 | 0.021 |

## What the ranker learned

- **Mean weekly cross-sectional rank correlation, 2018-2026:**
  - with ridge: 0.53;
  - with 12-1 momentum: -0.00;
  - with the composite: -0.12.
- **So the model is not the "smoothed momentum plus low volatility" blend the ML note expected.** Its score is
  uncorrelated with momentum, slightly negatively correlated with the composite, and behaves like a short-horizon
  signal: it does better on the 5-session label, loses about 2 points a
  year to a one-day delay, turns over 20 times a year, and has a deeper drawdown than the universe.
- **The validation set had already warned about this.** On 2010-2014 the best ranker configuration scored a mean IC
  of +0.006, below plain 12-1 momentum (+0.020).

## Trials logged

- **The four models are charged as one family,** `cross_sectional_ranker`, with 4 trials. They are recorded in
  `yardstick.RESEARCH_TRIALS`, so `trial_families` and the discovery gate and engine hurdles now count them. No strategy
  belongs to this family, so no current gate changes.
- **No migration and no audit row.** The repository has no offline helper for writing an audit row without a database.
  The JSON artifact is the record.

## Limits

- **Upper bound.** 392 delisted tickers have no yfinance history and are excluded. Their absence flatters the
  universe's survivors, and likely helps any model that learns "losers recover".
- **Ticker reuse.** Yahoo serves only the current issuer's history for a ticker, so a reused ticker could attach the
  wrong company to a past membership interval. This was not checked name by name.
- **Pre-2015 training coverage is 46-71%.** The hyperparameters were frozen on that survivor-heavy sample, as the
  feasibility report anticipated.
- **Delisted names.** A name whose history ends while held is marked at its last close and sits as cash until the next
  rebalance.
- **Costs.** Costs are flat brackets, not the published per-name model. Monthly arm rebalancing in rule 9 is uncharged.

## Reproduce

From `artifacts/api-server/backend`, with the yardstick cache (`<dir>/<YAHOO>.csv`, `<dir>/universe/sp500_membership.csv`
and `<dir>/IRX_close.csv`):

    PYTHONPATH=. python scripts/run_pooled_ranker.py --data-dir <dir> --output reports/pooled-ranker-20261107.json --log run.log

`--smoke N` (first N tickers, one seed, one grid point) exists for debugging only. It was used on 80 and 300 tickers
before the run to check the mechanics, and never for the reported numbers.

## Outcome under the plan

This is a documented negative. The architecture is not iterated, and no shadow or tie-breaker is built. The
price-and-volume ML branch of direction 6 is closed on this evidence. A fundamental tilt (value, profitability and
quality from free SEC filings) stays deferred until the filing timestamps are confirmed usable point in time.
