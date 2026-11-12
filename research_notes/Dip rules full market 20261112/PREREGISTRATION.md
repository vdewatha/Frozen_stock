# Dip rules and new-high breakouts on the whole market, delisted stocks included: pre-registration (2026-11-12)

Frozen before any price bar was downloaded for this test. Before writing it, the only data looked at was the
existing SEC ticker map from the earnings test (`ticker_map.parquet`: row counts, date range, how many tickers
are reused; no prices). Do not edit after the run; corrections go in the report.

## Question

Three rules looked good on the bot's 29 single stocks, all of them companies that are still listed today. Do they
still make money after costs on every liquid US stock from 2016 to 2026, including the ones that were later
delisted? And if a rule does pass, how fast would it collect trades on a wide universe?

Two of the rules are frozen forward experiments (`reports/local-sweep-20261110/forward-experiments.md`). On the
bot's 45 symbols they need 10 to 18 years to reach their verdict sample. If they fail here, they are survivor
artefacts and a wider shadow run is not worth it. If they pass, a shadow run on a wide universe is.

## Hypotheses (copied from the frozen spec and `strategy_discovery.candidate_library`; not tuned)

Feature names are the discovery rule language (`app/services/strategies/rules.py`), computed on each stock's own
adjusted daily bars, using bars up to and including the decision bar only.

| # | Name | Entry on the close of bar t | Indicator exit (decided on a close, filled at the next open) | Stop | Time stop | Gate |
|---|---|---|---|---|---|---|
| H2 | Candidate 2, `rule_1cd460d679` (sweep id `1cd460d679:none`) | `dist_sma_200 > 0` and `ret_5 < -0.08` (close above its 200-day mean, 5-day return below −8%) | `ret_1 > 0` (first up close) | 10% | 10 sessions | none |
| H5 | Candidate 5, `rule_7df4090f31` (sweep id `7df4090f31:vix_above_20`) | `down_streak >= 4` (4 or more down closes in a row) | `dist_sma_5 > 0` (close above its 5-day mean) | 10% | 10 sessions | VIX ≥ 20 on the decision bar |
| HB | 50-day breakout, `94d4afa886` | `at_high_50 == 1` (close is the highest of the last 50 closes) | `dist_sma_50 < 0` (close below its 50-day mean) | 10% | 40 sessions | four settings, below |

**Breakout settings.** The sweep's step-down correction at 11 bps named six survivors
(`reports/local-sweep-20261110/summary.json`, `reality_check.11.0.stepdown_survivors`). Four are this rule:
`94d4afa886:none`, `94d4afa886:adx_not_downtrend`, `94d4afa886:spy_sma10`, `94d4afa886:spy_sma50`. The other two are
earnings drift, which failed with delisted stocks on 2026-11-11. HB is tested in exactly those four settings:

* `none`: no gate.
* `adx_not_downtrend`: the stock's own Wilder ADX(14) regime on the decision bar is not `trend_down` (ADX ≥ 25 with
  −DI above +DI), as in `strategies/regime.py` and the sweep's `build_gate_masks`.
* `spy_sma10`, `spy_sma50`: SPY's close on the decision bar is above the mean of its last 10 (50) closes
  (`market_gate.above_sma`).

**VIX gate (H5).** The CBOE VIX close (yfinance `^VIX`) dated on the decision bar, carried forward at most 5
sessions; the gate is open when it is ≥ 20, and also open when no close is known (fails open, as the paper gate
does).

## Execution (the sweep's, unchanged)

The app's own labeler, `triple_barrier.label_events`, imported from the repo, with a flat cost per side:

* decision on the close of bar t, fill at the open of bar t+1;
* inside a bar the stop is checked first; an open at or below the stop fills at the open (gap);
* the indicator exit is decided on a close and filled at the next open; the time stop fills at the open of the
  session that is N sessions after entry;
* one position per stock per rule: a new entry only when that rule is flat in that stock (`_executable`);
* long only; no target.

**Cost.** 25 bps per side (50 bps a round trip), the sweep's 25 bps case. That is the acting cost for every
criterion. 11 bps per side is reported, not decisive.

