# Dip rules and new-high breakouts on the whole market, delisted stocks included: 2026-11-12

Pre-registration: `research_notes/Dip rules full market 20261112/PREREGISTRATION.md` (commit `4194e0d`, made before
any price bar was downloaded). Script, tables and trades: the same folder. Local research only: nothing was sent to
the deployed app, no app code or frozen experiment was changed, and no order endpoint was touched.

## Verdict: all three fail

| Hypothesis | Trades (stocks) | Net a trade at 25 bps a side | Against SPY over the same holds | Years beating SPY | Result |
|---|---|---|---|---|---|
| Candidate 2, `rule_1cd460d679` (5-day drop below −8% in an uptrend, exit first up close) | 66,832 (4,193) | **−18.8 bps** (t −1.88) | −41.1 bps (t −6.15) | 1 of 11 | **fail** (0 of 4 criteria) |
| Candidate 5, `rule_7df4090f31` (4 down closes, VIX ≥ 20, exit above 5-day mean) | 75,015 (4,439) | +25.6 bps (t 1.03) | −36.4 bps (t −3.01) | 1 of 10 | **fail** (0 of 4) |
| 50-day breakout `94d4afa886`, best of its four settings (`spy_sma10`) | 84,262 (4,823) | +10.0 bps (t 0.26, needed 2.50) | −94.7 bps (t −4.03) | 1 of 11 | **fail** (0 of 4) |

Every rule fails every criterion. The other three breakout settings are within 6 bps of the ungated rule
(+4.3 bps, t 0.12; −99.5 bps against SPY).

**What this means for the bot.** Candidates 2 and 5 are not worth a shadow run on a wide universe. They stay
registered as they are (45 symbols, "inconclusive" for a decade), because changing them would be a new experiment.
The breakout family is closed. Nothing here changes the frozen experiments.

**Why they looked good before.** Not mainly because of delisted stocks. Survivors alone fail as well (candidate 2:
−16.0 bps; candidate 5: +30.0 bps, t 1.21, and −34.6 against SPY; breakout: +8.5 bps). The good record came from
the stocks the sweep was run on. Those are today's 29 largest companies, so they were picked knowing they won. Run
through this same pipeline, the same 29 stocks over the same years reproduce the sweep almost exactly (candidate 2
+54 bps, sweep +48; candidate 5 +58, sweep +69; breakout +152 with t 3.19, sweep +151). On the 500 most liquid stocks of each
year, chosen on the year before, all three fail (candidate 2 −1.3 bps, candidate 5 +28.9 with t 1.19, breakout
+23.9 with t 0.62, all negative against SPY). The edge belonged to the stock list, not to the rules.

**A second reason, for the two dip rules.** Stock dips cluster on market-wide sell-offs, and the market itself
rebounds after them. Buying SPY at the same moments made +22 bps over candidate 2's average 1.6-session hold, and
+62 bps over candidate 5's 3 sessions. Buying the dipped stocks instead made less after costs. Candidate 5 is
positive before costs (+75.8 bps gross; +53.8 at 11 bps a side, t 2.16), but it loses to SPY in 9 of the 10 years
with trades.

## How fast each rule collects trades on a wide universe

None passes, so the following only answers the sampling question in the brief. Completed trades per year (2026
scaled to a full year). "Top 500" is the 500 stocks with the highest median daily dollar volume in the previous
calendar year. "All" is every eligible stock.

| Rule | Mean a year, top 500 (2017–2025) | Mean a year, all | Verdict sample | Years to verdict, top 500 | All | On the bot's 45 symbols (frozen spec) |
|---|---|---|---|---|---|---|
| Candidate 2 | 769 | 6,267 | 800 | 1.0 | 0.13 | ~18 |
| Candidate 5 | 1,538 (none in 2017: the VIX never closed at 20) | 7,680 | 800 | 0.5 | 0.10 | ~10 |
| Breakout (any setting) | 1,665–1,754 | 7,865–8,344 | 800 | 0.5 | 0.10 | not registered |

A wide universe would get these rules to their verdict sample in about a year. But the sample is a floor. With
hundreds of trades on the same day, trades are much less independent than the frozen spec's design effect of 2.5
assumes. And on the evidence above, a verdict reached that fast would be a fail.

## Each criterion (25 bps a side, completed trades 2016-01 to 2026-11-11)

