# ADX Regime Gate (Range vs Trend), 2026-11-06

**Question:** do the bot's range strategies do better when they only buy in a sideways market, and do its
breakout/trend strategies do better when they only buy in a rising trend?

**Decision rule (same as `regime-filter-20261029.md`):** turn a gate on for a strategy only if net expectancy
per trade is not worse in any of the three windows AND portfolio max drawdown improves or stays similar.

**Answer:** only for the two slow mean-reversion strategies. The gate makes the bot's best strategies (RSI(2)
and IBS) worse, so the idea "range strategies need a range market" holds for RSI(14) and Bollinger reversion,
but not for 1–2 day pullback strategies.

| Strategy | Best gate | Verdict | Why |
|---|---|---|---|
| RSI Mean Reversion | `range` | **Turn on** | Expectancy up in all three windows (+3.4 → +18.4, +30.5 → +37.4, +24.4 → +35.1 bps); portfolio drawdown 7.4% → 2.5%, 3.7% → 1.0%, 3.7% → 1.0% |
| Bollinger Mean Reversion | `range` | **Turn on (borderline)** | Better since 2015 (+38.8 → +42.3) and 2020 (+30.9 → +38.3); full history −0.4 bps (+11.1 → +10.7, inside noise); drawdown halves in every window. Strictly, the rule fails by 0.4 bps on full history |
| RSI(2) Pullback | none | Keep off | Every gate lowers expectancy in every window (e.g. full history +24.9 → +4.4 with `range`) |
| IBS Mean Reversion | none | Keep off | Every gate lowers expectancy in every window |
| Channel Breakout | none | Keep off | `trend_up` lowers expectancy in every window; `not_downtrend` removes almost no trades |
| Trend Pullback | `trend_up` | Keep off, revisit | Much better on full history (+2.5 → +31.7) and since 2015 (+8.9 → +14.1), worse since 2020 (−1.2 → −6.8) |

**Win rate:** no gate moves any strategy toward 80%. RSI(14) and Bollinger reversion win about 40–44% of
trades with bigger wins than losses; RSI(2) and IBS win 60–64%. The gate is a drawdown and expectancy
tool, not a win-rate tool.

All confidence intervals overlap; none of these differences is statistically significant. The gate was not
switched on for any strategy. It ships as an opt-in `regime_gate` parameter that does nothing until a
strategy row sets it.

## The regime gate

`app/services/strategies/regime.py` labels the last completed daily bar with Wilder's ADX(14) and the
directional indicators:

* `trend_up` / `trend_down`: ADX ≥ 25 (`regime_trend_adx`), direction from +DI vs −DI;
* `range`: ADX < 20 (`regime_range_adx`);
* `neutral`: in between.

A strategy row opts in with `regime_gate` (`range`, `not_trending`, `not_downtrend` or `trend_up`). The gate
only turns a BUY into a HOLD (sells and exits are never touched) and records the regime and ADX in the
signal features. `registry.get_strategy` applies it, so the backtester and paper trading run the same code.

## Data and method

* **Data:** yfinance daily OHLCV, split/dividend adjusted, from the 2026-11-02 scratch cache used by
  `rsi2-scope-20261102.md`, for the 45 symbols in `settings.paper_execution_symbols`, through 2026-11-02.
* **Engine:** `strategy_scorecard.run_scorecard`, the same backtester and costs as the scorecard. Signals on
  completed bars, fills at the next open, volatility-scaled costs each side. RSI(2) uses its seeded settings
  (RSI < 10, exit above SMA5, 200-day trend filter on) and IBS its seeded settings (IBS < 0.1, exit IBS > 0.8),
  both with the 10% catastrophe stop and 10-session time stop. The other four use their default parameters.
* **Variants:** 22 (each strategy with no gate plus 2–3 gates) × 45 symbols = 990 backtests, 29 minutes.
* **Windows:** all history per symbol, 2015+, 2020+. Trades are pooled across symbols. Expectancy is the mean
  net return per trade in bps with a 95% cluster-bootstrap CI by entry month. Port DD is the max drawdown of
  the equal-weight daily portfolio; Sym DD is the mean per-symbol max drawdown.
* Script: scratch only (`regime_gate_bt.py`, not committed), built on `StrategyVariant(..., {"regime_gate": ...})`.

## Results

