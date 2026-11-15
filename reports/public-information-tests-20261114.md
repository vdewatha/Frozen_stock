# Three tests of public information: company filings, politicians' trades, news (2026-11-14)

Pre-registration: `research_notes/Public information tests 20261114/PREREGISTRATION.md` (commit `dec380b`, made
before any event was joined to a price). Scripts and result files: the same folder. Local research only:
nothing was sent to the deployed app and no account or order endpoint was touched. The one change to app code
that came out of it (the news reader, Test 3) is a separate pull request.

**The question.** Can the bot earn money after costs by reacting to information everyone can see: company
filings, politicians' disclosed trades, news?

## Verdict

**No, not with daily prices and an entry the next morning. All three fail.**

| Test | What was bought | Trades | After costs, a trade | Against SPY, a trade | Verdict |
|---|---|---|---|---|---|
| 1. Company event filings (Form 8-K) | Stocks after each of 25 filing types, 3 signals each, 20 sessions | 116,654 across the 25 "any filing" signals (2016-2021) | +0.34% to +1.24% for the six largest types | −0.58% to −1.02% for the six largest types | **Fail.** None of 75 signals selected; held-out period not opened |
| 2. Politicians' disclosed purchases | Stocks a House member disclosed buying, 60 sessions | 7,391 (2016-2026) | +2.92% (t 3.39) | **−0.91%** (t −2.26) | **Fail** on 3 of 4 criteria |
| 3. News | Stocks picked by the app's twelve news rules, after its news reader was audited and improved | 4,659 to 51,710 a rule (2016-2026) | −0.51% to +0.22% | −0.40% to −0.91% (t −4.6 to −19.8) | **Fail.** All twelve rules, with the old reader and the new |

All three use about 8,700 US stocks from 2016 to 2026 with delisted stocks included, a price of $5 or more and
$5 million a day traded, entry at the next session's open, and costs of 25 bps a side.

**One fact explains most of the first two results.** Over these years SPY, which is weighted towards the
largest companies, beat the average stock. Any eligible stock bought on any day and held 60 sessions made
+2.42% after costs and lagged SPY by 1.19% (t −2.91); held 20 sessions, +0.65% and −0.59%. A rule has to beat
that drag before it beats SPY. Stocks politicians bought (−0.91% against SPY) and stocks with a fresh filing
(−0.58% to −1.02%) are, to within noise, just average stocks.

**The news result has a different cause.** The news is right about the day: a stock with a story the reader
calls positive moved +1.81% against SPY on the session the story first traded, a stock with a negative story
−2.79%. By the next morning's open, the earliest the rules can buy, nothing is left.

---

## Test 1: company event filings (Form 8-K)

**What was tested.** Companies must file a Form 8-K within four business days of a material event, and each
filing lists numbered items for the kind of event. For each of 25 item types the test bought the stock at the
open of the second session after the filing became public and sold at the close 20 sessions later, three
ways: after any filing with that item, only after the stock rose 5% or more against SPY on the news, and
only after it fell 5% or more. Filings that announce earnings (Item 2.02) were left out; that idea was tested
and closed in PR #92.

**How selection worked.** 75 signals is a lot of chances for luck, so the data were split. A signal had to
show itself in 2016-2021 first (300 or more trades, a profit after costs, and a mean excess over SPY with a t
of 3.2 or more, which is the 5% level across 75 tries). Only a signal that cleared that bar would have been
judged on 2022-2026.

**Result: no signal cleared the bar, so the test ended there.**

* 685,204 filings since 2016 from 10,239 issuers; 188,173 are earnings announcements and were left out;
  186,225 are events on a stock that was priced, at $5 or more and liquid on the day.
* Only 26 of the 75 signals had 300 or more trades in 2016-2021. Three of those were ahead of SPY at all, none
  by more than noise:

| Signal | Trades | After costs | Against SPY | t |
|---|---|---|---|---|
| Delisting notice or listing-rule failure (3.01), any | 308 | +2.23% | +0.36% | +0.28 |
| New debt obligation (2.03), after a 5% fall | 309 | +4.80% | +0.35% | +0.23 |
| Shareholder vote results (5.07), after a 5% rise | 441 | +2.93% | +0.16% | +0.08 |

