# Subsets (added after the run; reported, not decisive)

Same trades as tables.md, 25 bps a side. Bot's 29 = today's paper single stocks (survivors by construction); top 500 = each year's 500 stocks with the highest median dollar volume the year before.

| Variant | Subset | Trades | Net bps | t (net) | vs SPY bps | t (vs SPY) | Years + / counted | Pass |
|---|---|---|---|---|---|---|---|---|
| H2:none | bot_29 | 482 | +54.2 | +2.17 | +21.3 | +1.36 | 8 / 9 | pass |
| H2:none | top500 | 8,345 | -1.3 | -0.09 | -27.7 | -2.71 | 1 / 10 | fail |
| H2:none | top500_survivors | 7,840 | +3.0 | +0.22 | -24.0 | -2.38 | 1 / 10 | fail |
| H5:vix_above_20 | bot_29 | 853 | +57.7 | +2.72 | -12.7 | -0.83 | 4 / 10 | fail |
| H5:vix_above_20 | top500 | 14,598 | +28.9 | +1.19 | -39.0 | -3.22 | 1 / 9 | fail |
| H5:vix_above_20 | top500_survivors | 13,578 | +31.5 | +1.33 | -37.9 | -3.13 | 1 / 9 | fail |
| HB:adx_not_downtrend | bot_29 | 1,240 | +152.3 | +3.20 | +36.9 | +1.27 | 7 / 11 | fail |
| HB:adx_not_downtrend | top500 | 17,074 | +23.9 | +0.62 | -82.5 | -4.11 | 1 / 10 | fail |
| HB:adx_not_downtrend | top500_survivors | 15,850 | +29.4 | +0.75 | -78.9 | -3.94 | 1 / 10 | fail |
| HB:none | bot_29 | 1,241 | +151.8 | +3.19 | +36.3 | +1.25 | 7 / 11 | fail |
| HB:none | top500 | 17,079 | +23.9 | +0.62 | -82.4 | -4.10 | 1 / 10 | fail |
| HB:none | top500_survivors | 15,855 | +29.3 | +0.75 | -78.8 | -3.93 | 1 / 10 | fail |
| HB:spy_sma10 | bot_29 | 1,186 | +166.7 | +3.47 | +51.8 | +1.75 | 8 / 11 | fail |
| HB:spy_sma10 | top500 | 16,109 | +34.1 | +0.85 | -75.1 | -3.65 | 1 / 10 | fail |
| HB:spy_sma10 | top500_survivors | 14,968 | +39.2 | +0.96 | -70.9 | -3.46 | 1 / 10 | fail |
| HB:spy_sma50 | bot_29 | 1,183 | +168.0 | +3.38 | +58.7 | +1.96 | 7 / 11 | pass |
| HB:spy_sma50 | top500 | 16,205 | +29.0 | +0.71 | -75.2 | -3.54 | 1 / 10 | fail |
| HB:spy_sma50 | top500_survivors | 15,061 | +34.7 | +0.83 | -70.9 | -3.33 | 1 / 10 | fail |
