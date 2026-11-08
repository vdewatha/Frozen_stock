# Pairs kill test: 2026-11-07

Step 5 of the build plan in [Retail bot edge feasibility](Retail%20bot%20edge%20feasibility.md). Branch `feat/short-horizon-reverdict`. Paper only. Nothing is built and nothing changes live.

**Outcome: the market-neutral lane closes for good.** The test hedges each sector-reversal trade with its sector ETF. Hedging leaves a real but small residual: +12.5 bps a trade since 2015, with an entry-date clustered 95% interval of [+6.3, +18.6] (t 4.0). That is a fifth of the 60 bps the rule requires, and it is 7.5 bps short of the 20 bps that four fills cost at 5 bps each. The one variant without the uptrend filter does worse: +6.6 bps [−10.1, +23.3]. Both fail, so under the pre-registered row the lane closes, and Stage A of the pairs note is not built.

The pairs note's deciding question was whether long-only sector reversal failed because of beta noise or because no residual edge exists. The answer is in between. About 9 of the 21 bps of gross per trade since 2015 was the sector's own move. What remains is a genuine residual reversal: it is the same sign in most slices and positive with t ≈ 4. But it is far too small to pay for a two-legged trade, even at the note's optimistic 5 bps a fill. The edge sits where the note expected it, at VIX ≥ 25 (+48.8 bps [+26, +71], 1,059 trades since 2015). Even that bucket alone misses the 60 bps bar, and the rule does not let an edge confined to a VIX bucket proceed.

## The rule and the numbers

Pre-registered in `artifacts/api-server/backend/scripts/run_pairs_kill_test.py`:

* **Residual gross**, per trade: the stock's gross return over the hold, from the next open to the exit fill, minus beta × the sector ETF's return over the same hold. Beta is the 60-session OLS slope of the stock's daily returns on its sector index, ending on the decision bar.
* **Proceed to Stage A only if** the mean 2015+ residual gross is at least 60 bps (three times four fills at 5 bps each) **and** its 95% interval, clustered by entry date, is above zero.
* On a fail, one variant **without the uptrend filter** goes through the same rule. If it also fails, the lane closes for good. If it passes, only the uptrend version closes.

| Run | Trades (2015+) | Stock gross 2015+ | ETF leg (beta-weighted) | Mean beta | Residual gross 2015+ [95% CI] | t | ≥ 60 bps | CI > 0 | Result |
|---|---|---|---|---|---|---|---|---|---|
| Default rule (close > SMA200, `rel_z_5 < −2`) | 18,469 (10,211) | +21.3 | +8.7 | 0.92 | **+12.5 [+6.3, +18.6]** | 4.0 | fail | pass | **fail** |
| Variant without the uptrend filter (`rel_z_5 < −2`) | 34,085 (19,172) | +16.1 | +9.9 | 0.97 | **+6.6 [−10.1, +23.3]** | 0.8 | fail | fail | **fail** |

Lane decision: **close the market-neutral lane for good** (`close_market_neutral_lane`).

The default run reproduces PR #42's trade count exactly. It uses the same code (`run_sector_reversal_backtest.variant_trades`) on the same cache: 18,469 trades from 691 single stocks with a sector benchmark, 30 of them on the static map and 661 assigned statistically.

## By VIX bucket

The bucket is the VIX close on the decision bar. ≥ 25 is the pairs note's crisis bucket. The last two rows use PR #42's split, above or below the trailing 252-session median.

| Slice, 2015+ | Default: trades | Stock gross | Residual [95% CI] | t | No-uptrend variant: trades | Residual [95% CI] | t |
|---|---|---|---|---|---|---|---|
| VIX < 15 | 3,696 | +12.6 | +17.1 [+8.8, +25.4] | 4.0 | 5,626 | +12.9 [+5.4, +20.3] | 3.4 |
| VIX 15-20 | 3,820 | +13.9 | +2.3 [−7.8, +12.4] | 0.4 | 6,545 | −7.7 [−18.3, +2.9] | −1.4 |
| VIX 20-25 | 1,636 | +32.6 | +2.4 [−15.4, +20.1] | 0.3 | 3,186 | −6.2 [−22.6, +10.1] | −0.7 |
| VIX ≥ 25 | 1,059 | +61.1 | **+48.8 [+26.2, +71.4]** | 4.2 | 3,815 | +32.7 [−46.5, +111.9] | 0.8 |
| VIX above its 252-day median | 4,539 | +20.8 | +12.7 [+2.7, +22.7] | 2.5 | 10,437 | +5.4 [−24.6, +35.3] | 0.4 |
| VIX below its 252-day median | 5,672 | +21.8 | +12.3 [+4.7, +20.0] | 3.2 | 8,735 | +8.1 [+0.4, +15.8] | 2.1 |

Over all history the default rule's VIX ≥ 25 bucket earns +32.6 bps [+16.9, +48.4] (2,119 trades) and the variant's +44.0 bps [+4.9, +83.2] (8,131 trades). In calm markets (VIX < 15) the residual is small but steady. In between, it is indistinguishable from zero.

## By crisis sub-period

These are the pairs note's sub-periods, as calendar years except 2018 Q4. "Rest of 2015+" is every other 2015+ trade.