* The eleven largest signals, 2016-2021 (after 25 bps a side):

| Filing type (item), any filing | Trades | After costs | Against SPY | t |
|---|---|---|---|---|
| Other events (8.01) | 23,122 | +0.67% | −0.85% | −2.71 |
| Director or officer changes (5.02) | 23,065 | +0.34% | −0.95% | −2.81 |
| Regulation FD disclosure (7.01) | 20,599 | +0.65% | −0.83% | −2.31 |
| Material agreement signed (1.01) | 15,723 | +0.43% | −1.02% | −2.88 |
| Shareholder vote results (5.07) | 11,979 | +1.24% | −0.58% | −1.21 |
| New debt obligation (2.03) | 8,474 | +0.90% | −0.80% | −2.75 |
| Bylaw amendments (5.03) | 4,181 | +0.89% | −0.74% | −2.19 |
| Completed acquisition or disposal (2.01) | 2,280 | −0.10% | −1.48% | −3.05 |
| Unregistered share sales (3.02) | 1,977 | −0.65% | −2.08% | −2.35 |
| Material agreement ended (1.02) | 1,759 | +0.21% | −1.23% | −3.18 |
| Changes to shareholder rights (3.03) | 1,141 | +0.86% | −0.82% | −1.09 |

* **Buying after a filing did worse than buying at random.** Any eligible stock on any day in those years
  lagged SPY by 0.35% over 20 sessions after the same costs (t −0.79); the big filing types lagged by 0.6% to
  1.0%. (This comparison was added after the result, to explain it; it is not part of the rule.)
* **No avoid list either.** The mirror test (a filing type after which stocks reliably lag, which could have
  become a BUY blackout) needed a t of −3.2 or less with 300 or more trades. The closest were "material
  agreement ended" (t −3.18) and "completed acquisition or disposal" (t −3.05). "Unregistered share sales after
  a 5% fall" lost 7.0% against SPY (t −3.92) but on 182 trades, under the 300 the rule required.

**Limits.** 49 of the 75 signals had too few trades to be judged in 2016-2021 (the price and liquidity floors
remove about half of all filings, and a 5% reaction is rare); for those the answer is "not testable here", not
"no effect". Item 1.05 (cybersecurity incidents) only exists since December 2023. The filing's text and size
were not read: the test asks whether the *type* of event is enough, which is all a simple rule could use.

---

## Test 2: politicians' disclosed purchases

**What was tested.** Members of the House must report stock trades within 45 days. For every report in the
House Clerk's public index from 2016 to 2026, the test read the transactions, and for each **purchase of a
stock** bought at the open of the first session after the report's filing date (the day it became public, not
the day of the trade) and sold at the close of the 60th session held.

* 7,010 reports in the index; 5,317 filed electronically and readable, 1,693 scanned paper reports left out.
* 51,198 transaction rows read; 41,509 are stocks with a ticker; 20,316 of those are purchases.
* 15,786 purchases map to a stock in the price data that was $5 or more and liquid; 3,632 do not map (funds,
  foreign listings and tickers outside the universe), 869 were too small or illiquid.
* Purchases of one stock disclosed for the same day are one event (12,584 events); one position per stock at a
  time leaves **7,391 trades in 1,570 stocks**.
* The reports arrive late: the median filing is 28 days after the trade, and 17% arrive after the 45-day limit.

**Result: fail on three of the four criteria.**

| Criterion | Needed | Measured | |
|---|---|---|---|
| 1. Profit after costs | mean above zero, t ≥ 2.0 | **+2.92%** a trade, t 3.39 | pass |
| 2. Beats SPY | mean excess above zero, t ≥ 2.0 | **−0.91%** a trade, t −2.26 | fail |
| 3. Most years | ahead of SPY in more than half of 11 years | **2 of 11** (2020 and 2022) | fail |
| 4. Not a few big winners | profit and excess above zero without the top 1% | +2.03% and **−1.76%** | fail |

Win rate 56.3%; median trade +2.27%; 9.1% of trades are in stocks since delisted. Against QQQ the purchases
lag by 2.33% a trade (t −3.72).

