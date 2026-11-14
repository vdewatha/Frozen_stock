# Pre-registration: three tests of public information, 2026-11-14

Written and committed before any event was joined to a price. Each test is run once. Nothing here changes
app code, contacts the deployed app, or touches an account or an order endpoint. Local research only.

The question behind all three: can the bot earn money after costs by reacting to information everyone can
see (company filings, politicians' disclosed trades, news)?

## What has been looked at before this commit

Counts and file formats only; no price or return was read for any event below.

* Alpaca news: the number of items on three days (2017-03-15: 488, 2021-06-16: 802, 2026-10-14: 2,745, of
  which 1,809 from a new source, `fiscal.ai`) and their fields.
* House Clerk: the 2024 index (451 periodic transaction reports, 394 electronic) and the text of two reports.
* Senate: the community mirror `timothycarambat/senate-stock-watcher-data` (1,442 reports, 2012-07-25 to
  2021-03-10; last update 2021-03-16).
* SEC: a count of 8-K filings by item from the bulk `submissions.zip` already on disk (685,205 filings since
  2016 from 10,239 issuers in the ticker map).
* The app's news code and its report `reports/news-entries-20261106.md`.

## Shared by all three tests

* **Prices.** The continuous bar cache of the whole-market test (PR #95,
  `~/.paper-training/dip-rules-full-market-20261112/`): Alpaca SIP daily bars from 2016-01-04 to 2026-11-11,
  raw and split/dividend adjusted, for US companies that file insider forms, delisted ones included, after
  that test's identity check and duplicate removal (`identity.csv.gz`, `kept == True`; years it blocked for
  a series stay blocked). No new price is fetched except the benchmark QQQ (reported only).
* **Which stock.** Test 1 maps by issuer (SEC CIK): the issuer's series with bars at the decision session;
  if several (share classes), the one with the highest 20-session median dollar volume there. Tests 2 and 3
  map by ticker: the series with that ticker and a bar on the decision session; if several, the one with the
  most bars.
* **Eligible at the decision close:** raw close of $5 or more and a 20-session median dollar volume of
  $5,000,000 or more.
* **Execution.** Buy at the next session's open after the decision close. Costs: **25 bps a side** (the
  decisive reading); 12.5 bps a side is reported. One position per stock per rule: a new signal while one is
  open is skipped.
* **A stock that stops trading during a hold** exits at its last close. Reported beside it: the same trades
  with −100% when the issuer filed an 8-K Item 1.03 (bankruptcy) within a year before or 30 days after the
  last bar.
