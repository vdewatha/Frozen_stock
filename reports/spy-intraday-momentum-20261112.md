# SPY intraday momentum (noise band), with a held-out period: 2026-11-12

Pre-registration: `research_notes/SPY intraday momentum 20261112/PREREGISTRATION.md` (commit `8d32a0e`, pushed
before any minute bar was downloaded). Script, tables and daily returns: the same folder. Local research only:
nothing was sent to the deployed app, no app code was changed, and no order endpoint was touched.

## Verdict: FAIL

The long-only, unlevered form of the rule lost money in the period the paper never saw, before any cost is
charged. It fails all four pre-registered criteria.

Primary variant (c): long-only, current band + VWAP stop, 100% of equity, 1 bp a side.
Held-out period: 2024-05-01 to 2026-11-11, 636 sessions.

| Criterion | Needed | Measured | |
|---|---|---|---|
| 1. Profit after costs | net return above 0 | **−6.6%** (−2.7% a year) | fail |
| 2. Sharpe | at least 0.5 | **−0.68** | fail |
| 3. Correlation of daily returns with SPY | below 0.3 | **0.35** | fail |
| 4. SPY plus the strategy against SPY alone | higher Sharpe and smaller maximum drawdown | Sharpe 0.91 against 1.17; drawdown 19.5% against 18.8% | fail |

* **Break-even cost: none.** The rule lost 0.7% over the period at zero cost (−0.08 bp per side traded), so no
  cost assumption rescues it.
* SPY held over the same sessions made +55.8% (19.2% a year, Sharpe 1.17).
* The pipeline is trusted: it reproduces the paper's own monthly table to a correlation of 0.993 (below).
* **What the fail means.** 636 sessions is 2.5 years. A Sharpe ratio measured over that span has a standard
  error of about 0.65, so this is "not supported by the period the paper did not see", not "disproven". The
  rule made money in the replication years (below). What the held-out period adds is that there is no sign of
  an edge for the long-only form since April 2024, at any cost.

**What this means for the bot.** Do not build an intraday decision loop for this rule. No forward paper test
is proposed, so the list of what one would need is left out. Of the two untested ideas in the day-trader
research, this was the better documented one. The other (stocks-in-play opening-range breakout) is still
untested.

## The rule, in plain words

Every half hour from 10:00 to 15:30, compare SPY's price with a band around the day's open. The band's width at
each time of day is SPY's average move from the open to that time of day over the last 14 sessions, so it is
narrow in the morning and wide by the afternoon. The band is anchored on the higher of today's open and
yesterday's close for the upper edge, and the lower of the two for the lower edge. A price above the band is
read as unusual buying, and the rule goes long. Everything is closed by 16:00.

| | Variant | What it does | Role |
|---|---|---|---|
| (a) | Long-short, opposite-band stop | Long above the band, short below it; a long is held until the price is below the lower edge | Replication only |
| (a2) | Long-short, band + VWAP stop | Long only while the price is above both the band and the day's VWAP; the reverse for shorts | Replication only |
| (d) | (a2) with the paper's sizing | Position sized to a 2% daily volatility target, up to 4× leverage. The paper's 19.6% headline | Replication only |
| (b) | Long-only, opposite-band stop | The long half of (a) | Reported |
| **(c)** | **Long-only, band + VWAP stop** | **The long half of (a2)** | **Primary: the verdict** |

Fills are at the open of the minute bar that starts at the decision time. The exit at the close is the last
regular bar's close. Bars are Alpaca's consolidated tape (`feed=sip`), split- and dividend-adjusted.

## Replication against the paper: passed

The replication stage read bars up to 2024-04-30 only, and its output was committed (`fde3e4c`) before the
held-out stage was run. Alpaca's minute history starts 2016-01-04, so this covers the last 8 of the paper's 17
years.

| Check | Needed | Measured | |
|---|---|---|---|
| R1. Monthly returns of the headline rule (d), February 2016 to April 2024, against the paper's table (99 months) | correlation at least 0.85 | 0.993 | pass |
| | mean absolute difference at most 1.5 pp | 0.32 pp | pass |
| | same sign in at least 6 of the 7 years 2017 to 2023 | 7 of 7 | pass |
| R2. The paper's dated examples | at least 3 of 4 | 3 of 4 | pass |

* 2022-01-20, variant (a): long from 10:00, the day loses 2.19%. The paper says −2.19%.
* The one miss: on 2022-01-20 variant (a2) closes its long at the 13:30 decision, not 13:00 as in the paper's
  figure. The day ends −0.23%, inside the "about break-even" range. I did not chase it. A one-cent difference
  between data vendors at 13:00 is enough to cause it.
