# Local full-history walk-forward sweep: 2026-11-10

**Verdict.** Nothing survives. 3,104 trials (350 library rules × 10 gate settings, 54 opening-gap rules, and the
bot's 9 hand-written strategies × 10 gates) were replayed on 26 years of free daily bars for the 45 paper symbols and
judged on 24 six-month windows from 2015-01 to 2026-11, net of 11 and 25 bps a side. At 25 bps, White's reality check
over all trials gives p = 0.087 and the step-down version names no survivor; at 11 bps it names six, and every one of
them is a post-earnings-drift or 50-day-breakout rule on today's megacaps whose excess over simply holding the same
stock is about zero (t between −0.8 and +1.0): the survivorship trap already documented on 2026-11-02, not an edge.
The only rules that beat their symbol's own drift after a Bonferroni correction are four index-ETF rules with 13–32
trades, which is a handful of 2020 crash days, not a strategy. Among the 2,522 rules with at least 100 trades, the
count whose excess over drift reaches t ≥ 2 at 25 bps is 6, where chance alone would give 57. The bot's own strategies all lose or break even at 25
bps (RSI(2) −13, IBS 0, Bollinger +21, channel breakout −5, trend pullback −11, MA crossover −14, MACD +1, ensemble −6
bps per trade); the "proof" they failed in paper is consistent with history. The one theme the sweep keeps finding is
*dip buying when the VIX is at or above 20* (86% of rules improve, median +25 bps), but that gate was chosen with
hindsight across 3,000 trials, it keeps only 41% of trades, and for the bot's IBS rule it loses money before 2015 at
25 bps. A 6-month lookback overstates badly: the fifteen best rules of the last six months average +283 bps there and
+82 bps over the full walk-forward, the correlation between a rule's last-6-month and 2015→2026 expectancy is −0.23,
and picking the best rule per family on a 6-month fit promises +153 bps and delivers +25. Six candidates are worth
pre-registering as forward tests (below), none as a claim. To go further the data has to change, not the search:
point-in-time index membership (to test PEAD and breakouts without survivorship), a dated earnings calendar for
delisted names, and the bot's own measured round-trip cost on the symbols it would actually trade one share of.

## 1. Method

* **Data.** yfinance split/dividend-adjusted daily OHLCV for the 45 symbols in `settings.paper_execution_symbols`,
  2000-01-03 → 2026-11-09 (6,755 sessions), cached locally; `^VIX` closes for the VIX gate; yfinance earnings history
  (56–99 reports per stock, from 2002) for the earnings flag; FOMC statement days from the Fed's historical pages
  (2000–2027, 225 dates incl. the two unscheduled March 2020 cuts); CPI and jobs-report release dates from the BLS
  archive filenames (2000–2026, 322 each). The deployed host was never touched; no paid data.
  Symbols starting after 2000: IWM and SMH (mid-2000), NFLX 2002, CRM and GOOGL 2004, MA 2006, V 2008, AVGO 2009,
  TSLA 2010, META 2012, ABBV 2013, XLRE 2015, XLC 2018. No symbol has a gap longer than 7 days. The earnings history
  is survivor-biased (only today's names) and shorter for the newer ones (ABBV, META, TSLA: 56–58 reports).
* **Rules.** The discovery library (`strategy_discovery.candidate_library`, 362 candidates) minus the 12 news-tone
  candidates, which cannot fire without a news history: 296 close-timed rules and 54 opening-gap rules over index ETFs,
  sector ETFs and single stocks. Plus the bot's hand-written strategies on the whole 45-symbol universe, re-expressed
  as vectorised signals with the exits each is seeded with: RSI(2) pullback and IBS (10% stop, no target, 10-session
  time stop, indicator exit), and RSI(14), Bollinger, channel breakout, trend pullback, MA crossover, MACD and the
  ensemble with the 3% / 6% / 20-session bracket they were scored with, including the backtester's confidence ≥ 0.55
  entry gate. The model-predictive strategy refits a random forest per bar and was not replayed.
* **Gates.** Each close-timed rule was run ungated and under nine gates: ADX(14) regime `range`, `not_trending`,
  `not_downtrend`, `trend_up` (`strategies/regime.py`); SPY above its 10-, 20- and 50-day SMA (`market_gate`); VIX
  < 20 and VIX ≥ 20 on the decision bar (`strategies/vix_gate.py`, fails open when the VIX is missing).
* **Execution.** The app's own labeler (`triple_barrier.label_events` / `label_open_session`): decision on the close,
  fill at the next open, stop first inside a bar, gaps fill at the open, indicator exit at the next open, one
  position per symbol, with a flat 11 or 25 bps per side instead of the volatility-scaled model. Returns are per
  trade in bps, so the one-share minimum does not change them; the tables also give the mean net dollars per trade
  at one share (adjusted price), which is what the paper account would actually book.
* **Walk-forward.** Everything before 2015-01-01 is the fit window (anchored, expanding); the test period is 24
  six-month windows 2015H1 → 2026H2 (the last one is 2026-07-01 → 2026-11-09). None of these rules has fitted
  parameters, so "fit" means selection: at each window start the best variant by in-sample mean net return is
  chosen on the expanding window (≥ 30 trades) or on the trailing six months only (≥ 10 trades), and judged on the
  next window. Per rule × symbol × window the sweep records trades, net return, max drawdown, profit factor and
  whether the window was positive (`reports/local-sweep-20261110/per_symbol.csv.gz` is the rule × symbol level; the
  window level, 1.1 M rows, stayed in the scratch output).
* **Statistics.** Mean net bps with month-clustered t; excess over the symbol's own *causal* drift (mean daily return
  up to the decision bar × holding days), the check that catches "a stock that went up"; max drawdown of the
  equal-weight daily mark-to-market portfolio; share of positive windows. Multiple comparisons: White's reality check
  (stationary bootstrap, 1,000 draws, mean block 20 days) over the 2015→2026 daily portfolio returns of all 3,086
  trials with at least one OOS trade, its Romano-Wolf style step-down, Bonferroni and Benjamini-Hochberg on
  per-trial bootstrap p-values, and Bonferroni on the clustered t of net and excess returns (z = 4.16 for 3,086
  trials).
* **Code.** `artifacts/api-server/backend/scripts/local_sweep.py` (sweep, 6,208 evaluations in 6.5 minutes on 8
  workers, ~2 GB) and `scripts/local_sweep_report.py` (corrections and tables). Full tables:
  `reports/local-sweep-20261110/tables.md`; per-trial summary: `candidates.csv.gz`; calendars used for the event
  check are in the same folder.

## 2. Trial count and corrections

| | 11 bps | 25 bps |
|---|---|---|
| Trials configured / with ≥1 OOS trade | 3,104 / 3,086 | same |
| Trials with positive net expectancy | 70% | 37% |
| Trials with positive excess over own drift | 27% | 10% |
| Median net bps per trade (gross 38.7) | +16.7 | −11.6 |
| Reality check p-value (best trial vs cash) | 0.038 | 0.087 |
| Step-down survivors | 6 (all PEAD / 50-day breakout on megacaps) | 0 |
| Bonferroni on bootstrap p | 82 | 16 (11 PEAD, 5 breakout) |
| Benjamini-Hochberg 5% FDR discoveries | 193 | 16 |
| Bonferroni on clustered t, net | 41 | 6 |
| Bonferroni on clustered t, **excess over drift** | 5 | 2 |
| Trials (≥100 trades) with excess t ≥ 2.0, vs 57 expected by chance | 48 | 6 |
| Trials (≥100 trades) with excess t ≥ 2.5, vs 16 expected by chance | 23 | 0 |

Every survivor of the net-return corrections is one of two things. (a) Post-earnings drift and new-high breakouts on
single stocks: +180 to +330 bps per trade and 83–88% positive windows, but the excess over simply holding the same
stock on random days is −80 to +37 bps (t −2.2 to +1.0). These are the 29 megacaps that survived to be in today's
list; the 2026-11-02 report reached the same conclusion with the library's own gate. (b) Index-ETF rules with 13–32
trades (a 5-day drop in an uptrend or a 2% gap down, with a SPY-trend gate): t of 5–8 on a dozen trades, most of them
March 2020. The two excess survivors at 25 bps are of kind (b).

## 3. What the bot trades today, on history

Hand-written strategies on the 45-symbol universe, 2015 → 2026 (the last-6-month column is 2026-05-11 → 2026-11-09):

| Strategy | Gate | Trades | Net bps @11 | @25 | PF @11 | Max DD | Windows + | Excess bps | t @11 | t @25 | $/trade, 1 share | Last 6m trades / bps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RSI(2) pullback | none | 4,120 | +16.0 | −13.4 | 1.16 | 11.8% | 62% | −9.2 | 1.29 | −1.05 | 0.17 | 191 / +18 |
| IBS | none | 10,088 | +28.7 | +0.3 | 1.29 | 11.1% | 71% | +4.8 | 3.46 | 0.03 | 0.35 | 423 / +3 |
| IBS | VIX ≥ 20 | 4,368 | +71.7 | +42.9 | 1.70 | 10.0% | 90% | +50.0 | 4.67 | 2.80 | 0.89 | 57 / +72 |
| RSI(14) reversion | none | 3,872 | +39.9 | +12.3 | 1.22 | 10.5% | 83% | +2.1 | 2.06 | 0.65 | 0.59 | 159 / +48 |
| Bollinger reversion | none | 2,817 | +50.0 | +21.2 | 1.28 | 8.3% | 83% | +12.0 | 2.28 | 1.00 | 0.63 | 84 / +37 |
| Channel breakout | none | 3,018 | +24.9 | −4.8 | 1.14 | 6.6% | 67% | −36.1 | 1.92 | −0.38 | 0.25 | 124 / −55 |
| Trend pullback | none | 4,935 | +17.0 | −10.9 | 1.09 | 12.1% | 58% | −36.9 | 1.22 | −0.80 | 0.05 | 239 / −1 |
| MA crossover | none | 9,410 | +16.6 | −13.9 | 1.09 | 19.6% | 67% | −35.6 | 1.44 | −1.23 | −0.01 | 505 / −46 |
| MACD momentum | none | 7,283 | +30.4 | +0.9 | 1.16 | 13.3% | 67% | −18.3 | 2.28 | 0.07 | 0.23 | 357 / −37 |
| Ensemble | none | 5,076 | +22.8 | −5.5 | 1.12 | 12.3% | 67% | −25.7 | 1.68 | −0.41 | −0.04 | 295 / −46 |

* At 11 bps every one is positive but no t clears 2 except IBS and the two slow reversions, and none clears the
  Bonferroni bar. At 25 bps, which is where the cost calibration put the bot's own fills, six of nine lose.
* Every trend/momentum strategy has a clearly negative excess over drift (−18 to −37 bps): on this universe they are
  worse than holding.
* Gates that look good on one strategy were found by searching nine gates × nine strategies. The best gate per
  strategy is listed in `tables.md`; only IBS with VIX ≥ 20 is discussed below.
* IBS with VIX ≥ 20 is the strongest paper-executable result in the whole sweep (t 4.67 @11, 2.80 @25, 90% positive
  windows, excess +50 bps). Three facts argue against promoting it as is: in 2000–2014 it makes +20.6 bps at 11 and
  **−7.5 bps at 25** (6,150 trades), so the edge is a post-2015 phenomenon at realistic cost; the excess-over-drift t
  (3.2 @11, 1.4 @25) does not survive the correction; and it keeps 43% of IBS trades (57 in the last six months), so
  at one share it books about $0.89 per trade. The 2026-11-06 VIX-gate report measured the same thing on the index
  ETFs and declined it on total profit. Ex-2020 the numbers barely move (+67.7 / +38.6 bps, 89% / 84% windows), so it
  is not just the crash.

## 4. Families and gates

Median net bps per trade across each family's trials (11 / 25 bps), share of trials positive, share with positive
excess over drift, and the best clustered t:

| Family | Trials | Median @11 | @25 | Positive @11 | @25 | Excess > 0 | Best t |
|---|---|---|---|---|---|---|---|
| earnings_drift (single stocks) | 240 | +177 | +151 | 100% | 100% | 15% | 5.06 |
| breakout_momentum | 360 | +67 | +38 | 98% | 86% | 11% | 4.81 |
| weekly_reversal | 342 | +34 | +5 | 91% | 57% | 71% | 5.90 |
| bollinger_low | 240 | +23 | −6 | 79% | 42% | 41% | 3.22 |
| sector_reversal | 80 | +22 | −7 | 99% | 32% | 8% | 3.06 |
| down_streak | 360 | +12 | −16 | 67% | 24% | 42% | 4.71 |
| calm_market_dip | 120 | +10 | −18 | 72% | 17% | 36% | 4.81 |
| turn_of_month | 360 | +8 | −21 | 62% | 20% | 16% | 3.63 |
| pullback_in_uptrend | 240 | +3 | −25 | 56% | 12% | 21% | 3.02 |
| n_day_low | 360 | 0 | −28 | 50% | 7% | 15% | 2.73 |
| gap_down_recovery | 240 | −16 | −44 | 20% | 6% | 11% | 7.83 |
| opening_gap_fill (open-timed) | 54 | −24 | −52 | 0% | 0% | 0% | −1.33 |

Weekly reversal (a 3–8% five-day drop, exit on the first up close or above the 5-day SMA) is the only family where
most trials also beat their symbol's drift. The opening-gap fill loses in every one of its 54 variants, as on
2026-11-02.

Gates, paired against the ungated rule (11 bps, 297–305 close-timed rules each):

| Gate | Median Δ bps | Rules improved | Trades kept | Median Δ max DD | t improved |
|---|---|---|---|---|---|
| VIX ≥ 20 | **+25.1** | **86%** | 41% | −1.7 pts | 67% |
| SPY > SMA50 | −2.6 | 42% | 66% | −2.3 pts | 40% |
| ADX not downtrend | −1.1 | 34% | 85% | −1.4 pts | 31% |
| ADX trend up | −0.9 | 49% | 18% | −6.0 pts | 31% |
| ADX not trending | −3.2 | 38% | 67% | −2.8 pts | 36% |
| ADX range | −6.0 | 35% | 46% | −4.2 pts | 34% |
| SPY > SMA20 | −6.7 | 29% | 50% | −3.1 pts | 26% |
| SPY > SMA10 | −7.8 | 31% | 40% | −3.3 pts | 30% |
| VIX < 20 | −11.1 | 18% | 68% | −1.4 pts | 16% |

Every gate cuts drawdown because it cuts exposure. Only "buy dips when fear is elevated" raises expectancy for most
rules; its mirror image (VIX < 20) is the worst gate in the sweep. The ADX and SPY-trend gates adopted on 2026-11-06
do not generalise beyond the strategies they were fitted on.

## 5. Walk-forward selection, and how much a 6-month lookback overstates

Picking the best in-sample variant at each window start and trading it the next six months (11 bps):

| Fit window | Pool | In-sample bps promised | Next-window bps realised | OOS trades | Windows + |
|---|---|---|---|---|---|
| Anchored (2000 → window) | all 3,050 close-timed trials | +496 | +197 | 92 | 61% |
| Anchored | best variant per family × universe (38 pools) | +83 | +27 | 32,124 | 63% |
| Trailing 6 months | all close-timed trials | +715 | +161 | 328 | 62% |
| Trailing 6 months | per family × universe | +153 | +25 | 41,877 | 63% |

The global pick is always a rare PEAD rule (1–5 trades a window), so its +197 bps is 92 trades in 12 years. The per
pool rows are the honest number: selection on the expanding window overstates by 3×, selection on six months by 6×,
and the realised +25 bps is before the 14 bps of extra cost the 25 bps case adds.

What a 6-month lookback would pick today (top 15 by net bps over 2026-05-11 → 2026-11-09, ≥ 10 trades) against the
same rule's full record:

| | Last 6 months | Walk-forward 2015 → 2026 |
|---|---|---|
| Mean net bps of the top 15 | +283 | +82 |
| Correlation of last-6-month and full-period expectancy, 2,004 trials | −0.23 | |
| Trials whose sign agrees between the two | 61% | |
| Median ratio of absolute last-6-month to absolute full-period expectancy | 2.1× | |

Thirteen of those fifteen are single-stock rules under a VIX or ADX gate with 10–43 trades in the window; their
2000–2014 record is −27 to +50 bps. The one exception is the PEAD rule, which is survivorship.

## 6. Event check

Per trade the sweep records the next-day return, whether a yfinance earnings report fell inside the holding window
(survivor-biased calendar) and whether an FOMC statement, CPI or jobs release did. Pooled over the OOS trades of all
ungated close-timed trials (11 bps):

| Event inside the realised holding window | Trades with | Net bps with | Trades without | Net bps without | Trials where "with" beats "without" |
|---|---|---|---|---|---|
| Earnings | 23,571 | +101 | 392,709 | +19 | 44% |
| FOMC | 80,289 | +86 | 335,991 | +9 | 43% |
| CPI | 90,236 | +83 | 326,044 | +8 | 36% |
| Jobs | 133,437 | +53 | 282,843 | +10 | 30% |
| Any macro | 210,639 | +35 | 205,641 | +13 | 28% |

The pooled "with" columns are dominated by the 20–40-session PEAD and breakout rules, which hold through everything
and are the survivorship trials; across rules, holding through a macro day is better for only 28–44% of them. The
flag that is actually knowable at the entry close is "an event in the next H sessions" (H = the rule's time stop,
since all three calendars are published in advance). As a filter:

| Rule (11 bps) | Trades | Net bps | Skip if earnings ahead: trades / bps | Skip if macro ahead: trades / bps | Only if macro ahead: trades / bps |
|---|---|---|---|---|---|
| IBS, VIX ≥ 20 | 4,368 | +71.7 | 3,986 / +69.2 | 975 / +65.6 | 3,393 / +73.4 |
| IBS | 10,088 | +28.7 | 9,147 / +25.5 | 2,060 / +11.3 | 8,028 / +33.1 |
| Bollinger reversion | 2,817 | +50.0 | 2,279 / +41.8 | 6 / −4.0 | 2,811 / +50.1 |
| RSI(14) reversion | 3,872 | +39.9 | 3,127 / +38.3 | 4 / +20.5 | 3,868 / +39.9 |
| RSI(2) pullback | 4,120 | +16.0 | 3,719 / +16.4 | 770 / −10.3 | 3,350 / +22.0 |
| Channel breakout | 3,018 | +24.9 | 2,336 / +20.7 | 10 / −28.1 | 3,008 / +25.1 |
| 4-day down streak, VIX ≥ 20 | 940 | +97.6 | 810 / +88.4 | 115 / +22.2 | 825 / +108.1 |
| Sector-ETF 5-day drop, SPY > SMA50 | 142 | +109.5 | 142 / +109.5 | 30 / +163.7 | 112 / +95.0 |

With three macro releases a month, "no macro day in the next 10–20 sessions" removes 80–100% of trades, so it is not
a usable filter, and the few trades it keeps are not better. Skipping entries with an earnings report ahead removes
10–20% of trades and moves expectancy by a few bps in either direction. **Neither excluding nor targeting event days
changes any verdict.** The next-day return after entry averages +5 to +15 bps for the reversion rules and is not a
useful discriminator on its own.

## 7. Candidates worth pre-registering (forward tests, not claims)

Selection bar: close-timed, ≥ 100 OOS trades, positive in ≥ 60% of windows, positive excess over drift, t ≥ 2 at 11
and ≥ 1.5 at 25 bps, **and** positive at 25 bps in 2000–2014, ex-2020 and since 2022. 92 trials pass the first five
conditions (`summary.json`, `shortlist`); the last three leave these:

| # | Rule | Universe | Trades | Net bps @11 / @25 | Windows + | Excess bps | 2000–14 @25 | Ex-2020 @25 | Since 2022 @25 | $/trade, 1 sh |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `c8e129d06c` close > SMA200 and 5-day return < −5%; exit close > SMA5 (already *explore* since 2026-11-02) | sector ETFs | 201 | +91 / +60 | 74% / 65% | +79 | +30 | +60 | +49 | 1.45 |
| 2 | `1cd460d679` close > SMA200 and 5-day return < −8%; exit first up close | single stocks | 537 | +80 / +48 | 79% / 62% | +61 | +49 | +41 | +28 | 1.31 |
| 3 | `213239cdf2` 5-day return < −5%; exit close > SMA5; SPY > SMA50 gate | sector ETFs | 142 | +110 / +77 | 71% | +98 | +18 | +51 | +70 | 1.21 |
| 4 | `4ec6c68526` calm-market RSI(2) < 10 dip (the other *explore* candidate), VIX ≥ 20 gate | index ETFs | 105 | +81 / +53 | 78% | +70 | +31 | +50 | +35 | 2.87 |
| 5 | `7df4090f31` 4 consecutive down closes; exit close > SMA5; VIX ≥ 20 gate | single stocks | 940 | +98 / +69 | 90% | +71 | +18 | +68 | +48 | 1.22 |
| 6 | IBS < 0.1 (the bot's rule) with a VIX ≥ 20 gate | all 45 | 4,368 | +72 / +43 | 90% / 86% | +50 | **−8** | +39 | +14 | 0.89 |

Notes for the registration:

* 1–3 are one family (weekly reversal) and will be correlated; register them as one hypothesis with three variants,
  and count them as three trials. 1 and 4 are the two 2026-11-02 explore candidates; this sweep confirms 1 and says
  4 only pays when the VIX is elevated (ungated it is −3.7 bps at 25 bps).
* 4–6 all depend on the VIX ≥ 20 gate, which is the single most-selected parameter in the sweep; a forward test is
  the only way to find out whether it is a regime (2015–2026 had more fear spikes than 2004–2014) or a fact.
* 6 fails the pre-2015 condition at 25 bps and is included only because it is the bot's own active rule: the
  cheapest test is to tag IBS entries by VIX regime in paper and compare, which costs nothing.
* At one share, every candidate books $1–3 per trade. The passive VOO/SGOV arm's daily move is larger. Trade counts
  are 12–80 a year per candidate (6: about 400), so a forward verdict at 25 bps needs one to three years.
* None of these is a claim: after the correction for 3,086 trials, the best excess-over-drift t among them is 4.0
  (candidate 3, z-bar 4.16) and the others are 2.5–3.3. Among the 2,522 close-timed trials with ≥ 100 trades, the
  number whose excess-over-drift t reaches 2.0 is 48 at 11 bps and **6 at 25 bps**, against 57 expected from pure
  chance if the trials were independent; at t ≥ 2.5 it is 23 vs 16 expected at 11 bps and **0 at 25 bps**. At the
  bot's measured cost the sweep finds fewer apparent edges than noise would produce.

## 8. What would be needed to go further

* **Point-in-time membership** for the single-stock universe. PEAD and breakouts are the only families whose net
  returns survive every correction, and the only reason they are rejected is that the universe is today's winners.
  Index constituent histories (S&P 500 additions and deletions) are available free from Wikipedia change logs and
  from the SEC's EDGAR filings but need to be assembled; `point_in_time_universe` already supports a membership mask.
* **A dated earnings calendar for delisted and small names** to go with it; yfinance only has today's tickers.
* **The bot's own round-trip cost by symbol class** at one share (`paper_cost_calibration`): the verdict flips
  between 11 and 25 bps for most families, and the 5 / 10 / 25 bps brackets of the yardstick are what should be
  applied to any forward test.
* **Longer VIX regimes.** The VIX ≥ 20 finding rests on 2015–2026; the 1990–1999 VIX history is free and the
  SPY/sector ETFs exist from 1993/1998, so the gate can be checked on one more regime without new data.
* **Not more rules.** 350 rules × 10 gates on 45 symbols found nothing that a 6-month lookback would not have
  flattered by 3–6×; the search itself is the problem, and every further trial raises the bar.
