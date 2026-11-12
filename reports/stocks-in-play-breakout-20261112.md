# 5-minute opening-range breakout on "stocks in play", with trading costs: 2026-11-12

Pre-registration: `research_notes/Stocks in play breakout 20261112/PREREGISTRATION.md` (commit `df4b279`, made
before any minute bar was downloaded apart from a 30-request probe of what the free plan serves). Script, tables
and trades: the same folder. Local research only: nothing was sent to the deployed app, no app code was changed,
and no account or order endpoint was touched.

## Verdict: fail, on all three criteria

The primary variant is long-only, unlevered, with 2 cents a share of slippage on each fill, judged on
2024-01-02 to 2026-11-11 (719 sessions the paper's authors never saw).

| Criterion (held-out, long-only, unlevered, 2 cents a side) | Needed | Result | |
|---|---|---|---|
| 1. Profit per trade | mean net > 0 with day-clustered t ≥ 2.0 | **−33.9 bps** a trade, t **−16.45** (5,836 trades, 1,625 stocks) | fail |
| 2. Beats SPY | annual return above SPY's +19.2% | **−27.8%** a year | fail |
| 3. Not a few big winners | mean net > 0 without the top 1% of trades | **−44.7 bps** | fail |

Other figures for the same run: win rate 7.6%, Sharpe −9.7, maximum drawdown −60.5%, annual volatility 3.3%,
correlation with SPY 0.11, average exposure 40% of equity.

**Break-even slippage: there is none.** Under the pre-registered worst-case reading the rule loses 17.3 bps a
trade before commission and slippage. Its break-even is **−2.8 cents** a share a side: it would have to be paid
2.8 cents on every fill to reach zero. Under the most favourable reading of the same bars (explained below) the
break-even is **+0.76 cents** a share a side, and the rule still fails all three criteria at zero slippage.

**What this means for the bot.** The one idea the day-trader research left open is closed. There is no case for
paying for a real-time consolidated feed for it.

## Why the published 41.6% a year does not survive

The paper's stop-loss is 10% of the stock's daily range (ATR). On the stocks it selects that is a median of
**12.8 cents** on a $37.71 stock. These are the most active stocks of the morning, minutes after the open, and a
single 1-minute bar is usually wider than that.

So in **half of all trades the entry and the stop-loss are inside the same 1-minute bar** (49.7% of 32,267
triggered trades in 2016-2023; 55.9% of 11,343 in 2024-2026; 57.0% of the long-only held-out trades). Minute
bars cannot say which came first. The two readings give opposite answers:

| Paper's own setup: long and short, 4x leverage, commission only, 2016-2023 | Annual return | Sharpe | Max drawdown | Days with a profit | Win rate a trade |
|---|---|---|---|---|---|
| Paper (table 2) | +41.6% | 2.81 | 12% | 48.4% | 17-21% |
| This test, optimistic: the stop-loss can only be hit from the next minute on | **+35.2%** | 2.17 | 17.1% | 47.8% | 14.5% |
| This test, worst case: if the entry minute also touches the stop-loss, the trade is stopped out | **−42.8%** | −5.77 | 98.9% | 27.1% | 9.8% |

The optimistic reading lands close to the paper on every column. The worst case loses nearly everything. The
paper does not say how it handled this. My inference, not a fact from the paper: its result needs the favourable
assumption about what happens inside the entry minute. The truth for a live trader is somewhere between the two
rows, and the brief for this test was to assume the worse one.

The verdict does not depend on which reading is right. Even the optimistic one:

* is wiped out by **1 cent** of slippage a share a side (paper's setup, 2016-2023: +35.2% a year at zero, −2.9% at
  1 cent, −30.3% at 2 cents);
* earns its whole profit from the top 1% of trades (without them: −8.9 bps a trade at zero slippage);
* long-only and unlevered on the held-out period makes +6.7% a year at zero slippage against SPY's +19.2%.

One cent is less than the usual spread on these stocks at 09:35, and both the entry and the stop-loss are stop
orders that become market orders in a fast move.

## Replication against the paper

Pre-registered check: the paper's figure must lie between this test's worst-case result minus 10 points and its
optimistic result plus 10 points, for both the top-20 strategy and the base strategy (every filtered stock, no
relative-volume rule), and the top 20 must beat the base strategy under both readings.

| | Paper | This test, worst case | This test, optimistic | Inside the bracket |
|---|---|---|---|---|
| Top 20 by relative volume, 2016-2023 (2,012 sessions) | +41.6% a year | −42.8% | +35.2% | yes |
| Base strategy, every 5th session of 2016-2023 (400 sessions, 283,608 trades) | +3.2% a year | −20.0% | −1.0% | yes |
| Top 20 above the base strategy | yes | **no** (−42.8% against −20.0%) | yes (+35.2% against −1.0%) | **fails** |

**By the registered rule the pipeline did not reproduce the paper**, because the third line fails under the worst
case. The reason is the one above: the top-20 stocks have the entry and stop-loss in the same minute 49.7% of the
time, the average filtered stock 16.6% of the time, so the worst-case rule punishes exactly the stocks the paper
favours. Under the optimistic reading the reproduction is close, including the paper's figure 4 (profit in units
of risk by relative volume, base strategy, commission only):

