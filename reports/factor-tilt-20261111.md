# Factor tilt: 2026-11-11

**Question.** The [edge-feasibility report](Retail%20bot%20edge%20feasibility.md) closed every price-based lane and left one family "deferred, not closed": low-turnover fundamental tilts (quality, profitability, value). Would such a tilt, held in place of VOO in the slow book's passive arm, have beaten the arm after costs?

**Answer: no. All eight candidates fail the pre-registered rule, so VOO stays.** No candidate raised the arm's Sharpe ratio in any cell. Seven of the eight returned less than SPY. The one that beat SPY, momentum (+0.6%/yr), did so with higher volatility, so the arm's Sharpe still fell. Nothing in the app changes. Paper only.

Code and outputs are in [research_notes/Factor tilt 20261111](../research_notes/Factor%20tilt%2020261111/): `factor_tilt_check.py`, `tables.txt` and `results.json`.

## Why factor ETFs

A home-built tilt cannot be backtested for $0. The [11-10 coverage audit](point-in-time-coverage-20261110.md) found that free bars miss 74% of past S&P 500 members, so any test on today's survivors is biased. A factor ETF avoids this, for three reasons:

* Its price history includes every stock it ever held, delisted or not.
* Its adjusted price is already net of its expense ratio.
* It is exactly what the bot would buy: one symbol, rebalanced by the issuer, with near-zero turnover for the bot.

Because these are professionally run versions of the tilts, a home-built tilt on 45 names would be a weaker version of the same bet.

## The test, as pre-registered

The rule was written into the script's docstring and committed (009e351, 11:13 ET) before any price was fetched.

* **Candidates.** Each one replaces the whole equity leg:

  | Ticker | Fund | Tilt |
  |---|---|---|
  | QUAL | iShares MSCI USA Quality | quality and profitability |
  | SPHQ | Invesco S&P 500 Quality | quality, longest history |
  | VLUE | iShares MSCI USA Value | value |
  | RPV | Invesco S&P 500 Pure Value | value, longest history |
  | MTUM | iShares MSCI USA Momentum | momentum |
  | USMV | iShares MSCI USA Min Vol | low volatility |
  | SPLV | Invesco S&P 500 Low Volatility | low volatility |
  | BLEND | equal weights of QUAL, VLUE, MTUM and USMV, rebalanced monthly | multi-factor |

* **Benchmark leg.** SPY stands in for VOO, which starts in 2010. The two track within a few bps.
* **Arm.** 20% equity leg and 80% BIL, rebalanced at each month's last close, as in the live passive arm. Cost is charged on one-way turnover at 11 and 25 bps a side, on both arms.
* **Windows.** W1 runs from the first month after the candidate's inception (and after BIL starts) to the latest close. W2 starts 2015-01-01.
* **Rule.** A candidate replaces VOO only if all three hold:
  * The arm's Sharpe ratio (daily excess over BIL) is higher with the candidate, at both costs and in both windows.
  * Its maximum drawdown is no more than 2 points worse, at both costs and in both windows.
  * The leg's monthly excess over SPY is significant over W1: one-sided p < 0.05 after a Holm correction across all 8 candidates, using Newey-West t-statistics with 3 lags.
* **Data.** yfinance adjusted closes ($0), 2005-01-03 to 2026-11-10. The cache md5 is `33a5ce404fcdef9922809802e9d7c36b`. The first fetch included today's still-open bar, which the rule's "latest close" excludes, so I trimmed it and re-ran. The verdict was the same with and without that bar.

## Results

**Equity leg against SPY, 2015 onward (W2).** Monthly excess, annualized.

