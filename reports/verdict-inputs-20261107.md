# Verdict inputs: 2026-11-07

> **Note added 2026-11-10.** The point-in-time replay behind these inputs included 18 past members whose tickers yfinance now serves under unrelated companies, ETFs or post-bankruptcy shells. The "corrupt bars" dropped as cost artifacts (EP, CPWR, PARA, SII, GENZ) are exactly this ticker reuse, FNMA/FMCC's two flagged bars are the conservatorship days, and 13 more reused tickers (BBT on Brookline Bancorp's bars, CCU on a Chilean brewer's ADR, CSR, KG, WEN, NC and others) passed the cost filter and stayed in every figure. `point_in_time_universe.coverage()` now rejects those histories before a replay; this report's numbers were not rerun. See `reports/point-in-time-coverage-20261110.md`, section 4.

Experiment 1 of `reports/Frozen stock research priorities.md` (ranked table, row 1): measure the inputs every
threshold of the forward verdict depends on, before building the always-valid judge on assumed ones. Paper only.

## Summary

* **Source.** The bot's own ledger was not reachable from this machine (and held no adoption at the last engine
  audit), so every value is from a replay of the 9 live replayable strategies under their live setups on the
  point-in-time S&P 500 universe (707 symbols, 2000+), run through `experiment_engine.run_experiment` for every
  gate and exit template (63 experiments, 18 candidates per strategy). `scripts/measure_verdict_inputs.py
  --database-url ... --record` measures the ledger instead and stores a `verdict_inputs` audit row, which the
  judge reads in preference to the constants.
* **Per-trade std is about twice the research's assumption**: 410-440 bps net for the technical strategies
  (3% stop / 6% target / 20 sessions), 266-272 bps for RSI(2) and IBS, against the assumed 200. SPY over the same
  hold removes only ~10% of it (sector ETFs almost nothing more): these are mostly idiosyncratic outcomes.
* **Pairing is where the variance goes.** Candidate minus live per decision has a median std of 110-230 bps,
  because most decisions are the same trade under both setups (zero difference): an earnings blackout changes
  4-5% of decisions, a VIX gate 18-55%.
* **Stops gap through about 10% of the time** (15-27% for the 10% reversion stop, on few stops), on average 130-160
  bps past the stop, 95th percentile 465-620 bps. The realistic worst case per trade, the 99.9th percentile of
  |SPY-adjusted return|, is ~1300-1530 bps for the technical class and ~800-1060 bps for reversion; the bounds the
  betting e-process reads are **1600 bps (technical) and 1100 bps (reversion)**, breached by 0.07-0.18% of trades.
* **Same-date residual correlation rho = 0.07** (trade-weighted; 0.06-0.14 technical, 0.15-0.18 reversion). Small
  per pair, but the point-in-time replay enters 5-30 names per entry date for the technical strategies, a design
  effect of up to ~3. Live slot caps keep same-day entries far fewer.
* **Data hygiene.** 79k replayed trades across all setups (up to 1.7k per strategy baseline) were cost-model artifacts
  on corrupt bars of five delisted names (EP, CPWR, PARA, SII, GENZ): intrabar stop exits at -50%. Left in, they
  alone set the technical tail at -5151 bps. They are dropped from every measurement here; the cached history
  should be repaired at the source (feed hygiene).


**Question:** what per-trade dispersion, tails, worst-case loss given the stops, paired shadow-difference variance and same-date correlation do the bot's strategies actually have? The research behind the always-valid verdict (`app/services/verdict_stats.py`) sized its tests on an assumed 2% per-trade std and no correlation; these are the measured values the verdict code now reads.

**Source:** the bot's ledger was not reachable or had too few trades, so every value is from the replay: `gate_replay.simulate` of the 9 live replayable strategies under their live setups on the point-in-time S&P 500 universe (707 symbols, membership mask), 2000-01-01 onward, published cost model, run through `experiment_engine.run_experiment` for every gate and exit template.

## Values the verdict code reads

| Input | Technical | Reversion | Where it acts |
|---|---|---|---|
| Loss bound B (bps, abs residual q99.9, rounded up) | 1600 | 1100 | betting e-process support [-B, B]; a trade beyond it raises the alarm and switches to the Catoni process |
| Variance bound sigma (bps, 1.1 x max residual std) | 450 | 300 | Catoni-style fallback (Wang & Ramdas 2023, Assumption 2) |
| Same-date residual correlation rho | 0.070 (all classes) | | effective trades, simulation and group-sequential comparator |

