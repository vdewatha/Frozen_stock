# Insider cluster buys: 2026-11-11

**Question.** Company insiders must report their own share purchases to the SEC, and the filings are free and dated. Do stocks that several insiders buy at once beat a small-cap index after costs?

**Answer: no. The idea is closed, and no forward paper test is built.** A first check on yfinance prices passed its rule. That pass was survivorship bias. A second check, pre-registered before its data was fetched, repeats the test on Alpaca's price history, which includes delisted stocks. There the excess over IWM is +0.20% per three-month hold after costs (t = 0.31), and it is negative against SPY.

The work also found something more useful than the idea itself: **Alpaca's free history has daily bars for delisted stocks from 2016.** That is the data several earlier tests were blocked for (see "What this unlocks").

Files are in [research_notes/Insider cluster buys 20261111](../research_notes/Insider%20cluster%20buys%2020261111/): the two pre-registrations, both scripts, the events, both trade tables and both result files. Nothing in the app changes. Paper only.

## The test

* **Data.** The SEC's Insider Transactions Data Sets: 83 quarterly files, 2006 Q1 to 2026 Q3, 891 MB. They hold 190,414 qualifying purchase filings.
* **Event.** At least 3 different officers or directors of one company each report an open-market purchase of $10,000 or more within 30 days. Late filings, amendments and pre-planned (10b5-1) purchases are left out. A company raises at most one event in 90 days. That gives **12,262 events** in 5,321 tickers, about 36 a month in recent years.
* **Trade.** Buy at the next session's open after the filing that completed the cluster, and sell at the close 63 sessions later. The stock must close at $2 or more and trade $500,000 a day.
* **Measure.** Each trade's return minus IWM's over the same days, minus 50 bps for the round trip. Months are averaged equally, with a Newey-West t-statistic.
* **Rule.** Go on to a forward paper test only if the average is above zero with t >= 2.0 from the window's start, and above zero from 2020.

Both pre-registrations were committed before their data was downloaded (8fa8cb6 and 7ad8a6b), and both scripts before any price was fetched.

## Result

| | First check: yfinance | Second check: Alpaca |
|---|---|---|
| Window | 2015-01 onward | 2016-03 onward |
| Delisted stocks | none | included |
| Events with usable prices | 43% of all events | 87% of events since 2016 |
| Trades | 2,512 | 3,444 |
| Excess over IWM per hold, after 50 bps | **+1.60%** (t = 2.42) | **+0.20%** (t = 0.31) |
| The same, from 2020 | +1.79% | +0.45% |
| Excess over SPY, after 50 bps | +0.89% (t = 0.89) | −0.40% (t = −0.38) |
| Share of trades that beat IWM | 49% | 46% |
| Median trade | −0.4% | −1.6% |
| Verdict by its rule | pass | **fail** |

The second check overrides the first, as its pre-registration says.

### Why the first check was wrong

yfinance serves no delisted ticker, so every stock in the first sample is still listed today. Selecting on survival years later turns out to select on the three months after the event too.

* **The two sources agree where they overlap.** On the 2,283 events both can price, returns correlate 0.998, and the average excess is the same (+1.08% and +1.07% per event).
* **The difference is the 1,161 events only Alpaca can price**, the stocks delisted since. Their average monthly excess is **−2.86%** a hold, against +1.53% for the survivors.
* **Almost none of them stopped trading during the hold itself** (6 of 3,444). They were alive for the three months and failed or were bought later. A backtest on survivors drops them all the same.

### What is left after the correction

* **No edge after costs.** +0.20% a hold is about 0.8% a year over IWM, with a t-statistic of 0.31. At 100 bps a round trip it is negative.
* **It loses to the slow book's own benchmark.** Against SPY the excess is −0.40% a hold.
* **The average is a few big winners.** Without the top 1% of trades the excess is −1.09% a hold (t = −1.85). The median trade loses 1.6% to IWM.
* **No liquidity group stands out.** The thirds by dollar volume range from −0.06% to +0.82%, none significant.
* **Year by year it has no pattern**: six positive years and five negative, with the last two negative.

Raw returns look good (+4.1% a hold on average), because clusters of insider buying follow sell-offs and the whole market tends to recover. The index earned almost all of it.

## What this unlocks

Earlier reports said honest tests of some ideas needed paid data on delisted stocks:

* [Point-in-time coverage](point-in-time-coverage-20261110.md): 74% of past S&P 500 member-days had no bar, and none of 28 bankruptcies was priced.
* [Gap fill and earnings drift](gap-fill-earnings-drift-20261102.md): earnings drift was blocked by survivorship.

Alpaca's delayed SIP history answers that for 2016 onward, on the free plan the bot already uses:

* **Daily bars exist for delisted tickers.** `asof` resolves a ticker to the company that held it on a given date, so reused tickers are handled.
* **Coverage here was 87% of events since 2016.** Most of the rest are over-the-counter stocks, which Alpaca cannot trade either.
* **Limits:** nothing before 2016, and no delisting returns. A stock's last bar is its last trade.

This check also shows how large the bias is: on the same events, leaving out the stocks delisted since turned +0.2% into +1.6% and a fail into a pass. Every earlier positive result on today's tickers should be read with that in mind.

## Interpretations and limits

* **Identity check.** I applied the price check to the filing that completed the cluster (its share-weighted price against the close on its latest purchase date), not to all the cluster's purchases. I chose that before any return was computed.
* **Filing date, not time.** The data sets give the filing date. Entry is the next session's open, which is safe for filings accepted up to 10 p.m.
* **The first window is 2015 and the second 2016-03**, because Alpaca's history starts in 2016. The first check's own 2016-onward figures are in its trade table.
* **Costs are a flat 50 bps.** Small stocks can cost more. A higher cost only makes the result worse.
* **13% of events since 2016 are unpriced on Alpaca**: over-the-counter tickers and listings with fewer than 20 sessions of history.
* **One definition was tested.** Other definitions (two insiders, larger purchases, chief executives only, "opportunistic" insiders) were not. Trying them now would be a search over variants on data already seen, so any of them would need its own pre-registration and a forward test.

## Reproduce

```
python -I insider_cluster_check.py events   <work_dir>     # needs <work_dir>/zips from sec.gov
python -I insider_cluster_check.py prices   <work_dir>
python -I insider_cluster_check.py evaluate <work_dir>
ALPACA_KEY=... ALPACA_SECRET=... python -I alpaca_check.py fetch <work_dir>
python -I alpaca_check.py evaluate <work_dir>
```

* The SEC refuses scripts that do not declare a contact in the User-Agent. The contact is not stored in the repository.
* `events.csv` md5: `0861cf825d8069089792f7a80fdebe3c`.
* The Alpaca keys are a paper account's, used for market data only.
