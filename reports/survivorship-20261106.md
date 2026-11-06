# Survivorship check: 2026-11-06

**Question:** how much does replaying today's 45-symbol list flatter the live strategies, and should the experiment engine and strategy discovery replay the S&P 500 as it stood on each date instead?

**Decision (pre-registered in `scripts/run_survivorship_check.py`):** `replay_point_in_time_universe = True`. Coverage since 2010 80.9% (passes the 70% bar); worst-case engine run 2.1 min (inside the 30-minute budget). Paper only.

## 1. Source and cached data

* Membership: `https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv` from https://github.com/fja05680/sp500, licence MIT (Copyright (c) 2019-2020 Farrell J. Aultman), downloaded 2026-11-06, 1261 membership intervals, 1996-01-02 to 2026-08-18. `end_date` is the first day out (exclusive); FB's days are credited to META.
* Cached under `meta_label_learning.history_dir()/universe/`: `sp500_membership.csv` (the source file), `sp500_membership.json` (source, licence, download date), `sp500_coverage.json` (per-year coverage, available and missing tickers), refreshed monthly. Daily bars share the yfinance cache (`<YAHOO>.csv`, dotted tickers as `BRK-B`). Format documented in `app/services/point_in_time_universe.py`.
* Replay data: bars from 2000-01-01 (end 2026-11-06), VIX present, earnings cache present (today's single stocks only). Today's list: 45 symbols with history; point in time: 707 (704 past members with history, 392 without).

## 2. Coverage per year (members with history / members)

| Year | Members | With data | Coverage |
|---|---|---|---|
| 2000 | 546 | 244 | 44.7% |
| 2001 | 522 | 247 | 47.3% |
| 2002 | 517 | 255 | 49.3% |
| 2003 | 503 | 258 | 51.3% |
| 2004 | 513 | 262 | 51.1% |
| 2005 | 511 | 272 | 53.2% |
| 2006 | 529 | 285 | 53.9% |
| 2007 | 535 | 302 | 56.5% |
| 2008 | 532 | 320 | 60.2% |
| 2009 | 527 | 326 | 61.9% |
| 2010 | 513 | 330 | 64.3% |
| 2011 | 515 | 339 | 65.8% |
| 2012 | 515 | 350 | 68.0% |
| 2013 | 516 | 361 | 70.0% |
| 2014 | 513 | 365 | 71.2% |
| 2015 | 529 | 381 | 72.0% |
| 2016 | 533 | 401 | 75.2% |
| 2017 | 533 | 417 | 78.2% |
| 2018 | 529 | 425 | 80.3% |
| 2019 | 534 | 446 | 83.5% |
| 2020 | 525 | 454 | 86.5% |
| 2021 | 526 | 464 | 88.2% |
| 2022 | 526 | 475 | 90.3% |
| 2023 | 522 | 487 | 93.3% |
| 2024 | 523 | 496 | 94.8% |
| 2025 | 524 | 505 | 96.4% |
| 2026 | 520 | 508 | 97.7% |

Mean since 2010: 80.9%. Before 2010 the point-in-time universe is itself only half covered: the names Yahoo dropped are the acquired and bankrupt ones, so even arm (b) keeps some survivorship, and the gap below is a lower bound.

Missing tickers (392, no yfinance history): AABA, AAMRQ, ABC, ABKFQ, ABMD, ABS, ACAS, ACKH, ACS, ADS, AET, AFS.A, AGC, AGN, AKS, AL, ALTR, ALXN, AMCC, ANDV, ANDW, ANRZQ, ANSS, ANTM, APCC, APOL, ARC, ARG, ARNC, ASN, AT, ATGE, ATVI, AVB, AVP, AW, AWE, AYE, AZA.A, BCR, BDK, BFO, BGEN, BGG, BHGE, BHMSQ, BIG, BJS, BK, BLL, BLS, BMC, BMET, BMS, BNI, BOL, BRCM, BRL, BSC, BTUUQ ... (full list in the JSON).

## 3. Live strategies: today's list (a) vs point in time (b)

Trades, win rate, net expectancy per trade (bps, month-clustered bootstrap CI) and equal-weight portfolio max drawdown per window; gap = (b) - (a).

### moving_average_crossover

Setup `{"market_gate_sma": 0, "earnings_blackout_sessions": 10}`; symbols 45 -> 707.

| Window | Arm | Trades | Win rate | Expectancy bps | Max DD |
|---|---|---|---|---|---|
| all | today | 19407 | 38.2% | -8.7 [-24, +7] | 55.9% |
| all | point_in_time | 205901 | 35.3% | -45.3 [-59, -32] | 82.0% |
| all | gap | +186494 | -2.9pp | -36.6 | +26.1pp |
| 2015+ | today | 8643 | 40.3% | +7.4 [-16, +32] | 21.6% |
| 2015+ | point_in_time | 104496 | 36.1% | -15.8 [-32, +1] | 25.7% |
| 2015+ | gap | +95853 | -4.1pp | -23.3 | +4.1pp |
| 2020+ | today | 5585 | 38.1% | +0.8 [-29, +32] | 21.6% |
| 2020+ | point_in_time | 73702 | 34.7% | -20.7 [-40, -1] | 24.6% |
| 2020+ | gap | +68117 | -3.4pp | -21.6 | +2.9pp |

Today's-list trades decided while the symbol was not an S&P 500 member: 1983 (10.2%), expectancy -30.6 bps vs -6.2 bps on member days.

### rsi_mean_reversion

Setup `{"market_gate_sma": 0, "earnings_blackout_sessions": 10}`; symbols 45 -> 707.

| Window | Arm | Trades | Win rate | Expectancy bps | Max DD |
|---|---|---|---|---|---|
| all | today | 2157 | 40.6% | +11.1 [-19, +42] | 10.9% |
| all | point_in_time | 24687 | 37.4% | -16.2 [-37, +4] | 11.9% |
| all | gap | +22530 | -3.2pp | -27.3 | +1.1pp |
| 2015+ | today | 1041 | 43.5% | +38.3 [-4, +82] | 3.7% |
| 2015+ | point_in_time | 13615 | 38.7% | +6.4 [-17, +31] | 3.2% |
| 2015+ | gap | +12574 | -4.9pp | -31.9 | -0.5pp |
| 2020+ | today | 685 | 41.3% | +33.1 [-21, +92] | 3.5% |
| 2020+ | point_in_time | 9164 | 37.6% | +2.1 [-28, +34] | 3.3% |
| 2020+ | gap | +8479 | -3.7pp | -31.0 | -0.2pp |

Today's-list trades decided while the symbol was not an S&P 500 member: 108 (5.0%), expectancy -18.7 bps vs +12.7 bps on member days.

### macd_momentum

Setup `{"market_gate_sma": 50, "earnings_blackout_sessions": 10}`; symbols 45 -> 707.

| Window | Arm | Trades | Win rate | Expectancy bps | Max DD |
|---|---|---|---|---|---|
| all | today | 10924 | 39.2% | -3.1 [-22, +16] | 43.0% |
| all | point_in_time | 131791 | 35.9% | -49.0 [-65, -33] | 70.2% |
| all | gap | +120867 | -3.3pp | -45.9 | +27.2pp |
| 2015+ | today | 5082 | 41.5% | +20.2 [-6, +47] | 11.1% |
| 2015+ | point_in_time | 71401 | 36.6% | -13.1 [-31, +6] | 16.6% |
| 2015+ | gap | +66319 | -4.9pp | -33.2 | +5.5pp |
| 2020+ | today | 3344 | 40.5% | +20.0 [-13, +51] | 7.8% |
| 2020+ | point_in_time | 50500 | 35.7% | -14.7 [-37, +7] | 13.6% |
| 2020+ | gap | +47156 | -4.8pp | -34.6 | +5.8pp |

Today's-list trades decided while the symbol was not an S&P 500 member: 1076 (9.8%), expectancy -25.4 bps vs -0.7 bps on member days.

### ensemble

Setup `{"market_gate_sma": 0, "earnings_blackout_sessions": 10}`; symbols 45 -> 707.

| Window | Arm | Trades | Win rate | Expectancy bps | Max DD |
|---|---|---|---|---|---|
| all | today | 10677 | 37.6% | -5.7 [-23, +12] | 38.0% |
| all | point_in_time | 114262 | 34.7% | -56.7 [-72, -42] | 69.8% |
| all | gap | +103585 | -2.9pp | -51.0 | +31.7pp |
| 2015+ | today | 4618 | 39.7% | +10.8 [-18, +40] | 14.0% |
| 2015+ | point_in_time | 56828 | 35.9% | -15.6 [-34, +4] | 16.4% |
| 2015+ | gap | +52210 | -3.9pp | -26.5 | +2.3pp |
| 2020+ | today | 3132 | 38.0% | +4.0 [-29, +41] | 14.0% |
| 2020+ | point_in_time | 41448 | 34.7% | -20.9 [-43, +3] | 17.2% |
| 2020+ | gap | +38316 | -3.4pp | -24.8 | +3.1pp |

Today's-list trades decided while the symbol was not an S&P 500 member: 1422 (13.3%), expectancy -23.4 bps vs -3.0 bps on member days.

### bollinger_mean_reversion

Setup `{"market_gate_sma": 0, "earnings_blackout_sessions": 10}`; symbols 45 -> 707.

| Window | Arm | Trades | Win rate | Expectancy bps | Max DD |
|---|---|---|---|---|---|
| all | today | 1899 | 40.3% | +5.1 [-29, +40] | 12.2% |
| all | point_in_time | 19964 | 36.6% | -17.7 [-39, +5] | 9.3% |
| all | gap | +18065 | -3.8pp | -22.9 | -3.0pp |
| 2015+ | today | 949 | 45.2% | +43.4 [-3, +96] | 4.6% |
| 2015+ | point_in_time | 11105 | 38.1% | -0.2 [-25, +26] | 2.7% |
| 2015+ | gap | +10156 | -7.1pp | -43.6 | -1.9pp |
| 2020+ | today | 575 | 43.3% | +41.9 [-18, +107] | 4.4% |
| 2020+ | point_in_time | 6977 | 36.9% | -5.0 [-34, +29] | 2.9% |
| 2020+ | gap | +6402 | -6.4pp | -46.9 | -1.5pp |

Today's-list trades decided while the symbol was not an S&P 500 member: 85 (4.5%), expectancy -36.2 bps vs +7.1 bps on member days.

### channel_breakout

Setup `{"market_gate_sma": 20, "vix_gate": "below", "vix_level": 20, "earnings_blackout_sessions": 10}`; symbols 45 -> 707.

| Window | Arm | Trades | Win rate | Expectancy bps | Max DD |
|---|---|---|---|---|---|
| all | today | 3879 | 45.2% | +24.1 [+2, +46] | 8.3% |
| all | point_in_time | 35667 | 39.4% | -10.1 [-26, +6] | 6.0% |
| all | gap | +31788 | -5.8pp | -34.2 | -2.3pp |
| 2015+ | today | 2082 | 45.3% | +32.4 [+1, +63] | 5.7% |
| 2015+ | point_in_time | 19886 | 38.5% | -13.3 [-35, +10] | 5.0% |
| 2015+ | gap | +17804 | -6.8pp | -45.6 | -0.7pp |
| 2020+ | today | 1014 | 46.4% | +56.3 [+9, +100] | 2.5% |
| 2020+ | point_in_time | 10706 | 36.8% | -14.1 [-48, +17] | 3.4% |
| 2020+ | gap | +9692 | -9.5pp | -70.4 | +0.9pp |

Today's-list trades decided while the symbol was not an S&P 500 member: 240 (6.2%), expectancy +21.8 bps vs +24.2 bps on member days.

### trend_pullback

Setup `{"market_gate_sma": 10, "earnings_blackout_sessions": 10}`; symbols 45 -> 707.

| Window | Arm | Trades | Win rate | Expectancy bps | Max DD |
|---|---|---|---|---|---|
| all | today | 5518 | 43.2% | +14.8 [-5, +34] | 10.3% |
| all | point_in_time | 56408 | 38.2% | -9.0 [-23, +5] | 9.0% |
| all | gap | +50890 | -5.1pp | -23.8 | -1.3pp |
| 2015+ | today | 2867 | 45.0% | +29.5 [+2, +55] | 5.6% |
| 2015+ | point_in_time | 31694 | 37.9% | -7.2 [-26, +13] | 7.0% |
| 2015+ | gap | +28827 | -7.1pp | -36.7 | +1.4pp |
| 2020+ | today | 1693 | 42.2% | +21.2 [-14, +56] | 5.6% |
| 2020+ | point_in_time | 20080 | 36.4% | -10.7 [-36, +15] | 7.4% |
| 2020+ | gap | +18387 | -5.8pp | -31.9 | +1.8pp |

Today's-list trades decided while the symbol was not an S&P 500 member: 226 (4.1%), expectancy -30.5 bps vs +16.7 bps on member days.

### rsi2_pullback

Setup `{"market_gate_sma": 50, "earnings_blackout_sessions": 10}`; symbols 33 -> 33.

| Window | Arm | Trades | Win rate | Expectancy bps | Max DD |
|---|---|---|---|---|---|
| all | today | 3497 | 62.0% | +16.9 [+2, +30] | 7.1% |
| all | point_in_time | 3325 | 62.1% | +13.7 [-0, +28] | 6.8% |
| all | gap | -172 | +0.0pp | -3.2 | -0.3pp |
| 2015+ | today | 1838 | 62.1% | +18.6 [-1, +39] | 7.1% |
| 2015+ | point_in_time | 1816 | 62.1% | +16.6 [-5, +37] | 6.8% |
| 2015+ | gap | -22 | +0.0pp | -1.9 | -0.3pp |
| 2020+ | today | 1126 | 64.1% | +34.1 [+9, +59] | 2.4% |
| 2020+ | point_in_time | 1123 | 64.0% | +32.0 [+7, +56] | 2.4% |
| 2020+ | gap | -3 | -0.1pp | -2.1 | +0.0pp |

Today's-list trades decided while the symbol was not an S&P 500 member: 172 (4.9%), expectancy +78.7 bps vs +13.7 bps on member days.

### ibs_mean_reversion

Setup `{"market_gate_sma": 0, "earnings_blackout_sessions": 10}`; symbols 4 -> 4.

| Window | Arm | Trades | Win rate | Expectancy bps | Max DD |
|---|---|---|---|---|---|
| all | today | 2193 | 61.6% | +13.0 [-2, +29] | 25.7% |
| all | point_in_time | 2193 | 61.6% | +13.0 [-2, +29] | 25.7% |
| all | gap | +0 | +0.0pp | +0.0 | +0.0pp |
| 2015+ | today | 883 | 63.5% | +22.0 [-2, +44] | 18.2% |
| 2015+ | point_in_time | 883 | 63.5% | +22.0 [-2, +44] | 18.2% |
| 2015+ | gap | +0 | +0.0pp | +0.0 | +0.0pp |
| 2020+ | today | 541 | 63.4% | +28.5 [-2, +57] | 18.2% |
| 2020+ | point_in_time | 541 | 63.4% | +28.5 [-2, +57] | 18.2% |
| 2020+ | gap | +0 | +0.0pp | +0.0 | +0.0pp |

Today's-list trades decided while the symbol was not an S&P 500 member: 0 (0.0%), expectancy n/a bps vs +13.0 bps on member days.

## 4. The gap, pooled

Across the strategies whose scope grows (every technical strategy; RSI(2) and IBS keep fixed scopes), the point-in-time minus today expectancy gap per window, and how many of the 9 strategies have a bootstrap CI above zero in each arm:

| Window | Strategies | Median gap bps | Mean gap bps | CI > 0 today | CI > 0 point in time |
|---|---|---|---|---|---|
| all | 7 | -34.2 | -34.5 | 2 | 0 |
| 2015+ | 7 | -33.2 | -34.4 | 2 | 0 |
| 2020+ | 7 | -31.9 | -37.3 | 2 | 1 |

Two readings. First, the flattery is in *which* names are on today's list, not in the days before they joined: the trades today's list takes on pre-membership days are mostly worse than its member-day trades. Second, the broad-scope technical strategies' positive expectancy on today's list does not survive the point-in-time universe in any window; what the experiment engine replays from now on is the harder number, so its baselines and candidate rows will move on the first run with the setting on. The sized live book trades today's list under the same rules, so the paper results, not this replay, remain the forward evidence.

## 5. Budget

Load 9.5s, signals 8.0s, point-in-time replay 5.4s per strategy and setup; the whole check took 87.5s. Worst-case engine run (4 experiments x 5 setups): 2.1 min of the 30-minute learning-queue limit. The first run with the setting on also downloads every past member's history once (about 20 minutes single-threaded, then monthly); later runs refresh only bars older than a week and fetch at most 60 new earnings histories per run.

## 6. Caveats

* Membership before 2001 may miss a few names (the source's own note), and coverage before 2010 is about half, so the early windows still carry survivorship.
* The extra members replay without an earnings blackout (no cached earnings history); the blackout binds only on today's 29 single stocks, in both arms.
* RSI(2) and IBS keep their fixed live scopes, so their point-in-time arm only drops bars outside membership; it never adds symbols.
* The three windows overlap and every CI is a bootstrap over the same history; this is a measurement of bias, not a new strategy claim.