| Relative volume | Paper (figure 4) | This test, optimistic | This test, worst case |
|---|---|---|---|
| under 100% | −0.02R | −0.026R (197,945 trades) | −0.098R |
| over 100% | +0.08R | +0.043R (84,749 trades) | −0.147R |
| over 30x | +0.38R | +1.26R (192 trades) | −0.016R |

Differences that remain: the paper's universe includes ETFs and foreign stocks (this one has US companies that
file insider forms, 8,719 of them, 3,559 since delisted), and its minute data come from another vendor.

## Held-out result at each slippage level (long-only, unlevered, 2024-01-02 to 2026-11-11)

Slippage is per share, on the entry and again on the exit, on top of the $0.0035 commission. SPY made +19.2% a
year over the same sessions. Criterion 1 needs t ≥ 2.0, criterion 2 an annual return above SPY's, criterion 3 a
positive mean without the top 1% of trades.

| Intrabar reading | Slippage | Net bps a trade | t (by day) | Annual return | Without top 1%, bps | Sharpe | Max drawdown | 1 | 2 | 3 |
|---|---|---|---|---|---|---|---|---|---|---|
| Worst case | none | −19.8 | −9.77 | −16.9% | −30.7 | −5.64 | −41.0% | no | no | no |
| Worst case | 1 cent | −26.8 | −13.15 | −22.5% | −37.7 | −7.72 | −51.7% | no | no | no |
| **Worst case (the verdict)** | **2 cents** | **−33.9** | **−16.45** | **−27.8%** | **−44.7** | −9.72 | −60.5% | **no** | **no** | **no** |
| Worst case | 5 cents | −55.1 | −25.58 | −41.5% | −65.8 | −15.06 | −78.3% | no | no | no |
| Worst case | 5 bps of price | −29.8 | −14.71 | −24.9% | −40.6 | −8.69 | −55.8% | no | no | no |
| Optimistic (side result) | none | +5.4 | +1.49 | +6.7% | −12.0 | 1.13 | −4.1% | no | no | no |
| Optimistic (side result) | 1 cent | −1.7 | −0.48 | −0.5% | −19.0 | −0.06 | −8.5% | no | no | no |
| Optimistic (side result) | 2 cents | −8.8 | −2.44 | −7.3% | −26.1 | −1.25 | −20.0% | no | no | no |
| Optimistic (side result) | 5 cents | −30.0 | −8.23 | −24.9% | −47.1 | −4.76 | −55.7% | no | no | no |
| Optimistic (side result) | 5 bps of price | −4.7 | −1.30 | −3.6% | −22.0 | −0.59 | −11.0% | no | no | no |

Correlation with SPY's daily return is 0.09 to 0.13 (worst case) and 0.18 to 0.20 (optimistic).

The other two variants on the held-out period, worst case, 2 cents: long and short unlevered −35.2 bps a trade
(t −23.63), −48.1% a year; long and short with 4x leverage −75.7% a year. Every variant, period, cost level and
reading is in `tables.md`.

### Break-even slippage (long-only, unlevered)

| | Mean trade is zero at | Annual return is zero at | Annual return equals SPY's at |
|---|---|---|---|
| Held-out, worst case | −2.80 cents (never) | never (−16.9% a year at zero slippage) | never |
| Held-out, optimistic | +0.76 cents | +0.93 cents | never (+6.7% at zero against +19.2%) |
| 2016-2023, worst case | −1.57 cents (never) | never (−11.1% at zero) | never |
| 2016-2023, optimistic | +0.89 cents | +0.81 cents | never (+5.9% at zero against +13.3%) |

## The rule that was tested

Taken from the paper (Zarattini, Barbon, Aziz, "A Profitable Day Trading Strategy for the U.S. Equity Market",
2024), sections 2.1 and 4. Each session:

1. Keep stocks that open above $5, traded at least 1,000,000 shares a day on average over the last 14 sessions,
   and have a 14-day average true range above $0.50.
2. Take each stock's first 5 minutes (09:30 to 09:35 ET). Relative volume is that 5-minute volume divided by the
   stock's average 5-minute opening volume over the last 14 sessions. It must be at least 100%.
