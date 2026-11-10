## Top 25 trials by month-clustered t of net OOS trade returns (2015-01 → 2026-11, 11 bps a side)

| Family | Universe | Gate | Rule | Trades | Net bps @11 | Net bps @25 | PF | Max DD | Windows + | Excess bps | t (net) | t (excess) | boot p | Bonf. p |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| gap_down_recovery | index_etfs | spy_sma10 | BUY when gap < -0.02; exit when ret_1 > 0 | 23 | 138.1 | 109.8 | 39.11 | 1.8% | 88% | 133.6 | 7.83 | 7.53 | 0.0620 | 1.000 |
| weekly_reversal | index_etfs | spy_sma50 | BUY when dist_sma_200 > 0 and ret_5 < -0.05; exit when dist_sma_5 > 0 | 13 | 167.8 | 139.3 | 48.56 | 1.2% | 100% | 159.6 | 5.90 | 5.63 | 0.0180 | 1.000 |
| weekly_reversal | index_etfs | spy_sma50 | BUY when dist_sma_200 > 0 and ret_5 < -0.05; exit when ret_1 > 0 | 14 | 137.7 | 109.3 | 41.04 | 0.9% | 100% | 131.7 | 5.35 | 5.17 | 0.0160 | 1.000 |
| weekly_reversal | index_etfs | spy_sma50 | BUY when ret_5 < -0.05; exit when dist_sma_5 > 0 | 22 | 186.3 | 157.8 | 17.54 | 1.5% | 100% | 177.5 | 5.09 | 4.80 | 0.0140 | 1.000 |
| earnings_drift | single_stocks | adx_range | BUY when earn_surprise > 0 | 453 | 326.3 | 299.8 | 2.37 | 4.9% | 83% | 37.4 | 5.06 | 0.60 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | adx_range | BUY when at_high_50 == 1; exit when dist_sma_50 < 0 | 892 | 174.3 | 146.4 | 1.82 | 4.3% | 88% | 34.4 | 4.81 | 0.99 | 0.0000 | 0.000 |
| calm_market_dip | index_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 10; exit when dist_sma_5 >  | 105 | 81.0 | 52.8 | 4.36 | 2.2% | 78% | 70.5 | 4.81 | 4.08 | 0.0000 | 0.000 |
| earnings_drift | single_stocks | adx_range | BUY when dist_sma_200 > 0 and earn_surprise > 0 | 324 | 313.7 | 287.6 | 2.36 | 3.2% | 83% | 14.4 | 4.78 | 0.23 | 0.0000 | 0.000 |
| down_streak | single_stocks | vix_above_20 | BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 940 | 97.6 | 69.1 | 1.91 | 3.7% | 90% | 71.0 | 4.71 | 3.33 | 0.0000 | 0.000 |
| hand:ibs_mean_reversion | paper_45 | vix_above_20 | hand-written ibs_mean_reversion (seeded parameters) | 4368 | 71.7 | 42.9 | 1.70 | 10.0% | 90% | 50.0 | 4.67 | 3.21 | 0.0000 | 0.000 |
| weekly_reversal | sector_etfs | spy_sma50 | BUY when ret_5 < -0.05; exit when dist_sma_5 > 0 | 142 | 109.5 | 77.2 | 2.95 | 2.3% | 71% | 97.7 | 4.63 | 4.02 | 0.0010 | 1.000 |
| down_streak | index_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and down_streak >= 5; exit when dist_sma_5 > 0 | 32 | 130.9 | 102.6 | 12.38 | 1.2% | 85% | 122.1 | 4.60 | 4.20 | 0.0050 | 1.000 |
| earnings_drift | single_stocks | adx_not_trending | BUY when earn_surprise > 0 | 716 | 257.5 | 230.2 | 1.97 | 6.5% | 83% | -33.3 | 4.56 | -0.62 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | spy_sma10 | BUY when at_high_100 == 1; exit when dist_sma_50 < 0 | 1018 | 216.0 | 184.3 | 1.82 | 7.2% | 88% | -34.0 | 4.55 | -0.81 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | spy_sma50 | BUY when at_high_100 == 1; exit when dist_sma_50 < 0 | 1010 | 225.9 | 194.9 | 1.86 | 7.5% | 83% | -24.1 | 4.52 | -0.54 | 0.0000 | 0.000 |
| earnings_drift | single_stocks | adx_range | BUY when earn_surprise > 0 | 454 | 191.0 | 164.6 | 2.06 | 4.2% | 79% | 38.0 | 4.47 | 0.90 | 0.0000 | 0.000 |
| earnings_drift | single_stocks | adx_not_trending | BUY when dist_sma_200 > 0 and earn_surprise > 0 | 525 | 240.2 | 214.1 | 1.94 | 4.1% | 79% | -63.8 | 4.44 | -1.23 | 0.0000 | 0.000 |
| down_streak | single_stocks | none | BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 2138 | 48.4 | 20.4 | 1.47 | 4.4% | 96% | 19.9 | 4.39 | 1.74 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | spy_sma20 | BUY when at_high_100 == 1; exit when dist_sma_50 < 0 | 1029 | 213.3 | 182.5 | 1.80 | 7.2% | 83% | -35.0 | 4.38 | -0.81 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | adx_not_trending | BUY when at_high_50 == 1; exit when dist_sma_50 < 0 | 1280 | 135.7 | 107.9 | 1.61 | 6.3% | 88% | -5.8 | 4.37 | -0.19 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | spy_sma50 | BUY when at_high_100 == 1; exit when dist_sma_50 < 0 | 1397 | 137.6 | 106.7 | 1.64 | 7.2% | 83% | -15.7 | 4.36 | -0.53 | 0.0000 | 0.000 |
| calm_market_dip | index_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 5; exit when dist_sma_5 > 0 | 60 | 95.2 | 66.9 | 5.71 | 2.1% | 88% | 85.5 | 4.33 | 3.82 | 0.0040 | 1.000 |
| breakout_momentum | single_stocks | none | BUY when at_high_100 == 1; exit when dist_sma_50 < 0 | 1068 | 203.1 | 171.6 | 1.76 | 7.2% | 88% | -42.1 | 4.33 | -1.01 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | adx_not_downtrend | BUY when at_high_100 == 1; exit when dist_sma_50 < 0 | 1068 | 203.0 | 171.5 | 1.76 | 7.2% | 88% | -42.2 | 4.32 | -1.01 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | adx_range | BUY when at_high_50 == 1 and vol_ratio_20_100 < 1; exit when dist_sma_50 < 0 | 638 | 172.5 | 144.5 | 1.82 | 3.4% | 88% | 30.6 | 4.32 | 0.80 | 0.0000 | 0.000 |

## Top 15 trials (≥100 trades) by t of excess over the symbol's own causal drift