**The trades make money because the market went up, not because politicians pick well.** A random eligible
stock held 60 sessions made +2.42% and lagged SPY by 1.19%; the disclosed purchases made +2.92% and lagged by
0.91%. The 0.3-point difference is far inside the noise.

| Reported, not decisive | Trades | After costs | Against SPY | t |
|---|---|---|---|---|
| Costs of 12.5 bps a side | 7,391 | +3.18% | −0.65% | −1.61 |
| Held 20 sessions | 9,833 | +0.58% | −0.57% | −3.10 |
| Held 120 sessions | 5,611 | +6.14% | −1.85% | −3.30 |
| Entered one session later | 7,332 | +2.92% | −0.96% | −2.53 |
| Before the first copy-trading fund (to 2023-02-06) | 4,819 | +2.46% | −0.65% | −1.31 |
| Since the first copy-trading fund | 2,596 | +3.75% | −1.43% | −2.11 |
| Reported within 15 days of the trade | 2,101 | +3.85% | +0.16% | +0.26 |
| Reported later than 15 days | 6,349 | +2.90% | −1.09% | −2.64 |
| Purchases of $50,001 or more | 590 | +4.81% | +0.72% | +1.14 |
| Several members bought the same stock the same day | 130 | +6.91% | +0.80% | +0.42 |
| **Sales** (the mirror: the stock after a disclosed sale) | 8,017 | +3.56% | −0.74% | −1.97 |
| Senate purchases, 2016 to March 2021 | 958 | +4.74% | −0.24% | −0.38 |
| Senate sales, 2016 to March 2021 | 1,055 | +4.14% | −1.03% | −2.09 |
| Both chambers' purchases, 2016 to March 2021 | 4,087 | +4.00% | −0.21% | −0.39 |

* Stocks members **sold** did about as well afterwards as stocks they bought (−0.74% against −0.91% against
  SPY). If the trades carried information, the two would differ.
* The three subsets that are ahead of SPY (quick reports, large purchases, several members at once) are small
  and none is statistically distinguishable from zero. They were not registered as rules; testing them now
  would be picking from results already seen.
* Most-bought stocks: Microsoft, Home Depot, Procter & Gamble, IBM, Johnson & Johnson, Apple, Amazon. This is
  a portfolio of large household names, bought a month after the fact.

**Limits.** Paper reports (24% of all) are not in the sample. The Senate's own site needs a click-through
agreement and was not used; the Senate figures come from a community mirror that stops in March 2021.
Options are left out, and some widely reported "politician trades" are option positions. A stock bought by
many members counts once. The law restricts some uses of these reports (5 U.S.C. 13107(c): no commercial use
other than by news media); this was a paper research test, and that limit would need a lawyer's reading before
anything traded real money on them.

---

## Test 3: news

**What was tested.** The bot reads Alpaca's news feed, gives each story a tone (good, bad or neutral for the
company) with a small rule-based reader, uses positive tone to choose up to 8 extra stocks to look at, and lets
trading rules use the tone. Three questions: how often does the reader get the tone right, can it be made
better, and do the app's twelve news rules make money on the whole market? (They failed on the bot's own 29
stocks on 2026-11-06, `reports/news-entries-20261106.md`.)

**The news.** Every story in Alpaca's archive from 2016-01-01 to 2026-11-11: 2,401,049 stories. 2,116,581 of
them name one to three companies, the only kind the app uses. 1,802,583 story-stock pairs land on one of 8,254
stocks in the price data; 529,034 name a ticker the price data does not hold (funds, foreign and
over-the-counter listings).

### 3a. How often the reader gets tone right