3. Pick the 20 stocks with the highest relative volume.
4. If the first 5 minutes closed above their open, place a buy stop at their high. If below, a sell-short stop
   at their low. If equal, no order.
5. Stop-loss 10% of the ATR from the fill. Otherwise exit at the close.
6. Size each position so that a stop-out loses 1% of its slice of capital (equity split into 20 slices), capped at
   4x the slice in the paper, at 1x in the unlevered variants. Commission $0.0035 a share.

Variants, fixed in advance: **A** long and short, 4x (replication only); **B** long and short, unlevered; **C**
long only, unlevered (the primary one: the bot is long-only and unlevered). Slippage levels: none, 1, 2 and 5
cents a share a side, and 5 bps of price. Primary: 2 cents. Nothing else was tried.

Each session's top 20 over the whole period: 54,340 selections, 27,316 long, 26,582 short, 442 with no direction.
Of the 53,898 orders, 80.9% triggered. The 20th stock's relative volume had a median of 3.4x; the selected
stocks' median was 5.3x.

## Results in groups (long-only, unlevered, worst case, 2 cents; net bps a trade)

No group is positive in either period.

| Opening price | 2016-2023 trades | bps | Held-out trades | bps |
|---|---|---|---|---|
| $5-10 | 986 | −72.1 | 391 | −119.0 |
| $10-20 | 2,784 | −42.4 | 1,056 | −52.1 |
| $20-50 | 6,424 | −23.4 | 1,949 | −27.8 |
| $50-100 | 3,764 | −13.7 | 1,279 | −21.0 |
| over $100 | 2,306 | −10.9 | 1,161 | −13.1 |

| ATR as a share of price | 2016-2023 trades | bps | Held-out trades | bps |
|---|---|---|---|---|
| under 3% | 6,796 | −13.4 | 1,928 | −16.9 |
| 3-5% | 4,919 | −24.2 | 2,111 | −25.9 |
| 5-8% | 2,923 | −37.6 | 1,244 | −49.8 |
| over 8% | 1,626 | −59.3 | 553 | −88.2 |

| Relative volume | 2016-2023 trades | bps | Held-out trades | bps |
|---|---|---|---|---|
| 1-2x | 187 | −19.8 | 22 | −47.1 |
| 2-5x | 8,134 | −20.6 | 2,190 | −31.3 |
| 5-10x | 5,271 | −30.0 | 2,325 | −32.4 |
| over 10x | 2,672 | −32.7 | 1,299 | −40.9 |

Higher relative volume is worse under the worst case, the opposite of the paper's claim: the more unusual the
opening volume, the wider the first minutes and the more often the stop-loss sits inside the entry bar.

**Stocks since delisted** (no daily bar in the last 10 sessions): 23.6% of the 2016-2023 long trades (3,845,
−30.1 bps against −24.2 for stocks still listed) and 5.6% of the held-out long trades (327, −38.2 bps against
−33.6). They are 19.1% of all selections. Leaving them out would not have changed the answer.

**Year by year** (worst case, 2 cents; return for the year):

| Year | C trades | C bps a trade | C | B | A (4x) | SPY |
|---|---|---|---|---|---|---|
| 2016 | 1,923 | −19.8 | −18.4% | −33.5% | −67.6% | +13.3% |
| 2017 | 2,010 | −20.8 | −18.8% | −37.5% | −74.1% | +21.7% |
| 2018 | 2,034 | −20.9 | −19.9% | −34.7% | −66.9% | −5.0% |
| 2019 | 2,018 | −22.1 | −19.7% | −37.1% | −70.7% | +31.1% |
| 2020 | 2,030 | −27.9 | −25.2% | −52.4% | −73.5% | +18.5% |
| 2021 | 2,083 | −41.2 | −33.2% | −52.2% | −72.6% | +28.6% |
| 2022 | 2,038 | −27.6 | −24.4% | −42.1% | −64.3% | −18.2% |
| 2023 | 2,128 | −23.7 | −21.4% | −41.8% | −73.1% | +26.2% |
| 2024 | 2,059 | −29.8 | −25.4% | −46.5% | −78.2% | +24.9% |
| 2025 | 1,988 | −36.6 | −28.9% | −48.6% | −73.5% | +17.7% |
| 2026 (to 11-11) | 1,789 | −35.5 | −25.5% | −44.1% | −69.6% | +12.3% |

Every year loses, in every variant. It loses in every year before slippage as well (`tables.md`).

## What trading it live would need

Not worth building, on the result above. For the record:

* **The ranking needs the whole market's volume at 09:35.** About 700 to 1,200 stocks pass the daily filters each
  session. Their 09:30-09:35 volume, and the same for the 14 sessions before, must be known within seconds of
  09:35, and up to 20 stop orders placed straight away.