| Family | Universe | Gate | Rule | Trades | Net bps @11 | Net bps @25 | PF | Max DD | Windows + | Excess bps | t (net) | t (excess) | boot p | Bonf. p |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| calm_market_dip | index_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 10; exit when dist_sma_5 >  | 105 | 81.0 | 52.8 | 4.36 | 2.2% | 78% | 70.5 | 4.81 | 4.08 | 0.0000 | 0.000 |
| weekly_reversal | sector_etfs | spy_sma50 | BUY when ret_5 < -0.05; exit when dist_sma_5 > 0 | 142 | 109.5 | 77.2 | 2.95 | 2.3% | 71% | 97.7 | 4.63 | 4.02 | 0.0010 | 1.000 |
| down_streak | single_stocks | vix_above_20 | BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 940 | 97.6 | 69.1 | 1.91 | 3.7% | 90% | 71.0 | 4.71 | 3.33 | 0.0000 | 0.000 |
| calm_market_dip | sector_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 10; exit when dist_sma_5 >  | 242 | 66.1 | 38.1 | 2.32 | 2.0% | 90% | 53.6 | 4.12 | 3.22 | 0.0000 | 0.000 |
| hand:ibs_mean_reversion | paper_45 | vix_above_20 | hand-written ibs_mean_reversion (seeded parameters) | 4368 | 71.7 | 42.9 | 1.70 | 10.0% | 90% | 50.0 | 4.67 | 3.21 | 0.0000 | 0.000 |
| calm_market_dip | sector_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 5; exit when dist_sma_5 > 0 | 126 | 84.4 | 56.3 | 3.61 | 2.0% | 85% | 71.7 | 3.75 | 3.10 | 0.0010 | 1.000 |
| turn_of_month | index_etfs | vix_above_20 | BUY when tdays_left == 3; exit when tdom == 3 | 148 | 139.6 | 111.3 | 3.86 | 6.9% | 93% | 119.1 | 3.63 | 3.10 | 0.0000 | 0.000 |
| weekly_reversal | single_stocks | vix_above_20 | BUY when dist_sma_200 > 0 and ret_5 < -0.05; exit when ret_1 > 0 | 831 | 79.6 | 49.0 | 1.75 | 4.3% | 86% | 61.9 | 3.96 | 3.05 | 0.0000 | 0.000 |
| down_streak | index_etfs | vix_above_20 | BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 138 | 87.8 | 59.6 | 2.91 | 5.6% | 79% | 77.5 | 3.46 | 3.01 | 0.0010 | 1.000 |
| down_streak | single_stocks | vix_above_20 | BUY when down_streak >= 5; exit when dist_sma_5 > 0 | 466 | 127.1 | 99.9 | 2.31 | 2.6% | 71% | 102.3 | 3.84 | 3.01 | 0.0000 | 0.000 |
| turn_of_month | sector_etfs | vix_above_20 | BUY when tdays_left == 3; exit when tdom == 3 | 436 | 134.2 | 106.0 | 2.89 | 8.9% | 86% | 113.6 | 3.50 | 2.97 | 0.0000 | 0.000 |
| down_streak | sector_etfs | vix_above_20 | BUY when down_streak >= 5; exit when dist_sma_5 > 0 | 214 | 90.9 | 63.5 | 2.56 | 3.3% | 90% | 80.5 | 3.32 | 2.88 | 0.0010 | 1.000 |
| turn_of_month | sector_etfs | vix_above_20 | BUY when tdays_left == 2; exit when tdom == 3 | 448 | 114.1 | 85.8 | 2.65 | 6.7% | 94% | 96.2 | 3.30 | 2.78 | 0.0020 | 1.000 |
| weekly_reversal | single_stocks | vix_above_20 | BUY when ret_5 < -0.05; exit when ret_1 > 0 | 2021 | 65.4 | 37.0 | 1.54 | 12.5% | 90% | 48.4 | 3.78 | 2.78 | 0.0000 | 0.000 |
| weekly_reversal | single_stocks | vix_above_20 | BUY when ret_5 < -0.08; exit when ret_1 > 0 | 943 | 86.1 | 58.5 | 1.59 | 12.3% | 81% | 68.7 | 3.34 | 2.69 | 0.0120 | 1.000 |

## Top 15 paper-executable trials (close-timed, ≥100 trades) by net t

| Family | Universe | Gate | Rule | Trades | Net bps @11 | Net bps @25 | PF | Max DD | Windows + | Excess bps | t (net) | t (excess) | boot p | Bonf. p |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| earnings_drift | single_stocks | adx_range | BUY when earn_surprise > 0 | 453 | 326.3 | 299.8 | 2.37 | 4.9% | 83% | 37.4 | 5.06 | 0.60 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | adx_range | BUY when at_high_50 == 1; exit when dist_sma_50 < 0 | 892 | 174.3 | 146.4 | 1.82 | 4.3% | 88% | 34.4 | 4.81 | 0.99 | 0.0000 | 0.000 |
| calm_market_dip | index_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 10; exit when dist_sma_5 >  | 105 | 81.0 | 52.8 | 4.36 | 2.2% | 78% | 70.5 | 4.81 | 4.08 | 0.0000 | 0.000 |
| earnings_drift | single_stocks | adx_range | BUY when dist_sma_200 > 0 and earn_surprise > 0 | 324 | 313.7 | 287.6 | 2.36 | 3.2% | 83% | 14.4 | 4.78 | 0.23 | 0.0000 | 0.000 |
| down_streak | single_stocks | vix_above_20 | BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 940 | 97.6 | 69.1 | 1.91 | 3.7% | 90% | 71.0 | 4.71 | 3.33 | 0.0000 | 0.000 |
| hand:ibs_mean_reversion | paper_45 | vix_above_20 | hand-written ibs_mean_reversion (seeded parameters) | 4368 | 71.7 | 42.9 | 1.70 | 10.0% | 90% | 50.0 | 4.67 | 3.21 | 0.0000 | 0.000 |
| weekly_reversal | sector_etfs | spy_sma50 | BUY when ret_5 < -0.05; exit when dist_sma_5 > 0 | 142 | 109.5 | 77.2 | 2.95 | 2.3% | 71% | 97.7 | 4.63 | 4.02 | 0.0010 | 1.000 |
| earnings_drift | single_stocks | adx_not_trending | BUY when earn_surprise > 0 | 716 | 257.5 | 230.2 | 1.97 | 6.5% | 83% | -33.3 | 4.56 | -0.62 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | spy_sma10 | BUY when at_high_100 == 1; exit when dist_sma_50 < 0 | 1018 | 216.0 | 184.3 | 1.82 | 7.2% | 88% | -34.0 | 4.55 | -0.81 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | spy_sma50 | BUY when at_high_100 == 1; exit when dist_sma_50 < 0 | 1010 | 225.9 | 194.9 | 1.86 | 7.5% | 83% | -24.1 | 4.52 | -0.54 | 0.0000 | 0.000 |
| earnings_drift | single_stocks | adx_range | BUY when earn_surprise > 0 | 454 | 191.0 | 164.6 | 2.06 | 4.2% | 79% | 38.0 | 4.47 | 0.90 | 0.0000 | 0.000 |
| earnings_drift | single_stocks | adx_not_trending | BUY when dist_sma_200 > 0 and earn_surprise > 0 | 525 | 240.2 | 214.1 | 1.94 | 4.1% | 79% | -63.8 | 4.44 | -1.23 | 0.0000 | 0.000 |
| down_streak | single_stocks | none | BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 2138 | 48.4 | 20.4 | 1.47 | 4.4% | 96% | 19.9 | 4.39 | 1.74 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | spy_sma20 | BUY when at_high_100 == 1; exit when dist_sma_50 < 0 | 1029 | 213.3 | 182.5 | 1.80 | 7.2% | 83% | -35.0 | 4.38 | -0.81 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | adx_not_trending | BUY when at_high_50 == 1; exit when dist_sma_50 < 0 | 1280 | 135.7 | 107.9 | 1.61 | 6.3% | 88% | -5.8 | 4.37 | -0.19 | 0.0000 | 0.000 |

## The bot's hand-written strategies on the 45-symbol paper universe (OOS 2015 →)

| Strategy | Gate | Trades | Net bps @11 | Net bps @25 | PF | Max DD | Windows + | Excess bps | t (net) | t @25 | t (excess) | $ / trade, 1 share | Last 6m: trades / bps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| rsi2_pullback | none | 4120 | 16.0 | -13.4 | 1.16 | 11.8% | 62% | -9.2 | 1.29 | -1.05 | -0.73 | 0.17 | 191 / 18.2 |
| rsi2_pullback | best: adx_trend_up | 886 | 34.6 | 4.7 | 1.43 | 2.0% | 54% | 8.2 | 2.48 | 0.33 | 0.57 | 0.27 | 38 / 19.5 |
| ibs_mean_reversion | none | 10088 | 28.7 | 0.3 | 1.29 | 11.1% | 71% | 4.8 | 3.46 | 0.03 | 0.56 | 0.35 | 423 / 3.2 |
| ibs_mean_reversion | best: vix_above_20 | 4368 | 71.7 | 42.9 | 1.70 | 10.0% | 90% | 50.0 | 4.67 | 2.80 | 3.21 | 0.89 | 57 / 71.5 |
| rsi_mean_reversion | none | 3872 | 39.9 | 12.3 | 1.22 | 10.5% | 83% | 2.1 | 2.06 | 0.65 | 0.12 | 0.59 | 159 / 48.0 |
| rsi_mean_reversion | best: adx_trend_up | 34 | 139.8 | 89.8 | 2.24 | 0.4% | 69% | 71.6 | 2.25 | 1.33 | 1.21 | 1.57 | 2 / 588.3 |
| bollinger_mean_reversion | none | 2817 | 50.0 | 21.2 | 1.28 | 8.3% | 83% | 12.0 | 2.28 | 1.00 | 0.57 | 0.63 | 84 / 36.6 |
| bollinger_mean_reversion | best: none | 2817 | 50.0 | 21.2 | 1.28 | 8.3% | 83% | 12.0 | 2.28 | 1.00 | 0.57 | 0.63 | 84 / 36.6 |
| channel_breakout | none | 3018 | 24.9 | -4.8 | 1.14 | 6.6% | 67% | -36.1 | 1.92 | -0.38 | -2.93 | 0.25 | 124 / -54.5 |
| channel_breakout | best: adx_range | 1137 | 44.8 | 13.3 | 1.27 | 1.9% | 67% | -19.1 | 2.73 | 0.81 | -1.21 | 0.45 | 50 / -24.6 |
| trend_pullback | none | 4935 | 17.0 | -10.9 | 1.09 | 12.1% | 58% | -36.9 | 1.22 | -0.80 | -2.86 | 0.05 | 239 / -0.5 |
| trend_pullback | best: spy_sma10 | 3153 | 30.3 | 3.3 | 1.18 | 6.3% | 50% | -28.3 | 2.28 | 0.25 | -2.25 | 0.22 | 177 / 13.2 |
| moving_average_crossover | none | 9410 | 16.6 | -13.9 | 1.09 | 19.6% | 67% | -35.6 | 1.44 | -1.23 | -3.31 | -0.01 | 505 / -45.5 |
| moving_average_crossover | best: spy_sma10 | 7008 | 18.5 | -10.5 | 1.10 | 15.4% | 58% | -38.7 | 1.62 | -0.95 | -3.60 | -0.06 | 382 / -56.2 |
| macd_momentum | none | 7283 | 30.4 | 0.9 | 1.16 | 13.3% | 67% | -18.3 | 2.28 | 0.07 | -1.40 | 0.23 | 357 / -37.1 |
| macd_momentum | best: adx_range | 3312 | 49.1 | 17.9 | 1.28 | 5.4% | 75% | -1.7 | 3.04 | 1.12 | -0.11 | 0.26 | 183 / -7.0 |
| ensemble | none | 5076 | 22.8 | -5.5 | 1.12 | 12.3% | 67% | -25.7 | 1.68 | -0.41 | -1.94 | -0.04 | 295 / -46.2 |
| ensemble | best: vix_above_20 | 2301 | 51.9 | 26.3 | 1.28 | 9.8% | 71% | 11.4 | 2.32 | 1.18 | 0.52 | 0.22 | 33 / -61.4 |