* Year by year, (d) at the paper's cost, paper against mine: 2017 −6.9% / −8.9%, 2018 61.1% / 54.5%, 2019 6.9% /
  5.7%, 2020 26.8% / 24.6%, 2021 34.8% / 29.7%, 2022 24.4% / 24.7%, 2023 37.2% / 39.2%.
* The match continues into the held-out period. The authors' FAQ prints May 2024 to January 2025 for (d); mine
  are within about 1 percentage point every month (largest gap December 2024: 5.7% against 6.7%).

The unlevered long-short variants at the paper's cost ($0.0045 a share), 2016-01-25 to 2024-04-30, next to the
paper's whole-sample figures (different years, so only a rough comparison):

| Variant | Return a year | Volatility | Sharpe | Beta to SPY |
|---|---|---|---|---|
| (a) here, 2016 to April 2024 | 5.3% | 8.9% | 0.63 | −0.01 |
| (a) paper, 2007 to 2024 | 6.2% | 10.9% | 0.61 | −0.05 |
| (a2) here, 2016 to April 2024 | 6.9% | 6.8% | 1.02 | −0.01 |
| (a2) paper, 2007 to 2024 | 9.7% | 7.7% | 1.24 | −0.03 |

So the paper's results are real in its own sample, on independent data.

## Held-out period, each criterion at each cost (primary variant (c))

| Cost a side | 1. Net return | 2. Sharpe | 3. Correlation with SPY | 4. Overlay Sharpe (SPY alone 1.17) | 4. Overlay max drawdown (SPY alone 18.8%) | All four |
|---|---|---|---|---|---|---|
| 0 bp | −0.7% fail | −0.05 fail | 0.38 fail | 1.04 fail | 19.3% fail | fail |
| 0.5 bp | −3.7% fail | −0.37 fail | 0.36 fail | 0.98 fail | 19.4% fail | fail |
| **1 bp (primary)** | **−6.6% fail** | **−0.68 fail** | **0.35 fail** | **0.91 fail** | **19.5% fail** | **fail** |
| 2 bp | −12.2% fail | −1.30 fail | 0.32 fail | 0.78 fail | 19.8% fail | fail |
| 5 bp | −26.9% fail | −3.01 fail | 0.23 pass | 0.37 fail | 20.5% fail | fail |

The paper's own cost is $0.0045 a share, which is 0.06 to 0.09 bp at SPY's price in this period. That is
close to the 0 bp row.

### Every variant on the held-out period

| Variant | At 0 bp: total, Sharpe | At 1 bp: total, Sharpe | Break-even cost a side | Correlation with SPY | Max drawdown at 1 bp |
|---|---|---|---|---|---|
| **(c) long-only, band + VWAP** | −0.7%, −0.05 | −6.6%, −0.68 | none (loses before costs) | 0.35 | 7.5% |
| (b) long-only, opposite band | +3.1%, 0.21 | −1.3%, −0.04 | 0.71 bp | 0.46 | 8.2% |
| (a) long-short, opposite band | +9.8%, 0.46 | +1.4%, 0.10 | 1.17 bp | 0.12 | 10.3% |
| (a2) long-short, band + VWAP | +3.4%, 0.25 | −7.8%, −0.50 | 0.29 bp | −0.04 | 15.6% |
| (d) the paper's levered headline rule | +10.6%, 0.34 | −19.0%, −0.48 | not computed (levered) | −0.12 | 35.6% |

* (d) at the paper's own cost made +8.2% in the held-out period (Sharpe 0.28, maximum drawdown 23.1%). It was
  up about 17% by December 2024, which the authors report, and has lost since.
* No variant, with or without shorting or leverage, reaches a Sharpe of 0.5 in the held-out period at any
  cost, including zero.
