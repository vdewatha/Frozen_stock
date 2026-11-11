# Insider cluster buys: addendum (2026-11-11)

Written after the first check ran and before any Alpaca bar was fetched for it. It adds a stricter second check. It does not change the first one.

## Why

The first check passed its rule on yfinance prices: 2015 onward, +1.60% a hold over IWM after 50 bps, t = 2.42; 2020 onward, +1.79%. But its priced sample is survivors only:

* 6,935 of 12,262 events had no price history. yfinance serves no delisted ticker.
* Not one of the 3,577 traded events ended early because its stock stopped trading. Every bankruptcy and every acquisition is missing.
* The excess shrinks as coverage rises. In 2025 and 2026, where 87% and 91% of events are priced, it is negative (−1.5% and −0.5%).
* The top 1% of events carry more than the whole excess: without them the total is negative.

Alpaca's delayed SIP history has daily bars for delisted tickers from January 2016, resolved by the ticker's owner on a given date (`asof`). That removes the survivorship hole for 2016 onward, at no cost.

## The second check

Everything in PREREGISTRATION.md stands (event, trade, identity check, eligibility, cost, statistic), with these changes only:

* **Prices.** Alpaca daily bars, `feed=sip`, for every event from 2016, delisted or not. Raw bars give the price and volume of the day (identity check, $2 floor, dollar volume). Bars with `adjustment=all` give the total return. Events are fetched by filing month with `asof` set inside that month, and each event is evaluated on its own month's bars only, so a ticker later reused by another company cannot leak in.
* **Benchmarks.** IWM and SPY from the same source.
* **Window.** Entry months from 2016-03 (the first month with 20 sessions of history) and from 2020-01.
* **A stock that stops trading during the hold** exits at its last close, as already pre-registered. Now this case can occur.

## Rule

The idea goes on to a forward paper test only if both hold on the Alpaca prices:

1. **2016-03 onward:** the mean monthly net excess over IWM at 50 bps is above zero with t >= 2.0.
2. **2020-01 onward:** the same mean is above zero.

This check overrides the first. If it fails, the idea is closed for this bot, whatever the survivor-only check said.

## Reported, not decisive

The same list as before, plus: the excess over SPY (the slow book holds an S&P 500 fund, so SPY is the bot's real alternative), the share of traded events whose stock stopped trading during the hold and their mean return, and the result without the top 1% of events.