Constants in `verdict_stats.MEASURED_INPUTS` (as of 2026-11-07); `--record` writes the same values as a `verdict_inputs` audit row, which the judge reads in preference.

## 1. Per-trade dispersion by strategy (2000+, point-in-time universe, live setup)

| Strategy | Trades | Mean | Std | Resid std | Resid+sector std | Kurtosis | q0.1% | q1% | q99% | q99.9% | Worst | 2020+ std |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| moving_average_crossover | 200867 | -21.9 | 428 | 381 | 378 | 2.7 | -1124 | -533 | 742 | 1327 | -5234 | 441 |
| rsi_mean_reversion | 24068 | -2.4 | 438 | 379 | 377 | 3.5 | -1432 | -527 | 738 | 1260 | -2666 | 452 |
| macd_momentum | 128025 | -18.6 | 435 | 393 | 391 | 7.9 | -1212 | -551 | 764 | 1345 | -9696 | 449 |
| ensemble | 110979 | -22.9 | 433 | 383 | 381 | 2.9 | -1170 | -533 | 754 | 1363 | -4756 | 444 |
| bollinger_mean_reversion | 19534 | -12.5 | 432 | 374 | 372 | 2.3 | -1294 | -556 | 746 | 1290 | -2722 | 448 |
| channel_breakout | 34879 | -7.7 | 411 | 374 | 371 | 3.5 | -1180 | -542 | 720 | 1359 | -2478 | 431 |
| trend_pullback | 55267 | -7.1 | 420 | 379 | 376 | 2.7 | -1147 | -517 | 705 | 1288 | -5328 | 434 |
| rsi2_pullback | 3325 | +13.7 | 272 | 238 | 210 | 8.0 | -1082 | -1019 | 675 | 1165 | -1168 | 302 |
| ibs_mean_reversion | 2193 | +13.0 | 266 | 122 | 122 | 11.6 | -1219 | -1021 | 664 | 1678 | -1481 | 233 |