| Sub-period | Default: trades | Stock gross | ETF leg | Residual [95% CI] | No-uptrend variant: trades | Stock gross | Residual [95% CI] |
|---|---|---|---|---|---|---|---|
| 2015-16 | 1,376 | +23.9 | +14.4 | +8.9 [−4.0, +21.8] | 2,719 | +28.7 | −0.2 [−14.3, +13.8] |
| 2018 Q4 | 213 | −113.5 | −125.3 | −8.4 [−58.7, +41.9] | 543 | −123.0 | −12.6 [−53.0, +27.9] |
| 2020 | 870 | −0.6 | −24.3 | +25.2 [−2.3, +52.8] | 2,361 | −44.7 | +26.1 [−100.3, +152.5] |
| 2022 | 651 | −9.0 | −14.3 | +6.7 [−19.2, +32.7] | 1,847 | +23.6 | +10.9 [−12.6, +34.5] |
| 2025 | 872 | +25.7 | +19.4 | +4.8 [−20.1, +29.8] | 1,722 | +41.0 | +10.7 [−12.3, +33.6] |
| Rest of 2015+ | 6,229 | +31.0 | +17.6 | +13.9 [+6.5, +21.4] | 9,980 | +29.0 | +3.4 [−4.8, +11.6] |

Hedging does what it should in the sell-offs. In 2018 Q4 the long legs lost 114 bps a trade and the residual is −8 bps. In 2020 a flat long leg becomes +25 bps once the sector's fall is removed. But no crisis sub-period's residual has an interval above zero, and none reaches 60 bps. The evidence does not show the crisis-burst payoff the literature reports for daily long-short stat-arb. That literature also tested short legs, which these trades do not have (see the limits).

## Robustness (reported, not decisive)

* **Outliers.** Winsorising the residual at its 1st and 99th percentiles barely moves the default rule (+12.5 → +12.0 bps). It moves the variant from +6.6 to +2.9 bps, because the variant's mean leans on a handful of March 2020 rebounds (RCL +50%, UAL +44%, HIG +41% residual). The median residual is about +31 bps in both runs, above the mean, because the losers are larger than the winners. The rule reads the mean, which is what a book earns.
* **Data glitches.** The run reproduces PR #42 unchanged, including the flat zero-volume bars yfinance serves for a few delisted tickers (see `reports/short-horizon-reverdict-20261107.md`). Since 2015 those five names contribute 9 default trades and 26 variant trades out of 10,211 and 19,172. They cannot move a mean that is 47 bps short of the bar.
* **Costs.** The residual is gross. The rule's 60 bps is 3 × (4 fills × 5 bps). PR #42's published model charges about 33 bps on the stock and 23-27 bps on the ETF, roughly 56-60 bps for one pair round trip, so at the model the residual is about −45 bps a trade.

## Stated limits

* **Every default-rule trade passed a close > SMA200 filter.** That filter removes much of the sell-off regime where the pairs note expects the edge to live. The no-uptrend variant is the test of that objection, and it fails too: removing the filter adds 15,616 trades in stocks below their 200-day average, and the residual falls.
* **Entries are long only.** The residual is the long leg's alpha with the sector ETF as a hedge. It is not a two-sided pairs book: there is no short-stock signal, and no test of whether a relative *rise* reverses. A two-sided design could, in principle, earn what this one does not. The plan's row closes the lane on this test, and short legs would add borrow fees, the dividends a short owes and Rule 201 restrictions that Alpaca paper does not simulate.
* **661 of the 691 stocks have a statistically assigned sector ETF.** It is the ETF whose SPY-residual returns tracked the stock best over the trailing year, refreshed monthly, and it is fixed for each trade at its decision bar. Only today's 30 mapped names use the static GICS map. A wrong assignment adds noise to the hedge, but the mean beta of 0.92 and the large cut in crisis-period dispersion say the hedge mostly works.
* **The ETF leg is approximated at the exits.** It runs from the entry session's open to the exit session's open, matching the stock's open exits. On an intraday stop exit, where the stock left at its stop level, the ETF leg uses that session's close. Beta comes from close-to-close returns on the chained sector index.
* **Point-in-time coverage** since 2010 is 80.9%, and names Yahoo dropped are missing, so some survivorship remains (`reports/survivorship-20261106.md`).
* **In-sample.** The sector-reversal trades were already seen in PR #42. This test only adds the hedge.

## What changes

Nothing live. No code path, strategy row or setting changes. The `sector_reversal` row stays paused as PR #42 created it. The short pilot remains a plumbing and cost-measurement lane, as the feasibility report's decision already said. The pairs note's Stage A backtest is not built. Under the row's rule this result closes the market-neutral lane for good, so no further pairs or stat-arb trials should be spent on this universe.

## Reproduce

From `artifacts/api-server/backend`, with PR #42's cache (yfinance bars for the 707 point-in-time names and the 11 sector ETFs, the membership file under `universe/`, and a `date,close` VIX csv):

    PYTHONPATH=. python scripts/run_pairs_kill_test.py --data-dir <cache> --vix-csv <vix.csv> \
        --output out.json --tables tables.md [--trades-dir <dir>]

The run takes about 2 minutes (70 s loading and sector assignment, 40 s for both runs). The decision functions are tested in `tests/test_pairs_kill_test.py`.