* (c) with fills at the close the decision read (the authors' code convention): −6.7% at 1 bp. The fill
  convention is not the reason.

### Activity (held-out)

| Variant | Days with a trade | Round trips | A day | Winning days, of days traded | Gross per side traded |
|---|---|---|---|---|---|
| (c) | 34.0% (216 of 636) | 305 | 0.48 | 41.7% | −0.08 bp |
| (b) | 34.0% | 216 | 0.34 | 54.2% | +0.85 bp |
| (a) | 60.7% | 400 | 0.63 | 50.5% | +1.29 bp |
| (a2) | 60.7% | 573 | 0.90 | 39.4% | +0.33 bp |

The rule trades on one day in three long-only, and on three days in five with shorts. That is the same rate
as in the replication years (33.3% and 60.5%).

### Year by year, net at 1 bp a side

| Year | SPY | (c) | (b) | (a) | (a2) | (d) |
|---|---|---|---|---|---|---|
| 2016 (from 01-25) | 19.2% | −0.5% | 1.6% | −6.6% | −7.3% | −22.3% |
| 2017 | 21.7% | −1.1% | −2.4% | −9.3% | −5.8% | −21.0% |
| 2018 | −5.0% | 3.1% | −2.8% | 16.7% | 16.9% | 38.5% |
| 2019 | 31.1% | −0.2% | 0.8% | −3.6% | −2.6% | −4.7% |
| 2020 | 18.5% | −1.9% | 3.7% | 6.0% | −2.4% | 17.4% |
| 2021 | 28.6% | 7.5% | 6.9% | 1.9% | 6.0% | 16.3% |
| 2022 | −18.2% | 8.0% | 8.2% | 8.8% | 11.8% | 16.6% |
| 2023 | 26.2% | 4.4% | 4.4% | 4.9% | 7.8% | 25.4% |
| 2024, January to April | 5.9% | 3.5% | 4.0% | 4.1% | 1.9% | 7.5% |
| **2024, May to December (held-out)** | 17.9% | −2.9% | −6.2% | −5.3% | −0.5% | 8.9% |
| **2025 (held-out)** | 17.7% | −0.6% | 8.2% | 13.3% | −2.9% | −12.7% |
| **2026, to 11-11 (held-out)** | 12.3% | −3.3% | −2.7% | −5.6% | −4.6% | −14.9% |

(c) lost in all three held-out years. Its good years were 2021 to early 2024. At 1 bp a side the levered rule
(d) loses heavily in most years, because 1 bp is more than ten times the paper's cost and leverage multiplies
it.

### The replication years, for scale (not the verdict)

| Variant, 2016-01-25 to 2024-04-30, 2,081 sessions | Total at 1 bp | A year | Sharpe | Break-even cost a side |
|---|---|---|---|---|
| (c) | +24.7% | 2.7% | 0.67 | 2.13 bp |
| (b) | +26.5% | 2.9% | 0.52 | 2.69 bp |
| (a) | +22.4% | 2.5% | 0.32 | 1.76 bp |
| (a2) | +26.4% | 2.9% | 0.45 | 1.63 bp |
| SPY | +201.4% | 14.3% | 0.84 | |

Two things stand out. First, the whole effect is 1.6 to 2.7 bp per side traded even in the paper's own sample.
It exists only at institutional costs, and a retail order that loses 2 bp a side to its fill has no edge left.
Second, in these years (c) at 1 bp meets criteria 1 to 3 but not 4: adding it to SPY raises the Sharpe (0.92
against 0.84) but does not reduce the worst drawdown (33.9% against 33.8%). The reason is that a long-only
intraday rule is flat during a crash and cannot offset it.

### The stretch nobody has reported: 2025-02-01 to 2026-11-11 (447 sessions)

The authors' FAQ prints the levered rule through January 2025. From February 2025: (c) at 1 bp −3.6% (Sharpe
−0.48, break-even 0.16 bp); (b) +7.5% (Sharpe 0.57, but correlation with SPY 0.49); (a) +9.5%; (d) at zero cost
−5.0%. SPY +28.7%.

## Why it failed (looked at after the run; description, not a test)

* **Long entries stopped paying.** (c) made +2.18 bp per side traded in the replication years and −0.08 bp in
  the held-out years. Its share of winning days went from 44.2% to 41.7%. The rule relies on a few large trend
  days to pay for many small losses, and in the held-out period those days were too few or too small.
* **One day carries the opposite-band variants.** On 2025-04-09 SPY rose 10.5% and variants (a) and (b) made
  +8.8% that day. Without that single day, (b) is −9.2% and (a) is −6.8% over the held-out period at 1 bp. (c)
  made +3.8% that day and is negative even with it.
* **The VWAP stop cost more than it saved.** In the paper's sample the VWAP stop doubles the Sharpe. In the
  held-out period it trades 43% more often than the opposite-band stop (573 round trips against 400 for the
  long-short pair) and earns less before costs.
* **The correlation is not low for a long-only rule.** The long-short rule has near-zero correlation with SPY
  (−0.04 to 0.12). The long-only half is long on days when SPY is rising, so its correlation is 0.35 to 0.46.
  The "low correlation diversifier" description in the paper belongs to the long-short rule.

## Feasibility on the free live feed (IEX only): not part of the verdict

Every input was recomputed from `feed=iex` bars alone: the open, the previous close, the 14-session band, VWAP
from IEX volume, and the decision price. Alpaca's IEX minute bars start 2020-07-27. One session since then has
no IEX bar at all (2025-03-10).