All figures in bps of net return per trade (costs on both sides, published cost model). Resid = minus SPY over the same hold (entry open to exit open/close); resid+sector = minus the sector ETF where `market_gate.sector_symbol` maps one (today's list), SPY otherwise.

## 2. Stops, gaps and the realistic worst case

| Strategy | Stop | Stop exits | Gaps through stop | Share of stops | Share of trades | Gap past stop: mean / p95 / max | Worst stop exit | abs resid q99.9 | Breach rate at B |
|---|---|---|---|---|---|---|---|---|---|
| moving_average_crossover | 300 | 125218 | 12704 | 10.2% | 6.32% | 142 / 472 / 4934 | -5234 | 1320 | 0.076% |
| rsi_mean_reversion | 300 | 14551 | 1505 | 10.3% | 6.25% | 151 / 591 / 2366 | -2666 | 1531 | 0.125% |
| macd_momentum | 300 | 79023 | 8348 | 10.6% | 6.52% | 150 / 494 / 9396 | -9696 | 1348 | 0.095% |
| ensemble | 300 | 70193 | 6791 | 9.7% | 6.12% | 146 / 472 / 4456 | -4756 | 1346 | 0.084% |
| bollinger_mean_reversion | 300 | 11960 | 1226 | 10.2% | 6.28% | 157 / 619 / 2422 | -2722 | 1479 | 0.092% |
| channel_breakout | 300 | 19519 | 2273 | 11.7% | 6.52% | 140 / 519 / 2178 | -2478 | 1348 | 0.075% |
| trend_pullback | 300 | 32280 | 3594 | 11.1% | 6.50% | 132 / 465 / 5028 | -5328 | 1310 | 0.065% |
| rsi2_pullback | 1000 | 74 | 11 | 14.9% | 0.33% | 74 / 142 / 168 | -1168 | 1058 | 0.180% |
| ibs_mean_reversion | 1000 | 44 | 12 | 27.3% | 0.55% | 153 / 392 / 481 | -1481 | 786 | 0.091% |

## 3. Paired shadow differences (candidate minus live, per decision)

Over the union of the two setups' decisions, zero where both take the same trade; SPY-adjusted except for the market-timing gates (market, VIX, breadth), whose mechanism is the market move.

| Strategy | Template | Candidate | Decisions | Nonzero | Mean | Std | abs q99.9 | rho (entry date) |
|---|---|---|---|---|---|---|---|---|
| moving_average_crossover | market_gate | sma10 | 254194 | 65.1% | +4.9 | 345 | 1340 | 0.158 |
| moving_average_crossover | market_gate | sma20 | 237493 | 55.4% | +5.7 | 319 | 1248 | 0.167 |
| moving_average_crossover | market_gate | sma50 | 222200 | 39.8% | +6.4 | 270 | 1135 | 0.174 |
| moving_average_crossover | vix_gate | below20 | 220281 | 54.7% | +10.8 | 320 | 1229 | 0.182 |
| moving_average_crossover | vix_gate | below25 | 214260 | 33.0% | +7.6 | 251 | 1052 | 0.200 |
| moving_average_crossover | vix_gate | below30 | 208827 | 17.8% | +4.0 | 185 | 940 | 0.213 |
| moving_average_crossover | earnings_blackout | off | 208834 | 5.3% | -0.6 | 95 | 937 | 0.002 |
| moving_average_crossover | earnings_blackout | blackout20 | 201850 | 3.8% | +1.0 | 75 | 782 | 0.008 |
| moving_average_crossover | sector_gate | sector10 | 204207 | 5.5% | +0.8 | 85 | 795 | 0.008 |
| moving_average_crossover | sector_gate | sector20 | 203009 | 4.3% | +0.7 | 75 | 778 | 0.011 |
| moving_average_crossover | sector_gate | sector50 | 202071 | 2.9% | +0.5 | 61 | 721 | 0.017 |
| moving_average_crossover | breadth_gate | breadth40 | 208605 | 19.7% | +3.8 | 192 | 891 | 0.216 |
| moving_average_crossover | breadth_gate | breadth50 | 211387 | 28.9% | +8.1 | 231 | 1016 | 0.205 |
| moving_average_crossover | breadth_gate | breadth60 | 218210 | 46.9% | +10.4 | 297 | 1238 | 0.173 |
| moving_average_crossover | stop_width | stop2 | 300195 | 84.5% | -8.4 | 280 | 1351 | 0.011 |
| moving_average_crossover | stop_width | stop5 | 232413 | 81.2% | +10.8 | 354 | 1546 | 0.011 |
| moving_average_crossover | time_stop | hold10 | 260365 | 46.8% | -3.4 | 222 | 1164 | 0.015 |
| moving_average_crossover | time_stop | hold30 | 213244 | 20.9% | +0.6 | 160 | 1021 | 0.013 |
| rsi_mean_reversion | market_gate | sma10 | 25860 | 71.8% | -3.4 | 375 | 1455 | 0.213 |
| rsi_mean_reversion | market_gate | sma20 | 25017 | 66.9% | +2.1 | 362 | 1436 | 0.216 |
| rsi_mean_reversion | market_gate | sma50 | 24820 | 51.0% | +2.0 | 314 | 1202 | 0.242 |
| rsi_mean_reversion | vix_gate | below20 | 24639 | 50.7% | -0.9 | 320 | 1203 | 0.252 |
| rsi_mean_reversion | vix_gate | below25 | 24452 | 29.2% | -3.5 | 244 | 1057 | 0.246 |
| rsi_mean_reversion | vix_gate | below30 | 24294 | 16.2% | -3.5 | 185 | 922 | 0.247 |
| rsi_mean_reversion | earnings_blackout | off | 24775 | 3.0% | -0.1 | 81 | 944 | 0.015 |
| rsi_mean_reversion | earnings_blackout | blackout20 | 24069 | 3.1% | +0.4 | 72 | 814 | 0.028 |
| rsi_mean_reversion | sector_gate | sector10 | 24157 | 5.1% | +1.7 | 83 | 882 | -0.001 |
| rsi_mean_reversion | sector_gate | sector20 | 24113 | 4.9% | +1.5 | 83 | 877 | -0.007 |
| rsi_mean_reversion | sector_gate | sector50 | 24116 | 3.9% | +1.3 | 73 | 832 | -0.004 |
| rsi_mean_reversion | breadth_gate | breadth40 | 24248 | 21.7% | +2.2 | 207 | 858 | 0.273 |
| rsi_mean_reversion | breadth_gate | breadth50 | 24353 | 31.0% | +4.5 | 246 | 1064 | 0.250 |
| rsi_mean_reversion | breadth_gate | breadth60 | 24498 | 50.3% | +4.4 | 313 | 1194 | 0.237 |
| rsi_mean_reversion | stop_width | stop2 | 28490 | 75.5% | -3.2 | 246 | 1443 | 0.032 |
| rsi_mean_reversion | stop_width | stop5 | 24642 | 66.8% | +2.0 | 310 | 1566 | 0.037 |
| rsi_mean_reversion | time_stop | hold10 | 24224 | 24.1% | +0.5 | 152 | 1038 | 0.086 |
| rsi_mean_reversion | time_stop | hold30 | 24110 | 9.2% | +0.4 | 90 | 772 | 0.088 |
| macd_momentum | market_gate | off | 198886 | 44.7% | -14.6 | 293 | 1207 | 0.172 |
| macd_momentum | market_gate | sma10 | 183560 | 49.2% | -6.6 | 307 | 1255 | 0.144 |
| macd_momentum | market_gate | sma20 | 171634 | 40.4% | -5.2 | 278 | 1173 | 0.153 |
| macd_momentum | vix_gate | below20 | 134056 | 40.4% | +9.5 | 282 | 1219 | 0.156 |
| macd_momentum | vix_gate | below25 | 131216 | 20.0% | +7.8 | 198 | 1034 | 0.188 |
| macd_momentum | vix_gate | below30 | 129909 | 10.4% | +4.3 | 143 | 835 | 0.180 |
| macd_momentum | earnings_blackout | off | 132665 | 4.3% | -0.9 | 90 | 909 | 0.001 |
| macd_momentum | earnings_blackout | blackout20 | 128126 | 3.4% | +1.1 | 75 | 800 | 0.003 |
| macd_momentum | sector_gate | sector10 | 128516 | 1.5% | +0.2 | 49 | 624 | 0.018 |
| macd_momentum | sector_gate | sector20 | 128393 | 1.2% | +0.1 | 43 | 594 | 0.019 |
| macd_momentum | sector_gate | sector50 | 128622 | 1.7% | +0.3 | 51 | 651 | 0.001 |
| macd_momentum | breadth_gate | breadth40 | 132267 | 17.6% | +4.5 | 192 | 1034 | 0.151 |
| macd_momentum | breadth_gate | breadth50 | 133233 | 24.9% | +2.5 | 224 | 1079 | 0.158 |
| macd_momentum | breadth_gate | breadth60 | 135275 | 40.1% | +7.3 | 279 | 1206 | 0.154 |
| macd_momentum | stop_width | stop2 | 169089 | 79.6% | -9.2 | 266 | 1350 | 0.011 |
| macd_momentum | stop_width | stop5 | 135497 | 72.0% | +9.3 | 339 | 1569 | 0.011 |
| macd_momentum | time_stop | hold10 | 140605 | 32.4% | -2.8 | 185 | 1080 | 0.021 |
| macd_momentum | time_stop | hold30 | 128924 | 10.8% | -0.1 | 103 | 856 | 0.022 |
| ensemble | market_gate | sma10 | 129452 | 60.7% | +1.4 | 338 | 1360 | 0.181 |
| ensemble | market_gate | sma20 | 122251 | 51.0% | +2.6 | 312 | 1280 | 0.196 |
| ensemble | market_gate | sma50 | 116418 | 33.8% | +4.7 | 253 | 1068 | 0.203 |
| ensemble | vix_gate | below20 | 117248 | 54.4% | +11.1 | 324 | 1233 | 0.202 |
| ensemble | vix_gate | below25 | 115543 | 31.9% | +5.9 | 250 | 1043 | 0.221 |
| ensemble | vix_gate | below30 | 113808 | 17.0% | +3.2 | 183 | 915 | 0.237 |
| ensemble | earnings_blackout | off | 114355 | 3.4% | -0.4 | 83 | 877 | 0.003 |
| ensemble | earnings_blackout | blackout20 | 111039 | 2.8% | +1.2 | 68 | 736 | 0.018 |
| ensemble | sector_gate | sector10 | 112229 | 5.2% | +1.1 | 84 | 784 | 0.018 |
| ensemble | sector_gate | sector20 | 111632 | 4.0% | +0.8 | 73 | 756 | 0.022 |
| ensemble | sector_gate | sector50 | 111254 | 2.4% | +0.5 | 58 | 713 | 0.038 |
| ensemble | breadth_gate | breadth40 | 113471 | 19.0% | +3.3 | 192 | 907 | 0.238 |
| ensemble | breadth_gate | breadth50 | 113944 | 28.2% | +8.9 | 232 | 1016 | 0.231 |
| ensemble | breadth_gate | breadth60 | 115814 | 45.3% | +10.9 | 294 | 1196 | 0.200 |
| ensemble | stop_width | stop2 | 150165 | 81.4% | -7.6 | 267 | 1336 | 0.016 |
| ensemble | stop_width | stop5 | 119694 | 76.4% | +11.8 | 342 | 1527 | 0.017 |
| ensemble | time_stop | hold10 | 120011 | 27.6% | -1.1 | 171 | 1071 | 0.030 |
| ensemble | time_stop | hold30 | 111316 | 8.4% | +0.2 | 89 | 820 | 0.023 |
| bollinger_mean_reversion | market_gate | sma10 | 19962 | 77.2% | +6.2 | 384 | 1446 | 0.213 |
| bollinger_mean_reversion | market_gate | sma20 | 19782 | 71.3% | +9.4 | 368 | 1428 | 0.211 |
| bollinger_mean_reversion | market_gate | sma50 | 19713 | 55.2% | +7.7 | 325 | 1295 | 0.242 |
| bollinger_mean_reversion | vix_gate | below20 | 19759 | 52.5% | +4.5 | 321 | 1210 | 0.249 |
| bollinger_mean_reversion | vix_gate | below25 | 19704 | 29.7% | -0.6 | 245 | 986 | 0.246 |
| bollinger_mean_reversion | vix_gate | below30 | 19594 | 16.6% | -2.0 | 184 | 881 | 0.265 |
| bollinger_mean_reversion | earnings_blackout | off | 20109 | 3.0% | -0.4 | 73 | 872 | -0.005 |
| bollinger_mean_reversion | earnings_blackout | blackout20 | 19535 | 3.4% | +0.1 | 73 | 838 | 0.037 |
| bollinger_mean_reversion | sector_gate | sector10 | 19553 | 5.2% | +1.7 | 84 | 883 | 0.018 |
| bollinger_mean_reversion | sector_gate | sector20 | 19547 | 4.9% | +1.4 | 82 | 880 | 0.008 |
| bollinger_mean_reversion | sector_gate | sector50 | 19546 | 4.0% | +1.1 | 72 | 774 | -0.028 |
| bollinger_mean_reversion | breadth_gate | breadth40 | 19568 | 22.5% | +10.4 | 204 | 864 | 0.297 |
| bollinger_mean_reversion | breadth_gate | breadth50 | 19609 | 31.2% | +13.2 | 241 | 1008 | 0.268 |
| bollinger_mean_reversion | breadth_gate | breadth60 | 19681 | 51.4% | +14.5 | 313 | 1192 | 0.255 |
| bollinger_mean_reversion | stop_width | stop2 | 22106 | 74.5% | -2.7 | 234 | 1301 | 0.060 |
| bollinger_mean_reversion | stop_width | stop5 | 19784 | 65.5% | +5.4 | 296 | 1493 | 0.049 |
| bollinger_mean_reversion | time_stop | hold10 | 19628 | 24.5% | +1.7 | 151 | 1014 | 0.094 |
| bollinger_mean_reversion | time_stop | hold30 | 19561 | 9.7% | +0.5 | 94 | 841 | 0.057 |
| channel_breakout | market_gate | off | 40263 | 17.5% | -2.9 | 169 | 857 | 0.189 |
| channel_breakout | market_gate | sma10 | 37861 | 17.1% | -2.8 | 169 | 883 | 0.146 |
| channel_breakout | market_gate | sma50 | 38364 | 14.7% | -3.4 | 153 | 820 | 0.174 |
| channel_breakout | vix_gate | off | 46275 | 26.7% | -9.9 | 219 | 1061 | 0.174 |
| channel_breakout | vix_gate | below25 | 42767 | 20.7% | -5.8 | 190 | 903 | 0.147 |
| channel_breakout | vix_gate | below30 | 45067 | 24.8% | -7.0 | 211 | 1063 | 0.168 |
| channel_breakout | earnings_blackout | off | 36463 | 5.5% | -1.1 | 104 | 1071 | 0.005 |
| channel_breakout | earnings_blackout | blackout20 | 34965 | 3.7% | +0.6 | 75 | 768 | 0.004 |
| channel_breakout | sector_gate | sector10 | 34956 | 1.1% | -0.1 | 38 | 549 | 0.029 |
| channel_breakout | sector_gate | sector20 | 34938 | 0.9% | -0.1 | 36 | 522 | 0.029 |
| channel_breakout | sector_gate | sector50 | 34926 | 0.9% | -0.2 | 32 | 494 | 0.041 |
| channel_breakout | breadth_gate | breadth40 | 34938 | 0.6% | -0.1 | 35 | 583 | 0.256 |
| channel_breakout | breadth_gate | breadth50 | 35120 | 3.1% | +0.0 | 72 | 602 | 0.226 |
| channel_breakout | breadth_gate | breadth60 | 35604 | 13.7% | +2.0 | 156 | 869 | 0.156 |
| channel_breakout | stop_width | stop2 | 38740 | 70.7% | -2.3 | 223 | 1215 | 0.013 |
| channel_breakout | stop_width | stop5 | 35688 | 59.4% | +4.7 | 274 | 1491 | 0.014 |
| channel_breakout | time_stop | hold10 | 39035 | 48.6% | -2.6 | 198 | 1037 | 0.033 |
| channel_breakout | time_stop | hold30 | 35788 | 25.2% | -0.4 | 154 | 880 | 0.013 |
| trend_pullback | market_gate | off | 107571 | 66.4% | +1.1 | 346 | 1186 | 0.196 |
| trend_pullback | market_gate | sma20 | 73939 | 48.4% | -2.3 | 291 | 1141 | 0.176 |
| trend_pullback | market_gate | sma50 | 85610 | 60.8% | +1.1 | 329 | 1185 | 0.176 |
| trend_pullback | vix_gate | below20 | 57319 | 27.9% | -1.3 | 228 | 986 | 0.193 |
| trend_pullback | vix_gate | below25 | 56383 | 11.2% | +0.3 | 148 | 833 | 0.235 |
| trend_pullback | vix_gate | below30 | 55635 | 3.6% | +0.9 | 82 | 632 | 0.234 |
| trend_pullback | earnings_blackout | off | 57225 | 4.1% | -0.3 | 87 | 863 | 0.003 |
| trend_pullback | earnings_blackout | blackout20 | 55414 | 3.8% | +1.0 | 79 | 803 | 0.010 |
| trend_pullback | sector_gate | sector10 | 55859 | 3.8% | +0.3 | 72 | 740 | 0.011 |
| trend_pullback | sector_gate | sector20 | 55745 | 3.5% | +0.4 | 69 | 738 | 0.011 |
| trend_pullback | sector_gate | sector50 | 55505 | 2.4% | +0.2 | 58 | 690 | 0.024 |
| trend_pullback | breadth_gate | breadth40 | 55635 | 4.8% | +0.3 | 95 | 653 | 0.262 |
| trend_pullback | breadth_gate | breadth50 | 56136 | 10.8% | +0.9 | 142 | 769 | 0.247 |
| trend_pullback | breadth_gate | breadth60 | 57331 | 24.9% | +0.7 | 215 | 1009 | 0.174 |
| trend_pullback | stop_width | stop2 | 69272 | 75.9% | -3.6 | 247 | 1296 | 0.015 |
| trend_pullback | stop_width | stop5 | 57322 | 67.0% | +4.8 | 299 | 1430 | 0.022 |
| trend_pullback | time_stop | hold10 | 64269 | 44.9% | -1.4 | 209 | 1120 | 0.026 |
| trend_pullback | time_stop | hold30 | 56611 | 21.1% | +1.4 | 150 | 984 | 0.020 |
| rsi2_pullback | market_gate | off | 5019 | 34.6% | +2.5 | 218 | 1384 | 0.467 |
| rsi2_pullback | market_gate | sma10 | 3585 | 57.0% | -10.1 | 209 | 1162 | 0.184 |
| rsi2_pullback | market_gate | sma20 | 3524 | 35.6% | -9.4 | 170 | 1029 | 0.216 |
| rsi2_pullback | vix_gate | below20 | 3363 | 22.8% | -11.3 | 144 | 1115 | 0.298 |
| rsi2_pullback | vix_gate | below25 | 3336 | 7.0% | -5.5 | 79 | 1022 | 0.385 |
| rsi2_pullback | vix_gate | below30 | 3332 | 2.8% | -1.8 | 56 | 961 | 0.263 |
| rsi2_pullback | earnings_blackout | off | 3798 | 13.5% | +3.3 | 142 | 1661 | 0.141 |
| rsi2_pullback | earnings_blackout | blackout20 | 3325 | 16.0% | +2.3 | 106 | 1052 | 0.128 |
| rsi2_pullback | sector_gate | sector10 | 3413 | 59.8% | +8.9 | 198 | 1172 | 0.079 |
| rsi2_pullback | sector_gate | sector20 | 3380 | 40.6% | +7.8 | 167 | 1120 | 0.071 |
| rsi2_pullback | sector_gate | sector50 | 3341 | 19.4% | +3.1 | 118 | 1016 | 0.173 |
| rsi2_pullback | breadth_gate | breadth40 | 3329 | 1.8% | -1.0 | 49 | 875 | 0.663 |
| rsi2_pullback | breadth_gate | breadth50 | 3334 | 4.6% | -3.2 | 70 | 1005 | 0.632 |
| rsi2_pullback | breadth_gate | breadth60 | 3345 | 14.9% | -4.8 | 115 | 1026 | 0.479 |
| rsi2_pullback | stop_width | stop7 | 3378 | 8.2% | -5.4 | 105 | 1003 | 0.210 |
| rsi2_pullback | stop_width | stop15 | 3326 | 2.6% | +1.5 | 71 | 813 | 0.134 |
| rsi2_pullback | time_stop | hold5 | 3427 | 21.2% | -4.7 | 82 | 653 | 0.140 |
| rsi2_pullback | time_stop | hold15 | 3326 | 1.9% | +0.8 | 17 | 272 | 0.289 |
| ibs_mean_reversion | market_gate | sma10 | 2210 | 65.4% | -8.6 | 226 | 1218 | 0.729 |
| ibs_mean_reversion | market_gate | sma20 | 2202 | 57.4% | -4.6 | 231 | 1449 | 0.734 |
| ibs_mean_reversion | market_gate | sma50 | 2201 | 48.7% | -9.6 | 228 | 1754 | 0.738 |
| ibs_mean_reversion | vix_gate | below20 | 2198 | 51.3% | -15.3 | 240 | 1755 | 0.737 |
| ibs_mean_reversion | vix_gate | below25 | 2202 | 29.0% | -13.9 | 202 | 1702 | 0.715 |
| ibs_mean_reversion | vix_gate | below30 | 2200 | 14.3% | -8.2 | 157 | 1181 | 0.764 |
| ibs_mean_reversion | earnings_blackout | off | 2193 | 0.0% | +0.0 | 0 | 0 | n/a |
| ibs_mean_reversion | earnings_blackout | blackout20 | 2193 | 0.0% | +0.0 | 0 | 0 | n/a |
| ibs_mean_reversion | sector_gate | sector10 | 2193 | 0.0% | +0.0 | 0 | 0 | n/a |
| ibs_mean_reversion | sector_gate | sector20 | 2193 | 0.0% | +0.0 | 0 | 0 | n/a |
| ibs_mean_reversion | sector_gate | sector50 | 2193 | 0.0% | +0.0 | 0 | 0 | n/a |
| ibs_mean_reversion | breadth_gate | breadth40 | 2195 | 20.0% | -8.4 | 164 | 1135 | 0.842 |
| ibs_mean_reversion | breadth_gate | breadth50 | 2195 | 27.8% | -12.9 | 184 | 1181 | 0.789 |
| ibs_mean_reversion | breadth_gate | breadth60 | 2202 | 46.1% | -11.7 | 223 | 1449 | 0.746 |
| ibs_mean_reversion | stop_width | stop7 | 2233 | 7.3% | -3.4 | 75 | 826 | 0.059 |
| ibs_mean_reversion | stop_width | stop15 | 2196 | 2.7% | +0.1 | 66 | 905 | -0.147 |
| ibs_mean_reversion | time_stop | hold5 | 2287 | 19.6% | +0.3 | 47 | 549 | 0.268 |
| ibs_mean_reversion | time_stop | hold15 | 2194 | 3.6% | +0.0 | 10 | 119 | 0.022 |

Per strategy, the std of the paired difference across its candidates (median / max):

| Strategy | Candidates | Median std | Max std |
|---|---|---|---|
| moving_average_crossover | 18 | 227 | 354 |
| rsi_mean_reversion | 18 | 225 | 375 |
| macd_momentum | 18 | 195 | 339 |
| ensemble | 18 | 212 | 342 |
| bollinger_mean_reversion | 18 | 219 | 384 |
| channel_breakout | 18 | 155 | 274 |
| trend_pullback | 18 | 149 | 346 |
| rsi2_pullback | 18 | 110 | 218 |
| ibs_mean_reversion | 18 | 116 | 240 |

## 4. Same-date residual correlation

| Strategy | rho (SPY + sector) | rho (SPY only) | Entry dates | Mean cluster | Trades on shared dates | Max cluster | rho 2020+ |
|---|---|---|---|---|---|---|---|
| moving_average_crossover | 0.059 | 0.059 | 6695 | 30.00 | 100.0% | 308 | 0.073 |
| rsi_mean_reversion | 0.125 | 0.127 | 4694 | 5.13 | 94.8% | 146 | 0.135 |
| macd_momentum | 0.056 | 0.057 | 4503 | 28.43 | 100.0% | 415 | 0.081 |
| ensemble | 0.078 | 0.080 | 6500 | 17.07 | 99.7% | 243 | 0.095 |
| bollinger_mean_reversion | 0.136 | 0.138 | 4057 | 4.82 | 93.3% | 102 | 0.153 |
| channel_breakout | 0.058 | 0.061 | 3083 | 11.31 | 99.7% | 151 | 0.089 |
| trend_pullback | 0.074 | 0.077 | 3959 | 13.96 | 99.7% | 116 | 0.102 |
| rsi2_pullback | 0.153 | 0.193 | 1677 | 1.98 | 72.5% | 18 | 0.067 |
| ibs_mean_reversion | 0.178 | 0.178 | 1122 | 1.96 | 75.9% | 4 | 0.117 |

Book (all strategies pooled): rho 0.056 over 6740 entry dates, mean cluster 85.92. The verdict judges one strategy's change, so the per-strategy trade-weighted value (0.070) is the one it reads.

## Data hygiene

Dropped before every measurement: 78982 replayed trades across all setups that no real fill can produce (intrabar stop/target exit losing more than 2 x stop + 100 bps (cost model on corrupt bars)), from EP (1450), CPWR (538), PARA (190), SII (16), GENZ (6), FNMA (1). They are corrupt daily bars of delisted names in the cached point-in-time history; at a 51% loss each they set the technical class's 0.1% tail on their own. The engine's replay rows (and the decisions in `reports/verdict-replay-20261107.md`, experiment 3) still contain them, mostly on both sides of each comparison.

## What changes against the research's assumptions

* Per-trade std: 266 to 438 bps across strategies (median 428), against the assumed 200. SPY adjustment brings it to a median 379 bps.
* Paired differences: a median 184 bps std per decision (range 10 to 384), because a median 21% of decisions differ between the two setups at all; pairing is where the variance reduction comes from, not the SPY adjustment.
* Same-date residual correlation: rho = 0.070 (trade-weighted over strategies). The design effect for a strategy that enters m names on one date is 1 + (m - 1) rho.
* Loss bounds: technical 1600 bps, reversion 1100 bps; gaps through the stop are reported per strategy in section 2.


## 5. The bot's own ledger

Not reachable from where this ran (no --database-url); every value above is from the replay. Rerun with `--database-url` on the host, and `--record` to store the ledger-based values as the `verdict_inputs` row, once the strategies have at least 30 closed paper trades each.

## 6. Run

707 symbols (13 without history), VIX present, breadth history present, coverage since 2010 80.9%. Load 9.7s, signals 7.6s, replays 4118.7s.