| Strategy | Window | Gate | Trades | Win % | Exp. bps [95% CI] | PF | Port DD % | Sym DD % |
|---|---|---|---:|---:|---|---:|---:|---:|
| rsi_mean_reversion | All | none | 11,060 | 38.4 | +3.4 [-15, 22] | 1.02 | 7.4 | 14.9 |
|  |  | not_downtrend | 5,562 | 39.3 | +9.5 [-11, 31] | 1.05 | 4.2 | 11.0 |
|  |  | not_trending | 5,485 | 39.2 | +8.8 [-12, 30] | 1.04 | 4.3 | 11.1 |
|  |  | **range** | 2,860 | 40.4 | **+18.4** [-8, 45] | 1.10 | **2.5** | 7.7 |
|  | 2015+ | none | 3,990 | 41.9 | +30.5 [-6, 70] | 1.16 | 3.7 | 11.3 |
|  |  | not_downtrend | 2,196 | 42.8 | +34.6 [1, 72] | 1.19 | 2.5 | 9.1 |
|  |  | not_trending | 2,168 | 42.5 | +33.3 [-0, 70] | 1.18 | 2.5 | 9.1 |
|  |  | **range** | 1,169 | 42.9 | **+37.4** [-3, 80] | 1.21 | **1.0** | 6.1 |
|  | 2020+ | none | 2,631 | 40.4 | +24.4 [-20, 74] | 1.12 | 3.7 | 10.6 |
|  |  | not_downtrend | 1,466 | 40.6 | +27.7 [-11, 74] | 1.14 | 2.5 | 8.5 |
|  |  | not_trending | 1,451 | 40.4 | +26.2 [-13, 73] | 1.14 | 2.5 | 8.5 |
|  |  | **range** | 776 | 41.2 | **+35.1** [-14, 92] | 1.19 | **1.0** | 5.8 |
| bollinger_mean_reversion | All | none | 7,010 | 39.5 | +11.1 [-10, 33] | 1.06 | 6.0 | 12.1 |
|  |  | not_downtrend | 4,270 | 39.8 | +11.0 [-12, 35] | 1.06 | 4.0 | 9.6 |
|  |  | not_trending | 4,247 | 39.9 | +11.2 [-12, 36] | 1.06 | 4.1 | 9.7 |
|  |  | **range** | 2,476 | 40.1 | +10.7 [-17, 40] | 1.06 | **2.9** | 7.7 |
|  | 2015+ | none | 2,863 | 42.8 | +38.8 [0, 81] | 1.21 | 2.3 | 9.3 |
|  |  | not_downtrend | 1,789 | 42.9 | +34.5 [-5, 78] | 1.19 | 2.6 | 7.6 |
|  |  | not_trending | 1,784 | 42.9 | +35.0 [-5, 78] | 1.19 | 2.6 | 7.6 |
|  |  | **range** | 1,066 | 44.3 | **+42.3** [-5, 91] | 1.24 | **1.5** | 5.8 |
|  | 2020+ | none | 1,752 | 40.8 | +30.9 [-17, 83] | 1.16 | 2.3 | 8.0 |
|  |  | not_downtrend | 1,102 | 40.4 | +23.4 [-21, 75] | 1.12 | 2.6 | 6.9 |
|  |  | not_trending | 1,100 | 40.4 | +23.2 [-21, 74] | 1.12 | 2.6 | 6.9 |
|  |  | **range** | 649 | 42.4 | **+38.3** [-15, 99] | 1.21 | **1.4** | 5.0 |
| rsi2_pullback | All | **none** | 9,821 | 63.7 | **+24.9** [11, 38] | 1.24 | 1.2 | 3.5 |
|  |  | not_downtrend | 8,510 | 63.2 | +20.1 [6, 33] | 1.20 | 0.8 | 3.3 |
|  |  | not_trending | 6,535 | 62.3 | +15.1 [-0, 30] | 1.15 | 0.6 | 3.3 |
|  |  | range | 4,477 | 61.5 | +4.4 [-15, 21] | 1.04 | 0.8 | 3.2 |
|  | 2015+ | **none** | 4,156 | 62.8 | **+12.7** [-10, 35] | 1.13 | 1.2 | 2.9 |
|  |  | not_downtrend | 3,589 | 62.8 | +10.6 [-13, 33] | 1.11 | 0.8 | 2.6 |
|  |  | not_trending | 2,786 | 62.7 | +7.9 [-17, 32] | 1.08 | 0.6 | 2.4 |
|  |  | range | 1,963 | 62.0 | +2.5 [-26, 27] | 1.03 | 0.5 | 2.1 |
|  | 2020+ | **none** | 2,448 | 62.9 | **+13.6** [-22, 45] | 1.13 | 0.8 | 2.1 |
|  |  | not_downtrend | 2,131 | 62.4 | +9.0 [-24, 39] | 1.09 | 0.5 | 2.0 |
|  |  | not_trending | 1,629 | 61.8 | +1.3 [-33, 33] | 1.01 | 0.5 | 1.9 |
|  |  | range | 1,143 | 60.5 | -4.2 [-40, 26] | 0.96 | 0.5 | 1.6 |
| ibs_mean_reversion | All | **none** | 25,983 | 60.2 | **+15.4** [5, 25] | 1.13 | 2.0 | 6.9 |
|  |  | not_downtrend | 21,630 | 59.7 | +12.0 [1, 21] | 1.10 | 2.1 | 6.6 |
|  |  | not_trending | 16,380 | 59.5 | +8.1 [-4, 19] | 1.07 | 2.1 | 6.2 |
|  |  | range | 10,789 | 59.0 | +4.5 [-8, 18] | 1.04 | 1.3 | 5.5 |
|  | 2015+ | **none** | 10,171 | 60.8 | **+21.3** [4, 39] | 1.21 | 1.2 | 4.0 |
|  |  | not_downtrend | 8,627 | 60.1 | +17.1 [-0, 35] | 1.17 | 1.2 | 4.0 |
|  |  | not_trending | 6,572 | 60.3 | +16.7 [-3, 36] | 1.17 | 1.0 | 3.7 |
|  |  | range | 4,381 | 59.3 | +8.9 [-12, 29] | 1.09 | 0.9 | 3.3 |
|  | 2020+ | **none** | 6,168 | 60.6 | **+23.9** [1, 47] | 1.22 | 1.2 | 3.6 |
|  |  | not_downtrend | 5,313 | 60.6 | +23.3 [1, 47] | 1.22 | 1.2 | 3.3 |
|  |  | not_trending | 4,049 | 60.3 | +19.4 [-5, 44] | 1.18 | 1.0 | 3.2 |
|  |  | range | 2,726 | 59.8 | +15.1 [-13, 41] | 1.14 | 0.9 | 2.8 |
| channel_breakout | All | **none** | 7,881 | 40.1 | **-2.6** [-18, 12] | 0.99 | 5.7 | 13.5 |
|  |  | not_downtrend | 7,879 | 40.1 | -2.7 [-18, 12] | 0.99 | 5.8 | 13.5 |
|  |  | trend_up | 4,237 | 38.1 | -15.0 [-33, 3] | 0.92 | 5.3 | 10.7 |
|  | 2015+ | **none** | 3,081 | 42.4 | **+17.3** [-8, 44] | 1.10 | 2.3 | 9.8 |
|  |  | not_downtrend | 3,080 | 42.4 | +17.1 [-8, 43] | 1.09 | 2.3 | 9.8 |
|  |  | trend_up | 1,617 | 41.2 | +8.8 [-22, 40] | 1.05 | 1.8 | 7.6 |
|  | 2020+ | **none** | 1,797 | 41.2 | **+20.7** [-12, 53] | 1.11 | 1.7 | 8.0 |
|  |  | not_downtrend | 1,796 | 41.1 | +20.4 [-12, 53] | 1.11 | 1.7 | 8.0 |
|  |  | trend_up | 996 | 40.0 | +14.3 [-30, 56] | 1.07 | 1.6 | 6.2 |
| trend_pullback | All | none | 12,891 | 39.8 | +2.5 [-13, 18] | 1.01 | 6.1 | 17.1 |
|  |  | not_downtrend | 11,726 | 40.2 | +6.1 [-9, 22] | 1.03 | 5.6 | 16.2 |
|  |  | trend_up | 2,510 | 43.9 | +31.7 [7, 57] | 1.18 | 1.1 | 6.9 |
|  | 2015+ | none | 5,122 | 41.2 | +8.9 [-17, 36] | 1.05 | 4.5 | 13.5 |
|  |  | not_downtrend | 4,639 | 41.5 | +12.4 [-13, 39] | 1.07 | 4.2 | 12.7 |
|  |  | trend_up | 960 | 42.2 | +14.1 [-28, 61] | 1.08 | 1.1 | 6.2 |
|  | 2020+ | none | 3,066 | 38.5 | -1.2 [-35, 32] | 0.99 | 4.5 | 12.2 |
|  |  | not_downtrend | 2,801 | 38.7 | +2.6 [-31, 35] | 1.01 | 4.2 | 11.7 |
|  |  | trend_up | 598 | 37.0 | -6.8 [-62, 52] | 0.97 | 1.1 | 5.5 |

## Reading

* **RSI(14) and Bollinger reversion** wait for a multi-day slide. In a strong trend that slide is usually the
  trend itself, so those buys lose; the `range` gate removes 65–75% of the trades, mostly those, and
  cuts the portfolio drawdown by half to two thirds.
* **RSI(2) and IBS** buy a 1–2 day dip and exit within days. Those dips snap back in trends too (RSI(2)
  already requires an uptrend through its 200-day filter), so the gate mostly removes good trades.
* **Channel Breakout:** requiring ADX ≥ 25 at the breakout means entering after the move is already strong,
  which is late; the plain breakout is better.
* **Caveats:** 45 symbols that exist today (survivorship bias makes every number look better than reality),
  three overlapping windows, 22 variants without a multiple-testing correction, and fixed ADX thresholds
  (20/25, not tuned). Treat the two "turn on" results as "no evidence against", not a proven edge.

## Turning it on (operator step, not done)

The two candidates would be set on their existing Strategy rows:
`rsi_mean_reversion: {"regime_gate": "range"}` and `bollinger_mean_reversion: {"regime_gate": "range"}`.
Paper results should then be compared with these backtests in the weekly report before anything else is gated.