**A stock that stops trading during a hold.** The labeler leaves such a trade unresolved. Here, when the stock's
bars end during the hold and it has no bar in the last 10 sessions of the data, the trade exits at its **last
close**, less the exit cost. Each such case is listed in the report with whether the issuer filed an 8-K with
Item 1.03 (bankruptcy) within 365 days before its last bar (from the earnings test's `bankruptcies.csv`). A
sensitivity with those bankruptcy cases booked at −100% is reported, not decisive. A trade still open at the end of
the data is left out.

## Universe

* **Who.** Every issuer in the SEC insider-filing ticker map (`ticker_map.parquet` from the earnings test: the
  issuer's own trading symbol on each Form 3, 4 or 5, dated, 2015 Q1 to 2026 Q3, delisted issuers included). One
  candidate series per (issuer CIK, ticker) pair whose filings reach 2016 or later.
* **Listed on each date** means the series has a SIP bar that day. Over-the-counter stocks have no SIP bars and are
  out.
* **Eligible on the decision bar t**, measured on bars up to t only: a raw (unadjusted) close of at least **$5**,
  and a median daily dollar volume (raw close × raw volume) over the last 20 bars of at least **$5,000,000**. The
  same floor as the earnings test, so that a flat cost is believable.
* **Common stock.** The insider map gives the issuer's common stock or units in nearly all cases; ETFs, ETNs and
  foreign private issuers (ADRs) do not file Forms 3/4/5 and are therefore not in it. Closed-end funds and BDCs do
  file them and are not removed. This is stated as a limit, not corrected.

## Bars and identity

* **Source.** Alpaca `https://data.alpaca.markets/v2/stocks/bars`, `feed=sip`, `timeframe=1Day`, from 2016-01-01 to
  the newest bar the free plan serves, with the earnings test's `fetch_bars` / `fetch_many` (100 symbols a request,
  3 threads, 0.32 s spacing). Fetched twice: `adjustment=raw` (eligibility, identity check) and `adjustment=all`
  (open, high, low, close for every rule, return and stop). SPY comes from the same source and is the session
  calendar and the benchmark. Read-only market data; no account or order endpoint is touched.
* **asof.** Each (CIK, ticker) series is fetched with `asof` set to the median date of that pair's insider filings
  (not before 2016-01-15), so the symbol resolves to the company that held it then. Alpaca's `asof` follows that
  company across later renames, so a ticker later reused by another company does not supply bars.
* **Identity check (price).** Where the issuer's insider filings report open-market or withholding transactions
  (codes P, S, F, price > 0) on days the series has a bar, the median of (reported price / raw close) over all of
  them must lie in 0.7 to 1.4, or the series is dropped. In any calendar year with at least 3 such transactions
  whose median ratio is outside that range, entries in that year are not allowed. A series with no such
  transaction is kept only if its (CIK, ticker) pair appears on insider filings on at least 3 different days
  (so a one-off typo cannot bring in an unrelated stock); it is marked unverified. The verified share is reported.
* **Duplicates.** One company can appear under several (CIK, ticker) pairs (a rename, or a holding-company CIK).
  Two series that carry the same raw close and volume on 3 or more of the same dates (checked on every 20th
  session) are the same stock; only the one with more bars is kept.
* **Delisted since.** A series with no bar in the last 10 sessions of the data.

## Statistics

Per trade: net return at 25 bps a side; SPY's return over the same hold (from SPY's open on the entry day to SPY's
open on the exit day when the trade exits at an open, and to SPY's close on the exit day when it exits inside the
bar at the stop or at a last close); excess over SPY = net − SPY. The t-statistic is the month-clustered t of the
per-trade mean (clusters = calendar month of the decision bar), the sweep's `_clustered_t`.

## Pass rule (the four criteria of `research_notes/Earnings drift delisted 20261111/PREREGISTRATION.md`)

On every completed trade from 2016 to the end of the data, at 25 bps a side, a rule passes only if all four hold:

1. **Profit after costs.** Mean net return per trade > 0 with month-clustered t ≥ 2.0.
2. **Beats SPY.** Mean excess over SPY over the same holds > 0.
3. **Most years.** Among calendar years (by decision date) with at least 20 trades, more than half have a positive
   mean excess over SPY.
4. **Not a few big winners.** After dropping the 1% of trades with the highest excess over SPY, the mean net return
   and the mean excess over SPY are both still > 0 (no t requirement).

H2 and H5 are judged on their own (each is a registered candidate). HB has four settings, so for HB the t in
criterion 1 must be ≥ 2.50 (Bonferroni over four, one-sided 5%), and HB passes if at least one setting passes all
four criteria with that bar. No other variant, threshold, gate, exit, cost or universe is tried.

A pass means: worth a shadow forward run on a wide universe. A fail means: the rule's survivor-only record does not
hold up, and the forward experiment stays as registered (45 symbols, inconclusive for a decade). Nothing here
changes the frozen experiments or any app code.

## Reported, not decisive

* The sweep's **excess over the stock's own causal drift**: net return minus (mean daily close-to-close return of
  that stock from its first bar up to the decision bar × holding sessions, at least 1), with its clustered t.
* Gross; net at 11 bps a side; win rate; mean hold; each calendar year.
* The share of trades in stocks **delisted since**, the result on those, and the result on **survivors only**
  (stocks still trading at the end).
* The trades that stopped trading during the hold: each case, its last-close return, the bankruptcy tag, and the
  −100% sensitivity.
* The verified-identity subset on its own.
* **Trade rate on a wide universe.** For each year 2017 to 2026, the 500 stocks with the highest median daily dollar
  volume over the previous calendar year (bars that year, any eligibility); the rule's completed trades whose
  decision falls in that year in those stocks (2026 scaled to a full year by sessions). The mean over full years
  2017 to 2025, and the years to reach the rule's verdict sample: `VERDICT_SAMPLE` of
  `forward_experiments.py` (800 for candidates 2 and 5; 800 for the breakout, which has no registered sample, as for
  every other single-stock candidate). The same for the whole eligible universe.
* Coverage: series requested, with bars, dropped by the identity check, duplicates, unverified, eligible
  stock-days per year.

## Known limits, stated before the run

* **Alpaca SIP daily bars start 2016-01-04.** The 200-day mean needs 200 bars, so H2 can first fire in about
  October 2016 and a stock listed later only after 200 sessions of its own; H5 and HB start earlier. One decade,
  mostly a rising market.
* **Who is missing.** Issuers with no insider filing from 2015 on (rare for a listed US company), foreign private
  issuers and ADRs, stocks whose insider-reported symbol is wrong on every filing, and listings after 2026-09-30
  (the map ends there). Stocks that listed and died between insider filings are covered only if their filings
  named the symbol.
* **No delisting returns.** A delisted stock's last bar is its last exchange trade; the shares may have kept some
  value over the counter, or none.
* **Flat cost.** 25 bps a side on a $5M-a-day stock is fair for the bot's sizes; a higher cost only lowers returns.
* **Sample size on a wide universe.** Trades on the same day across hundreds of stocks are correlated (the frozen
  spec's design effect grows with trades per day), so the verdict samples above are a floor; the clustered t
  already accounts for this within a month.
