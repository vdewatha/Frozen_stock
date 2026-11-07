# Liquid universe check: 2026-11-07

**Question (ranked experiment 4):** how wide can the exploration lane go? On the point-in-time S&P 500 replay, do the liquidity terciles outside the 45-name core list trade as well as the core list once their wider spreads are paid, and how much sooner would each strategy reach 30 effective trades?

**Decision (pre-registered in `scripts/run_liquid_universe_check.py`):** `exploration_universe = "core"`, `exploration_max_open = 40` (unchanged: the lane is closed), `exploration_effective_rho = 0.17`. Paper only; learning-sized lots only.

* T1: does not qualify (2015+: -45.5 bps is more than 5 bps below the core list's +7.4; 2020+: -54.0 bps is more than 5 bps below the core list's +2.7).
* T2: does not qualify (2015+: -25.1 bps is more than 5 bps below the core list's +7.4; 2020+: -29.0 bps is more than 5 bps below the core list's +2.7).
* T3: does not qualify (2015+: -33.0 bps is more than 5 bps below the core list's +7.4; 2020+: -40.0 bps is more than 5 bps below the core list's +2.7).

Rule: a tercile qualifies iff its pooled net expectancy (standard cost model + extra spread cost) in both 2015+ and 2020+ is within 5 bps of the core list's and its median spread proxy (2015+) is below 1.00%; the universe is the largest run of qualifying terciles from the most liquid down.

## 1. Data and method

