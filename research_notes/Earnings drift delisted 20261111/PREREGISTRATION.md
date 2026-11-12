# Post-earnings drift with delisted stocks: pre-registration (2026-11-11)

Frozen before any earnings filing or price was downloaded for this test. The only request made so far was an access check on one file's headers (the SEC's bulk `submissions.zip`). Do not edit after the run; corrections go in the report.

## Question

Do US stocks that jump on an earnings release keep rising over the next 20 trading days, after costs, when delisted companies are in the sample? Earlier work found +90 to +320 bps a trade on today's megacaps, but on survivors only (`reports/local-sweep-20261110.md`, `reports/point-in-time-coverage-20261110.md`). If this check passes, the idea is worth a forward paper test. If it fails, the idea is closed.

## Data

All free. Nothing is sent to the deployed app.

* **Earnings releases.** The SEC's bulk `submissions.zip` (every filer's filing list, delisted filers included). An earnings release is a filing of form type `8-K` whose item list contains `2.02` (Results of Operations and Financial Condition). Amendments (`8-K/A`) are ignored. Each filing carries its acceptance time.
* **Ticker of the filer on the event date.** The SEC's Insider Transactions Data Sets (Forms 3, 4 and 5, quarterly). Every insider filing names the issuer's CIK and its trading symbol on the filing date, so they give a dated CIK-to-ticker map that covers delisted companies. The zips are already on disk from the insider test of 2026-11-11 (2006 Q1 to 2026 Q3). They are used here for ticker identity only.
* **Prices.** Alpaca daily bars, `feed=sip`, which include delisted tickers from January 2016. Raw bars give the price and volume of the day. Bars with `adjustment=all` give total returns. SPY comes from the same source.

## Event

1. **Filing.** An `8-K` with Item 2.02, accepted from 2016-01-01 onward.
2. **Acceptance time.** EDGAR's acceptance time, read as US Eastern time. Before the run, the time zone of the field is checked against the header of a few filings (a format check, not a result).
3. **Sessions.** The session calendar is SPY's bar dates. The regular close is taken as 16:00 Eastern on every session.
   * **P, the last close before the news:** the last session that closed before the acceptance time. For a filing accepted before 16:00 on a session day, P is the previous session. For one accepted at or after 16:00, or on a day with no session, P is the last session on or before that day.
   * **R, the reaction session:** the first session that opens after the acceptance time. For a filing accepted before 09:30 on a session day, R is that day. Otherwise R is the next session after the acceptance day.
4. **Ticker.** Among the issuer's insider filings (same CIK, any of Forms 3, 4, 5 and their amendments) filed within 365 days of the acceptance date, either side, the symbol on the filing nearest in time (the earlier one on a tie). The symbol is upper-cased, an exchange prefix such as `NYSE:` is removed, the first symbol is taken when several are listed, and a class separator is written as `.`. It must match `^[A-Z]{1,5}(\.[A-Z])?$`. Blank, `NONE` and `N/A` are dropped. A filer with no such filing has no ticker and is left out (debt-only and unlisted filers).
5. **One event per stock per reaction session.** Several filings with the same ticker and the same R count once (the earliest accepted).

## Universe

US-listed operating companies that file an 8-K (foreign issuers file a 6-K and are out). The ticker is the one insiders report, which is the common stock, or the common units or shares of a partnership or trust. No sector is excluded.

Eligibility is measured at P, on bars up to and including P only:

* at least 20 sessions of bars, with a bar on P;
* a raw close on P of at least **$5**;
* a median daily dollar volume (raw close x volume) over the last 20 bars of at least **$5,000,000**. The floor is set this high so that a 25 bps round-trip cost is believable.

Stocks that trade over the counter have no SIP bars and are out. Alpaca cannot trade them either.

## Identity check

