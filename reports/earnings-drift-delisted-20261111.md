# Post-earnings drift with delisted stocks: 2026-11-11

**Question.** Do US stocks that jump on an earnings release keep rising over the next 20 trading days, after costs, when delisted companies are in the sample?

**Answer: no. The test fails all four parts of its rule, and the idea is closed.** Buying after an earnings jump made +0.76% per 20-day hold after costs (t = 1.41), which is less than SPY made over the same days. Against SPY the trades lost 0.44% a hold. No forward paper test is built.

This was the last idea set aside for lack of fair data. Earlier reports found +90 to +320 bps a trade on today's megacaps but could not trust it ([local sweep](local-sweep-20261110.md), [point-in-time coverage](point-in-time-coverage-20261110.md)). This test uses every US-listed company that filed an earnings release since 2016, including the ones that have since been delisted.

Files are in [research_notes/Earnings drift delisted 20261111](../research_notes/Earnings%20drift%20delisted%2020261111/): the pre-registration, both scripts, the trade table and the result files. Nothing in the app changes. Paper only.

## The test

The rule was committed before any filing or price was downloaded (5cef1ec), the script before any price was fetched (1411bce, revised in 0d6db66), and the evaluation ran once.

* **Event.** An 8-K with Item 2.02 (an earnings release), from the SEC. Each has an acceptance time, so the first session that could react is known.
* **Universe.** Every US-listed filer, delisted or not. The stock must close at $5 or more and trade a median $5 million a day over the 20 sessions before the news.
* **Signal.** The stock's return minus SPY's, from the last close before the filing to the close of the first session after it. A trade is taken when that is **+5% or more**. The cut-off was fixed in advance.
* **Trade.** Buy at the close of the next session (the paper path cannot trade at the open). Sell at the close 20 sessions later. One position per stock.
* **Cost.** 25 bps a round trip.
* **Measure.** Trades are averaged within each entry month, and the months are averaged equally, with a Newey-West t-statistic.

## Result

17,615 trades in 3,389 stocks, entered from February 2016 to October 2026 (129 months, a median of 109 trades a month).

| Pass criterion | Needed | Measured | |
|---|---|---|---|
| 1. Profit after costs | above zero, t >= 2.0 | **+0.76%** a hold, t = 1.41 | fail |
| 2. Beats SPY after costs | above zero | **−0.44%** a hold (t = −1.36) | fail |
| 3. Most calendar years beat SPY | more than half | 5 of 11 | fail |
| 4. Holds up without the top 1% of trades | both still above zero | +0.26% after costs, **−0.94%** against SPY (t = −3.14) | fail |

**Verdict: fail. The idea is closed for this bot.**

* **The profit is the market's.** On the same monthly measure SPY made +1.20% over the same holds. The trades made +1.01% before costs.
* **The typical trade loses to SPY.** The median trade is 0.70% behind SPY, and 47% of trades beat it.
* **A few big winners carry the average.** The top 1% is 177 trades. Without them the trades are 0.94% a hold behind SPY.
* **Cost is not the reason.** Before any cost the trades are still 0.19% a hold behind SPY. At 50 bps they are 0.69% behind (t = −2.12).

### Year by year

Each value is the mean of that year's monthly values, after 25 bps.

| Entry year | Trades | Return a hold | Against SPY |
|---|---|---|---|
| 2016 | 1,274 | +2.24% | +0.26% |
| 2017 | 1,235 | +1.24% | −0.31% |
| 2018 | 1,551 | +0.18% | +0.23% |
| 2019 | 1,533 | +0.14% | −1.22% |
| 2020 | 1,570 | +2.27% | +0.56% |
| 2021 | 1,511 | −0.25% | −1.70% |
| 2022 | 1,803 | −0.56% | +0.33% |
| 2023 | 1,747 | 0.00% | −1.73% |
| 2024 | 1,865 | +1.30% | −1.36% |
| 2025 | 1,917 | +1.79% | +0.49% |
| 2026 | 1,609 | 0.00% | −0.36% |

### The size of the jump does not predict what follows

If drift existed, bigger jumps would be followed by better returns and big drops by worse ones. They are not. Every group is taken at the same entry and exit, after 25 bps, against SPY:

| Reaction on the earnings day | Events | Against SPY, 20 sessions |
|---|---|---|
| +10% or more | 7,402 | −0.72% |
| +5% to +10% | 10,213 | −0.19% |
| between −5% and +5% | 56,398 | −0.49% |
| −5% or less | 17,981 | −0.28% |
| All eligible events | 92,105 | −0.43% |

The stocks that jumped did no better than the ones that fell. The largest jumps did worst.

### Delisted stocks

* **17% of the trades (3,015) are in stocks that no longer trade today.** Those trades lost 1.39% a hold to SPY (t = −2.56). The 14,600 trades in stocks still listed lost 0.30%.
* **So survivors alone fail too.** Leaving out the delisted stocks would have flattered the result by about 0.14 points a hold, not turned it into a pass. The earlier megacap result came from picking today's winners, a much stronger filter than survival.
* **Five trades stopped trading during the hold, all at their last price.** Four were mergers or buy-outs (AMSG, CBI, CLCT, LBAI) and one moved its listing abroad (LNW). Their mean return was +8.3%.
* **No bankruptcy occurred in a trade.** A stock that has just jumped on earnings, trades $5 million a day and costs $5 or more does not go bankrupt within 22 sessions in this sample. The full-loss rule fired twice among all 94,047 eligible events (XOG in 2021, VSTO in 2024), neither in the traded bucket. Both were in fact acquisitions, so the rule was too harsh there, as the pre-registration expected.

### Other cuts (reported, not decisive)