* 707 symbols with history (today's 45 core plus every past S&P 500 member yfinance still serves; 13 without usable history), bars from 2000-01-01, VIX present, earnings cache present. A single stock enters only while a member.
* Terciles of the 20-day median dollar volume among members on each decision date; mean size since 2020: T1 156, T2 156, T3 157 names (core names sit mostly in T1 and are counted as core).
* Spread proxy: 20-day mean Corwin-Schultz high-low estimate. The core list's median since 2015 is 0.48%: the estimator carries volatility too, so it overstates the quoted spread of liquid names; only the excess over the core median is charged.
* Signals per week and distinct signal dates per week are over every trading week of the window (unconstrained replay: one position per symbol per strategy, no book caps).

## Reading

* **The lane stays closed.** No tercile is within 5 bps of the core list. The gap is mostly there before any spread is charged: under the standard cost model alone, T1 trades -21.5 bps against the core's +17.5 (2015+), and -26.1 against +13.8 (2020+). The extra spread adds 15-28 bps on top.
* **Most of the core's edge is the core list itself.** The survivorship check found that today's list flatters these broad technical strategies by 20-70 bps per trade. The terciles here are the same strategies on the names that did not become today's megacaps, and they lose in every window and tercile. So the lane would not test the strategies on fresh names. It would pay for trades that, in the replay, lose money on average. The pre-registered rule exists to refuse exactly that.
* **The gap survives removing the market.** SPY-adjusted (net of the same days' SPY move and the extra spread), the core book is -33.7 bps per trade and T1 -72.4 (2015+). Even on the core list these long-only signals trail simply holding SPY over the same days; what they earn is mostly market exposure, so judging verdicts on residual returns (the research note's advice) would be stricter still.
* **Breadth would have bought speed.** The median technical strategy needs 29.0 weeks to 30 effective trades on the core book alone, against 10.4 with the lane on core+T1+T2+T3. Most of that gain comes from T1; T2 and T3 add little, because the lane's 4 lots per session and its 40 slots bind first. If a strategy with a point-in-time edge appears (the discovery library judges candidates point in time since PR #30), the lane is ready behind the setting: rerun this check.
* **The live gates hold on the wider names.** Each gate's removed trades are worse than the ones it keeps, in nearly every window and scope (table 6). Nothing here argues for loosening a gate.
* **Clustering matters.** Same-date correlation is 0.165 on raw returns and 0.0801 after removing SPY's move: 10 same-day trades of one strategy carry the information of about 4.0 independent ones. The evidence map now counts effective trades at rho = 0.17.

## 2. Pooled technical strategies by group

Net expectancy per trade in bps: standard cost model, then with the extra spread cost (month-clustered bootstrap CI), then SPY-adjusted with the spread cost (net return minus SPY's open-to-close move over the same days; reported, not part of the rule); equal-weight portfolio max drawdown.

| Window | Group | Trades | Signals/wk | Signal dates/wk | Exp. bps | Exp. + spread bps | Residual + spread bps | Median spread | Extra cost bps | Max DD |
|---|---|---|---|---|---|---|---|---|---|---|
| all | core | 49345 | 36.54 | 4.71 | +1.4 | -11.6 [-26, +3] | -40.2 | 0.50% | +13.0 | 83.7% |
| all | T1 | 164388 | 121.72 | 4.95 | -28.4 | -53.7 [-65, -43] | -73.9 | 0.65% | +25.3 | 73.4% |
| all | T2 | 186179 | 137.85 | 4.96 | -14.7 | -34.3 [-46, -22] | -55.7 | 0.60% | +19.6 | 44.9% |
| all | T3 | 173778 | 128.67 | 4.94 | -19.1 | -38.5 [-51, -26] | -61.1 | 0.57% | +19.4 | 54.1% |
| all | core+T1 | 213733 | 158.25 | 4.97 | -21.5 | -44.0 [-55, -33] | -66.2 | 0.61% | +22.5 | 70.1% |
| all | core+T1+T2 | 399912 | 296.10 | 4.98 | -18.3 | -39.5 [-51, -28] | -61.3 | 0.60% | +21.1 | 76.2% |
| all | core+T1+T2+T3 | 573690 | 424.77 | 4.99 | -18.6 | -39.2 [-51, -28] | -61.2 | 0.59% | +20.6 | 86.7% |
| 2015+ | core | 24315 | 40.80 | 4.75 | +17.5 | +7.4 [-14, +30] | -33.7 | 0.45% | +10.1 | 42.8% |
| 2015+ | T1 | 89024 | 149.37 | 4.99 | -21.5 | -45.5 [-60, -30] | -72.4 | 0.60% | +24.0 | 42.4% |
| 2015+ | T2 | 97613 | 163.78 | 4.97 | -8.5 | -25.1 [-40, -9] | -55.9 | 0.56% | +16.6 | 20.0% |
| 2015+ | T3 | 92139 | 154.60 | 4.96 | -17.2 | -33.0 [-49, -17] | -63.4 | 0.55% | +15.8 | 36.4% |
| 2015+ | core+T1 | 113339 | 190.17 | 4.99 | -13.1 | -34.1 [-49, -18] | -64.1 | 0.56% | +21.0 | 34.6% |
| 2015+ | core+T1+T2 | 210952 | 353.95 | 5.00 | -11.0 | -30.0 [-45, -14] | -60.3 | 0.56% | +19.0 | 38.2% |
| 2015+ | core+T1+T2+T3 | 303091 | 508.54 | 5.00 | -12.9 | -30.9 [-45, -15] | -61.3 | 0.56% | +18.0 | 53.8% |
| 2020+ | core | 15777 | 45.81 | 4.78 | +13.8 | +2.7 [-23, +30] | -38.9 | 0.50% | +11.1 | 42.8% |
| 2020+ | T1 | 63664 | 184.85 | 5.00 | -26.1 | -54.0 [-71, -36] | -78.7 | 0.67% | +27.9 | 41.5% |
| 2020+ | T2 | 66533 | 193.19 | 4.96 | -10.8 | -29.0 [-48, -10] | -58.4 | 0.61% | +18.2 | 22.5% |
| 2020+ | T3 | 62408 | 181.21 | 4.95 | -22.2 | -40.0 [-61, -20] | -68.1 | 0.60% | +17.9 | 39.4% |
| 2020+ | core+T1 | 79441 | 230.66 | 5.00 | -18.1 | -42.7 [-61, -24] | -70.8 | 0.63% | +24.6 | 35.4% |
| 2020+ | core+T1+T2 | 145974 | 423.85 | 5.00 | -14.8 | -36.5 [-54, -18] | -65.2 | 0.62% | +21.7 | 38.8% |
| 2020+ | core+T1+T2+T3 | 208382 | 605.06 | 5.00 | -17.0 | -37.5 [-55, -19] | -66.1 | 0.61% | +20.5 | 52.9% |

## 3. Per strategy (2015+ and 2020+, expectancy with the extra spread cost)

| Strategy | Window | core | T1 | T2 | T3 |
|---|---|---|---|---|---|
| moving_average_crossover | 2015+ | -3.2 (8300) | -51.5 (30735) | -27.6 (32991) | -32.5 (30626) |
| moving_average_crossover | 2020+ | -10.5 (5484) | -60.7 (22605) | -32.0 (23004) | -40.1 (21239) |
| rsi_mean_reversion | 2015+ | +33.5 (1016) | -15.5 (3580) | -2.1 (4222) | -17.1 (4528) |
| rsi_mean_reversion | 2020+ | +25.1 (684) | -19.2 (2468) | -7.8 (2785) | -25.8 (3038) |
| macd_momentum | 2015+ | +7.2 (4840) | -48.1 (20372) | -27.1 (22575) | -40.7 (22194) |
| macd_momentum | 2020+ | +5.9 (3293) | -56.7 (14722) | -28.2 (15804) | -44.8 (15654) |
| ensemble | 2015+ | -2.4 (4361) | -54.0 (18169) | -26.8 (17865) | -38.5 (15377) |
| ensemble | 2020+ | -9.3 (3047) | -60.8 (13785) | -32.2 (12874) | -48.7 (10963) |
| bollinger_mean_reversion | 2015+ | +38.8 (933) | -11.2 (2691) | -18.6 (3429) | -18.6 (3843) |
| bollinger_mean_reversion | 2020+ | +34.9 (574) | -19.4 (1707) | -20.8 (2141) | -27.0 (2421) |
| channel_breakout | 2015+ | +25.1 (2028) | -32.7 (5206) | -22.5 (6282) | -33.2 (5915) |
| channel_breakout | 2020+ | +49.8 (1008) | -39.0 (2971) | -26.8 (3420) | -37.9 (3045) |
| trend_pullback | 2015+ | +21.8 (2837) | -30.1 (8271) | -23.0 (10249) | -20.8 (9656) |
| trend_pullback | 2020+ | +12.6 (1687) | -36.3 (5406) | -27.0 (6505) | -24.9 (6048) |
| rsi2_pullback | 2015+ | +9.5 (1816) | n/a (0) | n/a (0) | n/a (0) |
| rsi2_pullback | 2020+ | +24.3 (1123) | n/a (0) | n/a (0) | n/a (0) |
| ibs_mean_reversion | 2015+ | +21.2 (883) | n/a (0) | n/a (0) | n/a (0) |
| ibs_mean_reversion | 2020+ | +27.4 (541) | n/a (0) | n/a (0) | n/a (0) |

Cells: bps per trade (trades). RSI(2) and IBS keep fixed scopes, so they never trade T1-T3.

## 4. Same-date correlation

One-way intraclass correlation of trade returns within (strategy, decision date), 2015+, technical strategies, every group: residual (SPY-adjusted) rho = **0.0801** over 15742 clusters (mean size 19.254); raw rho = 0.165; residual across strategies by date alone = 0.0631. The time-to-verdict estimate uses the residual value; the evidence map's default is the larger, 0.17 (verdicts judge raw P&L).

## 5. Time to 30 effective trades per breadth (2020+ book simulation)

Core book under the core caps (20 open, 5 per strategy, 3 per sector), lane book with 40 slots, 3 entries per strategy and per sector per date, 4 lane lots per session (the exploration budget), rho = residual. Weeks to 30 effective trades per strategy (core + lane):

| Strategy | core | core+T1 | core+T1+T2 | core+T1+T2+T3 |
|---|---|---|---|---|
| bollinger_mean_reversion | 49.9 | 26.7 | 26.2 | 24.3 |
| channel_breakout | 45.0 | 21.1 | 20.7 | 19.6 |
| ensemble | 14.0 | 5.3 | 5.2 | 5.5 |
| ibs_mean_reversion | 52.0 | 50.4 | 53.0 | 53.5 |
| macd_momentum | 15.5 | 5.6 | 5.5 | 5.7 |
| moving_average_crossover | 10.8 | 3.4 | 3.3 | 3.4 |
| rsi2_pullback | 41.5 | 41.6 | 41.9 | 42.8 |
| rsi_mean_reversion | 54.9 | 23.0 | 23.0 | 21.1 |
| trend_pullback | 29.0 | 10.8 | 11.1 | 10.4 |
| median (technical) | 29.0 | 10.8 | 11.1 | 10.4 |
| lane open (mean / p95) | 0.0 / 0 | 22.4 / 36 | 24.1 / 38 | 25.1 / 39 |

Slots on T1 (for the record; the lane is closed) (median weeks to 30 effective, technical strategies):

| Slots | Median weeks | Lane open mean | p95 |
|---|---|---|---|
| 20 | 12.8 | 17.2 | 20 |
| 30 | 11.3 | 21.2 | 30 |
| 40 | 10.8 | 22.4 | 36 |
| 60 | 10.8 | 22.5 | 37 |
| 80 | 10.8 | 22.5 | 37 |
| unbounded | 10.8 | 22.5 | 37 |

Pre-registered slot rule: the smallest of 20, 30, 40, 60, 80 within 10% of the unbounded median (10.8 weeks): 30; not applied while the lane is closed (default stays 40).

## 6. The live gates on the widened universe

Each gated strategy replayed with and without its gate (expectancy with the extra spread cost). 'Removed' are the trades the gate takes away; a gate earns its place when they are worse than what it keeps.

| Gate | Strategy | Window | Scope | Gated (trades, bps) | Ungated | Removed | Helps |
|---|---|---|---|---|---|---|---|
| spy_market_gate (SPY above its 50-day average) | macd_momentum | 2015+ | core | 4840, +7.2 | 6286, -1.9 | 2160, -5.2 | yes |
| spy_market_gate (SPY above its 50-day average) | macd_momentum | 2015+ | lane | 65141, -38.3 | 84015, -42.6 | 27898, -37.2 | yes |
| spy_market_gate (SPY above its 50-day average) | macd_momentum | 2015+ | widened | 69981, -35.2 | 90301, -39.7 | 30058, -34.9 | yes |
| spy_market_gate (SPY above its 50-day average) | macd_momentum | 2020+ | core | 3293, +5.9 | 4333, -3.1 | 1479, -8.6 | yes |
| spy_market_gate (SPY above its 50-day average) | macd_momentum | 2020+ | lane | 46180, -42.9 | 60043, -49.6 | 19472, -49.4 | yes |
| spy_market_gate (SPY above its 50-day average) | macd_momentum | 2020+ | widened | 49473, -39.7 | 64376, -46.5 | 20951, -46.6 | yes |
| spy_market_gate (SPY above its 20-day average) | channel_breakout | 2015+ | core | 2028, +25.1 | 2184, +17.2 | 226, -53.9 | yes |
| spy_market_gate (SPY above its 20-day average) | channel_breakout | 2015+ | lane | 17403, -29.2 | 19497, -32.7 | 2866, -58.1 | yes |
| spy_market_gate (SPY above its 20-day average) | channel_breakout | 2015+ | widened | 19431, -23.5 | 21681, -27.7 | 3092, -57.8 | yes |
| spy_market_gate (SPY above its 20-day average) | channel_breakout | 2020+ | core | 1008, +49.8 | 1116, +30.0 | 125, -117.1 | yes |
| spy_market_gate (SPY above its 20-day average) | channel_breakout | 2020+ | lane | 9436, -34.2 | 10837, -42.6 | 1697, -91.8 | yes |
| spy_market_gate (SPY above its 20-day average) | channel_breakout | 2020+ | widened | 10444, -26.1 | 11953, -35.8 | 1822, -93.6 | yes |
| spy_market_gate (SPY above its 10-day average) | trend_pullback | 2015+ | core | 2837, +21.8 | 4429, +7.2 | 2715, -3.0 | yes |
| spy_market_gate (SPY above its 10-day average) | trend_pullback | 2015+ | lane | 28176, -24.3 | 41984, -24.0 | 22688, -18.6 | no |
| spy_market_gate (SPY above its 10-day average) | trend_pullback | 2015+ | widened | 31013, -20.1 | 46413, -21.0 | 25403, -16.9 | yes |
| spy_market_gate (SPY above its 10-day average) | trend_pullback | 2020+ | core | 1687, +12.6 | 2674, -2.8 | 1593, -13.9 | yes |
| spy_market_gate (SPY above its 10-day average) | trend_pullback | 2020+ | lane | 17959, -29.1 | 27306, -30.2 | 14567, -24.6 | yes |
| spy_market_gate (SPY above its 10-day average) | trend_pullback | 2020+ | widened | 19646, -25.5 | 29980, -27.8 | 16160, -23.6 | yes |
| spy_market_gate (SPY above its 50-day average) | rsi2_pullback | 2015+ | core | 1816, +9.5 | 2639, +6.2 | 847, -1.9 | yes |
| spy_market_gate (SPY above its 50-day average) | rsi2_pullback | 2015+ | lane | 0, n/a | 0, n/a | 0, n/a | no |
| spy_market_gate (SPY above its 50-day average) | rsi2_pullback | 2015+ | widened | 1816, +9.5 | 2639, +6.2 | 847, -1.9 | yes |
| spy_market_gate (SPY above its 50-day average) | rsi2_pullback | 2020+ | core | 1123, +24.3 | 1553, +9.4 | 447, -31.3 | yes |
| spy_market_gate (SPY above its 50-day average) | rsi2_pullback | 2020+ | lane | 0, n/a | 0, n/a | 0, n/a | no |
| spy_market_gate (SPY above its 50-day average) | rsi2_pullback | 2020+ | widened | 1123, +24.3 | 1553, +9.4 | 447, -31.3 | yes |
| vix_gate (VIX below 20) | channel_breakout | 2015+ | core | 2028, +25.1 | 2368, +17.4 | 369, -26.7 | yes |
| vix_gate (VIX below 20) | channel_breakout | 2015+ | lane | 17403, -29.2 | 21303, -33.0 | 4244, -48.0 | yes |
| vix_gate (VIX below 20) | channel_breakout | 2015+ | widened | 19431, -23.5 | 23671, -27.9 | 4613, -46.3 | yes |
| vix_gate (VIX below 20) | channel_breakout | 2020+ | core | 1008, +49.8 | 1345, +30.5 | 362, -25.5 | yes |
| vix_gate (VIX below 20) | channel_breakout | 2020+ | lane | 9436, -34.2 | 13277, -39.2 | 4143, -49.8 | yes |
| vix_gate (VIX below 20) | channel_breakout | 2020+ | widened | 10444, -26.1 | 14622, -32.7 | 4505, -47.9 | yes |
| adx_range_gate (ADX(14) below 20) | rsi_mean_reversion | 2015+ | core | 1016, +33.5 | 3450, +26.2 | 2477, +22.3 | yes |
| adx_range_gate (ADX(14) below 20) | rsi_mean_reversion | 2015+ | lane | 12330, -11.5 | 45377, -28.7 | 33523, -34.8 | yes |
| adx_range_gate (ADX(14) below 20) | rsi_mean_reversion | 2015+ | widened | 13346, -8.1 | 48827, -24.8 | 36000, -30.9 | yes |
| adx_range_gate (ADX(14) below 20) | rsi_mean_reversion | 2020+ | core | 684, +25.2 | 2321, +17.0 | 1668, +10.7 | yes |
| adx_range_gate (ADX(14) below 20) | rsi_mean_reversion | 2020+ | lane | 8291, -17.8 | 30240, -38.2 | 22255, -46.0 | yes |
| adx_range_gate (ADX(14) below 20) | rsi_mean_reversion | 2020+ | widened | 8975, -14.5 | 32561, -34.3 | 23923, -42.0 | yes |
| adx_range_gate (ADX(14) below 20) | bollinger_mean_reversion | 2015+ | core | 933, +38.8 | 2528, +37.2 | 1599, +36.5 | yes |
| adx_range_gate (ADX(14) below 20) | bollinger_mean_reversion | 2015+ | lane | 9963, -16.6 | 28435, -25.3 | 18526, -29.6 | yes |
| adx_range_gate (ADX(14) below 20) | bollinger_mean_reversion | 2015+ | widened | 10896, -11.9 | 30963, -20.2 | 20125, -24.4 | yes |
| adx_range_gate (ADX(14) below 20) | bollinger_mean_reversion | 2020+ | core | 574, +34.9 | 1572, +25.3 | 1000, +20.1 | yes |
| adx_range_gate (ADX(14) below 20) | bollinger_mean_reversion | 2020+ | lane | 6269, -22.8 | 18113, -30.8 | 11869, -34.8 | yes |
| adx_range_gate (ADX(14) below 20) | bollinger_mean_reversion | 2020+ | widened | 6843, -18.0 | 19685, -26.4 | 12869, -30.6 | yes |

## 7. Today's screen

Of 503 S&P 500 members on 2026-11-07, T1 alone (were it admitted) would give 97 names (100k equity, one-share floor 1,500). Not admitted: core list 29, no daily history in the cache 5, outside the admitted terciles 332, spread above the ceiling 40. Skipped because one share exceeds the learning floor: none.
Sector assignment (SPY-residual correlation with the sector ETFs) agrees with the static map on 83.3% of the core single stocks.

## 8. Caveats

* The core list is today's survivors: its own replay is flattered by 20-70 bps per trade (reports/survivorship-20261106.md), so comparing the terciles with it is conservative, against widening.
* The replay applies no price floor (the live screen requires $10, per the research note); sub-$10 S&P 500 members are rare after 2010.
* Before 2010 the point-in-time universe is itself only about half covered (names Yahoo dropped); the decision windows (2015+, 2020+) are the better covered ones.
* The book simulation ignores the core exploration lots and news-found names that share the session budget, and fills every candidate at the replay's next-open price; it is an evidence-rate estimate, not a P&L forecast.
* Lane lots are 0.25% of equity: the lane is for evidence, not profit, until a strategy's verdict says otherwise.
* Runtime: load 10.1s, signals 14.5s, total 272.9s.