* Bars are fetched by acceptance month with `asof` set to the 15th of that month, so a symbol resolves to the company that held it then. Each event is evaluated on its own month's bars only. A ticker reused later by another company cannot supply bars.
* **Price check.** Where the issuer's insider filings report a non-derivative transaction with code `P`, `S` or `F` (open-market purchase, sale, tax withholding) at a price above zero, dated inside the fetched window on a day the stock has a bar, the median of (reported price / raw close that day) over those transactions must lie in 0.7 to 1.4. An event that fails is dropped as an identity mismatch. An event with no such transaction is kept on the `asof` resolution alone and marked unverified. The share verified is reported.

## Signal

* **Reaction.** The stock's total return from the close of P to the close of R, minus SPY's over the same two closes.
* **Top bucket only.** A trade is taken when the reaction is **+5.0% or more**. The cut-off is fixed here and is not tuned. No other bucket is traded.

## Trade

* **Entry.** The close of the session after R (R+1). The paper path cannot trade at the open. The stock must have a bar on R and on R+1.
* **Exit.** The close 20 sessions after entry (R+21). If the stock has no bar that day but trades later, its last close before that day.
* **One position per stock.** An event whose R falls on or before the exit session of an earlier trade in the same ticker is skipped.
* **A stock that stops trading during the hold** (its last bar is before R+21 and it has no later bar in the window):
  * If the issuer filed an 8-K with Item 1.03 (bankruptcy or receivership) from 365 days before its last bar to 30 days after it, the trade is booked as a **full loss (-100%)**. This is harsh on purpose: shares usually keep some value over the counter.
  * Otherwise it exits at its **last close** (an acquisition, or a move off the exchange).
  * The report says how many of each occurred.
* **Return.** Total return from the entry close to the exit close (`adjustment=all`).
* **Benchmark.** SPY over the same two dates. For a full loss, SPY over the full 20 sessions.
* **Cost.** 25 bps a round trip, taken off every trade. 50 bps is reported.
* **Completed holds only.** A trade counts when R+21 is on or before the last SPY bar downloaded, or the stock stopped trading first.

## Statistic

Trades are grouped by entry month. Each month's value is the equal-weighted mean of its trades. The statistic is the mean of the monthly values, with a Newey-West t-statistic (2 lags, since a 20-session hold overlaps the next month). Months with no trade are skipped.

## Rule

The idea goes on to a forward paper test only if all four hold, on every completed trade from 2016 to the end of the data:

1. **Profit after costs.** The mean monthly return net of 25 bps is above zero with t >= 2.0.
2. **Beats SPY.** The mean monthly return minus SPY, net of 25 bps, is above zero.
3. **Most years.** Among calendar years (by entry date) with at least 20 trades, more than half have a positive value of statistic 2 (the mean of that year's monthly values).
4. **Not a few big winners.** After dropping the 1% of trades with the highest return minus SPY, statistics 1 and 2 are both still above zero (no t requirement).

Otherwise the idea is closed for this bot.

## Reported, not decisive

* The t-statistic of the excess over SPY, the event-weighted mean and median, and the share of trades that beat SPY.
* Gross, and net of 50 bps.
* Each calendar year.
* Other reaction buckets (+10% or more; -5% or less; between) with the same entry and exit, to show whether returns line up with the reaction.
* Liquidity terciles of the traded events.
* The verified-identity subset on its own.
* Coverage: events by status (no ticker, no bars, ineligible, identity mismatch, traded) and by year.
* The share of events and of trades whose stock no longer has a bar in the last 10 sessions of the data (delisted since), and the result with and without them.
* The trades that stopped trading during the hold: count, kind and mean return.

## Known limits, stated before the run

* **The 8-K can trail the press release.** Most companies file it with the release, but the rule allows up to four business days. For a late filing the reaction session is not the true reaction day, which weakens the signal. It cannot create look-ahead: every date used is after the filing was public.
* **No delisting returns.** A stock's last bar is its last exchange trade. The full-loss rule covers bankruptcies only.
* **Nothing before 2016**, so the test covers one decade, most of it a rising market.
* **Costs are a flat 25 bps.** The dollar-volume floor is there to make that fair. A higher cost only makes the result worse.

## What a pass means

Not proof. One rule on one decade. A pass means the idea is not disproven on data without the survivorship hole and is worth a forward test on paper. A fail is taken as final.
