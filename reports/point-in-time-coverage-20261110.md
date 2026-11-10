# Point-in-time S&P 500 coverage audit: 2026-11-10

**Question.** Is the point-in-time universe the repo already has (`app/services/point_in_time_universe.py`, membership from https://github.com/fja05680/sp500, daily bars from yfinance) good enough to re-test post-earnings drift (PEAD) and new-high breakouts on *past* members, and if not, what would make it so? Every number below was recomputed today from a fresh download (nothing from the 2026-11-06 run survives on disk; see section 7). CSVs are in `reports/point-in-time-coverage-20261110/`.

## 0. Verdict

**Not sufficient as it stands, in any year.** The data lets a PEAD/breakout re-test run only on the survivors of the past: of the 593 names that left the index since 2000, 120 (20%) have a usable price history on their member days, and they are overwhelmingly the names that were *removed while still listed* (79 of 126 index-rule removals have history, 7 of 161 acquired names, 0 of 28 bankruptcies). Measured in constituent-days, 74% of past-member days have no bar at all, and the missing days are 95–100% past members in every year; the 80.9% headline from the survivorship report is reproduced exactly but is an average over current members (95–100% covered) and past members (14–23% covered before 2011, below 55% ever). Worse than missing data is wrong data: 18 past-member tickers (and 15 current ones) are now reused by unrelated companies, ETFs or post-bankruptcy shells, and the module's ≥60-row test counted them as "available", so the earlier point-in-time replays traded BB&T on Brookline Bancorp's prices, Clear Channel on a Chilean brewer's ADR, El Paso and Compuware on OTC penny stocks. The five "corrupt" names are exactly these reuses, and 13 more fail the same tests. The replay also drops any trade still open when a history ends, so a bankruptcy's last trades vanish. Earnings dates from yfinance are the binding constraint for PEAD: the free calendar covers current members back to 2002 (37% of expected member events in 2002, 62% in 2010, 85% in 2020, at the repo's 100-row limit) and the 120 still-listed past members, but of 40 sampled past members without price history, 34 return no earnings dates at all, 2 return another company's, and the 4 that do are 2025 delistings (JWN, HES, ODP, FL). **What a re-test on past members needs:** (1) a ticker-identity check before any bar is used (name match against the removal record, dollar-volume floor; both are in `coverage_per_ticker.csv` now), (2) delisted-name histories from a paid or archival source for the 381 names yfinance no longer serves (about $0 solutions do not exist for bars of names delisted before 2010; a CRSP-style or Norgate/EODHD subscription is the realistic route), (3) an earnings calendar that covers past members (same problem; yfinance has none), and (4) until then, every past-member result stated with documented sensitivity scenarios for the absent names rather than as a point estimate. Effort to make it sufficient: about two days of engineering once a delisted-bar source and an earnings-date source are licensed; without a licensed source the honest scope is **current survivors plus the 120 still-listed past members, 2010 onward, breakouts only**, labelled as such.

## 1. Membership: what the file contains

**Source.** `sp500_ticker_start_end.csv` from https://github.com/fja05680/sp500 (MIT, Farrell J. Aultman), downloaded 2026-11-10, SHA-256 `b2f4fe2f…a0b71f9f` (copy in the CSV folder as `sp500_ticker_start_end.source.csv`). 1,262 rows, 1,209 tickers, 1996-01-02 to 2026-08-18. The repository's last commit is dated 2026-09-07 ("2026-09-07 update"); the file's last membership change is 2026-08-18, so it lags today by almost three months (the October removals of KVUE, WBD, CTVA, TAP, TTD, BLDR are not in it; the module refreshes monthly and would still be stale). After the module's `parse_membership` (upper-case, FB credited to META, touching intervals merged): 1,261 intervals, 1,208 tickers, 503 current members, 52 tickers with more than one interval, **1,096 tickers active on or after 2000-01-01, of which 593 are past-only.**

**Provenance, per the repository README.** 1996–2019 is Andreas Clenow's list (distributed with *Trading Evolved*; the author cannot verify it independently and notes the first ~5 years may miss names: 487 symbols on the first row, 494 from 2001-01-16). 2019 onward is hand-maintained from Wikipedia's "Selected changes" table plus web searches for exact dates. The author states that Yahoo Finance does not keep delisted histories and that a paid source (Norgate, eoddata) is needed for them.

**Ticker convention is mixed and matters.** Companies that went bankrupt are recorded under their post-bankruptcy ticker for the *whole* interval (LEHMQ 1998–2008, ENRNQ, WCOEQ, MTLQQ for GM, AAMRQ for AMR, DALRQ, WAMUQ, EKDKQ for Kodak, FMCC/FNMA): 28 such intervals since 2000. These tickers never existed while the company was in the index and yfinance cannot serve them, so **every bankruptcy is missing by construction**. Renamed survivors, by contrast, appear as two tickers (FB→META, VIAC→PARA, CBS→VIAC) except where the file silently merges them (old AT&T Corp and SBC are one continuous `T` row, so 2000–2005 `T` bars are SBC's). Where a ticker was later reused by another company (DELL 2000–2013 and 2016–, DOW, CEG, HLT, HCA, DG, KMI, BR, IR, Q, SNDK, CF, CNC, FOX/FOXA), the file has two intervals under one ticker and the module credits both to one yfinance history that only covers the newer company.

**Constituents, additions and deletions per year** (`membership_by_year.csv`): the daily count is 491–507 throughout, mean 492 in 2000 and 503 in 2026; 649 additions and 638 deletions over 2000–2026 (622 distinct tickers deleted; 593 never returned). Turnover peaks in 2000 (54 deletions), 2007 (38), 2008 (34) and 2015–2017 (27–29 per year).

**Why each name left** (`deletions_by_year.csv`, `deletions_since_2000.csv`). Wikipedia's "Historical components of the S&P 500" table gives a reason for 389 removals in total, but only from about 2009: the share of deletions with a known reason is 4–29% in 2000–2008, 61–69% in 2009–2010 and 72–100% from 2011. Adding the file's Q-ticker convention for bankruptcies:

| Reason (2000–2026, 638 deletion intervals) | Count |
|---|---|
| Acquired or merged | 171 |
| Removed by index rule, still listed (market cap, moved to MidCap, domicile) | 139 |
| Bankruptcy (1 from Wikipedia, 28 from the ticker convention) | 29 |
| Spin-off or restructuring | 27 |
| Other delisting (FDIC receivership of FRC, SBNY, SIVB; MXIM; CMCSK) | 5 |
| Rename or reorganisation | 4 |
| Reason text unclassifiable | 4 |
| Unknown (no Wikipedia row, mostly 2000–2008) | 259 |

So of the deletions with a known reason, 54% were delistings (acquired, bankrupt, seized) and 37% were still-listed removals. The 259 unknowns are resolved below from the price data itself (a name whose yfinance history runs to today under the same company is still listed).

## 2. Price coverage by constituent-day

Definition: a constituent-day is one ticker on one NYSE trading day (SPY's calendar, 6,755 days 2000-01-01 to 2026-11-10) while the file says it was a member; it is covered when the yfinance history has a bar on that day. `coverage_by_year.csv`; per ticker in `coverage_per_ticker.csv`.

| Year | Member-days | Coverage | Clean coverage¹ | Current members | Past members | Past, clean¹ | Missing days that are past members |
|---|---|---|---|---|---|---|---|
| 2000 | 124,001 | 47.1% | 45.8% | 94.8% | 16.0% | 13.9% | 96.1% |
| 2002 | 124,665 | 50.3% | 49.1% | 94.8% | 17.2% | 15.0% | 95.5% |
| 2004 | 124,677 | 52.5% | 51.2% | 95.1% | 18.2% | 16.0% | 95.5% |
| 2006 | 124,746 | 55.8% | 54.6% | 95.4% | 20.3% | 18.0% | 95.1% |
| 2008 | 125,841 | 60.9% | 59.5% | 97.2% | 24.2% | 21.3% | 96.4% |
| 2010 | 125,537 | 65.5% | 64.4% | 97.8% | 25.5% | 23.0% | 96.4% |
| 2012 | 124,242 | 68.5% | 68.3% | 98.2% | 26.7% | 26.1% | 96.7% |
| 2014 | 125,534 | 71.9% | 71.7% | 98.7% | 29.2% | 28.7% | 97.1% |
| 2016 | 127,356 | 76.8% | 76.6% | 98.7% | 33.9% | 33.3% | 96.3% |
| 2018 | 126,917 | 81.5% | 81.3% | 99.5% | 36.3% | 35.6% | 97.9% |
| 2020 | 127,767 | 87.4% | 87.4% | 100.0% | 40.9% | 40.9% | 100.0% |
| 2022 | 126,437 | 91.8% | 91.7% | 99.8% | 50.6% | 49.6% | 98.2% |
| 2024 | 126,757 | 95.5% | 95.3% | 99.6% | 54.8% | 52.7% | 91.1% |
| 2026 | 108,650 | 98.7% | 98.7% | 99.4% | 45.8% | 45.8% | 55.9% |

¹ Clean = after removing the bars of the 13 reused or shell tickers in section 4 (EP, CPWR, GENZ, SII, PARA, CSR, BBT, CCU, KG, WEN, NC, FNMA, FMCC), which the module currently counts as covered.

* Mean yearly coverage 2010–2026: 81.7% by constituent-day (clean), **80.9% by the module's own definition** (members of the year with ≥60 rows overlapping their membership; replicated today with `coverage()` on the fresh download: 704 available, 392 missing, `module_coverage_by_year.csv`). The headline reproduces; its meaning does not survive the split.
* **Past members: 1,241,986 constituent-days since 2000, 321,270 with a bar (25.9%), 300,564 usable (24.2%).** 381 of 593 past members (64%) return no history at all; 74 more return a history that starts *after* the company left (the ticker now belongs to someone else: ABI is a VictoryShares ETF, BUD is AB InBev's ADR from 2009, EMC an emerging-markets ETF, APC "ARKO Petroleum" from 2026); 13 return a different instrument's bars *on the member days*; 5 return a single stub bar. Only 120 names (113 clean, 5 renamed survivors that Wikipedia lists under their old name, 2 with a late start) carry a usable history.
* **Current members: 2,133,456 constituent-days, 98.3% covered.** The 37,270 missing days belong to the 18 reused-ticker survivors above (DELL's 2000–2013 days are the old Dell's; DOW's pre-2019 days the old Dow Chemical's; `current_members_truncated_history.csv`), plus three names removed after the file's date whose histories Yahoo has already cut to a stub (WBD, KVUE, PSKY).
* **Concentration.** The missing share is dominated by delistings. Of the 920,716 missing past-member days: 415,323 (45%) are names that were acquired, 34,523 (4%) bankruptcies, 2,716 seized banks, 35,735 spin-offs, 304,848 (33%) names with no Wikipedia reason, and only 117,478 (13%) names removed by index rule while still listed. Among the 259 "unknown" names, 169 have no yfinance history and 45 have a reused ticker, i.e. they too are almost all delistings (a still-listed company keeps its history). Read the other way: a still-listed removal has history 63% of the time (79 of 126), an acquired name 4% (7 of 161), a bankrupt name never.

## 3. Delisting outcomes and how the replay treats a history that ends

`delisting_outcomes.csv` has one row per past member: exit reason, what yfinance serves, whether a bar exists within ten trading days of the removal, and whether the name would need an assumed delisting return.

| Past members by what yfinance serves | Names | Member-days | Final price at removal |
|---|---|---|---|
| No history (`YFTzMissingError: possibly delisted`) | 381 | 765,613 | none |
| History starts after the company left (ticker reused) | 74 | 119,901 | none (wrong company) |
| History on member days but a different instrument (section 4) | 13 | 26,860 | wrong company |
| One stub bar on or after the removal (AVB, EA, EQR, LEG, QRVO) | 5 | 25,120 | one bar, no path |
| History runs to today, same company (index-rule removals, spin-offs, a few acquirers' survivors) | 120 | 304,492 | 120 of 120 |

* **Final price and date.** A delisting date exists for every name (the file's `end_date` and, from 2009, Wikipedia's effective date). A final *price* exists only for the 120 names whose history runs to today (they were not delisted) and for the 5 stubs. **No acquired or bankrupt name has a usable last bar**: 0 of 28 bankruptcies and 7 of 161 acquisitions have any history, and those 7 are acquirers that kept the ticker (e.g. the file's `T`, `WEN` cases) or later re-listings.
* **How the replay treats a history that ends.** `triple_barrier.label_events` resolves each entry by walking forward to a stop, target, indicator or vertical exit; if the data ends first the trade is **dropped** (`continue  # path unresolved at the end of the data`), not held to the last bar and not marked. The membership mask gates *entries* only, so a trade entered before removal keeps running on whatever bars follow, including a reused ticker's. With the current data this is moot for delistings (there are no bars to end on) but it is the wrong rule for the day delisted histories are added: a bankruptcy's final collapse would be the one path that never resolves.
* **Names that would need an assumed delisting return** (`needs_assumed_delisting_return` in the CSV): 473 of 593 past members, 937,494 constituent-days: every name without a usable history on its member days (381 with none, 74 reused with no overlap, 13 substitutes, 5 stubs); every name with a usable history also has a final bar. Split by exit reason: 154 acquired, 28 bankrupt, 4 seized, 223 unknown, 45 index-rule removals, 14 spin-offs, 3 renames, 2 unclassified. The 45 index-rule and 14 spin-off names with no yfinance history were almost certainly acquired or failed *after* leaving the index (Yahoo keeps listed companies), so their member-day bars are missing for the same reason.
* **Recommended sensitivity scenarios** (documented, never a silent zero). For a test that enters a name while it is a member and the data runs out: (a) *exclude* the name entirely (the status quo, biased toward survivors); (b) *hold to the last bar* with the final exit at the last close net of the full exit cost; (c) for names whose exit reason is acquisition, (b) plus **0%** from the last bar (cash at the deal price); for bankruptcies and receiverships **−100%** from the last bar; for unknown exits a pessimistic **−30%** and a worst-case −100%; (d) for the 473 names with no bars at all the honest statement is that the event cannot be simulated; the scenario is a *universe* scenario: report the result on the covered universe, then the result if the absent names, when they were members and would have been signalled at the base rate of the covered names, had returned −30% / −100% on each trade. The bankruptcy list is 28 names (all Q-tickers), the acquisition list 154 names; both are in the CSV so the scenarios can be computed per name, not per average.

## 4. Corrupt histories: the five (six) reproduce, and they are ticker reuse

The earlier verdict-inputs run dropped 78,982 trades as "cost artifacts" (intrabar stop or target exits losing more than 2 × stop + 100 bps) from EP, CPWR, PARA, SII, GENZ and FNMA. Today's download reproduces all six and names the defect: **the ticker now belongs to another instrument and yfinance serves that instrument's bars on the old member's dates.** Thin OTC prints make the cost model's impact term (sigma × √(order / 20-day volume)) hit its 5,000 bps cap, so a 3% stop exit books a −50% trade.

| Ticker | S&P member | What yfinance serves today | Member-day bars | Zero-volume | Flat OHLC | Side cost > 200 bps | Max side cost |
|---|---|---|---|---|---|---|---|
| EP (El Paso, acquired by KMI 2012) | 2000–2012 | Empire Petroleum, NYSE American | 3,119 | 40% | 67% | 3,099 | 5,000 bps |
| CPWR (Compuware) | 2000–2012 | Ocean Thermal Energy, OTC | 1,279 | 76% | 92% | 1,114 | 5,000 bps |
| PARA (Paramount Global, merged into PSKY 2025) | 2022–2025 | Parabolic Technologies, Nasdaq CM | 870 | 33% | 28% | 421 | 2,395 bps |
| GENZ (Genzyme, acquired by Sanofi 2011) | 2001–2011 | VanEck Digital Native Economy ETF | 805 | 2% | 6% | 121 | 707 bps |
| SII (Smith International, acquired by SLB 2010) | 2006–2010 | Sprott Inc. | 131 | 73% | 95% | 106 | 946 bps |
| FNMA / FMCC | 2000–2008 | the same companies, OTC since 2010 | 2,185 each | 0% | 0% | 2 each | 314 / 249 bps |

FNMA and FMCC are *genuine*: the two flagged bars are the conservatorship days (2008-09-08, −90% and −83%) and the cost artifact there was one trade. The other four are substitutions; CPWR's series also has two day-over-day ratios of 0.2 and 0.1 with no split, and PARA's carries a bogus $100 "dividend" that moves its adjusted close 7.5× on 2025-06-16.

**Every symbol that fails the same tests** (`corrupt_bars.csv`; 639 symbols with member-day bars scanned on the full contiguous series, flags counted on member days):

* Side cost over 200 bps on member days (the repo's artifact rule needs about 200 bps a side): 13 symbols. Substitutions: EP, CPWR, PARA, GENZ, SII, **CSR** (Central & South West 2000; Yahoo serves Centerspace, 30 bars), **KG** (King Pharmaceuticals; Yahoo serves Kestrel Group, 8 bars). Genuine volatility, keep: WMB (20 bars after the −61% day of 2002-07-22), GNW (9, Nov 2008), FNMA, FMCC (2 each), AMCR (3, 2019 listing), FDXF (1, 2026 spin-off).
* Day-over-day adjusted close ratio outside [0.2, 5] without a split: CPWR (2), PARA (1), FNMA, FMCC (1 each, genuine).
* High below low: none. Open or close outside the day's range by more than 0.01%: 13 symbols, one bar each (A, ALL, AME, GIS, HII, MKC, MSI, MTB, NRG, UA, UHS, VRT, WBD's stub): Yahoo rounding, harmless.
* Zero-volume share of member bars above 5%: EP, CPWR, PARA, SII.
* **Substitutions the cost test does not catch**, because the substitute is a real listed stock with normal-looking bars: BBT (BB&T, member to 2019; Yahoo serves Brookline Bancorp, now Beacon Financial: $32 on 2019-12-06 against BB&T's $56, median member-day dollar volume $1.0M against BB&T's hundreds of millions), CCU (Clear Channel 2000–2008; a Chilean brewer's ADR), WEN (Wendy's International 2000–2008; Triarc's history), NC (NACCO 2000, plausible but $1.1M a day), plus the 74 no-overlap reuses. The dollar-volume floor ($2M median on member days) and the Wikipedia-name check in `coverage_per_ticker.csv` catch all of these; a replay must apply both before trusting a past member's bars.

The repo's `cost_artifacts` filter removed the symptom (−50% trades) but not the cause; trades on these names that did not hit the cost cap stayed in every point-in-time result since 2026-11-06, and BB&T, Clear Channel and Wendy's were never flagged.

## 5. Earnings events for past members

Fetched with the repo's own call, `Ticker(t).get_earnings_dates(limit=100)`, for the 715 tickers with bars and a random 40 of the 381 past members without any (`earnings_coverage_by_year.csv`, `earnings_coverage_per_ticker.csv`, raw events in `earnings_events_raw.csv.gz`). "Usable" is the repo's `clean_surprises` rule: a finite surprise within ±500%, a time of day, one report per day; "on member days" means the report fell inside the file's membership interval.

**Do past members have any earnings dates?** The 40-name sample of names with no price history: **34 return nothing** ("No earnings dates found, symbol may be delisted"), 2 return a reused ticker's calendar (PLL is now Piedmont Lithium, ARC is ARC Document Solutions; the members were Pall Corp and Atlantic Richfield), and 4 return a genuine calendar: JWN, HES, ODP and FL, all delisted in 2025 by acquisition or buy-out, whose *price* history Yahoo has already dropped while the earnings table survives. So a name delisted before about 2025 has no yfinance earnings calendar, and a name delisted recently has a calendar but no bars. All 120 still-listed past members return a calendar (176 of 212 past members with bars do; the 36 empties are the reused tickers and stubs), as do all 503 current members.

**How far back the calendar reaches.** 430 of 685 calendars hit the 100-row limit, so for continuously reporting companies the repo's loader sees about 25 years of quarters: the earliest usable event is 1999, and 41% of symbols have a usable event by 2003. Raising the limit would add pre-2002 rows for those 430 names; it adds nothing for delisted names.

**Usable events per year on member days, clean universe** (current members plus the 120 still-listed past members; events from reused tickers removed):

| Year | Usable events | of which past members | Symbols with an event | Expected (4 × member-years) | Share |
|---|---|---|---|---|---|
| 2000 | 5 | 3 | 2 | 1,968 | 0.3% |
| 2002 | 730 | 124 | 245 | 1,980 | 37% |
| 2004 | 976 | 160 | 252 | 1,980 | 49% |
| 2006 | 1,049 | 175 | 272 | 1,980 | 53% |
| 2008 | 1,150 | 197 | 301 | 1,996 | 58% |
| 2010 | 1,242 | 189 | 317 | 1,992 | 62% |
| 2012 | 1,329 | 207 | 339 | 1,972 | 67% |
| 2014 | 1,406 | 209 | 359 | 1,992 | 71% |
| 2016 | 1,516 | 214 | 393 | 2,020 | 75% |
| 2018 | 1,625 | 198 | 417 | 2,016 | 81% |
| 2020 | 1,726 | 167 | 450 | 2,028 | 85% |
| 2022 | 1,825 | 159 | 469 | 2,008 | 91% |
| 2024 | 1,903 | 97 | 490 | 2,012 | 95% |
| 2026 (to Nov 10) | 1,900 | 9 | 506 | 1,724 | 110%² |

² More than four reports per member-year because the file is stale (names removed in October still count as members) and some calendars carry both a date and a confirmed-date row.

So a PEAD test on the point-in-time universe has 0.4–0.5 of the index's earnings events in 2002–2005, 0.6–0.7 in 2010–2014, and only 120–215 past-member events a year, all from names that were removed while still listed. The 42–63% of events missing in 2002–2012 are the delisted names' events: the ones a drift-after-negative-surprise rule would have lost most on. There is no free fix: the sample shows the free calendar does not retain delisted names' reports. SEC EDGAR 8-K Item 2.02 filings (free, complete from 2003) carry the report dates but not the consensus surprise, and need a CIK-to-ticker history to be built for the 593 past members.

## 6. What a re-test on past members can honestly claim, and what it would cost to do better

* **With today's data**, a point-in-time replay is a replay of *current members plus the 120 still-listed past members*, with the 13 substitutes and 74 reused tickers excluded. From 2010 that covers 64–87% of constituent-days, but the uncovered 13–36% is almost entirely acquired and bankrupt names, the exact names where PEAD and breakout rules fail worst (a breakout into a failing company, a negative-surprise drift into a bankruptcy). The excess over own drift measured on this universe is still a survivor's excess; it is less flattered than today's 45-name list but not unbiased, and no sensitivity scenario on an absent name can be computed from bars that do not exist. Breakout tests can run on bars alone; PEAD cannot run on past members at all, because yfinance has no earnings dates for them (section 5).
* **To make it sufficient.** (1) Fix the identity check now: require a name match with the removal record or a $2M member-day dollar-volume floor before a past member's bars enter a replay, and hold-to-last-bar instead of dropping unresolved paths; half a day. (2) Buy or borrow delisted daily bars for the 473 names without usable history (Norgate Data, EODHD's delisted-equity feed, or academic CRSP access; the file's author recommends Norgate; licensing is the cost, not engineering: one loader and the same CSV layout, about one day). (3) An earnings-date source that covers delisted names 2000–2026 (the same vendors, or SEC EDGAR 8-K Item 2.02 filing dates, which are free and complete from 2003 but need the CIK-to-ticker history to be built, about two days). (4) Then the sensitivity scenarios in section 3 become checkable rather than assumed. Until (2) and (3) exist, the pre-registered scope for any past-member re-test should be **2010 onward, breakouts only, current plus still-listed past members, results reported with the scenario (a)/(b)/(c) bracket**, and the PEAD leg deferred.

## 7. Method and provenance

* Nothing from the 2026-11-06 survivorship run or the 2026-11-07 verdict-inputs run is on disk (no `universe/sp500_membership.csv`, no `sp500_coverage.json`, no past-member bar CSV anywhere under the repo, `.paper-training/`, `stock_training_artifacts/` or the session scratchpads); every number here is from a fresh download on 2026-11-10 into the session scratchpad, never on the deployed host.
* Bars: `yfinance 1.7.0`, `Ticker(t).history(period="max", auto_adjust=False, actions=True)` one ticker at a time, 0.4 s apart, 1,096 tickers in 15 minutes, no rate limiting; 715 returned bars, 381 failed with `possibly delisted; no timezone found`, none returned an empty frame. Adjusted OHLC rebuilt as the repo's `download_daily_history` does (OHLC × Adj Close / Close). `history_metadata` (long name, exchange, first trade date) kept per ticker to identify reuse.
* Trading calendar: SPY's bar dates from the local sweep snapshot (`.paper-training/local-sweep-20261110/bars/SPY.csv`), extended with weekdays for November 2026.
* Reasons: Wikipedia, "Historical components of the S&P 500" (fetched 2026-11-10; 415 change rows, 389 with a removed ticker), keyword-classified (`wiki_changes_classified.csv`); joined to the file's deletions by ticker and a ±45-day date window.
* Cost test: the repo's own `triple_barrier.prior_liquidity` and `side_cost_bps` with the default `BarrierConfig` ($10,000 notional) on the full contiguous adjusted series, flagged on member days. The earlier analysis of this audit computed it on member-day-only series and produced false clusters at membership gaps (JBL, PTC, CNC, CIEN, TER, OKE, OI); those are not in the final tables.
* Earnings: `Ticker(t).get_earnings_dates(limit=100)`, the same call as `app/services/earnings_history.fetch_surprises`, for every ticker with bars and a random 40 of the no-history past members (seed 20261110).
* Scripts are in the session scratchpad only (membership_audit.py, coverage_audit.py, outcomes_audit.py, deletions_audit.py, download_bars.py, download_earnings.py, earnings_audit.py); they read the CSV folder's source file and yfinance and are reproducible from the descriptions above.