| Leg | CAGR | SPY CAGR | Excess per year | Tracking error | Information ratio | t | Max DD (SPY −33.7%) |
|---|---|---|---|---|---|---|---|
| QUAL | 13.0% | 13.4% | −0.30% | 3.0% | −0.10 | −0.36 | −34.1% |
| SPHQ | 13.1% | 13.4% | −0.35% | 5.0% | −0.07 | −0.29 | −31.6% |
| VLUE | 11.9% | 13.4% | −0.73% | 9.1% | −0.08 | −0.23 | −39.5% |
| RPV | 9.4% | 13.4% | −2.64% | 11.8% | −0.22 | −0.74 | −50.7% |
| MTUM | 13.5% | 13.4% | **+0.56%** | 9.8% | 0.06 | 0.20 | −34.1% |
| USMV | 10.2% | 13.4% | −3.25% | 7.8% | −0.42 | −1.47 | −33.1% |
| SPLV | 8.6% | 13.4% | −4.66% | 10.5% | −0.45 | −1.67 | −36.3% |
| BLEND | 12.5% | 13.4% | −0.91% | 3.8% | −0.24 | −0.80 | −35.0% |

The same pattern holds over each fund's full history (W1, from 2007, 2011 or 2013). The two longest records, SPHQ and RPV from 2007, lagged SPY by 0.7 and 0.3 points a year, with drawdowns of −57.8% and −75.3% against SPY's −55.2%.

**The 20/80 arm, 2015 onward, at 25 bps a side.** This is the rule's test.

| Leg | Arm Sharpe with SPY | Arm Sharpe with candidate | Arm max DD with SPY | Arm max DD with candidate |
|---|---|---|---|---|
| QUAL | 0.682 | 0.661 | −7.1% | −7.2% |
| SPHQ | 0.682 | 0.667 | −7.1% | −6.6% |
| VLUE | 0.682 | 0.569 | −7.1% | −8.5% |
| RPV | 0.682 | 0.435 | −7.1% | −11.2% |
| MTUM | 0.682 | 0.590 | −7.1% | −7.2% |
| USMV | 0.682 | 0.608 | −7.1% | −7.0% |
| SPLV | 0.682 | 0.477 | −7.1% | −7.7% |
| BLEND | 0.682 | 0.652 | −7.1% | −7.4% |

All 32 cells fail the Sharpe condition: 8 candidates, 2 costs, 2 windows. Costs are not the reason, since 11 and 25 bps give nearly the same Sharpe ratios. The Holm-adjusted p-value is 1.000 for every candidate, and the smallest raw p-value is MTUM's 0.41. `tables.txt` has every cell.

## What this means

* **There is no sign of an edge, not merely too little data.** A true information ratio of 0.3, the realistic ceiling the feasibility report quotes, would need about 44 years to reach t = 2, so the significance test was always going to be hard to pass. But the point estimates fail too: seven of eight are negative, and the positive one lowers the arm's Sharpe ratio.
* **The fundamental-tilt family is closed for this bot, at least as an implementable, $0-data trade.** That was the last family the feasibility report named. A home-built version would need licensed delisted data (Norgate, EODHD or CRSP) to test honestly. Even then it would be competing against funds that already lost to SPY.
* **The passive arm stays as it is.** The realistic ceiling remains passive-like returns at a chosen drawdown. The equity weight (`passive_arm_equity_weight`, 0.20) sets the bot's real return far more than any signal does. Since 2015, each extra 10 points of equity adds about 1.1 points of return a year and about 3.4 points of drawdown. That weight is the owner's decision, not a research result.

## Not done, and why

* **The SEC XBRL point-in-time check** (whether filing dates allow as-of fundamentals) was planned as part B and dropped. Its only use would be a home-built tilt, and that backtest is still blocked by the missing delisted bars, whatever the SEC data shows.
* **No forward experiment was registered.** Nothing passed, and an ETF swap in the passive arm is a capital decision, not an exploration trade.

## Limits

* **The windows are short.** The iShares funds start in 2013, and the post-2015 window is dominated by mega-cap growth, which hurt value and low-volatility tilts in particular. A different decade could rank them differently. The test answers what would have worked in the bot's era, at the bot's size.
* **SPHQ changed its index** during its history (from the S&P High Quality Rankings to S&P 500 Quality), so its 2007 record mixes two methods.
* **Fund fees are included in the adjusted prices.** SPY's 0.09% fee is higher than VOO's 0.03%, which slightly flatters the candidates against VOO.

## Reproduce

```
python -I "research_notes/Factor tilt 20261111/factor_tilt_check.py" <out_dir>
```

The script reuses `<out_dir>/px.csv` if it exists. Otherwise it fetches the prices from yfinance.
