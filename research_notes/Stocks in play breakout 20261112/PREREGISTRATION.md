# 5-minute opening-range breakout on "stocks in play", with trading costs: pre-registration (2026-11-12)

Frozen before any minute bar was downloaded for this test, apart from the step-0 probe below. Do not edit after
the run; corrections go in the report.

## Question

Zarattini, Barbon and Aziz, "A Profitable Day Trading Strategy for the U.S. Equity Market" (February 2024,
SSRN 4729284) report 41.6% a year with a Sharpe ratio of 2.81 for a 5-minute opening-range breakout on the 20
stocks with the most abnormal opening volume each day, 2016 to 2023, against 3.2% a year for the same breakout on
every liquid stock. The study charges a commission of $0.0035 a share and nothing else: no spread, no slippage on
its stop orders. It uses up to 4x leverage and has no held-out period.

Does the rule still make money once slippage is charged, without leverage, long-only (which is what the bot can
trade), on the period after the paper ends? The expected answer is no. This test tries to show that.

## What was looked at before writing this

1. **Step-0 probe of Alpaca's free plan** (about 30 requests, `GET https://data.alpaca.markets/v2/stocks/bars`,
   `feed=sip`): 1-minute bars are served from 2016-01-04 (nothing on 2015-12-01) to yesterday; delisted tickers
   (LHO, ATW, XCO, FRBK, TCON) return full-session minute bars with and without `asof`; the 5-minute bar stamped
   09:30 ET equals the sum of its five 1-minute bars (859 of 859 symbols on 2019-06-03); one request accepts at
   least 2,500 symbols and takes about 7 ms a symbol; the rate limit is 200 requests a minute. No returns after
   09:35 were computed from any of it.
2. **The paper** (the text, tables 1 to 5 and figure captions).
3. **Daily bars already cached by the dip-rules test** (`~/.paper-training/dip-rules-full-market-20261112`): the
   count of stock-days that pass the paper's volume and ATR filters (2,481,287 over 2,731 sessions; about 700 a
   day in 2016, about 1,200 in 2026). No minute bar and no strategy return.

So the test can run on the paper's own period and on delisted names. Nothing is substituted.

## The paper's rule (section 2.1 and section 4), as read

Each session, for each stock:

1. **Filters, known at 09:35 ET.** Opening price above $5. Average daily volume over the previous 14 sessions at
   least 1,000,000 shares. Average true range (ATR) over the previous 14 sessions above $0.50.
2. **Opening range.** The first 5 minutes, 09:30 to 09:35 ET: its open, high, low, close and volume.
3. **Relative volume.** The opening-range volume divided by the mean opening-range volume of the same stock over
   the previous 14 sessions. It must be at least 100%.
4. **Selection.** The 20 stocks with the highest relative volume among those that pass 1 and 3.
5. **Direction.** Opening range closed above its open: long only. Closed below: short only. Equal: no order.
6. **Entry.** A stop order at the opening-range high (long) or low (short), working from 09:35.
7. **Stop-loss.** 10% of the ATR away from the entry price.
8. **Exit.** At the stop-loss, otherwise at the end of the session (16:00 ET).
9. **Size.** A stop-out loses 1% of the capital allocated to the position; leverage at most 4x.
10. **Cost.** $0.0035 a share commission. Starting capital $25,000.

The **base strategy** is steps 1, 2 and 5 to 10 on every stock that passes the filters (no relative-volume rule).

### Readings the paper leaves open, fixed here

* **Capital per position.** The paper says "the capital deployed for that position". Reading: equity is split
  into 20 equal slices; each selected stock gets one slice; the position is the smaller of (a) the size at which a
  stop-out loses 1% of the slice and (b) 4 times the slice. A slice whose stock places no order, or whose order
  never triggers, sits in cash. For the base strategy the equity is split equally among that day's orders. This
  reading reproduces the paper's scale: its 41.6% a year is about 0.14R a slot a day, in line with its figure 4,
  and its footnote 3 says the leverage cap binds "in some trades".
* **ATR.** The simple mean of the true range over the 14 previous sessions (t−14 to t−1), in the unadjusted price
  scale of each day.
* **Opening price.** The open of the opening-range bar.
* **Doji in the top 20.** It keeps its slot and places no order.
* **Fewer than 20 qualify.** The slices stay at 1/20 of equity.
* **Shares.** Fractional; no rounding, no minimum commission.
* **Inside a 1-minute bar.** The paper does not say. See "Simulation".

## Variants

