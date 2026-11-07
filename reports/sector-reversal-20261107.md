# Sector-relative reversal: 2026-11-07

**Question:** does short-term reversal measured against the stock's sector (the best-documented surviving short-horizon edge in liquid large caps) pay after costs on the point-in-time S&P 500, and does ranking same-day candidates by it help the existing reversion strategies?

**Decisions (pre-registered in `scripts/run_sector_reversal_backtest.py`):** `sector_reversal` is **created paused** (gate tier `rejected`; failed: excess_over_drift_ci_low_gt_0, holdout_positive, net_expectancy_ci_low_gt_0, post_period_positive). Sector-relative ranking adopted for: none. Paper only.

## Reading

* **The edge is gross, not net, and it is the stock's own drift.** Every one of the 8 variants earns +12 to +33 bps
  per trade *before* costs over a 3-5 session hold, and the published volatility-scaled model charges about
  29-44 bps for the round trip. Net expectancy is negative in every window and VIX state, and the excess over the
  stock's own drift (what holding the stock for those days would have earned) is -29 to -51 bps everywhere,
  with CIs below zero. So on the point-in-time S&P 500 the sector-relative dip does not even beat buying the
  stock, gross. That is the turnover risk the research warned about (Blitz et al.: high gross Sharpe, very high
  turnover): the default rule trades about 700 times a year across the universe.
* **The hysteresis exit did what it was meant to do and it was not enough.** Holding until the relative RSI(2)
  recovers (no SMA5 exit) lengthens the hold from 3.3 to 5.5 days and lifts gross per trade by ~5 bps for the
  same entry, which brings net closer to zero (-6.7 bps all, -2.5 bps since 2015) but adds more drift than
  edge (excess -43 bps). No variant has a net CI above zero in any window.
* **VIX.** High-VIX trades earn more gross (+24 vs +19 bps for the default, +32 vs +23 for the
  relative-recovered exit), as Nagel (2012) predicts, but cost is also higher in high VIX (the model scales with
  volatility) and the difference is inside the CIs.
* **Survivorship, again.** On the 30 names of today's list (static sector map) the default rule earns +44.7 bps
  gross / +14.6 bps net since 2015 (785 trades) against +19.4 / -12.4 bps on the 661 past members (9,426 trades);
  excess over drift is negative for both (-12.9 and -35.0 bps). Today's megacaps flatter the rule, as they
  flattered the technical strategies in `reports/survivorship-20261106.md`; the point-in-time number is the one
  the gate reads.
* **Ranking.** Ranking same-day candidates by `rel_z_5` changed 512 of RSI(2)'s trades and 5,544 of trend
  pullback's, and lowered expectancy over all history for both (RSI(2) +8.7 -> +8.5 bps, with deeper all-history
  and 2020+ drawdowns; trend pullback -17.8 -> -28.7 bps). It helped RSI(2) since 2015 (+16.0 -> +16.9 bps) and trend pullback
  since 2020, which the pre-registered rule does not allow to outweigh the other windows. IBS trades only the index
  ETFs, which have no sector benchmark, so ranking has nothing to rank there.

## Sanity checks

* **Statistical sector map vs the static one.** On today's 30 mapped stocks, the monthly statistical assignment
  (`sector_relative.assign_sectors`) names the same ETF as the static map in 88% of months since 2019 (100% for
  15 names; HD is mostly assigned to XLRE, and MA, V and ORCL agree only about half the time). Good
  enough for a benchmark: the relative return mostly strips the market and industry factor either way.
* **Parity.** `tests/test_sector_reversal.py` checks the four features on every prefix (causal), the class
  against the replica on every bar of a synthetic pair (entries and exits), `gate_replay.simulate` on the
  replica, and that features with and without the runtime ETF attach agree.

## What ships

* Features `rel_ret_N`, `rel_rsi_N`, `rel_z_N` in `strategies/rules.py` (NaN without a `sector_close` column);
  `app/services/sector_relative.py` (static map with XLC->XLK / XLRE->XLF fallback, causal statistical map,
  chained sector index, runtime attach from trusted DB bars); `strategy_discovery.load_frames` attaches the
  column by default, so the engine and discovery replay it.
* `strategies/sector_reversal.py` (`sector_reversal`, a `RuleStrategy`), registered;
  `meta_label_primary.sector_reversal` replica (not in `REPLAYABLE_STRATEGIES`, so the meta-label feature set is
  unchanged); `gate_replay` replays it (reversion exits, single stocks).
* `seed.admit_sector_reversal` (action `sector_reversal_admission`, once): creates the row **paused** with this
  report's numbers in the audit message; an operator's later choice stands. The row carries `rank_by: rel_z_5`,
  so if it is ever re-admitted its own same-day candidates are taken deepest-first, as in the book replay above.