* **Alpaca's free plan cannot supply that.** Free real-time data is the IEX exchange only, a small share of each
  stock's volume, with a 30-symbol streaming cap. Consolidated volume on the free plan is 15 minutes late. The
  real-time consolidated feed costs $99 a month. Ranking on IEX volume alone would be a different rule; it was
  not tested.
* **Fills.** Both the entry and the stop-loss are stop orders in fast markets, 12.8 cents apart at the median.
  A paper account fills them without market impact, so a paper run would not measure the slippage that decides
  the result.

**Side result, not part of the verdict: entries delayed 15 minutes.** This is what the free plan allows: rank at
09:50 on the late data and place the orders then (at the 09:50 open if the price is already beyond the level).
Worst-case reading, held-out period, 2 cents:

| Variant | Trades | Net bps a trade | t | Annual return |
|---|---|---|---|---|
| A, long and short, 4x | 10,096 | −19.4 | −9.53 | −51.4% |
| B, long and short, unlevered | 10,096 | −19.4 | −9.53 | −28.3% |
| C, long only, unlevered | 5,174 | −18.0 | −6.32 | −14.6% |

Nothing survives. The delayed version loses less than the on-time one only because fewer entries share a minute
with their stop-loss later in the morning (30% against 57%). At zero slippage it is still negative: −3.9 bps a
trade (t −1.40), −3.3% a year. The delayed version was run under the worst-case reading only.

## Data and coverage

* **Minute bars.** Alpaca's free plan serves consolidated (SIP) 1-minute bars from 2016-01-04, delisted tickers
  included, so the test runs on the paper's own period. Opening bars: 3,009,498 fetched across 2,731 sessions.
  Full-session bars: the top 20 and SPY on every session, and every filtered stock on 400 sessions of 2016-2023
  for the base strategy. No selected order was left without minute bars.
* **Daily bars** (filters, ATR, identity): the dip-rules test's cache, with its insider-price identity check.
* **Ticker identity.** Each stock was requested under every ticker it ever carried, with the request dated to the
  session. An opening bar was accepted only if it fits inside that stock's own daily bar for the session; 979
  bars failed and were discarded. A reused ticker therefore never supplied bars for the old company.
* **Eligible stock-days:** 2,481,287 pass the daily filters. 19,050 had no usable opening bar, 41,844 opened at
  $5 or below, 7,346 (0.3%) could not be ranked because a baseline session was missing. The first 14 sessions
  have no baseline and no trades.

## Corrections to the pre-registration

1. **Half days.** The registered detector (SPY's afternoon volume under 5% of the morning's) found 1 of the 21
   early-close sessions, because SPY keeps trading after a 13:00 close. On the other 20 the registered run held
   positions into after-hours bars. Found after the run. A sensitivity run with the exchange's list of early
   closes changes nothing: primary result −33.90 bps a trade (t −16.43), −27.8% a year; the paper's setup
   optimistic +35.4% a year, worst case −42.8% (`results_early_closes.json`). The figures in this report are the
   registered run's.
2. **Stocks with no opening bar.** The registration counted a session with a daily bar and no opening bar as zero
   opening volume. Before any return was computed, the first downloads showed 427 stocks with ten or more such
   sessions in a row: tickers the data vendor does not know under that name, not quiet opens. Counting them as
   zero would shrink the baseline and inflate relative volume. Changed: two or more such sessions in a row are
   missing (25,696 stock-days); a single one stays zero (4,658).
3. **Download.** Four processes instead of "at most 3 threads", still under 200 requests a minute. No effect on
   the data.
4. **Base-strategy sample:** 403 sessions, 400 with eligible stocks (registered as "about 402").
5. **Replication check:** fails on its third line, as reported above. I wrote that line expecting the worst-case
   rule to hit both strategies alike; it hits the top 20 three times as often.

## Limits

* Minute bars are trades, not quotes: the spread is not observed and slippage is an assumed flat cost.
* The intrabar order of entry and stop-loss is unknown in half the trades. Trade-by-trade data could settle it,
  but the verdict would not change: the favourable bound already fails.
* No ETFs or foreign stocks; shorts pay no borrow fee (flatters A and B only); no interest on idle cash;
  capacity not tested.

## Files

* `research_notes/Stocks in play breakout 20261112/PREREGISTRATION.md`: the frozen rule.
* `stocks_in_play_orb.py` (same folder): reproduces everything; stages and commands in its header.
* `tables.md`, `results.json`: every variant, period, cost and reading; `results_early_closes.json`: the
  half-day sensitivity.
* `trades_top20.csv.gz`: every top-20 trade under each reading (126,011 rows). The 567,216 base-strategy trades
  and all downloaded bars are in `~/.paper-training/stocks-in-play-breakout-20261112/`, outside the repo.