| Variant | Trades | Net bps | t | t needed | vs SPY bps | Years + / counted | Without top 1%: net / vs SPY | vs own drift bps (t) | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Candidate 2 | 66,832 | −18.8 | −1.88 | 2.00 | −41.1 | 1 / 11 | −44.6 / −66.7 | −52.5 (−5.07) | no | no | no | no |
| Candidate 5 | 75,015 | +25.6 | +1.03 | 2.00 | −36.4 | 1 / 10 | +0.6 / −60.6 | +0.1 (+0.01) | no | no | no | no |
| Breakout, no gate | 89,762 | +4.3 | +0.12 | 2.50 | −99.5 | 1 / 11 | −80.9 / −181.7 | −270.7 (−7.73) | no | no | no | no |
| Breakout, ADX not downtrend | 89,699 | +4.3 | +0.12 | 2.50 | −99.6 | 1 / 11 | −80.9 / −181.8 | −270.8 (−7.73) | no | no | no | no |
| Breakout, SPY > 10-day mean | 84,262 | +10.0 | +0.26 | 2.50 | −94.7 | 1 / 11 | −75.3 / −176.9 | −268.0 (−7.35) | no | no | no | no |
| Breakout, SPY > 50-day mean | 84,681 | +3.8 | +0.10 | 2.50 | −95.7 | 1 / 11 | −82.0 / −178.5 | −272.6 (−7.44) | no | no | no | no |

Criteria: (1) net mean > 0 with month-clustered t ≥ 2 (2.50 for the four breakout settings); (2) mean excess over
SPY > 0; (3) more than half of the years with ≥ 20 trades beat SPY; (4) net and excess over SPY both still > 0
after the top 1% of trades by excess are removed. Candidate 5's criterion 4 net is +0.6 bps, positive only after
rounding, and its excess over SPY is negative, so it fails criterion 4 anyway.

The only year in which any rule beat SPY: 2016 for candidate 2 (+1 bp on 813 trades), 2021 for candidate 5
(+14 bps), and 2020 for the breakout (+132 bps, the post-crash rally). Full per-year tables, gross returns and the
11 bps case are in `research_notes/Dip rules full market 20261112/tables.md`.

**Excess over the stock's own drift** (the sweep's measure, reported, not decisive) is strongly negative for the
breakout (−271 bps). Read it with care. Drift here is the mean daily return from 2016 (not 2000, as in the sweep)
up to the decision bar. An arithmetic mean overstates drift for volatile stocks, and stocks making new highs have
high past drift. So the excess over SPY is the measure to rely on, and it is −95 to −100 bps.

## Delisted stocks

| Variant | Share of trades in stocks delisted since | Those trades: net / vs SPY | Survivors only: net (t) / vs SPY | Verified-identity only: net (t) |
|---|---|---|---|---|
| Candidate 2 | 17.3% | −32.0 / −51.2 bps | −16.0 (−1.59) / −39.0 | −18.3 (−1.82) |
| Candidate 5 | 17.7% | +4.9 / −44.8 | +30.0 (+1.21) / −34.6 | +25.5 (+1.02) |
| Breakout, no gate | 18.4% | −14.5 / −111.3 | +8.5 (+0.23) / −96.9 | +5.3 (+0.14) |

"Delisted since" means the stock has no bar in the last 10 sessions of the data. Including delisted stocks makes
each rule worse, but each rule fails on survivors alone too.

**Stocks that stopped trading during a hold.** Each exited at its last close, as pre-registered.

* Candidate 2: 5 cases, all acquisitions or mergers, none tagged bankrupt (ISLE 2017, VMW 2023, ENLC, PARA, FL
  2025), mean −2.98% a trade.
* Candidate 5: 13 cases, all mergers or reorganisations (RTN 2020, ENBL, STL, ISBC, CCMP, NEOG, TMX, PBFX, CEQP,
  LSXMA, LSXMK, SASR, CFLT), mean −1.12%. One of them, NEOG, is a data break and not a delisting: Neogen kept
  trading after its 2022 merger with 3M's food-safety business, but Alpaca's history for it stops there.
* Breakout (no gate): 728 cases, mostly take-overs during the 40-session hold, mean +2.00%, 46% positive. Four were
  tagged by an Item 1.03 (bankruptcy) 8-K in the year before the last bar: MYCC, XOG, PING, VSTO. XOG is a real
  case (bankrupt in 2020, merged in 2021). The others look like acquisitions, so the tag is loose. Booking all four
  at −100% moves the breakout from +4.3 to +3.8 bps. No verdict changes.

## Universe and data

* **Who is in it.** The SEC insider-filing ticker map from the earnings test: 14,449 (issuer, ticker) pairs with
  filings from 2016 onward, from 11,579 issuers.
  * 11,628 had SIP bars. Those without are mostly over-the-counter, debt-only or unlisted filers, or have a wrong
    symbol.
  * Identity check: 9,058 verified against insider trade prices. 1,256 were kept unverified (no priced insider
    trade, but the symbol was named on at least 3 filing days). 946 thin unverified series and 368 price mismatches
    were dropped.
  * 1,595 duplicates were removed (renames and holding-company CIKs). That leaves 8,719 stocks, 3,559 of them
    delisted since.
  * Stocks eligible ($5 close, $5M median daily dollar volume) on at least one day: 2,570 to 3,543 a year.