## Families (11 bps, trials with ≥1 OOS trade)

| Family | Trials | Median net bps @11 | @25 | Trials > 0 @11 | @25 | Excess > 0 | Best t | Median windows + |
|---|---|---|---|---|---|---|---|---|
| gap_down_recovery | 240 | -15.8 | -43.7 | 20% | 6% | 11% | 7.83 | 47% |
| weekly_reversal | 342 | 33.5 | 5.2 | 91% | 57% | 71% | 5.90 | 71% |
| earnings_drift | 240 | 177.2 | 151.0 | 100% | 100% | 15% | 5.06 | 71% |
| breakout_momentum | 360 | 66.5 | 38.3 | 98% | 86% | 11% | 4.81 | 62% |
| calm_market_dip | 120 | 9.7 | -18.3 | 72% | 17% | 36% | 4.81 | 58% |
| down_streak | 360 | 12.0 | -16.0 | 67% | 24% | 42% | 4.71 | 62% |
| hand:ibs_mean_reversion | 10 | 22.5 | -5.5 | 100% | 20% | 20% | 4.67 | 69% |
| turn_of_month | 360 | 7.8 | -20.5 | 62% | 20% | 16% | 3.63 | 50% |
| bollinger_low | 240 | 23.1 | -5.5 | 79% | 42% | 41% | 3.22 | 63% |
| sector_reversal | 80 | 22.1 | -6.8 | 99% | 32% | 8% | 3.06 | 62% |
| hand:macd_momentum | 10 | 31.9 | 2.0 | 100% | 80% | 10% | 3.04 | 69% |
| pullback_in_uptrend | 240 | 2.6 | -25.4 | 56% | 12% | 21% | 3.02 | 58% |
| hand:channel_breakout | 10 | 29.6 | -1.3 | 90% | 40% | 0% | 2.73 | 66% |
| n_day_low | 360 | -0.3 | -28.2 | 50% | 7% | 15% | 2.73 | 58% |
| hand:rsi2_pullback | 10 | 15.4 | -13.5 | 100% | 20% | 20% | 2.48 | 62% |
| hand:ensemble | 10 | 21.8 | -5.9 | 100% | 20% | 10% | 2.32 | 58% |
| hand:bollinger_mean_reversion | 10 | 43.4 | 13.6 | 90% | 90% | 50% | 2.28 | 65% |
| hand:trend_pullback | 10 | 18.1 | -10.2 | 100% | 10% | 0% | 2.28 | 55% |
| hand:rsi_mean_reversion | 10 | 39.9 | 12.1 | 100% | 60% | 30% | 2.25 | 70% |
| hand:moving_average_crossover | 10 | 18.2 | -12.4 | 100% | 0% | 0% | 1.62 | 58% |
| opening_gap_fill | 54 | -24.1 | -52.3 | 0% | 0% | 0% | -1.33 | 0% |

## Gates: paired change in net bps per trade against the ungated rule (11 bps, close-timed trials)

| Gate | Rules | Median Δ bps | Mean Δ bps | Rules improved | Trades kept (median) | Median Δ max DD | t improved |
|---|---|---|---|---|---|---|---|
| adx_trend_up | 297 | -0.9 | -10.8 | 49% | 18% | -6.0 pts | 31% |
| spy_sma10 | 299 | -7.8 | -10.6 | 31% | 40% | -3.3 pts | 30% |
| spy_sma20 | 305 | -6.7 | -5.8 | 29% | 50% | -3.1 pts | 26% |
| spy_sma50 | 305 | -2.6 | 2.2 | 42% | 66% | -2.3 pts | 40% |
| vix_below_20 | 301 | -11.1 | -11.6 | 18% | 68% | -1.4 pts | 16% |
| vix_above_20 | 305 | 25.1 | 34.3 | 86% | 41% | -1.7 pts | 67% |
| adx_not_trending | 305 | -3.2 | 1.8 | 38% | 67% | -2.8 pts | 36% |
| adx_not_downtrend | 305 | -1.1 | 0.0 | 34% | 85% | -1.4 pts | 31% |
| adx_range | 305 | -6.0 | 3.9 | 35% | 46% | -4.2 pts | 34% |

## Walk-forward selection: pick the best in-sample variant, trade it the next 6 months (11 bps)

| Fit window | Selection pool | Windows picked | In-sample bps (promised) | Next-window bps (realised) | OOS trades | Windows + |
|---|---|---|---|---|---|---|
| anchored | all close-timed trials | 24 | 496.4 | 197.3 | 92 | 61% |
| anchored | per family × universe (38 pools, trade-weighted) | 24 | 83.4 | 26.6 | 32124 | 63% |
| 6m | all close-timed trials | 24 | 715.4 | 161.0 | 328 | 62% |
| 6m | per family × universe (38 pools, trade-weighted) | 23 | 153.4 | 25.2 | 41877 | 63% |

### Global pick per window, anchored fit

| Window | Pick | In-sample bps | OOS trades | OOS bps |
|---|---|---|---|---|
| 2015H1 | 90a14f88d3:spy_sma10 | 591.7 | 6 | -33.1 |
| 2015H2 | 05cdcbebfd:vix_above_20 | 590.5 | 0 | n/a |
| 2016H1 | 05cdcbebfd:vix_above_20 | 590.5 | 4 | -152.2 |
| 2016H2 | 05cdcbebfd:vix_above_20 | 552.4 | 1 | -192.2 |
| 2017H1 | 05cdcbebfd:vix_above_20 | 543.0 | 0 | n/a |
| 2017H2 | 05cdcbebfd:vix_above_20 | 543.0 | 0 | n/a |
| 2018H1 | 05cdcbebfd:vix_above_20 | 543.0 | 1 | -357.3 |
| 2018H2 | 05cdcbebfd:vix_above_20 | 531.7 | 1 | -1009.9 |
| 2019H1 | 05cdcbebfd:vix_above_20 | 512.7 | 0 | n/a |
| 2019H2 | 05cdcbebfd:vix_above_20 | 512.7 | 0 | n/a |
| 2020H1 | 05cdcbebfd:vix_above_20 | 512.7 | 5 | 327.5 |
| 2020H2 | 05cdcbebfd:vix_above_20 | 501.9 | 27 | 264.1 |
| 2021H1 | 060aa8a70d:adx_not_trending | 466.7 | 19 | 85.1 |
| 2021H2 | 768b7c4c93:adx_not_trending | 455.0 | 5 | 357.1 |
| 2022H1 | 768b7c4c93:adx_not_trending | 451.3 | 3 | -307.7 |
| 2022H2 | 768b7c4c93:adx_range | 435.8 | 4 | 138.0 |
| 2023H1 | 768b7c4c93:adx_not_trending | 426.1 | 4 | -311.0 |
| 2023H2 | 768b7c4c93:adx_range | 420.9 | 3 | 591.1 |
| 2024H1 | 768b7c4c93:adx_range | 427.1 | 1 | 900.7 |
| 2024H2 | 768b7c4c93:adx_range | 432.7 | 2 | 1114.3 |
| 2025H1 | 768b7c4c93:adx_range | 448.5 | 1 | 2611.4 |
| 2025H2 | 768b7c4c93:adx_range | 473.4 | 3 | 527.4 |
| 2026H1 | 768b7c4c93:adx_range | 475.2 | 0 | n/a |
| 2026H2 | 768b7c4c93:adx_range | 475.2 | 2 | 432.3 |

