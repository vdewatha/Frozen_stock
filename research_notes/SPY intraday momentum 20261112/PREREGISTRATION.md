# SPY intraday momentum (noise band), with a held-out period: pre-registration (2026-11-12)

Frozen before any minute bar was downloaded for this test. Before writing it, the only things read were the
paper (the 3 February 2025 version, 43 pages, with its FAQ), the authors' published Python tutorial code, and
this project's research report `How day traders make money` (2026-11-12). No SPY minute bar, no daily bar and
no result of this rule on any period after April 2024 was looked at, other than the monthly table the authors
print in their own FAQ (described under "Known limits"). Do not edit after the run; corrections go in the
report.

## Question

Zarattini, Aziz and Barbon, "Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)",
report that a simple intraday trend rule on SPY made money from May 2007 to April 2024. The bot is long-only
and unlevered. Does the long-only, unlevered form of the rule make money after SPY-sized costs in the period
the paper never saw (2024-05-01 onward), and does it add anything to simply holding SPY?

The expected outcome, written down before the run, is a negative or marginal result.

## The rule (from the paper, section 3, and the authors' code)

All times are New York time. A minute bar is named by the minute it starts. The regular session is the bars
09:30 to 15:59 (09:30 to 12:59 on an early-close day).

1. **Move from the open.** For each past session and each minute, `move = |close of that minute bar / open of
   that session's 09:30 bar − 1|`.
2. **Sigma.** For today and each minute, `sigma` = the mean of `move` at that same minute over the previous
   **14 sessions** that have that minute (today is not included). All 14 are required, so the first trade can
   come on the 15th session of the data.
3. **Noise band.** With `Open` = the open of today's 09:30 bar and `PrevClose` = the close of the previous
   session's last regular bar:
   `Upper = max(Open, PrevClose) × (1 + sigma)`, `Lower = min(Open, PrevClose) × (1 − sigma)`.
   The volatility multiplier is 1, as in the paper.