* `app/services/sector_ranking.py`, wired into `generate_cycle_signals` (records `features["sector_rank"] =
  {by, value, rank, of}` for a ranked strategy's BUYs) and the autotrader's symbol order; off unless a row sets
  `rank_by`. `seed.apply_sector_ranking` / `SECTOR_RANKED_STRATEGIES` is empty (nothing adopted).
* The 8 variants are in the discovery library (`sector_reversal` family, single stocks), so weekly discovery
  re-tests them on fresh data and charges them to its cumulative trial count.

## Limits

* Costs: no paper cost calibration is recorded in the local ledger, so the published model priced every trade.
  Its ~33 bps round trip on S&P 500 names is the whole verdict here: if the Saturday calibration fits a
  single-stock multiplier well under 1, re-run this script (cache only, ~4 minutes), since gross is +20-30 bps.
* Sector map: today's GICS map for today's names (V and MA moved from XLK to XLF in 2023 and are treated as XLF
  throughout); past members by correlation, not by their GICS code of the day.
* Design source: `reports/Frozen stock research priorities.md` (ranked experiment 5) landed on `main` (#39) while
  this ran; the design was built from the brief's summary of it and matches it (relative RSI(2) < 5 entry with an
  exit above 70 is the `rel_rsi_2<5/relative_recovered` variant; the VIX split and raw-vs-relative ranking are
  sections 2 and 3). Two things the report mentions were not tested: variants *without* the uptrend filter
  ("optionally only in an uptrend"; every variant here keeps close > SMA200), and Hameed & Mian's "stronger after
  market declines" condition. Both are one-line library additions for weekly discovery, each a new trial.

## 1. Data and method

* Bars 2000-01-01 to 2026-11-06, holdout from 2024-11-06; VIX present; costs: published volatility-scaled model (no paper cost calibration recorded in this ledger).
* Point-in-time universe: 707 symbols with history (coverage since 2010 80.9%); 691 single stocks with a sector benchmark: 30 on today's static map, 661 past members assigned statistically (`sector_relative`).
* Trials counted: 17 (8 sector-reversal variants + 3 book orders x 3 strategies).

## 2. Sector reversal variants

Per variant: discovery-gate tier, deflated Sharpe over all trials, then trades / win rate / net expectancy (bps, month-clustered CI) / excess over drift / gross and cost per trade / holding days / trades per year / equal-weight max drawdown, by window and VIX state (VIX above or below its trailing 252-day median).

### `rel_z_5<-2/hysteresis` (default rule)

Tier **rejected**; deflated Sharpe 0.000; failed checks: net_expectancy_ci_low_gt_0, excess_over_drift_ci_low_gt_0, post_period_positive, deflated_sharpe, holdout_positive. Holdout 1874 trades at -6.5 bps.

| Window | VIX | Trades | Win | Net bps | Excess bps | Gross | Cost | Hold d | /yr | Max DD |
|---|---|---|---|---|---|---|---|---|---|---|
| all | all | 18469 | 56.6% | -12.2 [-23, -2] | -33.9 [-45, -23] | +20.7 | +32.9 | 3.3 | 709 | 3.0% |
| all | high | 7261 | 57.9% | -10.9 [-31, +8] | -31.7 [-52, -12] | +23.9 | +34.9 | 3.1 | 279 | 1.5% |
| all | low | 11208 | 55.8% | -13.1 [-23, -3] | -35.3 [-46, -25] | +18.6 | +31.7 | 3.4 | 433 | 1.8% |
| 2015+ | all | 10211 | 57.1% | -10.3 [-26, +5] | -33.3 [-49, -17] | +21.3 | +31.7 | 3.3 | 862 | 1.7% |
| 2015+ | high | 4539 | 57.9% | -12.3 [-40, +14] | -34.5 [-62, -8] | +20.8 | +33.1 | 3.1 | 383 | 1.5% |
| 2015+ | low | 5672 | 56.5% | -8.7 [-23, +5] | -32.3 [-47, -18] | +21.8 | +30.5 | 3.4 | 479 | 0.6% |
| 2020+ | all | 6256 | 57.4% | -7.4 [-29, +13] | -31.0 [-52, -10] | +26.1 | +33.5 | 3.2 | 914 | 0.9% |
| 2020+ | high | 2811 | 58.1% | -9.5 [-49, +25] | -32.5 [-71, +2] | +25.2 | +34.7 | 3.1 | 411 | 0.9% |
| 2020+ | low | 3445 | 56.8% | -5.7 [-29, +14] | -29.8 [-54, -10] | +26.8 | +32.5 | 3.3 | 504 | 0.6% |

### `rel_z_5<-2/relative_recovered`

Tier **rejected**; deflated Sharpe 0.000; failed checks: net_expectancy_ci_low_gt_0, excess_over_drift_ci_low_gt_0, post_period_positive, deflated_sharpe, holdout_positive. Holdout 1765 trades at -5.3 bps.

| Window | VIX | Trades | Win | Net bps | Excess bps | Gross | Cost | Hold d | /yr | Max DD |
|---|---|---|---|---|---|---|---|---|---|---|
| all | all | 17185 | 56.4% | -6.7 [-21, +7] | -43.1 [-57, -29] | +26.3 | +33.1 | 5.5 | 660 | 2.0% |
| all | high | 6742 | 57.4% | -2.8 [-28, +22] | -38.4 [-64, -13] | +32.3 | +35.1 | 5.3 | 259 | 1.2% |
| all | low | 10443 | 55.7% | -9.3 [-24, +5] | -46.1 [-60, -31] | +22.5 | +31.7 | 5.6 | 403 | 1.6% |
| 2015+ | all | 9473 | 56.8% | -2.5 [-23, +19] | -41.3 [-62, -20] | +29.3 | +31.7 | 5.5 | 800 | 1.3% |
| 2015+ | high | 4195 | 57.3% | -2.5 [-38, +32] | -40.5 [-75, -6] | +30.9 | +33.4 | 5.4 | 354 | 1.2% |
| 2015+ | low | 5278 | 56.3% | -2.5 [-22, +16] | -41.9 [-62, -23] | +28.0 | +30.4 | 5.7 | 446 | 0.8% |
| 2020+ | all | 5793 | 56.2% | -5.7 [-35, +23] | -45.9 [-76, -17] | +27.9 | +33.5 | 5.5 | 847 | 1.2% |
| 2020+ | high | 2605 | 56.1% | -10.7 [-62, +39] | -50.0 [-99, -1] | +24.2 | +35.0 | 5.4 | 381 | 1.1% |
| 2020+ | low | 3188 | 56.2% | -1.5 [-33, +27] | -42.6 [-73, -14] | +30.8 | +32.4 | 5.7 | 466 | 0.8% |

### `rel_z_5<-1.5/hysteresis`

Tier **rejected**; deflated Sharpe 0.000; failed checks: net_expectancy_ci_low_gt_0, excess_over_drift_ci_low_gt_0, post_period_positive, deflated_sharpe, holdout_positive. Holdout 4207 trades at -13.0 bps.

| Window | VIX | Trades | Win | Net bps | Excess bps | Gross | Cost | Hold d | /yr | Max DD |
|---|---|---|---|---|---|---|---|---|---|---|
| all | all | 42837 | 56.2% | -13.1 [-21, -5] | -33.0 [-42, -25] | +20.2 | +33.3 | 3.0 | 1644 | 7.0% |
| all | high | 16739 | 57.5% | -11.9 [-28, +3] | -30.9 [-47, -16] | +22.6 | +34.5 | 2.8 | 643 | 2.9% |
| all | low | 26098 | 55.4% | -13.9 [-22, -6] | -34.3 [-43, -26] | +18.6 | +32.5 | 3.1 | 1007 | 4.5% |
| 2015+ | all | 23297 | 56.3% | -11.2 [-24, +1] | -32.4 [-45, -20] | +19.8 | +31.0 | 3.0 | 1968 | 3.4% |
| 2015+ | high | 10221 | 56.8% | -12.4 [-33, +8] | -32.5 [-53, -12] | +19.9 | +32.2 | 2.9 | 863 | 2.7% |
| 2015+ | low | 13076 | 56.0% | -10.3 [-24, +2] | -32.3 [-47, -19] | +19.7 | +29.9 | 3.1 | 1104 | 1.6% |
| 2020+ | all | 14315 | 56.4% | -9.3 [-27, +7] | -31.2 [-49, -15] | +23.5 | +32.8 | 3.0 | 2092 | 2.1% |
| 2020+ | high | 6208 | 56.6% | -12.9 [-42, +16] | -33.8 [-64, -5] | +21.0 | +33.9 | 2.9 | 908 | 1.7% |
| 2020+ | low | 8107 | 56.2% | -6.5 [-27, +12] | -29.2 [-50, -10] | +25.4 | +31.9 | 3.1 | 1185 | 1.2% |

### `rel_z_5<-1.5/relative_recovered`

Tier **rejected**; deflated Sharpe 0.000; failed checks: net_expectancy_ci_low_gt_0, excess_over_drift_ci_low_gt_0, post_period_positive, deflated_sharpe, holdout_positive. Holdout 3714 trades at -11.7 bps.

| Window | VIX | Trades | Win | Net bps | Excess bps | Gross | Cost | Hold d | /yr | Max DD |
|---|---|---|---|---|---|---|---|---|---|---|
| all | all | 37145 | 56.6% | -7.8 [-20, +4] | -42.3 [-54, -30] | +25.9 | +33.7 | 5.2 | 1426 | 4.1% |
| all | high | 14553 | 57.7% | -3.8 [-26, +18] | -37.5 [-59, -16] | +31.1 | +34.9 | 5.0 | 559 | 2.4% |
| all | low | 22592 | 55.9% | -10.4 [-23, +2] | -45.4 [-58, -33] | +22.6 | +33.0 | 5.3 | 872 | 3.0% |
| 2015+ | all | 20129 | 56.5% | -4.3 [-22, +13] | -41.2 [-59, -24] | +26.9 | +31.2 | 5.3 | 1700 | 2.5% |
| 2015+ | high | 8818 | 57.5% | -0.2 [-29, +29] | -36.0 [-65, -7] | +32.3 | +32.6 | 5.1 | 745 | 2.0% |
| 2015+ | low | 11311 | 55.8% | -7.4 [-27, +11] | -45.4 [-65, -26] | +22.7 | +30.1 | 5.4 | 955 | 1.6% |
| 2020+ | all | 12336 | 56.2% | -4.5 [-31, +21] | -42.8 [-69, -17] | +28.5 | +33.0 | 5.3 | 1803 | 2.5% |
| 2020+ | high | 5339 | 57.1% | -4.8 [-48, +39] | -41.7 [-84, +2] | +29.5 | +34.3 | 5.1 | 781 | 1.6% |
| 2020+ | low | 6997 | 55.5% | -4.4 [-33, +23] | -43.7 [-72, -16] | +27.7 | +32.0 | 5.4 | 1023 | 1.6% |

### `rel_rsi_2<5/hysteresis`

Tier **rejected**; deflated Sharpe 0.000; failed checks: net_expectancy_ci_low_gt_0, excess_over_drift_ci_low_gt_0, post_period_positive, deflated_sharpe, holdout_positive. Holdout 4105 trades at -20.4 bps.

| Window | VIX | Trades | Win | Net bps | Excess bps | Gross | Cost | Hold d | /yr | Max DD |
|---|---|---|---|---|---|---|---|---|---|---|
| all | all | 44418 | 56.9% | -18.0 [-26, -10] | -38.4 [-47, -30] | +22.5 | +40.4 | 3.0 | 1705 | 9.8% |
| all | high | 15398 | 58.8% | -12.2 [-28, +3] | -32.1 [-48, -17] | +24.9 | +37.1 | 2.9 | 591 | 2.7% |
| all | low | 29020 | 55.9% | -21.0 [-30, -12] | -41.7 [-51, -32] | +21.1 | +42.2 | 3.1 | 1120 | 7.7% |
| 2015+ | all | 24124 | 57.1% | -13.6 [-26, -2] | -35.1 [-47, -23] | +16.2 | +29.8 | 3.1 | 2037 | 4.2% |
| 2015+ | high | 9617 | 58.2% | -13.8 [-35, +6] | -34.5 [-56, -14] | +17.4 | +31.1 | 3.0 | 812 | 2.7% |
| 2015+ | low | 14507 | 56.4% | -13.5 [-26, -2] | -35.6 [-49, -23] | +15.4 | +28.9 | 3.2 | 1225 | 2.4% |
| 2020+ | all | 14708 | 57.1% | -12.4 [-29, +3] | -34.7 [-52, -19] | +19.2 | +31.6 | 3.1 | 2150 | 3.0% |
| 2020+ | high | 5897 | 57.5% | -14.6 [-41, +12] | -36.0 [-63, -9] | +18.1 | +32.7 | 3.0 | 862 | 1.7% |
| 2020+ | low | 8811 | 56.8% | -10.9 [-30, +7] | -33.8 [-54, -16] | +20.0 | +30.8 | 3.2 | 1288 | 1.8% |

### `rel_rsi_2<5/relative_recovered`

Tier **rejected**; deflated Sharpe 0.000; failed checks: net_expectancy_ci_low_gt_0, excess_over_drift_ci_low_gt_0, post_period_positive, deflated_sharpe, holdout_positive. Holdout 3737 trades at -25.5 bps.

| Window | VIX | Trades | Win | Net bps | Excess bps | Gross | Cost | Hold d | /yr | Max DD |
|---|---|---|---|---|---|---|---|---|---|---|
| all | all | 39247 | 57.0% | -12.5 [-24, -1] | -46.7 [-58, -35] | +29.5 | +42.0 | 5.2 | 1507 | 6.0% |
| all | high | 13792 | 58.3% | -5.9 [-28, +15] | -39.7 [-62, -18] | +32.1 | +38.0 | 5.0 | 530 | 2.0% |
| all | low | 25455 | 56.3% | -16.1 [-29, -3] | -50.5 [-63, -37] | +28.1 | +44.2 | 5.2 | 983 | 5.2% |
| 2015+ | all | 21346 | 56.9% | -8.7 [-25, +8] | -45.0 [-62, -29] | +21.4 | +30.0 | 5.2 | 1803 | 3.0% |
| 2015+ | high | 8554 | 57.8% | -6.8 [-39, +23] | -42.3 [-74, -13] | +24.6 | +31.3 | 5.1 | 723 | 2.0% |
| 2015+ | low | 12792 | 56.4% | -9.9 [-28, +7] | -46.7 [-65, -29] | +19.2 | +29.1 | 5.3 | 1080 | 1.8% |
| 2020+ | all | 13050 | 56.7% | -9.1 [-34, +15] | -46.5 [-71, -23] | +22.7 | +31.8 | 5.2 | 1907 | 3.0% |
| 2020+ | high | 5250 | 56.9% | -11.6 [-54, +30] | -48.0 [-91, -7] | +21.3 | +32.9 | 5.0 | 768 | 1.8% |
| 2020+ | low | 7800 | 56.5% | -7.4 [-34, +17] | -45.6 [-73, -21] | +23.6 | +31.1 | 5.3 | 1140 | 1.8% |

### `rel_rsi_2<10/hysteresis`

Tier **rejected**; deflated Sharpe 0.000; failed checks: net_expectancy_ci_low_gt_0, excess_over_drift_ci_low_gt_0, post_period_positive, deflated_sharpe, holdout_positive. Holdout 7770 trades at -21.4 bps.

| Window | VIX | Trades | Win | Net bps | Excess bps | Gross | Cost | Hold d | /yr | Max DD |
|---|---|---|---|---|---|---|---|---|---|---|
| all | all | 83168 | 56.3% | -19.8 [-27, -13] | -39.7 [-47, -32] | +19.8 | +39.7 | 3.0 | 3193 | 19.3% |
| all | high | 29311 | 58.0% | -15.8 [-30, -3] | -35.2 [-49, -22] | +21.7 | +37.6 | 2.9 | 1125 | 6.4% |
| all | low | 53857 | 55.4% | -22.0 [-30, -14] | -42.1 [-50, -34] | +18.8 | +40.8 | 3.0 | 2079 | 14.4% |
| 2015+ | all | 44882 | 56.6% | -16.0 [-27, -6] | -37.0 [-48, -26] | +14.0 | +30.0 | 3.0 | 3790 | 9.0% |
| 2015+ | high | 18075 | 57.7% | -15.2 [-34, +3] | -35.4 [-54, -17] | +16.2 | +31.3 | 2.9 | 1527 | 5.0% |
| 2015+ | low | 26807 | 55.8% | -16.6 [-28, -6] | -38.1 [-50, -27] | +12.5 | +29.0 | 3.1 | 2264 | 5.4% |
| 2020+ | all | 27390 | 56.4% | -15.6 [-30, -2] | -37.4 [-53, -23] | +16.2 | +31.8 | 3.0 | 4003 | 6.1% |
| 2020+ | high | 10996 | 57.2% | -14.9 [-40, +10] | -35.8 [-61, -11] | +18.0 | +32.9 | 2.9 | 1608 | 3.0% |
| 2020+ | low | 16394 | 55.8% | -16.0 [-33, -1] | -38.5 [-56, -23] | +15.0 | +31.0 | 3.1 | 2396 | 4.0% |

### `rel_rsi_2<10/relative_recovered`

Tier **rejected**; deflated Sharpe 0.000; failed checks: net_expectancy_ci_low_gt_0, excess_over_drift_ci_low_gt_0, post_period_positive, deflated_sharpe, holdout_positive. Holdout 6695 trades at -22.5 bps.

| Window | VIX | Trades | Win | Net bps | Excess bps | Gross | Cost | Hold d | /yr | Max DD |
|---|---|---|---|---|---|---|---|---|---|---|
| all | all | 69284 | 57.0% | -13.8 [-24, -3] | -47.4 [-58, -37] | +28.0 | +41.8 | 5.1 | 2660 | 11.2% |
| all | high | 24839 | 58.3% | -7.5 [-28, +10] | -40.8 [-60, -23] | +31.3 | +38.9 | 5.0 | 954 | 3.6% |
| all | low | 44445 | 56.3% | -17.3 [-28, -5] | -51.2 [-62, -39] | +26.2 | +43.5 | 5.2 | 1716 | 9.4% |
| 2015+ | all | 37369 | 56.8% | -10.9 [-26, +4] | -46.8 [-62, -32] | +19.3 | +30.2 | 5.2 | 3156 | 5.5% |
| 2015+ | high | 15174 | 58.1% | -7.0 [-33, +19] | -42.1 [-68, -16] | +24.6 | +31.6 | 5.0 | 1282 | 3.6% |
| 2015+ | low | 22195 | 56.0% | -13.6 [-31, +3] | -50.0 [-67, -34] | +15.7 | +29.3 | 5.2 | 1874 | 3.8% |
| 2020+ | all | 22894 | 56.4% | -11.0 [-33, +10] | -47.9 [-70, -27] | +21.1 | +32.1 | 5.1 | 3346 | 5.0% |
| 2020+ | high | 9247 | 57.5% | -6.7 [-44, +31] | -42.6 [-80, -5] | +26.5 | +33.3 | 5.0 | 1352 | 2.4% |
| 2020+ | low | 13647 | 55.6% | -13.9 [-37, +8] | -51.5 [-75, -29] | +17.4 | +31.3 | 5.2 | 1995 | 3.8% |

### Default rule through the live book

18469 executable trades compete for the slots (<= 5 per strategy, <= 3 per sector, <= 20 total, most negative `rel_z_5` first); 7963 are taken. Book statistics (each position 5% of equity):

| Window | VIX | Trades | Win | Net bps | Total return | Max DD |
|---|---|---|---|---|---|---|
| all | all | 7963 | 56.1% | -15.3 [-25, -6] | -41.3% | 42.2% |
| 2015+ | all | 3758 | 56.0% | -17.4 [-33, -2] | -25.4% | 25.8% |
| 2020+ | all | 2175 | 56.3% | -21.6 [-44, -0] | -19.4% | 20.4% |
| all | high | 3220 | 57.8% | -11.9 [-30, +5] | -14.9% | 18.0% |
| 2015+ | high | 1716 | 56.9% | -19.2 [-43, +3] | -14.0% | 16.2% |
| 2020+ | high | 976 | 57.2% | -29.4 [-65, +4] | -12.6% | 12.8% |
| all | low | 4743 | 55.0% | -17.6 [-28, -8] | -30.9% | 32.0% |
| 2015+ | low | 2042 | 55.2% | -15.8 [-33, -1] | -13.2% | 14.7% |
| 2020+ | low | 1199 | 55.5% | -15.3 [-42, +6] | -7.7% | 11.4% |

## 3. Ranking inside the reversion strategies

Each strategy alone through the live book on its live scope and setup; the same candidates in three orders: `live` (execution-list order, today's behaviour), `confidence` (raw signal strength) and `rel_z_5` (most negative first, unrankable last).

### rsi2_pullback

Setup `{"market_gate_sma": 50, "earnings_blackout_sessions": 10}`; 3325 candidates, rankable share 84.9%; accepted {'live': 2363, 'confidence': 2362, 'rel_z_5': 2373}; trades changed by ranking 512. Decision: **not adopted** (fails: all expectancy, 2020+ drawdown). Deflated Sharpe: live 0.051, confidence 0.037, rel_z_5 0.050.

| Window | VIX | Order | Trades | Win | Net bps | Total return | Max DD |
|---|---|---|---|---|---|---|---|
| all | all | live | 2363 | 61.0% | +8.7 [-6, +23] | 14.1% | 6.0% |
| all | all | confidence | 2362 | 60.7% | +8.1 [-7, +22] | 13.2% | 6.8% |
| all | all | rel_z_5 | 2373 | 60.4% | +8.5 [-7, +23] | 13.8% | 7.0% |
| all | high | live | 648 | 64.5% | +33.5 [+2, +64] | 12.5% | 3.3% |
| all | high | confidence | 651 | 64.4% | +33.1 [+1, +64] | 12.4% | 3.4% |
| all | high | rel_z_5 | 657 | 64.5% | +37.3 [+6, +67] | 14.1% | 3.1% |
| all | low | live | 1715 | 59.7% | -0.7 [-17, +15] | 1.5% | 6.1% |
| all | low | confidence | 1711 | 59.3% | -1.4 [-18, +14] | 0.8% | 7.8% |
| all | low | rel_z_5 | 1716 | 58.7% | -2.5 [-19, +13] | -0.2% | 7.9% |
| 2015+ | all | live | 1274 | 61.9% | +16.0 [-4, +37] | 12.6% | 5.0% |
| 2015+ | all | confidence | 1276 | 61.5% | +15.4 [-5, +35] | 12.2% | 5.1% |
| 2015+ | all | rel_z_5 | 1282 | 61.6% | +16.9 [-3, +37] | 13.3% | 4.9% |
| 2015+ | high | live | 409 | 65.8% | +39.9 [-7, +80] | 9.1% | 3.3% |
| 2015+ | high | confidence | 412 | 64.8% | +34.0 [-12, +75] | 7.8% | 3.4% |
| 2015+ | high | rel_z_5 | 417 | 65.7% | +40.6 [-4, +80] | 9.4% | 3.1% |
| 2015+ | low | live | 865 | 60.0% | +4.7 [-17, +24] | 3.3% | 3.0% |
| 2015+ | low | confidence | 864 | 60.0% | +6.5 [-15, +26] | 4.0% | 3.2% |
| 2015+ | low | rel_z_5 | 865 | 59.7% | +5.4 [-18, +25] | 3.5% | 3.3% |
| 2020+ | all | live | 798 | 63.0% | +27.3 [+2, +54] | 12.7% | 2.3% |
| 2020+ | all | confidence | 801 | 63.0% | +29.5 [+6, +55] | 13.7% | 2.9% |
| 2020+ | all | rel_z_5 | 804 | 62.9% | +27.9 [+3, +55] | 13.1% | 2.9% |
| 2020+ | high | live | 273 | 68.5% | +61.8 [+7, +114] | 9.2% | 2.2% |
| 2020+ | high | confidence | 275 | 67.3% | +57.6 [+3, +111] | 8.6% | 2.0% |
| 2020+ | high | rel_z_5 | 277 | 67.9% | +57.6 [+4, +110] | 8.7% | 2.1% |
| 2020+ | low | live | 525 | 60.2% | +9.4 [-19, +36] | 3.2% | 3.0% |
| 2020+ | low | confidence | 526 | 60.8% | +14.8 [-13, +41] | 4.7% | 3.2% |
| 2020+ | low | rel_z_5 | 527 | 60.3% | +12.3 [-17, +40] | 4.0% | 3.3% |

### ibs_mean_reversion

Setup `{"market_gate_sma": 0, "earnings_blackout_sessions": 10}`; 2193 candidates, rankable share 0.0%; accepted {'live': 1875, 'confidence': 1870, 'rel_z_5': 1875}; trades changed by ranking 0. Decision: **not adopted** (no_effect). Deflated Sharpe: live 0.068, confidence 0.073, rel_z_5 0.068.

| Window | VIX | Order | Trades | Win | Net bps | Total return | Max DD |
|---|---|---|---|---|---|---|---|
| all | all | live | 1875 | 61.0% | +10.4 [-5, +26] | 12.8% | 5.7% |
| all | all | confidence | 1870 | 60.9% | +10.7 [-5, +27] | 13.0% | 5.4% |
| all | all | rel_z_5 | 1875 | 61.0% | +10.4 [-5, +26] | 12.8% | 5.7% |
| all | high | live | 1031 | 62.5% | +16.1 [-7, +39] | 10.1% | 4.8% |
| all | high | confidence | 1028 | 62.2% | +16.1 [-8, +39] | 10.1% | 4.8% |
| all | high | rel_z_5 | 1031 | 62.5% | +16.1 [-7, +39] | 10.1% | 4.8% |
| all | low | live | 844 | 59.2% | +3.4 [-17, +22] | 2.4% | 4.2% |
| all | low | confidence | 842 | 59.3% | +4.0 [-15, +21] | 2.7% | 3.7% |
| all | low | rel_z_5 | 844 | 59.2% | +3.4 [-17, +22] | 2.4% | 4.2% |
| 2015+ | all | live | 762 | 62.7% | +21.5 [-3, +44] | 9.5% | 3.0% |
| 2015+ | all | confidence | 759 | 62.1% | +17.8 [-7, +41] | 7.9% | 3.2% |
| 2015+ | all | rel_z_5 | 762 | 62.7% | +21.5 [-3, +44] | 9.5% | 3.0% |
| 2015+ | high | live | 444 | 62.8% | +21.7 [-13, +53] | 5.5% | 3.3% |
| 2015+ | high | confidence | 443 | 62.5% | +19.3 [-16, +52] | 4.9% | 3.4% |
| 2015+ | high | rel_z_5 | 444 | 62.8% | +21.7 [-13, +53] | 5.5% | 3.3% |
| 2015+ | low | live | 318 | 62.6% | +21.3 [-1, +43] | 3.8% | 1.3% |
| 2015+ | low | confidence | 316 | 61.4% | +15.8 [-8, +39] | 2.9% | 1.5% |
| 2015+ | low | rel_z_5 | 318 | 62.6% | +21.3 [-1, +43] | 3.8% | 1.3% |
| 2020+ | all | live | 466 | 62.7% | +27.6 [-2, +57] | 7.3% | 3.0% |
| 2020+ | all | confidence | 465 | 62.4% | +26.0 [-4, +55] | 6.9% | 3.0% |
| 2020+ | all | rel_z_5 | 466 | 62.7% | +27.6 [-2, +57] | 7.3% | 3.0% |
| 2020+ | high | live | 241 | 57.7% | +24.5 [-26, +74] | 3.4% | 3.0% |
| 2020+ | high | confidence | 241 | 58.1% | +27.1 [-22, +77] | 3.7% | 3.0% |
| 2020+ | high | rel_z_5 | 241 | 57.7% | +24.5 [-26, +74] | 3.4% | 3.0% |
| 2020+ | low | live | 225 | 68.0% | +31.0 [+2, +57] | 3.8% | 1.3% |
| 2020+ | low | confidence | 224 | 67.0% | +24.8 [-7, +53] | 3.1% | 1.5% |
| 2020+ | low | rel_z_5 | 225 | 68.0% | +31.0 [+2, +57] | 3.8% | 1.3% |

### trend_pullback

Setup `{"market_gate_sma": 10, "earnings_blackout_sessions": 10}`; 55335 candidates, rankable share 95.0%; accepted {'live': 3104, 'confidence': 4460, 'rel_z_5': 3570}; trades changed by ranking 5544. Decision: **not adopted** (fails: all expectancy, 2015+ expectancy). Deflated Sharpe: live 0.000, confidence 0.000, rel_z_5 0.000.

| Window | VIX | Order | Trades | Win | Net bps | Total return | Max DD |
|---|---|---|---|---|---|---|---|
| all | all | live | 3104 | 38.7% | -17.8 [-35, +0] | -23.0% | 26.3% |
| all | all | confidence | 4460 | 33.8% | -37.0 [-54, -20] | -55.1% | 55.3% |
| all | all | rel_z_5 | 3570 | 35.5% | -28.7 [-46, -11] | -38.9% | 39.6% |
| all | high | live | 1265 | 38.2% | -13.0 [-44, +19] | -7.4% | 16.4% |
| all | high | confidence | 1632 | 35.8% | -15.4 [-48, +17] | -11.1% | 20.4% |
| all | high | rel_z_5 | 1432 | 36.0% | -20.1 [-49, +11] | -12.8% | 20.2% |
| all | low | live | 1839 | 39.0% | -21.0 [-42, -1] | -16.7% | 19.4% |
| all | low | confidence | 2828 | 32.7% | -49.4 [-69, -29] | -49.4% | 49.4% |
| all | low | rel_z_5 | 2138 | 35.2% | -34.4 [-56, -13] | -29.8% | 30.3% |
| 2015+ | all | live | 1362 | 40.3% | -3.9 [-31, +25] | -1.9% | 11.9% |
| 2015+ | all | confidence | 2054 | 34.9% | -21.8 [-44, +3] | -19.1% | 19.8% |
| 2015+ | all | rel_z_5 | 1605 | 36.7% | -10.6 [-35, +15] | -7.4% | 11.8% |
| 2015+ | high | live | 592 | 40.4% | +5.3 [-42, +52] | 1.9% | 8.7% |
| 2015+ | high | confidence | 824 | 36.5% | -0.3 [-41, +42] | 0.2% | 8.6% |
| 2015+ | high | rel_z_5 | 693 | 37.7% | +5.5 [-35, +47] | 2.1% | 7.7% |
| 2015+ | low | live | 770 | 40.3% | -10.9 [-42, +22] | -3.7% | 6.8% |
| 2015+ | low | confidence | 1230 | 33.7% | -36.2 [-63, -9] | -19.3% | 19.5% |
| 2015+ | low | rel_z_5 | 912 | 36.0% | -22.8 [-54, +4] | -9.3% | 9.7% |
| 2020+ | all | live | 871 | 37.3% | -15.2 [-51, +21] | -6.0% | 11.9% |
| 2020+ | all | confidence | 1365 | 33.6% | -26.1 [-58, +3] | -15.7% | 16.7% |
| 2020+ | all | rel_z_5 | 1020 | 35.2% | -13.7 [-46, +19] | -6.3% | 11.8% |
| 2020+ | high | live | 370 | 37.3% | -18.9 [-76, +42] | -3.2% | 8.7% |
| 2020+ | high | confidence | 545 | 34.1% | -14.6 [-67, +41] | -3.7% | 8.6% |
| 2020+ | high | rel_z_5 | 430 | 34.4% | -14.4 [-67, +36] | -2.9% | 7.7% |
| 2020+ | low | live | 501 | 37.3% | -12.5 [-50, +24] | -2.9% | 6.5% |
| 2020+ | low | confidence | 820 | 33.2% | -33.6 [-65, -2] | -12.5% | 13.0% |
| 2020+ | low | rel_z_5 | 590 | 35.8% | -13.1 [-52, +25] | -3.4% | 6.3% |

## 4. Run

Load 17.4s, sector variants 149.3s, total 237.0s. JSON next to this report holds every number.
