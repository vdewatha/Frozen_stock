# 200-Day Trend (Regime) Filter for RSI(2) and IBS, 2026-10-29

**Question:** should RSI(2) Pullback and IBS Mean Reversion only buy when the last completed close is above its
200-day SMA (`trend_filter_sma`, 0 disables)?

**Decision rule (set in advance):** default ON only if net expectancy per trade is not worse AND max drawdown
improves or stays similar.

**Decision:**
* **RSI(2) Pullback: ON (`trend_filter_sma = 200`).** For the seeded variant (RSI < 10), expectancy is the same on
  2001+ and better on 2015+ and 2020+. Portfolio max drawdown falls from 3.7% to 1.7% and the worst trade improves
  from -13.7% to -11.3%.
* **IBS Mean Reversion: OFF (`trend_filter_sma = 0`).** For the seeded variant (IBS < 0.1), the filter lowers
  expectancy in every window: 2001+ +3.3 vs +15.3 bps, 2015+ +13.9 vs +21.3, 2020+ +17.9 vs +29.1. Drawdown is
  similar on 2001+ (2.1% vs 2.0%) and lower only on the later windows, so the rule says off. The class default and
  the seed for new rows are now 0. **An existing IBS Strategy row still has `trend_window: 200` in its
  parameters and keeps the filter until an operator sets `trend_filter_sma: 0` on it.** The seeder never edits
  existing rows.

All confidence intervals overlap. None of these differences are statistically significant. Win rates stay at 59–68%
either way, far from the 80% goal.

## Data and method

* **Data:** yfinance daily OHLCV, split/dividend adjusted (`auto_adjust=True`), downloaded on 2026-10-29 with
  `strategy_scorecard.load_daily_history(..., download=True, end="2026-10-28")` into a scratch cache (not
  committed). Coverage: SPY 1993-01-29, QQQ 1999-03-10, IWM 2000-05-26 and DIA 1998-01-20, each through
  2026-10-27 (8,494 / 6,952 / 6,644 / 7,238 bars). This is the same source and the same index-ETF scope that
  `reports/strategy-scorecard-20261028.md` used. That report pooled 45 symbols, so its numbers differ from these.
* **Engine:** `strategy_scorecard.score_cell` / `pool_variant`, the same shared backtester (`run_backtest`) the
  scorecard uses. Signals come from completed bars and fill at the next open. Costs are volatility-scaled (1 bp fee
  plus a 5 bp slippage floor plus volatility and impact, each side). Exits: 10% catastrophe stop, no target, a
  10-session time stop and the indicator exit. Sizing: `risk_per_trade` 1% with a 10% stop, which is 10% of equity
  per position. That sizing is why the drawdowns are small in absolute terms.
* **Windows:** 2001-06-01+, 2015+ and 2020+. Every symbol has at least 200 bars before 2001-06, so the filtered
  and unfiltered runs cover the same calendar dates.
* **Metrics:** pooled trades across the four ETFs. A win means net return > 0. Expectancy is the mean net return
  per trade in bps, with a 95% cluster-bootstrap CI by entry month. Avg win and avg loss are net % per trade.
  Port DD is the max drawdown of the equal-weight daily portfolio. Sym DD is the mean per-symbol max drawdown.
* Script: `/private/tmp/.../scratchpad/regime.py` (one-off, scratch only). It calls `StrategyVariant(...,
  {"trend_filter_sma": 200 | 0, ...})` once per variant.

## Results