4. **VWAP.** Running, from 09:30, of `volume × (high + low + close) / 3` over running `volume`, regular
   session only, including the current bar (the authors' code).
5. **When decisions happen.** Only on the half-hour: 10:00, 10:30, …, 15:30 (10:00 to 12:30 on an early-close
   day). The decision at HH:MM reads the close of the bar that ends at HH:MM (the 09:59 bar for 10:00), with
   that bar's sigma and VWAP. Twelve decisions on a full day.
6. **Flat by the close.** Every position is closed at the close of the last regular bar.

Position rules, by variant:

| | Name | Position rule at each half-hour decision | Sizing | Use |
|---|---|---|---|---|
| (a) | Long-short, opposite-band stop | Close above `Upper`: be long. Close below `Lower`: be short. Otherwise keep what is held (a long is held until a decision finds the close below `Lower`, and the reverse). | 100% of equity | Replication only |
| (a2) | Long-short, current band + VWAP stop | Close above `max(Upper, VWAP)`: be long. Close below `min(Lower, VWAP)`: be short. Otherwise be flat. | 100% of equity | Replication only |
| (d) | (a2) with the paper's volatility-target sizing | As (a2). | `min(4, 2% / s)` × equity, `s` = sample standard deviation of SPY's last 14 daily close-to-close returns (paper's formula, days t−1 to t−14) | Replication only. This is the paper's headline rule. It uses leverage, so it is in no verdict. |
| (b) | Long-only, opposite-band stop | The long leg of (a): long when the close is above `Upper`; flat when a decision finds the close below `Lower` (where (a) would go short); otherwise keep what is held. | 100% of equity | Reported |
| **(c)** | **Long-only, current band + VWAP stop** | The long leg of (a2): long when the close is above `max(Upper, VWAP)`; otherwise flat. | 100% of equity | **PRIMARY. The verdict is on (c) only.** |

(c) is primary because the bot is long-only and unlevered, and of the two long-only forms it is the long leg
of the paper's better rule (unlevered Sharpe 1.24 against 0.61 for the opposite-band stop). No leverage, no
shorting and no volatility-target sizing appears in the verdict.

**Fills.** A position change decided at HH:MM fills at the **open of the bar that starts at HH:MM**, which is
the price the paper measures its slippage against. If that bar does not exist, the fill is the close the
decision read. The end-of-day exit fills at the close of the last regular bar. The authors' code fills at the
close of the bar the decision read (one tick earlier); I use the open of the next bar everywhere because that
is a price an order sent at the decision could get. This choice was made once, here.

**Daily return.** As in the paper: shares are fixed at the day's open, `shares = equity at the previous close
× size / Open` (fractional, no rounding), and the day's return is the day's profit over the previous equity.
A day with no trade has a return of zero. No interest is credited on idle cash and no financing is charged.

## Choices the paper leaves open, fixed here

* **Dividends.** Bars are fetched with `adjustment=all` (split- and dividend-adjusted). The authors use raw
  prices and subtract the dividend from the previous close on the ex-date; adjusted bars do the same job (the
  overnight gap is free of the dividend) and leave every within-day ratio unchanged.
* **Sigma on early-close days and after them.** A minute's sigma uses the last 14 sessions that have that
  minute, so early-close days count for minutes up to 12:59 and are skipped for later minutes.
* **Early-close days** (regular session ends 13:00): 2016-11-25, 2017-07-03, 2017-11-24, 2018-07-03,
  2018-11-23, 2018-12-24, 2019-07-03, 2019-11-29, 2019-12-24, 2020-11-27, 2020-12-24, 2021-11-26, 2022-11-25,
  2023-07-03, 2023-11-24, 2024-07-03, 2024-11-29, 2024-12-24, 2025-07-03, 2025-11-28, 2025-12-24. The script
  checks this list against the data (a day whose 13:00 to 15:59 volume is under 5% of its regular-hours volume
  must be on the list, and the reverse) and stops if they disagree; a correction to the list is a calendar
  fact, not a tuning choice, and is recorded in the report.
* **Missing minutes.** A regular-session minute with no bar carries the previous close forward with zero
  volume. A session with fewer than 300 regular bars on the consolidated feed (150 on an early-close day) is
  reported and not traded; it still enters later sigmas only for the minutes it has.
* **A session is traded** only when the previous session exists in the data and sigma is defined.

## Data

* **Source.** Alpaca `https://data.alpaca.markets/v2/stocks/bars`, symbol SPY, `timeframe=1Min`,
  `adjustment=all`, fetched twice: `feed=sip` (the consolidated tape, used for every result) and `feed=iex`
  (one exchange, used only for the feasibility comparison). Also SPY `1Day` bars on `feed=sip`, both
  `adjustment=all` (the benchmark's total return) and `adjustment=raw` (to turn a cost in dollars per share into
  basis points for the replication). Read-only market data. No account or order endpoint is touched, and the
  deployed app is never contacted.
* **Window.** From the first minute bar Alpaca serves (expected 2016-01-04) to **2026-11-11**, the last
  complete session before the fetch.
* Downloads live outside the repo in `~/.paper-training/spy-intraday-momentum-20261112/`.

## Periods

* **Replication:** first tradable session (expected late January 2016) to **2024-04-30**, the end of the
  paper's sample.
* **Held-out:** **2024-05-01 to 2026-11-11** (about 636 sessions). The verdict is on this period only. Sigma
  and the previous close for the first held-out sessions come from April 2024 bars, as they would live.

## Order of work (so the held-out result cannot shape the pipeline)

1. `replicate` reads bars up to 2024-04-30 only and writes the replication check below.
2. If the check fails, I may look for a mistake using replication-period data only. Every fix is listed in the
   report with what it changed. Rule parameters (14 sessions, multiplier 1, half-hour decisions) are not
   touched.
3. `heldout` is run **once**, after step 1 or 2 is settled. If the replication check still fails at that
   point, the held-out numbers are reported with the label "pipeline not validated" and the verdict is
   "not supported" whatever they show.

## Replication check (decides whether the pipeline is trusted)

The paper prints unlevered figures only for its whole sample, 2007 to 2024, most of which Alpaca does not have.
The only figures it gives for 2016 onward are the monthly returns of its headline rule (variant (d), FAQ Q4 of
the 3 February 2025 version), and three dated examples. So the check has two decisive parts and one reported
part.

**R1. Monthly table, variant (d), February 2016 to April 2024 (99 months).** Costs as the paper states them:
$0.0035 commission plus $0.001 slippage per share per side, converted with the raw share price. All three
must hold:

* the correlation between my monthly returns and the paper's is at least **0.85**;
* the mean absolute difference between them is at most **1.5 percentage points**;
* in at least **6 of the 7** full calendar years 2017 to 2023 my yearly return has the same sign as the
  paper's (−6.9, 61.1, 6.9, 26.8, 34.8, 24.4, 37.2).

The tolerance is this wide because the data vendor differs (the paper uses IQFeed), and at up to 4× leverage
one decision that lands a cent on the other side of a band moves a month by a percentage point or more.

**R2. Dated examples from the paper's text.** At least **3 of these 4** must hold:

* 2022-01-20, variant (a): long from the 10:00 decision, and the day loses between 1.9% and 2.5% (paper: −2.19%);
* 2022-01-20, variant (a2): the long is closed at the 13:00 decision and the day's return is within ±0.4% of
  zero (paper: "break-even");
* 2022-01-31, variant (a): the first position of the day is long, opened at the 10:30 decision;
* 2022-04-29, variant (a): the first position of the day is short, opened at the 10:30 decision.

**R3. Variant (a) against the paper's whole-sample figures (reported; the periods differ, so loose).** The
paper gives variant (a) 6.2% a year, volatility 10.9%, Sharpe 0.61, beta to SPY −0.05 over 2007 to 2024. On
2016 to April 2024 I expect, and will flag if not: annual volatility between 7% and 12.5%, and beta to SPY
between −0.20 and +0.10. Its return and Sharpe on this shorter window have no published counterpart and are
reported without a threshold; the paper's own monthly table shows 2016 and 2017 were losing years for the
headline rule. The same is reported for (a2) against the paper's 9.7%, 7.7%, 1.24, −0.03.

The pipeline is **trusted** if R1 and R2 both hold.

## Costs

Costs are charged per side, as basis points of the traded notional (shares × fill price), on every entry and
every exit.

* Every result is reported at **0, 0.5, 1, 2 and 5 bps per side**.
* **Primary: 1 bp per side.** The paper's own figure, $0.0045 a share, is under 0.1 bp at SPY's recent price,
  and SPY's quoted spread is normally one cent. Alpaca charges no commission but routes retail orders to
  wholesalers, and its real fill quality on SPY is unmeasured: a paper account fills at quoted prices and
  cannot measure it. 1 bp a side is therefore an assumption on the cautious side of the paper's, not a
  measurement.
* **Break-even cost:** the cost per side at which the compounded net return over the period is zero (found
  numerically). Also reported: mean gross return per side traded.

## Pass rule (primary variant (c), held-out period, 1 bp per side)

Daily returns over every held-out session, days without a trade counted as zero. SPY's daily return is its
adjusted close-to-close return on the same sessions. All four must hold:

1. **Profit after costs.** Compounded net return above zero.
2. **Sharpe at least 0.5.** Mean daily return over its standard deviation, times √252, with no risk-free rate
   subtracted and no interest credited on idle cash (the paper's convention; the two omissions roughly cancel
   for a rule that sits in cash most of the time).
3. **Low correlation.** Pearson correlation of its daily returns with SPY's below 0.3.
4. **Improves a SPY holding.** The overlay portfolio, whose daily return is SPY's plus the strategy's, has a
   **higher Sharpe** and a **smaller maximum drawdown** (on compounded daily values) than SPY alone, same
   convention for both. Note: the overlay holds up to 200% SPY inside the day on a signal day, so it needs
   intraday buying power; it is a test of whether the strategy diversifies, not a portfolio the bot runs today.

**PASS** = all four hold and the pipeline is trusted. Anything else is **FAIL**.

**What the verdict can and cannot mean.** About 636 sessions is 2.5 years. The standard error of a Sharpe
ratio over 2.5 years is about 0.63, so a true Sharpe of 0.6 gives t ≈ 0.95, and reaching t = 2 at that Sharpe
needs about 11 years. A rule with no edge at all would show a Sharpe of 0.5 or more about one time in five
over a window this short; a rule with the paper's long-short Sharpe of 0.6 would fall below 0.5 almost half
the time. So a pass means "consistent with the paper out of sample", not "proven", and a fail means "not
supported by the period the paper did not see", not "disproven".

## Reported, not decisive

* Every variant on both periods at every cost: compounded and annualised return, volatility, Sharpe with its
  standard error, maximum drawdown, correlation and beta to SPY, share of days with a trade, round trips per
  day, win rate of days with a trade, best and worst day.
* Year by year for (c), (b) and the long-short variants, next to SPY.
* Each pass criterion at each of the five costs.
* The held-out months May 2024 to January 2025 for variant (d) next to the authors' own FAQ table, and the
  primary variant on **2025-02-01 onward** alone, the stretch no version of the paper reports.
* (c) with fills at the close the decision read (the authors' code convention).
* **Feasibility on the free live feed.** Every input (open, previous close, sigma, VWAP, decision close)
  recomputed from `feed=iex` bars alone, over the dates IEX bars exist. Against the consolidated-feed
  signals for (c), (b) and (a), per session: both trade or both do not; same direction of the first
  position; first entry at the same half-hour decision ("within one bar": a decision is one bar, so entries
  either match or differ by at least 30 minutes; the share within one decision either side is also given);
  the share of half-hour decisions with the same position. Also the return of (c) when IEX signals are
  filled at consolidated prices, and the share of regular minutes with no IEX bar. Held-out period shown
  separately. This is not part of the verdict.

## Known limits, stated before the run

* **The authors have already published part of the held-out period.** Their FAQ (Q24) prints monthly returns
  of the levered headline rule through January 2025: May to December 2024 are −4.3, 1.6, 8.2, −2.8, 4.1, 6.7,
  −2.6, 5.7, and January 2025 is −1.2. I have seen those numbers. They are for the levered long-short rule,
  not the long-only unlevered one, and the verdict period was fixed by the task, not chosen after seeing them.
  The stretch nobody has reported is February 2025 onward, shown separately.
* **Replication covers 8 of the paper's 17 years**, and not 2007 to 2015, where much of its return was made
  (2008: +63%).
* **Long-only takes half the rule.** The short leg, which earns in falling markets, is not in the verdict.
* **One instrument, one rule, one short period.** No parameter is searched; nothing else is tried.
* **Fills are assumed.** Minute-bar opens are trades that happened, not prices this bot's order would have
  got; the cost grid stands in for that gap.
* **Free live data is one exchange.** The bot's real-time feed is IEX, so a live run would act on the IEX
  signals measured in the feasibility section, not on the consolidated ones the verdict uses.
