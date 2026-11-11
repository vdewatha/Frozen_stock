# Insider cluster buys: pre-registration (2026-11-11)

Frozen before any insider filing or price was downloaded for this test. The only request made so far was an access check on one file's headers. Do not edit after the run; corrections go in the report.

## Question

Do stocks bought by a cluster of company insiders beat a small-cap index after costs, on data a $0 bot can get? If the historical check passes, the idea goes to a forward paper test. If it fails, the idea is closed.

## Data

* **Filings.** The SEC's Insider Transactions Data Sets (Forms 3, 4 and 5 as quarterly flat files, 2006 Q1 to the latest quarter). Free. Each filing carries its filing date, which is the day it became public.
* **Prices.** yfinance daily bars with splits and dividends, one download per ticker. Free. Most delisted tickers are missing, so the check cannot count as proof (see "What a pass means").

## Event: a cluster buy

1. **Qualifying purchase.** A row of the non-derivative transaction table with transaction code `P` (open-market purchase) and acquired/disposed code `A`, in a filing of document type `4` (amendments `4/A` are ignored).
2. **Qualifying insider.** The reporting owner is an officer or a director of the issuer. Owners who are only 10% holders or "other" do not count.
3. **Size.** The insider's purchases in that filing total at least $10,000 (shares x price, price above zero).
4. **Timely.** The filing date is at most 10 calendar days after the latest purchase in it.
5. **Not pre-planned.** A filing flagged as made under a Rule 10b5-1 plan is ignored (the flag exists from 2023).
6. **Cluster.** On filing date D the issuer has at least 3 distinct qualifying insiders with qualifying filings dated from D minus 29 days to D. The event date is the first such D.
7. **One event per issuer per 90 days.** After an event, the same issuer raises no new event for 90 calendar days.
8. **Ticker.** The issuer trading symbol on the filing that completed the cluster, upper-cased, with `.` mapped to `-`. Blank, `NONE` and `N/A` are dropped.

## Trade

* **Entry.** The open of the first session after D.
* **Exit.** The close of the 63rd session of the hold (the entry session is the first). If the price history ends sooner, the last available close.
* **Identity check.** The insiders' share-weighted purchase price must be within 0.7x to 1.4x of the stock's close on the latest purchase date, after undoing later splits. An event that fails is treated as unpriced: the ticker now belongs to another company.
* **Eligibility at D** (bars up to and including D only): at least 20 sessions of history, a close of at least $2 (in the prices of the day), and a median daily dollar volume over the last 20 sessions of at least $500,000.
* **Return.** Total return from the entry open to the exit close (splits and dividends included).
* **Benchmark.** IWM over the same entry open to exit close. Excess = stock minus IWM. SPY is reported beside it.
* **Cost.** 50 bps a round trip is the acting cost. Gross and 100 bps are reported.
* **Completed holds only.** An event counts when its 63 sessions have ended by the last downloaded bar (or its history ended first).

## Statistic

Events are grouped by entry month. Each month's value is the equal-weighted mean of its events' net excess returns. The statistic is the mean of the monthly values, with a Newey-West t-statistic (3 lags, since holds overlap about three months). Months with no event are skipped.

## Rule

The idea goes on to a forward paper test only if both hold:

1. **2015-01 onward:** the mean monthly net excess over IWM at 50 bps is above zero with t >= 2.0.
2. **2020-01 onward:** the same mean is above zero.

Otherwise the idea is closed for this bot.

## Reported, not decisive

* 2006-2014 on its own.
* The event-weighted mean and median, and the share of events that beat IWM.
* A 126-session hold.
* Liquidity terciles (by the 20-session median dollar volume at D).
* Price coverage by year: the share of events with a usable, identity-checked price history.

## What a pass means

Not proof. yfinance lacks most delisted stocks: bankruptcies are missing, which flatters the result, and so are acquisitions, which hurts it. A pass means the idea is not disproven and is worth a forward test on paper, where no survivorship problem exists. A fail on data that leans in the idea's favour for bankruptcies is taken as final.