### Global pick per window, 6m fit

| Window | Pick | In-sample bps | OOS trades | OOS bps |
|---|---|---|---|---|
| 2015H1 | e021f66e7b:adx_range | 455.1 | 18 | 144.0 |
| 2015H2 | 05cdcbebfd:adx_not_trending | 479.9 | 5 | -753.1 |
| 2016H1 | fabab1f5c0:vix_above_20 | 655.8 | 15 | -156.1 |
| 2016H2 | e038b8cfbe:vix_above_20 | 509.6 | 1 | -192.2 |
| 2017H1 | 060aa8a70d:vix_below_20 | 872.3 | 12 | 137.8 |
| 2017H2 | b5ea73f43b:adx_range | 792.9 | 10 | 320.6 |
| 2018H1 | 40ecdfdd25:adx_trend_up | 408.9 | 4 | -309.7 |
| 2018H2 | 060aa8a70d:adx_not_trending | 579.3 | 4 | -501.5 |
| 2019H1 | 6470c32867:spy_sma50 | 719.5 | 18 | 311.6 |
| 2019H2 | e038b8cfbe:adx_not_trending | 508.4 | 40 | 318.1 |
| 2020H1 | 016321bc97:adx_trend_up | 957.6 | 23 | 288.4 |
| 2020H2 | 3e9fd1b29a:vix_above_20 | 1077.7 | 46 | 383.0 |
| 2021H1 | 1e0728b6a9:none | 935.6 | 12 | 168.2 |
| 2021H2 | 40ecdfdd25:adx_trend_up | 474.9 | 14 | -154.1 |
| 2022H1 | 5c8137a50e:adx_trend_up | 517.4 | 5 | -359.0 |
| 2022H2 | 327e6b159a:adx_trend_up | 169.6 | 22 | -10.0 |
| 2023H1 | 851e831a23:vix_above_20 | 467.7 | 1 | 1615.1 |
| 2023H2 | 6470c32867:adx_trend_up | 1022.5 | 7 | 653.9 |
| 2024H1 | 54f0e5819b:spy_sma10 | 742.5 | 12 | 216.8 |
| 2024H2 | 016321bc97:adx_range | 1322.8 | 14 | -272.2 |
| 2025H1 | 54f0e5819b:vix_below_20 | 1214.0 | 8 | 467.1 |
| 2025H2 | 6470c32867:adx_not_downtrend | 784.3 | 15 | 147.8 |
| 2026H1 | 40ecdfdd25:vix_below_20 | 735.4 | 19 | 150.6 |
| 2026H2 | b47e404774:adx_trend_up | 766.4 | 3 | 227.3 |

## What a 6-month lookback would pick today (2026-05 → 2026-11) vs its full walk-forward record (11 bps)

| Trial | Family | Universe | 6m trades | 6m net bps | WF 2015→ net bps | WF trades | Windows + | WF t | 2000–14 bps |
|---|---|---|---|---|---|---|---|---|---|
| e038b8cfbe:adx_range | earnings_drift | single_stocks | 14 | 418.8 | 326.3 | 453 | 83% | 5.06 | 242.4 |
| 69c981b2e7:vix_above_20 | turn_of_month | single_stocks | 29 | 367.9 | 132.8 | 1102 | 75% | 2.24 | -27.4 |
| f0a40cf349:vix_above_20 | bollinger_low | single_stocks | 10 | 366.7 | 45.3 | 1008 | 86% | 1.10 | 50.4 |
| b9232b8ef2:vix_above_20 | turn_of_month | single_stocks | 29 | 359.6 | 122.5 | 1102 | 88% | 2.94 | -20.1 |
| 67a9a25901:vix_above_20 | pullback_in_uptrend | single_stocks | 11 | 318.5 | 39.9 | 1142 | 71% | 2.41 | 4.8 |
| f9df434caa:adx_range | bollinger_low | single_stocks | 14 | 258.3 | 46.2 | 442 | 71% | 1.97 | 7.8 |
| 5befd53477:vix_above_20 | n_day_low | single_stocks | 16 | 256.4 | 31.9 | 1624 | 76% | 2.43 | 8.0 |
| e6e618a4f5:vix_above_20 | n_day_low | single_stocks | 11 | 256.2 | 42.4 | 1317 | 81% | 1.74 | 35.8 |
| c6a8ef20d1:adx_not_trending | down_streak | single_stocks | 14 | 246.7 | 31.8 | 326 | 67% | 1.74 | 2.9 |
| 339ee59127:adx_trend_up | turn_of_month | single_stocks | 43 | 240.6 | 23.0 | 1037 | 58% | 0.76 | 48.2 |
| bollinger_mean_reversion:vix_above_20 | hand:bollinger_mean_reversion | paper_45 | 17 | 240.0 | 57.0 | 1788 | 76% | 1.82 | -9.9 |
| 31e87e6fa9:vix_above_20 | pullback_in_uptrend | single_stocks | 12 | 231.1 | 45.2 | 1032 | 76% | 1.56 | 45.4 |
| 3d4cbc8732:vix_above_20 | turn_of_month | sector_etfs | 12 | 229.5 | 114.1 | 448 | 94% | 3.30 | -5.7 |
| 75272aef62:vix_above_20 | pullback_in_uptrend | single_stocks | 15 | 228.2 | 34.7 | 1222 | 76% | 2.24 | 10.2 |
| ea55f320b1:vix_above_20 | turn_of_month | sector_etfs | 12 | 227.9 | 144.2 | 448 | 75% | 2.53 | -29.1 |

## Event check (OOS trades of ungated close-timed trials, 11 bps): net bps with vs without an event inside the holding window

| Event | Trades with event | Net bps with | Trades without | Net bps without | Trials where 'with' beats 'without' |
|---|---|---|---|---|---|
| earnings_held | 23571 | 101.1 | 392709 | 19.3 | 44% |
| fomc_held | 80289 | 86.1 | 335991 | 9.1 | 43% |
| cpi_held | 90236 | 82.6 | 326044 | 7.7 | 36% |
| jobs_held | 133437 | 53.2 | 282843 | 10.1 | 30% |
| macro_held | 210639 | 34.6 | 205641 | 13.0 | 28% |
| earnings_ahead | 48517 | 51.4 | 367763 | 20.3 | 72% |
| macro_ahead | 358195 | 29.4 | 58085 | -9.6 | 73% |

### Causal event filters on the top paper-executable trials and the hand-written strategies (11 bps)