* **Benchmark.** SPY over the same sessions (entry open to exit close).
* **The pass rule** (four criteria, all needed; "t" is the trade-level t with errors clustered by decision
  month):
  1. mean net return a trade above zero with t at or above the bar stated in each test;
  2. mean excess over SPY a trade above zero **with t ≥ 2.0** (stricter than the earlier tests, which asked
     only for a positive mean: a rule that matches SPY's drift is not an edge);
  3. excess over SPY positive in more than half of the years with 20 or more trades;
  4. without the 1% of trades with the highest excess: net above zero and excess above zero.
* **Verdict.** Pass on all four: a forward paper test is proposed (not built here). Otherwise the idea is
  closed. Low power is reported as such ("not supported", not "disproven").
* **Data ends** 2026-11-11. A trade whose exit would fall after it is left out.

---

## Test 1: company event filings (Form 8-K items)

**Hypothesis.** After some type of 8-K event, the stock drifts for the next 20 sessions by more than SPY, by
enough to pay costs.

**Events.** Every Form 8-K (not 8-K/A) accepted from 2016-01-01 to 2026-11-11 by an issuer with a series in
the price cache, **excluding any filing that lists Item 2.02** (earnings results: tested and closed in
PR #92). Item numbers come from the bulk `submissions.zip` (downloaded 2026-11-11). Acceptance times come
from the SEC's live submissions API, which serves UTC for every filer (the bulk file's times mix three
clocks, PR #92); times already fetched for 6,996 issuers are reused and the rest are fetched now.

**Sessions.** As in PR #92: accepted before 09:30 ET on a session day, P is the previous session and R that
day; accepted from 09:30 to 16:00, P is the previous session and R the next session; accepted later or on a
non-session day, P is the last session and R the next one. **Reaction** = adjusted close R / adjusted close
P − 1, minus the same for SPY. Decision at the close of R (eligibility is measured there); **buy at the open
of R+1; sell at the close of R+20** (20 sessions held).

**Candidates: 25 items × 3 signals = 75.** Items 1.01, 1.02, 1.03, 1.04, 1.05, 2.01, 2.03, 2.04, 2.05,
2.06, 3.01, 3.02, 3.03, 4.01, 4.02, 5.01, 5.02, 5.03, 5.04, 5.05, 5.06, 5.07, 5.08, 7.01, 8.01 (Item 9.01,
exhibits, is ignored). A filing counts for every item it lists. Signals:

* **any**: the filing lists the item;
* **up**: it lists the item and the reaction is +5% or more;
* **down**: it lists the item and the reaction is −5% or less (a long position after a fall: reversal).

When one stock has several qualifying filings with the same R for one candidate, they are one event.

**Two periods.**

* **Discovery: decisions from 2016-01-01 to 2021-12-31.** A candidate is **selected** when it has 300 or
  more trades, a mean net return above zero, and a mean excess over SPY above zero with **t ≥ 3.2** (one-sided
  5% over 75 candidates). The mirror image (300 or more trades, mean excess below zero with t ≤ −3.2) is an
  **avoid-list candidate**.
* **Held-out: decisions from 2022-01-01 on.** Computed **only for selected candidates and avoid-list
  candidates**, once. A selected candidate passes when it meets the four criteria on the held-out period
  alone, with a t bar of 2.0 in criterion 1. An avoid-list candidate is confirmed when its held-out mean
  excess is below zero with t ≤ −2.0 (a side result: it could become a BUY blackout for the experiment
  engine; it is not a trade).

If no candidate is selected, the test ends there with the verdict "closed", and the held-out period is not
opened for the long side.

**Reported, not decisive:** the discovery table for all 75 candidates; 5- and 60-session holds for selected
candidates; delisted-since share; the bankruptcy reading.

---

## Test 2: politicians' disclosed trades

**Hypothesis.** Buying a stock after a member of Congress discloses having bought it beats SPY over the
next 60 sessions after costs.

**Source (primary): the House.** The Clerk's yearly indexes
(`https://disclosures-clerk.house.gov/public_disc/financial-pdfs/<year>FD.zip`, 2016 to 2026), filing type
`P` (periodic transaction report), and each electronic report's PDF
(`.../public_disc/ptr-pdfs/<year>/<DocID>.pdf`). Reports filed on paper (scanned; DocID not of the
electronic form) cannot be read and are counted and left out. From each report: every row with asset type
`[ST]` (stock), a ticker in parentheses, a transaction type, a transaction date and an amount range.

**Event.** A **purchase** (type `P`) of a stock that maps to a series in the price cache by ticker on the
filing date. The **public date is the report's filing date in the Clerk's index**, not the trade date.
Decision at the last close on or before the filing date; **buy at the open of the first session after the
filing date; sell at the close of the 60th session held** (the entry session is the first). Several purchases
of one stock disclosed for the same entry session are one event. Filing dates from 2016-01-01.

**Rule.** One rule, no selection: every such event. t bar in criterion 1: 2.0. Period: all of it (no rule
was fitted, so no part is held out); years counted 2016 to 2026.

**Reported, not decisive:**

* **Sales** (types `S`, `S (partial)`): the same trade after a disclosed sale, as the mirror (does a sold
  stock lag SPY?).
* **Senate**, from the community mirror (reports received 2012-07 to 2021-03; only 2016 on is used, with
  `date_recieved` as the public date): alone, and with the House for 2016-01 to 2021-03. The Senate's own
  site needs a click-through agreement and is not used.
* 20- and 120-session holds; entry one session later; by year; before and after 2023-02-07 (the first
  exchange-traded fund that copies these trades); reporting delay (filing date minus trade date) of 15 days
  or less against longer; amount of $50,001 or more; excess over QQQ.

**Limits known now.** Tickers are as the filer typed them; rows without a readable ticker, options and
funds are left out. A stock bought by many members is one event. The law restricts some uses of these
reports (5 U.S.C. 13107(c): no commercial use other than by news media); this is a paper research test.

---

## Test 3: the news reader

The app reads Alpaca (Benzinga) news, scores each story's tone (`news_discovery._article_sentiment`:
analyst-rating actions, routine notes neutral, keyword tone otherwise), admits up to 8 well-covered stocks
with positive tone to the paper universe, and lets rules use the tone as a feature. On 2026-11-06 twelve
news rules were tested on the bot's 29 single stocks and none cleared the gate
(`reports/news-entries-20261106.md`). Two things are unknown: how often the reader gets tone right, and
whether any of those rules works on the whole market.

### 3a. Audit of the reader (no prices)

* **Sample.** From the full archive fetched below, focused stories only (1 to 3 named companies, as the
  app stores them): 200 from 2016-01-01 to 2025-12-31 and 200 from 2026 (100 `benzinga`, 100 `fiscal.ai`),
  drawn with seed 20261114. Split by the same seed into a **design half** and a **check half** (200 each,
  same strata).
* **Labels.** Each headline is labelled positive, negative or neutral for the named company, by Claude,
  from the headline and summary alone, **before the reader's scores for the sample are computed**. These
  labels are one reader's judgement, not ground truth, and are published with the sample.
* **Measure.** Share of the reader's positive calls labelled positive, of its negative calls labelled
  negative, and of labelled-directional stories it calls neutral; overall agreement.

### 3b. Fix

Changes to the reader are derived from the **design half only**. The **check half is scored once**, after
the change is frozen in a commit, with the old and the new reader. The change ships (a separate app-code
pull request) only if agreement on the check half is higher with the new reader; otherwise the reader is
left as it is and the audit is the result. No price or return is used to design the change.

### 3c. The twelve news rules on the whole market

**News.** Every Alpaca news item created from 2016-01-01 to 2026-11-11 (`/v1beta1/news`, headline and
summary, no content). A story counts for a stock when it names 1 to 3 companies and one of them maps to a
series in the price cache by ticker on the story's session. Session and daily tone exactly as
`news_history.session_day` and `news_history.aggregate_daily`.

**Rules.** The app's own twelve news candidates as generated by `strategy_discovery._families` on the day
of this commit (families `positive_news_trend`, `negative_news_dip`, `post_upgrade_drift`), replayed with
the app's own rule engine and labeler (`rules.entry_signals`, `triple_barrier.label_events`), as PR #95 did
for the dip rules: entries at the next open, the candidates' own exits, stops and maximum holds.

**Readers.** The **primary run uses the reader that will be live after 3b** (the fixed reader if 3b ships
one, frozen in a commit before any return is joined to news; else today's). The same twelve rules with
today's reader (commit `a530b16`) are reported beside it when the two differ.

**Pass rule.** The four criteria on the whole period, with the t bar in criterion 1 at **2.87** (two-sided
5% over twelve rules). A rule that passes must also beat the stock's own drift (mean excess over own drift
above zero), the check that undid the post-upgrade result on 2026-11-06.

**Reported, not decisive:** each rule on the bot's own 29 stocks within the same data (to show whether the
pipeline reproduces the 2026-11-06 pattern); the reaction-session return (stock minus SPY) of stories the
reader calls positive, negative and neutral (does the reader's tone match the market's on the day?); years
before and after `fiscal.ai` appears in the feed.

---

## Corrections

Anything that turns out to need changing after this commit (a parser rule, a data gap) is listed in the
report under "Corrections to the pre-registration", with the reason and whether it was decided before or
after returns were seen.