| Slice | Trades | Return a hold | Against SPY |
|---|---|---|---|
| Liquidity, lowest third (median $9M a day) | 5,872 | +0.67% | −0.53% |
| Liquidity, middle third ($29M) | 5,871 | +1.02% | −0.20% |
| Liquidity, highest third ($146M) | 5,872 | +0.84% | −0.36% |
| Identity verified by insider prices | 15,883 | +0.75% | −0.43% |

No slice is positive against SPY.

## The sample

| Step | Count |
|---|---|
| Item 2.02 filings accepted since 2016-01-01, all SEC filers | 196,185 |
| From issuers with insider filings (6,995 issuers) | 188,165 |
| With a ticker within 365 days of the filing | 186,085 |
| After one event per ticker and reaction session | 185,131 |
| With Alpaca bars | 178,842 (97%) |
| Eligible ($5, $5M a day, 20 sessions of history, identity check) | 94,047 |
| Reaction of +5% or more | 18,190 (19% of eligible) |
| Trades taken | 17,615 |
| Still open when the data ended (2026-11-11) | 463 |
| Skipped, position already open in the stock | 112 |

Events that did not qualify: 48,307 traded under $5 million a day, 33,609 were under $5, 6,289 had no bars (mostly over-the-counter), 1,394 had under 20 sessions of history, 1,068 failed the identity check, and 417 had no bar on a needed day.

**Share that later delisted.** Of the events with bars, 24% are in stocks that no longer trade. It is 41% for events of 2016 and falls to 2% for 2026. Among eligible events it is 17% (31% in 2016). A test on today's tickers would have dropped all of them.

**Identity.** 87% of eligible events, and 90% of trades, were confirmed by an insider transaction price within the same window. 0.6% of priced events failed that check and were dropped.

## What differed from the pre-registration

None of these was decided after seeing a return.

* **Acceptance times came from the SEC's live API, not the bulk file.** The pre-registration asked for a format check of the time zone before the run. The check found that the bulk `submissions.zip` mixes three clocks by filer: Eastern time, UTC, and a batch shifted to UTC twice. The SEC changed the field in 2026 and a filer's entry is rewritten only when it files again. The live API serves UTC for every filer, so the times were downloaded from it (6,996 issuers). A fixed random sample of 200 events matches the "Accepted" line of EDGAR's own filing page 200 times out of 200, and no event falls outside EDGAR's 06:00 to 22:00 hours.
* **One symbol per issuer and filing day.** When insider filings of the same day disagree on the symbol, the most common one is used.
* **The `asof` date for November 2026 is the last day of data**, since the 15th had not arrived.
* **The full events table is not in the repository** (7 MB). The trade table is. The events table and all inputs are in the scratch folder named under "Reproduce", with hashes.

## Limits

* **The 8-K can trail the press release.** The rule allows four business days, though most companies file with the release. For a late filing the measured reaction is not the true one. About 39% of eligible events moved 5% or more against SPY on the measured day, so the day is the reaction day for most.
* **The signal is the price reaction, not the earnings surprise.** Free data has no analyst estimates for delisted companies. A surprise-based rule was not tested.
* **One decade, mostly a rising market.** Alpaca's history starts in 2016.
* **No delisting returns.** A stock's last bar is its last exchange trade.
* **One definition was tested.** Other cut-offs, holds, small stocks under the liquidity floor and the short side were not. The bucket table gives no reason to expect a different answer from this signal, and any variant would be a search on data already seen. It would need its own pre-registration.

## What this closes

* Post-earnings drift on the price reaction is closed for this bot. No forward paper test is wired.
* The earlier +90 to +320 bps came from choosing today's megacaps. On the full population the same kind of trade loses to SPY.
* Two ideas that earlier reports could not test fairly have now been tested with delisted stocks: insider cluster buys ([2026-11-11](insider-cluster-buys-20261111.md)) and earnings drift. Both fail. New-high breakouts on past index members have not been re-tested on this data.

## Reproduce

```
python -I earnings_drift_check.py tickers  <work_dir>     # needs <work_dir>/insider_zips from sec.gov
python -I earnings_drift_check.py filings  <work_dir>     # needs <work_dir>/sec/submissions.zip from sec.gov
SEC_USER_AGENT=... python -I sec_fetch.py submissions <work_dir>
python -I earnings_drift_check.py events   <work_dir>
SEC_USER_AGENT=... python -I sec_fetch.py check <work_dir> 200
ALPACA_KEY=... ALPACA_SECRET=... python -I earnings_drift_check.py fetch <work_dir>
python -I earnings_drift_check.py evaluate <work_dir>
```

* Python 3.9 with pandas 2.3.2, numpy 1.26.4 and pyarrow 21.0.0.
* The SEC refuses scripts that do not declare a contact in the User-Agent. The contact is passed through `SEC_USER_AGENT` and is not stored in the repository. Requests ran at about five a second.
* The Alpaca keys are a paper account's, used for market data only. No order was placed and the deployed app was never contacted.
* Bars were fetched on 2026-11-12 between 00:23 and 01:06 Eastern; the last bar is 2026-11-11. Before the fetch, a handful of requests on well-known tickers (SPY, FB, META, TWTR, XLNX and 30 large caps) checked how `asof` and `end` behave.
* Work folder on the research machine: `~/.paper-training/earnings-drift-delisted-20261111/` (bars per month, events, inputs).
* md5: `trades.csv.gz` `fc3f133bfdfea6186440e1196c9ce5a3`, `events.csv.gz` `4f8d66338bcb6d4fb0b9d17451017f52`, `events_raw.csv` `8ceba02439ca39d7111560e69016b19e`, `sec/acceptance.csv` `0d268e830ac35ee74ba4c523a11ce9b2`, `ticker_map.parquet` `838f7a29736b276a09e5a4877f86f0fa`.