| Trial | Trades | Net bps | t | Skip-earnings trades | bps | t | Skip-macro trades | bps | t | Only-macro trades | bps |
|---|---|---|---|---|---|---|---|---|---|---|---|
| e038b8cfbe:adx_range | 453 | 326.3 | 5.06 | 453 | 326.3 | 5.06 | 0 | n/a | n/a | 453 | 326.3 |
| 6b6bf1cd5c:adx_range | 892 | 174.3 | 4.81 | 570 | 135.0 | 3.57 | 1 | -152.6 | n/a | 891 | 174.7 |
| 4ec6c68526:vix_above_20 | 105 | 81.0 | 4.81 | 105 | 81.0 | 4.81 | 23 | 111.0 | 3.52 | 82 | 72.6 |
| b5ea73f43b:adx_range | 324 | 313.7 | 4.78 | 324 | 313.7 | 4.78 | 0 | n/a | n/a | 324 | 313.7 |
| 7df4090f31:vix_above_20 | 940 | 97.6 | 4.71 | 810 | 88.4 | 4.57 | 115 | 22.2 | 0.62 | 825 | 108.1 |
| ibs_mean_reversion:vix_above_20 | 4368 | 71.7 | 4.67 | 3986 | 69.2 | 4.60 | 975 | 65.6 | 1.54 | 3393 | 73.4 |
| 213239cdf2:spy_sma50 | 142 | 109.5 | 4.63 | 142 | 109.5 | 4.63 | 30 | 163.7 | 5.09 | 112 | 95.0 |
| e038b8cfbe:adx_not_trending | 716 | 257.5 | 4.56 | 716 | 257.5 | 4.56 | 0 | n/a | n/a | 716 | 257.5 |
| rsi2_pullback:none | 4120 | 16.0 | 1.29 | 3719 | 16.4 | 1.35 | 770 | -10.3 | -0.36 | 3350 | 22.0 |
| ibs_mean_reversion:none | 10088 | 28.7 | 3.46 | 9147 | 25.5 | 3.18 | 2060 | 11.3 | 0.46 | 8028 | 33.1 |
| rsi_mean_reversion:none | 3872 | 39.9 | 2.06 | 3127 | 38.3 | 1.84 | 4 | 20.5 | n/a | 3868 | 39.9 |
| bollinger_mean_reversion:none | 2817 | 50.0 | 2.28 | 2279 | 41.8 | 1.79 | 6 | -4.0 | n/a | 2811 | 50.1 |
| channel_breakout:none | 3018 | 24.9 | 1.92 | 2336 | 20.7 | 1.44 | 10 | -28.1 | n/a | 3008 | 25.1 |
| trend_pullback:none | 4935 | 17.0 | 1.22 | 3908 | 21.0 | 1.41 | 8 | 54.0 | n/a | 4927 | 16.9 |
| moving_average_crossover:none | 9410 | 16.6 | 1.44 | 7383 | 15.1 | 1.22 | 29 | -99.7 | n/a | 9381 | 16.9 |
| macd_momentum:none | 7283 | 30.4 | 2.28 | 5526 | 25.3 | 1.80 | 21 | -14.5 | n/a | 7262 | 30.5 |
| ensemble:none | 5076 | 22.8 | 1.68 | 4035 | 22.7 | 1.50 | 14 | -53.6 | n/a | 5062 | 23.0 |

## Per-symbol view of the top three paper-executable trials (11 bps)


**e038b8cfbe:adx_range** (BUY when earn_surprise > 0): 28 of 29 symbols positive; median symbol windows+ 67%

| Symbol | Trades | Net bps | PF | Max DD | Windows + | Excess bps |
|---|---|---|---|---|---|---|
| AMD | 11 | 1283.5 | 4.14 | 23.7% | 67% | 941.9 |
| AAPL | 14 | 964.3 | 9.38 | 20.6% | 85% | 503.2 |
| LLY | 12 | 741.8 | 5.43 | 18.6% | 70% | 561.6 |
| ADBE | 14 | 556.9 | 3.77 | 18.3% | 73% | 216.6 |
| BAC | 14 | 534.6 | 2.76 | 28.3% | 60% | 340.2 |
| MSFT | 17 | 530.6 | 4.36 | 16.7% | 73% | 356.0 |
| JPM | 15 | 511.7 | 3.52 | 18.3% | 77% | 287.3 |
| NFLX | 12 | 504.7 | 2.60 | 33.9% | 45% | -86.1 |
| XOM | 18 | 470.1 | 3.28 | 24.7% | 73% | 318.6 |
| AMZN | 12 | 465.7 | 2.84 | 19.7% | 80% | 71.1 |
| COST | 13 | 463.2 | 9.01 | 13.9% | 100% | 221.4 |
| NVDA | 16 | 453.2 | 2.35 | 40.9% | 60% | -77.8 |
| ABBV | 14 | 419.2 | 2.60 | 19.5% | 67% | 97.1 |
| HD | 17 | 391.8 | 3.13 | 20.5% | 80% | 208.2 |
| META | 17 | 377.4 | 3.25 | 19.8% | 77% | -70.6 |
| GOOGL | 10 | 358.6 | 3.10 | 13.3% | 67% | -37.6 |
| ORCL | 15 | 297.5 | 2.63 | 15.9% | 67% | 115.0 |
| MA | 17 | 180.7 | 1.76 | 37.3% | 79% | -301.9 |
| AVGO | 18 | 173.0 | 1.51 | 22.0% | 62% | -337.4 |
| MRK | 17 | 155.7 | 1.68 | 18.2% | 57% | 33.7 |
| JNJ | 20 | 140.4 | 1.71 | 18.7% | 60% | -8.0 |
| V | 18 | 132.9 | 1.49 | 43.1% | 67% | -219.2 |
| WMT | 16 | 103.3 | 1.44 | 16.5% | 54% | -15.8 |
| PEP | 21 | 78.1 | 1.39 | 17.1% | 59% | -89.6 |
| UNH | 18 | 47.0 | 1.21 | 21.8% | 57% | -329.0 |
| KO | 21 | 41.2 | 1.27 | 24.9% | 58% | -85.7 |
| PG | 23 | 13.3 | 1.05 | 21.3% | 56% | -112.6 |
| CRM | 16 | 8.3 | 1.02 | 24.6% | 58% | -391.2 |
| TSLA | 7 | -76.2 | 0.91 | 31.4% | 43% | -594.9 |

**6b6bf1cd5c:adx_range** (BUY when at_high_50 == 1; exit when dist_sma_50 < 0): 25 of 29 symbols positive; median symbol windows+ 56%

| Symbol | Trades | Net bps | PF | Max DD | Windows + | Excess bps |
|---|---|---|---|---|---|---|
| AMD | 23 | 925.6 | 4.15 | 35.2% | 56% | 758.9 |
| NVDA | 36 | 749.3 | 3.98 | 23.2% | 81% | 486.0 |
| NFLX | 19 | 556.6 | 3.10 | 19.7% | 67% | 235.6 |
| TSLA | 21 | 511.2 | 2.28 | 35.0% | 47% | 200.5 |
| LLY | 30 | 463.8 | 3.67 | 15.8% | 79% | 386.7 |
| AVGO | 34 | 309.4 | 2.36 | 26.5% | 55% | 54.3 |
| ABBV | 29 | 290.9 | 2.69 | 14.8% | 71% | 124.0 |
| AAPL | 25 | 215.7 | 2.21 | 13.6% | 69% | 14.3 |
| COST | 25 | 198.6 | 2.46 | 13.6% | 71% | 93.8 |
| CRM | 28 | 187.5 | 1.67 | 38.4% | 53% | -22.0 |
| META | 33 | 187.3 | 1.72 | 28.1% | 50% | -11.7 |
| BAC | 30 | 176.1 | 1.85 | 27.0% | 67% | 80.4 |
| GOOGL | 32 | 172.7 | 1.94 | 25.3% | 58% | -7.3 |
| JPM | 38 | 154.5 | 1.90 | 24.6% | 60% | 52.2 |
| ADBE | 29 | 148.1 | 1.98 | 16.9% | 53% | -20.2 |
| V | 37 | 121.0 | 1.90 | 31.5% | 57% | -64.3 |
| XOM | 29 | 99.8 | 1.48 | 28.9% | 47% | 32.4 |
| MSFT | 39 | 96.5 | 1.47 | 18.3% | 60% | 25.5 |
| AMZN | 36 | 85.2 | 1.33 | 33.0% | 45% | -98.2 |
| HD | 27 | 76.7 | 1.50 | 14.6% | 65% | -10.1 |
| JNJ | 32 | 55.5 | 1.36 | 27.0% | 47% | -7.8 |
| PEP | 25 | 42.5 | 1.33 | 11.9% | 50% | -26.9 |
| ORCL | 30 | 19.2 | 1.07 | 34.9% | 48% | -45.0 |
| PG | 35 | 16.8 | 1.11 | 22.3% | 40% | -35.2 |
| MA | 35 | 3.0 | 1.01 | 42.3% | 56% | -228.7 |
| WMT | 30 | -26.3 | 0.88 | 26.1% | 56% | -72.0 |
| MRK | 37 | -35.3 | 0.85 | 29.6% | 35% | -81.4 |
| KO | 34 | -103.1 | 0.53 | 38.0% | 42% | -149.8 |
| UNH | 34 | -132.9 | 0.52 | 47.8% | 37% | -271.3 |

**4ec6c68526:vix_above_20** (BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 10; exit when dis): 4 of 4 symbols positive; median symbol windows+ 85%

| Symbol | Trades | Net bps | PF | Max DD | Windows + | Excess bps |
|---|---|---|---|---|---|---|
| IWM | 21 | 91.2 | 4.03 | 7.2% | 78% | 79.7 |
| SPY | 31 | 90.1 | 9.87 | 3.5% | 100% | 81.4 |
| QQQ | 25 | 90.0 | 3.72 | 5.6% | 79% | 78.8 |
| DIA | 28 | 55.2 | 3.04 | 5.7% | 92% | 44.0 |

## Shortlist: close-timed, ≥100 trades, windows+ ≥60%, excess over drift >0, t≥2 @11 and t≥1.5 @25