| Variant | Window | Filter | Trades | Win % | Exp. bps [95% CI] | Avg win / loss % | PF | Worst % | Port DD % | Sym DD % |
|---|---|---|---:|---:|---|---|---:|---:|---:|---:|
| RSI(2) < 10, exit > SMA5 (seeded) | 2001+ | SMA200 | 806 | 66.2 | +16.8 [-6.2, 36.5] | 1.12 / 1.71 | 1.29 | -11.3 | **1.7** | 2.1 |
| | 2001+ | off | 1,242 | 65.3 | +16.9 [-9.4, 40.6] | 1.57 / 2.46 | 1.20 | -13.7 | 3.7 | 4.0 |
| | 2015+ | SMA200 | 404 | 66.1 | **+13.7** [-17.4, 40.0] | 1.09 / 1.73 | 1.23 | -11.3 | **1.7** | 2.1 |
| | 2015+ | off | 565 | 63.4 | +3.0 [-37.2, 38.1] | 1.34 / 2.23 | 1.04 | -13.7 | 3.7 | 3.8 |
| | 2020+ | SMA200 | 242 | 67.8 | **+23.4** [-20.4, 63.2] | 1.24 / 1.87 | 1.39 | -11.3 | **1.0** | 1.2 |
| | 2020+ | off | 348 | 63.5 | +14.4 [-47.7, 61.0] | 1.52 / 2.26 | 1.17 | -13.7 | 2.4 | 2.5 |
| RSI(2) < 5, exit > SMA5 | 2001+ | SMA200 | 421 | 64.6 | +18.8 [-17.0, 48.2] | 1.29 / 1.82 | 1.29 | -11.3 | 1.5 | 1.7 |
| | 2001+ | off | 684 | 64.6 | +35.7 [3.0, 66.3] | 1.94 / 2.54 | 1.40 | -13.7 | 1.6 | 2.4 |
| | 2015+ | SMA200 | 222 | 65.3 | +19.6 [-26.3, 57.5] | 1.29 / 1.87 | 1.30 | -11.3 | 1.2 | 1.6 |
| | 2015+ | off | 310 | 63.5 | +18.5 [-26.5, 59.4] | 1.60 / 2.28 | 1.22 | -13.7 | 1.6 | 2.3 |
| | 2020+ | SMA200 | 133 | 67.7 | +23.1 [-51.2, 83.2] | 1.47 / 2.36 | 1.30 | -11.3 | 1.0 | 1.2 |
| | 2020+ | off | 185 | 64.9 | +39.4 [-12.9, 95.0] | 1.84 / 2.28 | 1.49 | -13.7 | 0.9 | 1.2 |
| IBS < 0.1, exit IBS > 0.8 (seeded) | 2001+ | SMA200 | 1,329 | 60.2 | +3.3 [-9.9, 17.1] | 1.04 / 1.49 | 1.06 | -11.1 | 2.1 | 2.8 |
| | 2001+ | off | 2,044 | 62.3 | **+15.3** [0.3, 30.0] | 1.42 / 1.95 | 1.21 | -14.8 | 2.0 | 3.7 |
| | 2015+ | SMA200 | 645 | 61.7 | +13.9 [-9.1, 35.5] | 1.16 / 1.51 | 1.24 | -11.1 | 1.2 | 1.9 |
| | 2015+ | off | 891 | 63.6 | **+21.3** [-3.7, 44.5] | 1.38 / 1.84 | 1.32 | -11.1 | 1.9 | 2.7 |
| | 2020+ | SMA200 | 388 | 62.9 | +17.9 [-14.6, 49.2] | 1.26 / 1.65 | 1.29 | -11.1 | 1.1 | 1.6 |
| | 2020+ | off | 547 | 63.6 | **+29.1** [-3.2, 58.9] | 1.53 / 1.87 | 1.43 | -11.1 | 1.9 | 2.1 |
| IBS < 0.2, exit IBS > 0.8 | 2001+ | SMA200 | 2,052 | 58.8 | -0.9 [-12.7, 10.9] | 1.00 / 1.44 | 0.98 | -11.1 | 2.7 | 4.0 |
| | 2001+ | off | 3,054 | 60.6 | +9.3 [-2.5, 21.0] | 1.37 / 1.87 | 1.13 | -14.8 | 2.8 | 4.2 |
| | 2015+ | SMA200 | 1,013 | 57.6 | +1.9 [-14.6, 19.3] | 1.09 / 1.44 | 1.03 | -11.1 | 2.1 | 2.9 |
| | 2015+ | off | 1,387 | 60.2 | +11.0 [-6.7, 28.8] | 1.32 / 1.72 | 1.16 | -11.1 | 2.8 | 3.4 |
| | 2020+ | SMA200 | 609 | 59.9 | +6.6 [-17.5, 30.7] | 1.17 / 1.58 | 1.10 | -11.1 | 2.1 | 2.7 |
| | 2020+ | off | 854 | 61.2 | +18.3 [-5.5, 40.5] | 1.43 / 1.79 | 1.26 | -11.1 | 2.2 | 2.9 |

## Reading

* The filter removes 25–35% of trades. Those trades are mostly bear-market dips, which bring bigger wins and
  bigger losses: avg loss drops from about 2.3–2.5% to 1.7–1.9% for RSI(2), and the worst trade improves from
  -13.7% to -11.3%.
* For RSI(2) < 10 (the seeded threshold), the trades the filter removes have roughly zero or negative expectancy
  since 2015. Removing them keeps or raises expectancy and halves drawdown, so the filter stays on.
* For IBS, the trades below the 200-day SMA are profitable on average, because one-day weakness snaps back harder
  in down markets. The filter gives up 7–12 bps per trade for a drawdown gain that only appears after 2015.
* RSI(2) < 5 does better without the filter (+35.7 vs +18.8 bps on 2001+, with similar drawdown). It is not the
  seeded threshold, so it does not change the default. It is a candidate to revisit with forward paper data.
* Caveat: this is 4 symbols and 3 overlapping windows, with no multiple-testing correction. Treat the defaults as
  "no evidence against", not as a proven edge.