* **Who it misses.** Foreign private issuers and ADRs (they do not file Forms 3/4/5); ETFs; issuers with no insider
  filing since 2015; stocks whose insiders always wrote a wrong symbol; listings after 2026-09-30, where the map
  ends. In about 1% of the delisted-since trades, the company is still trading under another series (88 of 3,559
  ended series), because Alpaca breaks an entity's history at some corporate actions (Neogen above). Survivors
  fail too, so this changes nothing.
* **Bars.** Alpaca `feed=sip` daily bars, raw and `adjustment=all`, 2016-01-04 to 2026-11-11.
  * Fetched with `asof` set to each pair's median insider-filing date. That is 2,626 request groups, in 45 minutes
    at the pre-registered 3 threads and 0.32 s spacing.
  * SPY comes from the same source (the session calendar and the benchmark). VIX comes from yfinance `^VIX`.
* **Execution.** The app's own `triple_barrier.label_events` and `strategy_discovery._executable`, imported from
  the repo, with a flat 25 (and 11) bps a side.

## Choices made where the brief or the spec left room

1. **Breakout rule.** The step-down survivors at 11 bps include four settings of `94d4afa886` (`at_high_50`, exit
   below the 50-day mean, 10% stop, 40-session time stop): none, `adx_not_downtrend`, `spy_sma10` and `spy_sma50`.
   All four were tested, and the t bar was raised to 2.50 (Bonferroni over four). The other two survivors are
   earnings drift, which was closed on 2026-11-11.
2. **Breakout verdict sample.** 800, as for every other single-stock candidate. It has no registered sample.
3. **SPY over the same hold.** From SPY's open on the entry day to its open on the exit day for exits at an open
   (indicator, time stop, gap through the stop). For exits inside the bar (stop, last close of a delisted stock),
   to SPY's close that day.
4. **End of data.** The fetch ran during the 2026-11-12 session, whose bar was still forming. Everything is cut at
   2026-11-11. This is written in the script, committed before the evaluation ran.
5. **Prices.** Eligibility uses the raw close and raw volume. Rules, stops and returns use the fully adjusted bars.
6. **Identity and duplicates.** As pre-registered. The 0.7–1.4 band on insider trade price ÷ raw close is copied
   from the earnings test. A stock is a duplicate when it shares raw close and volume on 3 or more sampled sessions.
7. **Added after the run, reported only.** The subsets in `subsets.md`: the bot's 29 single stocks (a replication
   check) and the top 500 each year. They were not pre-registered and decide nothing.

## Reproduce

From the repo root, with the repo's venv (`.venv`, Python 3.11). The `inputs` stage needs any Python with
pyarrow. Scratch data lives outside the repo, in `~/.paper-training/dip-rules-full-market-20261112/` (bars 390 MB).

```
S="research_notes/Dip rules full market 20261112/dip_rules_check.py"; W=~/.paper-training/dip-rules-full-market-20261112
<python with pyarrow> -I "$S" inputs $W ~/.paper-training/earnings-drift-delisted-20261111
.venv/bin/python -I "$S" series $W
ALPACA_KEY=... ALPACA_SECRET=... .venv/bin/python -I "$S" fetch $W
.venv/bin/python -I "$S" vix $W
.venv/bin/python -I "$S" evaluate $W --workers 8
.venv/bin/python -I "$S" tables $W "research_notes/Dip rules full market 20261112"
.venv/bin/python -I "$S" subsets $W "research_notes/Dip rules full market 20261112"
```

Input hashes (SHA-256) of the run:

* `series.csv`: `1c162a44…03f4a`
* `spy.pkl.gz`: `eee54f55…cda9e4`
* `vix.csv`: `f3a6cd88…8f571c`
* `ticker_map.csv.gz`: `ef459320…6b7696`
* `insider_prices.csv.gz`: `4ab0f63b…b19773`
* full trades: `aa27f8b9…b6f468`

Files in the research folder:

* `results.json`: every statistic.
* `tables.md`: every table.
* `subsets.md` and `subsets.json`.
* `trades_25bps.csv.gz`: every trade at 25 bps a side, 490,251 rows.
* `identity.csv.gz`: each series' identity status.
* `fetch.out.txt` and `evaluate.out.txt`: the run logs.

## Limits

* One decade, 2016–2026, mostly a rising market. Candidate 2 cannot fire before about October 2016, because the
  200-day mean needs 200 bars and SIP bars start on 2016-01-04.
* No delisting returns. Last close only.
* A flat cost. A higher cost only makes these results worse. A much lower one would not rescue them. Before
  costs, the two dip rules beat SPY over the same holds by only +9 and +14 bps a trade, less than any round trip
  the bot could pay. The breakout loses to SPY even before costs (−49 bps).