| Held-out period, 635 sessions | (c) | (b) | (a) |
|---|---|---|---|
| Days traded: consolidated / IEX | 216 / 215 | 216 / 215 | 385 / 384 |
| Trade-or-not agrees | 99.2% | 99.2% | 98.6% |
| On consolidated trade days, IEX also trades | 98.6% | 98.6% | 98.7% |
| ...same first direction | 100.0% | 100.0% | 100.0% |
| ...first entry at the same half-hour decision | 91.5% | 92.0% | 94.5% |
| ...first entry within one decision (30 minutes) either side | 96.2% | 96.2% | 97.9% |
| Same day, direction and entry time, of consolidated trade days | 90.3% | 90.7% | 93.2% |
| Half-hour decisions holding the same position | 99.1% | 99.1% | 98.5% |
| Net at 1 bp, IEX signals filled at consolidated prices (consolidated signals) | −7.6% (−6.6%) | −1.6% (−1.3%) | +0.2% (+0.8%) |
| Correlation of the two daily return series | 0.980 | 0.995 | 0.995 |

* "Within one bar": decisions happen only on the half-hour, so an entry either matches exactly or differs by 30
  minutes or more. Both readings are in the table.
* 1.9% of regular minutes and 2.3% of decision minutes have no IEX bar. The decision then uses the last IEX
  price. The IEX open and close are a median 0.5 bp from the consolidated ones.
* Over all sessions with IEX signals (2020-08-14 on, 1,568 sessions) the figures are the same within two points.
  Full tables: `iex_feasibility.md`.
* **Reading:** the free feed would have produced the same trade on about 9 trading days in 10, and nearly the
  same return. Data is not what stops this strategy. The missing edge is.

## Costs: what is assumed and what is not known

* Costs are assumptions, not measurements. A paper account fills at quoted prices and cannot measure what a
  real order would get, so this report does not claim a measured cost.
* 1 bp a side is cautious next to the paper's $0.0045 a share (under 0.1 bp). The verdict does not depend on
  the choice: (c) fails at zero cost.
* In the replication years the choice would have mattered a great deal. (c)'s break-even was 2.13 bp a side.

## Departures from the pre-registration

1. **The early-close data check was wrong and was replaced. The list was not changed.** I had written that an
   early-close day would have under 5% of its volume from 13:00 to 15:59. SPY keeps trading after an early
   close, and those hours hold 6% to 34% of the day's volume, so the check rejected every listed day. The
   check now requires each listed day to have fewer than 180 minute bars from 13:00, and names any other
   session that does. Two unlisted sessions are named: 2019-08-12 (30 afternoon minutes missing from Alpaca's
   history) and 2020-03-18 (a market-wide trading halt). Both were traded with the last price carried
   forward, as pre-registered. This was found and fixed in the replication stage, before the held-out stage ran.
2. **The feasibility table covers 635 held-out sessions, not 636**, because 2025-03-10 has no IEX bar. The
   consolidated figures in that table therefore differ slightly from the main tables for (a).
3. Nothing else. No parameter, period, cost or criterion was changed, and each variant was run once.

## Limits

* **Short held-out period.** 2.5 years, one market regime (SPY up 56%, one sharp drop in spring 2025). A rule
  that earns on volatile, falling days had few of them.
* **Replication covers 2016 onward only.** The paper's best years (2008 to 2011) cannot be checked on free data.
* **Long-only takes half the rule.** The verdict says nothing about the long-short rule as a strategy. For
  what it is worth, that also fell short in the held-out period (table above).
* **The authors had already published May 2024 to January 2025** for the levered rule. I saw those numbers
  before the run; they are stated in the pre-registration.
* **Minute-bar opens are trades that happened**, not fills this bot would have received.
* **One vendor.** Alpaca's consolidated history has small gaps (0.01% of regular minutes).

## Reproduce

```
W=~/.paper-training/spy-intraday-momentum-20261112
ALPACA_KEY=... ALPACA_SECRET=... .venv/bin/python -I "research_notes/SPY intraday momentum 20261112/spy_intraday_momentum.py" fetch "$W"
.venv/bin/python -I "research_notes/SPY intraday momentum 20261112/spy_intraday_momentum.py" replicate "$W"
.venv/bin/python -I "research_notes/SPY intraday momentum 20261112/spy_intraday_momentum.py" heldout "$W"
.venv/bin/python -I "research_notes/SPY intraday momentum 20261112/spy_intraday_momentum.py" iex "$W"
```

Files in `research_notes/SPY intraday momentum 20261112/`:

* `PREREGISTRATION.md`: the frozen plan.
* `spy_intraday_momentum.py`: fetch, replicate, heldout, iex.
* `replication.md`, `replication.json`: the replication check, month by month.
* `tables.md`, `results.json`: every variant, period and cost.
* `daily_returns.csv.gz`: one row per session, with SPY's return and each variant's gross return, turnover,
  sides, first entry slot and net return at 1 bp.
* `iex_feasibility.md`, `iex_feasibility.json`: the IEX comparison.
* `fetch.out.txt`: bar counts per month and feed.

The minute bars (about 1.06 million regular-session bars on the consolidated feed and 0.6 million on IEX) stay
outside the repo in
`~/.paper-training/spy-intraday-momentum-20261112/`.