400 stories were drawn at random (200 from 2016-2025, 100 from each of the feed's two sources in 2026) and
labelled by hand as good news, bad news or neither for the named company, from the headline and summary alone,
**before the reader was run on any of them**: 90 good, 38 bad, 272 neither. The labels are one reader's
judgement (Claude's) under conventions written down before the sample was drawn (`LABELLING.md`); they are
published with the sample (`test3_news/sample.csv`, `labels.csv`).

Today's reader against those labels, all 400 stories:

| Measure | Result |
|---|---|
| Agrees with the label | 309 of 400 (77%) |
| Its "positive" calls that are labelled positive | 39 of 51 (76%) |
| Its "negative" calls that are labelled negative | 20 of 32 (63%) |
| Plainly good or bad stories it calls neutral | 67 of 128 (52%) |
| Stories it reads backwards | 2 of 128 |

It is almost never backwards. Its weakness is silence: it misses half of the stories that have a direction,
and one in three of its "negative" calls is on a story that is not bad news.

* **Missed, because no listed word appears:** "Vertex Reports Phase 3 Study Of KALYDECO Met Primary Endpoint",
  "Bank7 Increases Quarterly Cash Dividend From $0.27 To $0.30", "JFrog Board Authorized Up To $300M Share
  Repurchase Program", "Prime Medicine Says FDA Clears IND Application", "JA Solar Likely To Lose Money For The
  First Time In 16 Quarters".
* **Missed, because the headline is all numbers:** "Installed Building Products Reports Q2 Adj. EPS $0.59 vs
  $0.55 Est., Sales $282.2M vs $275.3M Est." Most earnings headlines before 2019 are written this way.
* **Wrong, because a word has two meanings:** "Copel Reaches Record Date" (read as a record), "LG Display
  Announced 1.1T Won Investment" (read as a win), "Taco Bell Launches 'Loss Or Celebration Outcome Support'"
  (read as a loss), "Cigna Healthcare Expands AI-Driven ... Programs" (read as growth).

### 3b. A fix, and whether it holds on stories it was not built on

The 400 stories were split in half when they were drawn. The fix was built on one half only (the design half)
and frozen in a commit (`5d2967b` on the app branch). Then both readers were scored, once, on the other half
(the check half).

| | Today's reader | Fixed reader |
|---|---|---|
| Design half, which the fix was built on: agrees with the label | 155 of 200 (77.5%) | 177 of 200 (88.5%) |
| **Check half, scored once: agrees with the label** | **154 of 200 (77.0%)** | **164 of 200 (82.0%)** |
| Check half: "positive" calls labelled positive | 22 of 28 | 31 of 39 |
| Check half: "negative" calls labelled negative | 9 of 14 | 9 of 11 |
| Check half: good or bad stories called neutral | 35 of 67 (52%) | 26 of 67 (39%) |
| Check half: read backwards | 1 | 1 |

On the check half the fixed reader is right on 13 stories where today's is wrong, and wrong on 3 where today's
is right (exact McNemar test, p = 0.02). The registered rule was "ship only if agreement on the check half is
higher". It is, so the fix ships as a separate app-code pull request.

Three limits, stated plainly:

* The gain on unseen stories (+5 points) is half the gain on the stories the fix was built on (+11).
* The fixed reader still calls 39% of good or bad stories neutral.
* It fixed the old backwards call and made a new one: "Dollar Tree Says Higher Fuel Prices Are Becoming a
  'Very, Very Meaningful' Headwind" scores positive on "beat" and "share repurchases" in its summary.

What the fix changes:

* Words with no direction by themselves no longer count: "profit", "loss", "risks", "expands". "Record date"
  and the Korean won are skipped.
* The shapes headlines use are read: "shares are trading higher", "shares rose", "Up From / Down From ... YoY",
  a dividend increase, a share repurchase, "secures" or "awarded" a contract, "FDA clears", a trial that "met
  its primary endpoint", "failed", "lose", "bullish", "bearish".
* Numbers are compared with their reference: "EPS $0.59 vs $0.55 Est." is a beat, "Sees FY Adj EPS
  $4.62-$4.80 vs $4.97 Est" is a miss, a range around the estimate is neither.
* The app's stored tone history is kept per reader version, so the new reader's scores are never mixed with
  the old one's.

### A finding outside the plan: the feed's second source often names the wrong company

Until April 2026 the feed was Benzinga alone. On 2026-04-29 a second source, `fiscal.ai`, appears, and it has
supplied more than half of all stories since (279,529 of the 504,676 stories of 2026). It covers companies
listed abroad and tags them with their home-market ticker, which in the US often belongs to an unrelated
company:

| The story is about | Tagged | In the US that ticker is |
|---|---|---|
| Costain (UK builder) | COST | Costco |
| Roche | ROP | Roper Technologies |
| Allianz | ALV | Autoliv |
| Tesco | TSCO | Tractor Supply |
| Siemens Energy | ENR | Energizer |
| Sirius Real Estate | SRE | Sempra |
| Metacon (Sweden) | META | Meta Platforms |

It also tags some analyst notes with the brokerage instead of the stock ("Piper Sandler Raises Price Target on
AbbVie", tagged PIPR).

Measured without any price (`news_names.py`): among the 2026 story-stock pairs that land on a US stock in the
price data, the story mentions neither the company's name nor its ticker in 11.7% of `fiscal.ai` pairs (19,014
of 162,464) and 12.2% of Benzinga pairs (21,926 of 179,593). The two rates are alike. What is behind them is
not. Reading 60 of each by hand (`test3_news/name_misses_*_read.csv`): 44 of the 60 `fiscal.ai` misses are
about a different company; 1 of the 60 Benzinga misses is (the others are the right company named another
way, or market and sector stories).

**So about 8.6% of the `fiscal.ai` stories that reach a US stock are about another company (95% range 7.3% to
9.9%), against about 0.2% for Benzinga.** That is a floor: a wrong story whose company shares a name word with
the US one passes the test.

The app has no guard against this today. Such a story counts towards the tone of the US stock, for the bot's
own 29 stocks and for the stocks news discovery admits. A guard (count a story for a stock only when it
mentions that company) is the proposed next change; it is not built here.

### 3c. The app's twelve news rules on the whole market

The twelve rules the app generates fall into three families: buy a stock in an uptrend after positive news;
buy an oversold stock after negative news; buy after a day whose news is all positive and whose price gapped
up more than 1% (the app calls this one "post-upgrade drift"). They were replayed with the app's own rule
engine on every stock with news, 2016 to 2026, entering at the next open, with each rule's own exit, a 10%
stop, and costs of 25 bps a side. The primary run uses the fixed reader, frozen before any return was joined
to news. Today's reader is run beside it.

**Result: all twelve fail, with either reader. None makes money after costs with the required confidence, and
every one lags SPY by a wide margin.**

| Rule family (4 rules each) | Trades a rule | Held, sessions | After costs, a trade | Against SPY, a trade | t against SPY | Years ahead of SPY |
|---|---|---|---|---|---|---|
| Positive news in an uptrend | 49,471 to 49,743 | about 2 | −0.41% to −0.51% | −0.54% to −0.64% | −17.3 to −19.8 | 0 of 11 |
| Negative news, oversold | 4,659 to 19,245 | about 4 | −0.14% to −0.35% | −0.40% to −0.56% | −5.1 to −7.1 | 0 to 2 of 11 |
| All-positive news day with a gap up | 29,814 to 51,710 | 9 to 16 | −0.31% to +0.22% | −0.73% to −0.91% | −4.6 to −6.9 | 1 of 11 |

Fixed reader, 25 bps a side. With today's reader every figure is within a few basis points of these
(`test3_news/results_live.json`): a better reader does not change the answer. At 12.5 bps a side every rule
still lags SPY (t −1.95 to −12.0).

* **Before costs, the largest family earns nothing.** The positive-news rules make between −0.01% and +0.09% a
  trade before costs over a two-session hold, then pay 0.50% to get in and out.
* **The tone is real. It is old news by the next morning.** On the session a story first trades, a stock with
  a story the fixed reader calls positive moved +1.81% against SPY on average and was up on 62% of such days;
  with a negative story, −2.79% and up on 32%; with a neutral story, +0.15%. That is 1.36 million story-stock
  pairs. Much of that move comes before the story is written ("Why X shares are trading higher"), so it is a
  check that the reader reads the market's direction, not a return anyone could have traded.
* **None beats the stock's own drift:** every rule earns less than the same stock's average daily drift over
  the same number of sessions, by 0.42% to 1.98% a trade.
* **On the bot's own 29 stocks the 2026-11-06 pattern comes back**, which shows this pipeline matches the
  app's: the gap-up rules make +0.35% to +1.64% a trade there and none beats SPY reliably (−0.19% to +0.29%,
  t −0.8 to +1.1); the positive-news rules lose there too (−0.28% to −0.44%).
* **Since `fiscal.ai` joined the feed** the gap-up rules lag SPY by 1.5% to 2.5% a trade (t −4.5 to −6.5),
  about twice as badly as before (−0.66% to −0.76%). The other two families are no worse than before. Seven
  months cannot separate the new source from the market of those months.

**Limits.** The labels are one reader's judgement, and a second labeller would differ on some stories. The
conventions call a maintained rating with a changed price target neutral; had those been labelled by the
direction of the target, agreement on the check half would be 127 and 137 of 200 for the two readers, lower
for both with the same gap. The archive holds each story as it reads today, so a headline edited after
publication is read in its edited form. News is tested only through the app's twelve daily rules; trading
within minutes of a story would need minute prices and a faster feed than the bot polls, and was not tested.

---

## Corrections to the pre-registration

All decided before any price was joined to the data they concern.

* **House reports, older layout.** The registered rule read stocks by their type code `[ST]`. Reports before
  mid-2018 carry no type codes, write days without a leading zero, and come out of the PDF with some capital
  letters in lower case. The first parser found no row in 166 of the 450 reports of 2016; the corrected one in
  7. In the older layout a row counts as a stock when it has a ticker and no option or bond wording; tickers,
  transaction types and type codes are matched without regard to letter case (commits `5fb6087`, `a4690fe`).
* **Stricter criterion 2.** Registered as written: a mean excess over SPY with t ≥ 2.0, where the earlier tests
  asked only for a positive mean.

* **A fourth label note, `wrong_company`.** Some stories carry a ticker that is not the company the story is
  about (an analyst note tagged with the brokerage; a tag the feed later corrected). The labelling conventions
  did not foresee this. Such stories are labelled neutral for the tagged company and flagged: 8 of the 400.
  Decided after the sample was drawn and before any score or price was seen. Without those rows the check half
  reads 149 and 159 of 195 for the two readers.
* **The fix was also read against archive stories outside the audit sample.** Registered: changes derived from
  the design half only. Done: derived from the design half, then the effect of each new pattern was read on
  105,808 archive stories outside the sample, with no label and no price, and five patterns were tightened
  ("rose" needs a price or a business line beside it; "failure" skips "heart failure"; "better" counts only
  in "better than expected"; economic releases are not compared with their estimates; debt and
  return-on-capital explainers are routine). The check half played no part. All of it before the check half
  was scored and before any return was joined to news.
* **The labeller had read the check half.** All 400 stories were read while labelling, before the fix was
  designed. The labels were written without the reader's scores, and the scores on the check half were first
  computed after the fix was frozen; but the designer of the fix had seen those headlines.
* **Stories created before 2016.** Alpaca returns 41 older stories as later updates; they are left out, as the
  registered window says.
* **The reaction-session report** counts a story-stock pair when the stock was eligible at the close before
  the story's session, a detail the registration did not state.


## Data

* **Prices:** PR #95's bar cache (`~/.paper-training/dip-rules-full-market-20261112/`): Alpaca SIP daily bars,
  2016-01-04 to 2026-11-11, 8,719 series after its identity check and duplicate removal, 3,559 of them since
  delisted. Nothing was re-fetched except QQQ.
* **8-K filings:** item numbers from the SEC bulk `submissions.zip` of 2026-11-11; acceptance times from the
  SEC's live submissions API (UTC), 6,996 issuers reused from PR #92 and 3,243 fetched now.
* **House reports:** the Clerk's yearly indexes and 5,317 report PDFs, read with macOS PDFKit.
* **Senate:** `timothycarambat/senate-stock-watcher-data` (reports received 2012-07 to 2021-03).
* **News:** Alpaca's news archive (`/v1beta1/news`), every story created from 2016-01-01 to 2026-11-11,
  fetched on 2026-11-14: headline, summary, tickers, source and time, in
  `~/.paper-training/public-information-20261114/news/`. The trade lists of the twelve rules (46 MB and 43 MB)
  stay in that folder under `news_rules/`.
* **Issuer names** for the name check: the SEC bulk `submissions.zip` of 2026-11-11.