| | Sides | Leverage | Purpose |
|---|---|---|---|
| **A** | long and short | 4x per slice (the paper's) | replication of the paper only |
| **B** | long and short | none | reported |
| **C** | long only | none | **primary: the verdict is on C** |

* **Unlevered (B and C):** each position is the smaller of the 1%-risk size and its slice, so gross exposure
  never exceeds 100% of equity.
* **Long only (C):** the selection is unchanged (top 20 by relative volume). Stocks whose opening range closed
  down are skipped and their slices sit in cash. C is primary because the bot is long-only and unlevered.
* Shorts pay no borrow fee and are assumed always borrowable. This flatters A and B and does not touch C.
* No other variant, filter, threshold, range length, stop distance or exit is tried.

## Costs

Every variant is reported at each of these, on top of the $0.0035 a share commission per side:

| Label | Slippage per share, per side, on the entry and on the exit |
|---|---|
| 0c | none (the paper's cost) |
| 1c | $0.01 |
| **2c** | **$0.02: the primary cost for the verdict** |
| 5c | $0.05 |
| 5bps | 0.05% of the fill price |

* 2 cents is fixed before seeing any result. The entry is a stop order that becomes a market order during a
  breakout in the day's most active stocks, and the stop-loss is another one, so both pay at least the half
  spread and usually more. No source was found that justifies a smaller figure; the smallest allowed stop
  distance is 5 cents (10% of a $0.50 ATR), so 2 cents a side (4 cents a round trip) is 80% of one unit of risk there.
* Slippage is charged as a cost on each fill. The price path, the trigger and the stop-loss level are those of the
  zero-slippage run, so results at every cost level come from the same trades and the break-even is exact.
* **Break-even slippage** for C on the held-out period: the cents per share per side at which the mean net
  return per trade is zero (it is linear in slippage). Also reported: the level at which C's annual return is
  zero and the level at which it equals SPY's.

## Periods

* **Replication:** 2016-01-04 to 2023-12-29 (the paper's 2016 to 2023).
* **Held-out:** 2024-01-01 to 2026-11-11, the last complete session when this was written. **The verdict is on
  the held-out period only.** The paper is dated February 2024, so none of it was available to its authors.

## Pass rule

Variant C, held-out period, 2 cents a side, worst-case intrabar rule. It passes only if all three hold:

1. **Profit per trade.** Mean net return per trade above zero, with a day-clustered t of at least 2.0.
   (Net return per trade = profit after commission and slippage, divided by the entry price. Clusters = sessions.)
2. **Beats SPY.** The net annual return of the C portfolio is above SPY's annual total return over the same
   sessions.
3. **Not a few big winners.** The mean net return per trade is still above zero after dropping the 1% of trades
   with the highest net return.

Also reported for every variant, period and cost: trades, win rate, mean net return per trade and its t, mean
profit in R, annual return, annual volatility, Sharpe ratio (mean daily return / standard deviation × √252, no
risk-free rate, as the paper), maximum drawdown, worst day, correlation of daily returns with SPY's
close-to-close returns, and the share of days with a profit.

A pass means the rule deserves a paper-trading shadow run and a decision about paying for the real-time feed. A
fail means it is closed like the daily-bar families.

## Replication check

Variant A at zero slippage, 2016-01-04 to 2023-12-29, is run under two intrabar readings (worst case and
optimistic, below). The pipeline counts as reproducing the paper when:

* **Top 20:** the paper's 41.6% a year lies inside [A worst case − 10 points, A optimistic + 10 points];
* **Base strategy:** the paper's 3.2% a year lies inside [base worst case − 10 points, base optimistic + 10
  points]; and
* the top-20 annual return is above the base strategy's under both readings.

The bracket is wide because the paper does not state its intrabar rule, its universe includes ETFs and foreign
stocks that this one lacks, and its minute data come from another vendor. If the check fails, the held-out
verdict is still reported, with the statement that the pipeline did not reproduce the paper and in which
direction.

**The base strategy is replicated on a sample.** It needs full-session minute bars for 700 to 1,000 stocks a
day. It is run on every 5th session of 2016-2023 (sessions 0, 5, 10, ... of the calendar, about 402 sessions),
which keeps the download near 12,000 requests. On that sample the standard error of its annual return is about 5
points.

## Data

* **Daily bars** (filters, ATR, identity): the dip-rules test's cache, 8,719 identity-checked, de-duplicated US
  stocks, 3,559 of them delisted since, 2016-01-04 to 2026-11-11, raw and adjusted
  (`research_notes/Dip rules full market 20261112`). Its identity check is kept: a series that failed the
  insider-price check is out, and so is any calendar year it blocked.
* **Universe limits inherited from it:** issuers that file SEC insider forms. No ETFs, no ETNs, no foreign
  private issuers (ADRs). The paper's universe had them (its tables list QQQ and ASML).
* **Filters on daily bars.** For session t, from the 14 sessions before it: mean raw volume ≥ 1,000,000; mean
  true range > $0.50. The 15 bars t−14 to t must be 15 consecutive market sessions. A stock-day is dropped when
  a split falls in t−14 to t (the ratio of raw to adjusted close changes by more than 25% up or 20% down from
  one session to the next), because the volume baseline and the ATR would mix two share scales.
* **Opening range.** Alpaca `timeframe=5Min`, `feed=sip`, `adjustment=raw`, the bar stamped 09:30 ET, requested
  one session at a time with `asof` set to that session, for every stock that passes the daily filters on that
  session or on any of the next 14 (so each stock's 14-session volume baseline is complete). A stock with a daily
  bar and no opening-range bar has an opening volume of zero.
* **Ticker identity.** A stock is requested under every ticker its series ever carried. A returned bar is
  accepted for a stock only if it fits inside that stock's own daily bar for the session: high ≤ daily high × 1.01,
  low ≥ daily low × 0.99, volume ≤ daily volume × 1.01. A bar that fails is discarded, so a ticker reused by
  another company cannot supply bars. A stock-day with a discarded bar is missing, not zero; a stock needs all 14
  baseline sessions present to be ranked. The count is reported.
* **Full-session bars.** Alpaca `timeframe=1Min`, 09:30 to 16:00 ET, for the 20 selected stocks each session
  (every session, both periods), for every eligible stock on the base-sample sessions, and for SPY.
* **Session end.** 16:00 ET, or 13:00 ET on a half day. A half day is a session where SPY's volume in bars
  stamped 13:05 to 15:59 is under 5% of its volume from 09:30 to 12:59.
* **Benchmark.** SPY's adjusted daily closes from the same cache.
* Fetched by session across symbols, at most 3 threads and 0.32 s between requests (under 200 a minute), cached
  outside the repo. Read-only market data; no account or order endpoint; nothing sent to the deployed app.

## Simulation

On 1-minute bars, from the bar stamped 09:35:

* **Entry.** Long: the first bar whose high is at or above the opening-range high. Fill at the opening-range
  high, or at the bar's open if it opens above it. Short: mirror image.
* **Stop-loss.** 10% of the ATR from the fill. In a later bar: hit when the low is at or below it (long); fill at
  the stop, or at the bar's open if it opens beyond it.
* **Same bar (worst case, used for the verdict).** If the entry bar's range also reaches the stop-loss, the
  trade is stopped out in that bar at the stop price. How often this happens is reported.
* **Same bar (optimistic, replication and a labelled side result only).** The stop-loss is checked from the bar
  after the entry bar.
* **Exit at the close.** The close of the stock's last 1-minute bar stamped before the session end.
* **Stops trading during the session.** Exit at its last bar's close.
* **Portfolio.** Daily return = sum over the day's trades of (position value / equity) × net return. Equity
  compounds daily. Annual return = (final / initial)^(252 / sessions) − 1.

## Reported, not decisive

* C and B at every cost under the optimistic intrabar reading.
* C by price bucket (opening price $5-10, 10-20, 20-50, 50-100, over 100), by ATR as a share of price (under 3%,
  3-5%, 5-8%, over 8%), by relative volume (1-2x, 2-5x, 5-10x, over 10x) and by calendar year.
* The share of trades in stocks delisted since (no daily bar in the last 10 sessions), and their result.
* **15-minute delay** (what the free real-time plan allows): the 09:35 ranking is known at 09:50. Orders go in
  at 09:50: if the 09:50 bar opens at or beyond the entry level, the entry is at that open; otherwise the stop
  order works from 09:50. Same stop-loss and exit. Reported for A, B and C at 2 cents.
* Coverage: stock-days requested, with an opening-range bar, discarded by the identity check, ranked; sessions
  with fewer than 20 stocks; trigger rate.

## Known limits, stated before the run

* **No ETFs or ADRs**, so the top 20 each day are drawn from a narrower list than the paper's.
* **Minute bars are trades, not quotes.** The spread is not observed; slippage is an assumed flat cost.
* **Worst-case rule and a tight stop.** The stop is 10% of the ATR, often smaller than a 1-minute range in the
  first minutes. The worst-case rule will stop out many trades the optimistic one keeps. Both are reported; the
  verdict uses the worst case.
* **No interest on idle cash** in the strategy; SPY is total return.
* **Capacity** is not tested. The paper starts with $25,000; so does this.
* **Held-out length:** under three years, in a rising market.