| Family | Universe | Gate | Rule | Trades | Net bps @11 | Net bps @25 | PF | Max DD | Windows + | Excess bps | t (net) | t (excess) | boot p | Bonf. p |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| earnings_drift | single_stocks | adx_range | BUY when earn_surprise > 0 | 453 | 326.3 | 299.8 | 2.37 | 4.9% | 83% | 37.4 | 5.06 | 0.60 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | adx_range | BUY when at_high_50 == 1; exit when dist_sma_50 < 0 | 892 | 174.3 | 146.4 | 1.82 | 4.3% | 88% | 34.4 | 4.81 | 0.99 | 0.0000 | 0.000 |
| calm_market_dip | index_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 10; exit when dist_sma_5 >  | 105 | 81.0 | 52.8 | 4.36 | 2.2% | 78% | 70.5 | 4.81 | 4.08 | 0.0000 | 0.000 |
| earnings_drift | single_stocks | adx_range | BUY when dist_sma_200 > 0 and earn_surprise > 0 | 324 | 313.7 | 287.6 | 2.36 | 3.2% | 83% | 14.4 | 4.78 | 0.23 | 0.0000 | 0.000 |
| down_streak | single_stocks | vix_above_20 | BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 940 | 97.6 | 69.1 | 1.91 | 3.7% | 90% | 71.0 | 4.71 | 3.33 | 0.0000 | 0.000 |
| hand:ibs_mean_reversion | paper_45 | vix_above_20 | hand-written ibs_mean_reversion (seeded parameters) | 4368 | 71.7 | 42.9 | 1.70 | 10.0% | 90% | 50.0 | 4.67 | 3.21 | 0.0000 | 0.000 |
| weekly_reversal | sector_etfs | spy_sma50 | BUY when ret_5 < -0.05; exit when dist_sma_5 > 0 | 142 | 109.5 | 77.2 | 2.95 | 2.3% | 71% | 97.7 | 4.63 | 4.02 | 0.0010 | 1.000 |
| earnings_drift | single_stocks | adx_range | BUY when earn_surprise > 0 | 454 | 191.0 | 164.6 | 2.06 | 4.2% | 79% | 38.0 | 4.47 | 0.90 | 0.0000 | 0.000 |
| down_streak | single_stocks | none | BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 2138 | 48.4 | 20.4 | 1.47 | 4.4% | 96% | 19.9 | 4.39 | 1.74 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | adx_range | BUY when at_high_50 == 1 and vol_ratio_20_100 < 1; exit when dist_sma_50 < 0 | 638 | 172.5 | 144.5 | 1.82 | 3.4% | 88% | 30.6 | 4.32 | 0.80 | 0.0000 | 0.000 |
| earnings_drift | single_stocks | adx_range | BUY when earn_surprise > 0 and ret_1 > 0.02 | 142 | 261.9 | 233.9 | 2.86 | 2.0% | 96% | 94.2 | 4.24 | 1.57 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | adx_range | BUY when at_high_100 == 1; exit when dist_sma_50 < 0 | 599 | 174.4 | 145.9 | 1.85 | 3.2% | 79% | 29.0 | 4.20 | 0.74 | 0.0000 | 0.000 |
| earnings_drift | single_stocks | adx_not_trending | BUY when earn_surprise > 10 | 214 | 360.2 | 335.5 | 2.41 | 3.3% | 83% | 41.6 | 4.18 | 0.49 | 0.0000 | 0.000 |
| calm_market_dip | sector_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 10; exit when dist_sma_5 >  | 242 | 66.1 | 38.1 | 2.32 | 2.0% | 90% | 53.6 | 4.12 | 3.22 | 0.0000 | 0.000 |
| down_streak | single_stocks | vix_above_20 | BUY when dist_sma_200 > 0 and down_streak >= 4; exit when dist_sma_5 > 0 | 450 | 74.8 | 42.5 | 1.77 | 2.3% | 81% | 49.0 | 4.01 | 2.53 | 0.0000 | 0.000 |
| earnings_drift | single_stocks | adx_range | BUY when dist_sma_200 > 0 and earn_surprise > 0 | 324 | 192.6 | 166.1 | 2.07 | 2.7% | 83% | 35.2 | 4.00 | 0.74 | 0.0000 | 0.000 |
| weekly_reversal | single_stocks | vix_above_20 | BUY when dist_sma_200 > 0 and ret_5 < -0.05; exit when ret_1 > 0 | 831 | 79.6 | 49.0 | 1.75 | 4.3% | 86% | 61.9 | 3.96 | 3.05 | 0.0000 | 0.000 |
| down_streak | single_stocks | vix_above_20 | BUY when down_streak >= 5; exit when dist_sma_5 > 0 | 466 | 127.1 | 99.9 | 2.31 | 2.6% | 71% | 102.3 | 3.84 | 3.01 | 0.0000 | 0.000 |
| weekly_reversal | single_stocks | vix_above_20 | BUY when ret_5 < -0.05; exit when ret_1 > 0 | 2021 | 65.4 | 37.0 | 1.54 | 12.5% | 90% | 48.4 | 3.78 | 2.78 | 0.0000 | 0.000 |
| calm_market_dip | sector_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 5; exit when dist_sma_5 > 0 | 126 | 84.4 | 56.3 | 3.61 | 2.0% | 85% | 71.7 | 3.75 | 3.10 | 0.0010 | 1.000 |
| earnings_drift | single_stocks | adx_range | BUY when dist_sma_200 > 0 and earn_surprise > 0 and ret_1 > 0.02 | 118 | 249.8 | 221.8 | 2.71 | 1.3% | 87% | 83.2 | 3.66 | 1.25 | 0.0000 | 0.000 |
| turn_of_month | index_etfs | vix_above_20 | BUY when tdays_left == 3; exit when tdom == 3 | 148 | 139.6 | 111.3 | 3.86 | 6.9% | 93% | 119.1 | 3.63 | 3.10 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | adx_range | BUY when at_high_100 == 1 and vol_ratio_20_100 < 1; exit when dist_sma_50 < 0 | 413 | 173.9 | 145.0 | 1.86 | 2.8% | 71% | 25.9 | 3.62 | 0.57 | 0.0010 | 1.000 |
| earnings_drift | single_stocks | adx_not_downtrend | BUY when earn_surprise > 0 and ret_1 > 0.02 | 387 | 171.9 | 144.7 | 1.79 | 5.2% | 88% | 1.1 | 3.61 | 0.02 | 0.0000 | 0.000 |
| earnings_drift | single_stocks | adx_range | BUY when earn_surprise > 10 | 119 | 411.2 | 386.1 | 2.71 | 2.4% | 74% | 88.7 | 3.60 | 0.77 | 0.0020 | 1.000 |
| down_streak | single_stocks | none | BUY when dist_sma_200 > 0 and down_streak >= 5; exit when dist_sma_5 > 0 | 528 | 65.2 | 33.4 | 1.86 | 1.5% | 75% | 37.5 | 3.59 | 2.01 | 0.0010 | 1.000 |
| earnings_drift | single_stocks | adx_range | BUY when earn_surprise > 5 | 240 | 300.8 | 275.1 | 2.11 | 4.0% | 79% | 2.3 | 3.56 | 0.03 | 0.0010 | 1.000 |
| down_streak | single_stocks | none | BUY when down_streak >= 5; exit when dist_sma_5 > 0 | 976 | 61.8 | 34.1 | 1.62 | 3.3% | 75% | 34.8 | 3.54 | 1.93 | 0.0000 | 0.000 |
| breakout_momentum | single_stocks | adx_range | BUY when at_high_252 == 1; exit when dist_sma_50 < 0 | 481 | 157.7 | 130.0 | 1.73 | 3.2% | 75% | 9.4 | 3.52 | 0.22 | 0.0020 | 1.000 |
| turn_of_month | sector_etfs | vix_above_20 | BUY when tdays_left == 3; exit when tdom == 3 | 436 | 134.2 | 106.0 | 2.89 | 8.9% | 86% | 113.6 | 3.50 | 2.97 | 0.0000 | 0.000 |
| weekly_reversal | single_stocks | none | BUY when dist_sma_200 > 0 and ret_5 < -0.08; exit when ret_1 > 0 | 537 | 80.3 | 48.2 | 1.60 | 3.5% | 79% | 60.5 | 3.48 | 2.61 | 0.0000 | 0.000 |
| weekly_reversal | sector_etfs | spy_sma50 | BUY when ret_5 < -0.03; exit when dist_sma_5 > 0 | 553 | 51.5 | 22.8 | 1.82 | 4.5% | 71% | 38.6 | 3.47 | 2.54 | 0.0050 | 1.000 |
| down_streak | index_etfs | vix_above_20 | BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 138 | 87.8 | 59.6 | 2.91 | 5.6% | 79% | 77.5 | 3.46 | 3.01 | 0.0010 | 1.000 |
| earnings_drift | single_stocks | spy_sma50 | BUY when earn_surprise > 5 and ret_1 > 0.02 | 181 | 224.9 | 197.3 | 2.16 | 3.6% | 74% | 41.0 | 3.42 | 0.63 | 0.0020 | 1.000 |
| earnings_drift | single_stocks | adx_not_trending | BUY when earn_surprise > 0 and ret_1 > 0.02 | 244 | 173.9 | 146.6 | 1.90 | 2.7% | 83% | 6.1 | 3.41 | 0.12 | 0.0010 | 1.000 |
| weekly_reversal | single_stocks | vix_above_20 | BUY when ret_5 < -0.08; exit when ret_1 > 0 | 943 | 86.1 | 58.5 | 1.59 | 12.3% | 81% | 68.7 | 3.34 | 2.69 | 0.0120 | 1.000 |
| weekly_reversal | sector_etfs | spy_sma50 | BUY when dist_sma_200 > 0 and ret_5 < -0.03; exit when dist_sma_5 > 0 | 356 | 57.0 | 28.9 | 1.97 | 4.5% | 79% | 43.9 | 3.33 | 2.51 | 0.0010 | 1.000 |
| down_streak | sector_etfs | vix_above_20 | BUY when down_streak >= 5; exit when dist_sma_5 > 0 | 214 | 90.9 | 63.5 | 2.56 | 3.3% | 90% | 80.5 | 3.32 | 2.88 | 0.0010 | 1.000 |
| calm_market_dip | single_stocks | vix_above_20 | BUY when dist_sma_200 > 0 and vol_ratio_20_100 < 1 and rsi_2 < 5; exit when dist_sma_5 > 0 | 327 | 69.5 | 41.7 | 1.88 | 1.5% | 71% | 41.5 | 3.31 | 1.91 | 0.0000 | 0.000 |
| turn_of_month | sector_etfs | vix_above_20 | BUY when tdays_left == 2; exit when tdom == 3 | 448 | 114.1 | 85.8 | 2.65 | 6.7% | 94% | 96.2 | 3.30 | 2.78 | 0.0020 | 1.000 |
| weekly_reversal | single_stocks | spy_sma50 | BUY when dist_sma_200 > 0 and ret_5 < -0.08; exit when ret_1 > 0 | 268 | 77.5 | 49.7 | 1.72 | 1.2% | 71% | 57.0 | 3.28 | 2.41 | 0.0050 | 1.000 |
| bollinger_low | sector_etfs | spy_sma20 | BUY when bb_pctb_20 < 0; exit when bb_pctb_20 > 0.5 | 169 | 69.0 | 40.9 | 1.93 | 3.2% | 74% | 41.8 | 3.22 | 1.92 | 0.0020 | 1.000 |
| weekly_reversal | single_stocks | none | BUY when ret_5 < -0.08; exit when ret_1 > 0 | 1384 | 57.2 | 28.9 | 1.39 | 12.3% | 67% | 38.5 | 3.21 | 2.17 | 0.0130 | 1.000 |
| down_streak | sector_etfs | vix_above_20 | BUY when down_streak >= 4; exit when dist_sma_5 > 0 | 393 | 71.4 | 43.5 | 2.04 | 5.1% | 90% | 60.5 | 3.20 | 2.66 | 0.0020 | 1.000 |
| down_streak | single_stocks | vix_above_20 | BUY when down_streak >= 4; exit when ret_1 > 0 | 955 | 54.0 | 25.5 | 1.60 | 3.8% | 76% | 39.9 | 3.20 | 2.35 | 0.0010 | 1.000 |
| down_streak | single_stocks | vix_above_20 | BUY when dist_sma_200 > 0 and down_streak >= 5; exit when dist_sma_5 > 0 | 203 | 125.0 | 86.7 | 2.66 | 1.0% | 76% | 99.5 | 3.18 | 2.49 | 0.0070 | 1.000 |
| down_streak | single_stocks | adx_trend_up | BUY when down_streak >= 5; exit when dist_sma_5 > 0 | 101 | 89.0 | 60.8 | 2.96 | 0.6% | 79% | 61.3 | 3.12 | 2.10 | 0.0020 | 1.000 |
| earnings_drift | single_stocks | adx_range | BUY when earn_surprise > 5 | 240 | 189.1 | 164.0 | 1.90 | 3.7% | 79% | 28.4 | 3.12 | 0.47 | 0.0020 | 1.000 |
| earnings_drift | single_stocks | spy_sma50 | BUY when dist_sma_200 > 0 and earn_surprise > 5 and ret_1 > 0.02 | 167 | 211.5 | 184.0 | 2.06 | 3.6% | 70% | 31.3 | 3.11 | 0.47 | 0.0040 | 1.000 |
| earnings_drift | single_stocks | adx_range | BUY when dist_sma_200 > 0 and earn_surprise > 5 | 180 | 212.6 | 187.3 | 2.10 | 2.7% | 75% | 49.0 | 3.10 | 0.72 | 0.0030 | 1.000 |
| weekly_reversal | single_stocks | vix_above_20 | BUY when dist_sma_200 > 0 and ret_5 < -0.08; exit when ret_1 > 0 | 308 | 114.0 | 78.6 | 1.80 | 3.4% | 70% | 95.0 | 3.07 | 2.54 | 0.0060 | 1.000 |
| turn_of_month | index_etfs | vix_above_20 | BUY when tdays_left == 2; exit when tdom == 3 | 152 | 121.5 | 93.2 | 3.26 | 6.5% | 88% | 103.9 | 3.03 | 2.60 | 0.0030 | 1.000 |
| earnings_drift | single_stocks | adx_not_downtrend | BUY when earn_surprise > 5 and ret_1 > 0.02 | 235 | 197.0 | 169.9 | 1.88 | 4.7% | 79% | 18.7 | 3.02 | 0.30 | 0.0000 | 0.000 |
| bollinger_low | sector_etfs | spy_sma10 | BUY when bb_pctb_20 < 0; exit when bb_pctb_20 > 0.5 | 138 | 72.5 | 40.2 | 2.00 | 2.1% | 78% | 45.1 | 3.00 | 1.84 | 0.0030 | 1.000 |
| bollinger_low | sector_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and bb_pctb_20 < 0.05; exit when dist_sma_5 > 0 | 269 | 78.5 | 48.0 | 2.45 | 4.4% | 76% | 65.4 | 2.96 | 2.42 | 0.0050 | 1.000 |
| turn_of_month | single_stocks | vix_above_20 | BUY when tdays_left == 2; exit when tdom == 3 | 1102 | 122.5 | 93.8 | 2.00 | 5.3% | 88% | 80.2 | 2.94 | 1.94 | 0.0030 | 1.000 |
| weekly_reversal | sector_etfs | none | BUY when dist_sma_200 > 0 and ret_5 < -0.05; exit when dist_sma_5 > 0 | 201 | 91.1 | 60.4 | 2.05 | 4.6% | 74% | 78.7 | 2.93 | 2.52 | 0.0030 | 1.000 |
| weekly_reversal | index_etfs | spy_sma50 | BUY when ret_5 < -0.03; exit when dist_sma_5 > 0 | 112 | 73.5 | 45.3 | 2.57 | 5.6% | 78% | 60.5 | 2.90 | 2.32 | 0.0070 | 1.000 |
| breakout_momentum | single_stocks | adx_range | BUY when at_high_252 == 1 and vol_ratio_20_100 < 1; exit when dist_sma_50 < 0 | 322 | 158.2 | 130.3 | 1.73 | 2.8% | 67% | 3.7 | 2.89 | 0.07 | 0.0090 | 1.000 |
| down_streak | single_stocks | vix_above_20 | BUY when down_streak >= 5; exit when ret_1 > 0 | 468 | 80.2 | 50.8 | 1.99 | 2.6% | 76% | 67.0 | 2.88 | 2.39 | 0.0040 | 1.000 |
| pullback_in_uptrend | single_stocks | vix_above_20 | BUY when dist_sma_200 > 0 and rsi_3 < 10; exit when dist_sma_5 > 0 | 377 | 90.1 | 55.0 | 1.75 | 4.6% | 86% | 62.2 | 2.87 | 1.96 | 0.0010 | 1.000 |
| turn_of_month | single_stocks | vix_above_20 | BUY when tdays_left == 3; exit when tdom == 3 | 1073 | 116.6 | 87.6 | 1.88 | 6.9% | 93% | 67.3 | 2.86 | 1.67 | 0.0030 | 1.000 |
| bollinger_low | sector_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and bb_pctb_20 < 0; exit when dist_sma_5 > 0 | 220 | 76.0 | 48.1 | 2.35 | 4.3% | 76% | 63.0 | 2.82 | 2.30 | 0.0070 | 1.000 |
| earnings_drift | single_stocks | adx_range | BUY when earn_surprise > 10 | 119 | 237.4 | 214.3 | 2.21 | 1.9% | 61% | 67.0 | 2.81 | 0.78 | 0.0070 | 1.000 |
| turn_of_month | sector_etfs | vix_above_20 | BUY when tdays_left == 3; exit when tdom == 5 | 436 | 165.3 | 137.0 | 2.56 | 13.7% | 93% | 138.3 | 2.77 | 2.32 | 0.0030 | 1.000 |
| earnings_drift | single_stocks | adx_not_downtrend | BUY when dist_sma_200 > 0 and earn_surprise > 5 and ret_1 > 0.02 | 213 | 182.4 | 155.2 | 1.81 | 3.7% | 79% | 4.7 | 2.76 | 0.07 | 0.0030 | 1.000 |
| earnings_drift | single_stocks | spy_sma20 | BUY when earn_surprise > 5 and ret_1 > 0.02 | 181 | 188.9 | 161.4 | 1.90 | 2.7% | 75% | 12.3 | 2.75 | 0.18 | 0.0050 | 1.000 |
| n_day_low | index_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and at_low_20 == 1; exit when dist_sma_5 > 0 | 134 | 76.7 | 48.7 | 2.58 | 7.5% | 81% | 65.1 | 2.73 | 2.27 | 0.0080 | 1.000 |
| earnings_drift | single_stocks | adx_not_trending | BUY when earn_surprise > 5 and ret_1 > 0.02 | 145 | 178.9 | 151.6 | 1.94 | 2.5% | 75% | 7.0 | 2.70 | 0.11 | 0.0070 | 1.000 |
| earnings_drift | single_stocks | adx_not_trending | BUY when earn_surprise > 10 | 214 | 173.5 | 149.8 | 1.76 | 3.5% | 71% | 3.4 | 2.68 | 0.05 | 0.0110 | 1.000 |
| bollinger_low | single_stocks | none | BUY when bb_pctb_20 < 0; exit when bb_pctb_20 > 0.5 | 1655 | 81.9 | 54.2 | 1.38 | 18.5% | 88% | 19.9 | 2.68 | 0.66 | 0.0020 | 1.000 |
| turn_of_month | index_etfs | vix_above_20 | BUY when tdays_left == 3; exit when tdom == 5 | 148 | 167.5 | 139.2 | 2.94 | 14.4% | 100% | 140.6 | 2.65 | 2.22 | 0.0080 | 1.000 |
| turn_of_month | sector_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and tdays_left == 2; exit when tdom == 3 | 221 | 107.7 | 79.5 | 2.53 | 4.5% | 88% | 88.5 | 2.59 | 2.13 | 0.0120 | 1.000 |
| breakout_momentum | index_etfs | spy_sma50 | BUY when at_high_50 == 1 and vol_ratio_20_100 < 1; exit when dist_sma_50 < 0 | 233 | 68.9 | 40.7 | 1.66 | 9.7% | 67% | 6.8 | 2.56 | 0.26 | 0.0200 | 1.000 |
| turn_of_month | sector_etfs | vix_above_20 | BUY when tdays_left == 2; exit when tdom == 5 | 448 | 144.2 | 115.9 | 2.29 | 14.5% | 75% | 119.7 | 2.53 | 2.09 | 0.0140 | 1.000 |
| bollinger_low | single_stocks | adx_not_trending | BUY when bb_pctb_20 < 0; exit when bb_pctb_20 > 0.5 | 1157 | 73.6 | 45.8 | 1.39 | 7.9% | 83% | 13.6 | 2.53 | 0.46 | 0.0010 | 1.000 |
| turn_of_month | sector_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and tdays_left == 1; exit when tdom == 5 | 219 | 144.4 | 116.1 | 2.64 | 6.0% | 85% | 121.0 | 2.51 | 2.10 | 0.0180 | 1.000 |
| bollinger_low | index_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and bb_pctb_20 < 0; exit when dist_sma_5 > 0 | 102 | 77.5 | 49.4 | 2.73 | 7.2% | 80% | 65.7 | 2.50 | 2.08 | 0.0130 | 1.000 |
| breakout_momentum | single_stocks | vix_above_20 | BUY when at_high_100 == 1 and vol_ratio_20_100 < 1; exit when dist_sma_50 < 0 | 181 | 296.0 | 268.4 | 2.06 | 6.8% | 69% | 60.4 | 2.45 | 0.58 | 0.0370 | 1.000 |
| breakout_momentum | single_stocks | vix_above_20 | BUY when at_high_50 == 1 and vol_ratio_20_100 < 1; exit when dist_sma_50 < 0 | 302 | 147.5 | 120.2 | 1.60 | 6.4% | 76% | 5.7 | 2.44 | 0.10 | 0.0410 | 1.000 |
| turn_of_month | index_etfs | vix_above_20 | BUY when tdays_left == 2; exit when tdom == 5 | 152 | 153.0 | 124.6 | 2.61 | 15.2% | 81% | 128.8 | 2.40 | 2.01 | 0.0180 | 1.000 |
| earnings_drift | single_stocks | spy_sma50 | BUY when earn_surprise > 10 and ret_1 > 0.02 | 107 | 196.0 | 168.4 | 1.93 | 3.1% | 74% | 9.3 | 2.39 | 0.11 | 0.0210 | 1.000 |
| weekly_reversal | sector_etfs | adx_not_trending | BUY when dist_sma_200 > 0 and ret_5 < -0.05; exit when dist_sma_5 > 0 | 136 | 90.1 | 62.5 | 2.08 | 3.2% | 76% | 78.1 | 2.38 | 2.05 | 0.0080 | 1.000 |
| breakout_momentum | single_stocks | vix_above_20 | BUY when at_high_100 == 1; exit when dist_sma_50 < 0 | 324 | 142.0 | 114.8 | 1.63 | 6.2% | 72% | 4.9 | 2.34 | 0.09 | 0.0470 | 1.000 |
| breakout_momentum | single_stocks | vix_above_20 | BUY when at_high_50 == 1 and vol_ratio_20_100 < 1; exit when dist_sma_50 < 0 | 255 | 263.5 | 236.0 | 1.88 | 6.6% | 75% | 36.8 | 2.34 | 0.37 | 0.0340 | 1.000 |
| weekly_reversal | sector_etfs | adx_not_downtrend | BUY when dist_sma_200 > 0 and ret_5 < -0.05; exit when dist_sma_5 > 0 | 149 | 81.8 | 54.1 | 1.93 | 3.4% | 76% | 69.6 | 2.31 | 1.96 | 0.0090 | 1.000 |
| earnings_drift | single_stocks | adx_not_downtrend | BUY when earn_surprise > 10 and ret_1 > 0.02 | 143 | 183.3 | 156.1 | 1.77 | 3.1% | 75% | 1.0 | 2.31 | 0.01 | 0.0260 | 1.000 |
| turn_of_month | single_stocks | vix_above_20 | BUY when tdays_left == 3; exit when tdom == 5 | 1073 | 123.4 | 95.3 | 1.72 | 11.1% | 93% | 59.7 | 2.24 | 1.09 | 0.0130 | 1.000 |
| turn_of_month | single_stocks | vix_above_20 | BUY when tdays_left == 2; exit when tdom == 5 | 1102 | 132.8 | 103.5 | 1.77 | 12.4% | 75% | 75.6 | 2.24 | 1.27 | 0.0210 | 1.000 |
| turn_of_month | sector_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and tdays_left == 2; exit when tdom == 5 | 221 | 147.4 | 119.2 | 2.46 | 8.6% | 75% | 120.9 | 2.20 | 1.80 | 0.0260 | 1.000 |
| weekly_reversal | sector_etfs | vix_above_20 | BUY when dist_sma_200 > 0 and ret_5 < -0.05; exit when dist_sma_5 > 0 | 140 | 93.4 | 65.7 | 2.08 | 4.7% | 79% | 81.6 | 2.17 | 1.88 | 0.0200 | 1.000 |
| breakout_momentum | single_stocks | vix_above_20 | BUY when at_high_100 == 1 and vol_ratio_20_100 < 1; exit when dist_sma_50 < 0 | 220 | 164.1 | 136.8 | 1.75 | 6.3% | 69% | 20.2 | 2.06 | 0.28 | 0.0610 | 1.000 |