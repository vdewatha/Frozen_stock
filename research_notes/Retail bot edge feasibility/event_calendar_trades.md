# Event- and calendar-driven trades a free-data bot can time: what survives after 2015 in US large caps and ETFs

Scope: Frozen_stock (Alpaca paper trading, about $100k, small orders, long-only main book, small short pilot, yfinance daily bars and earnings dates, free Fed/CPI/jobs calendars). Not repeated here (covered 2026-11-07): the pre-FOMC drift (only survives when VIX is high), the classic turn of month (faded on SPY), the [-3,+3] turn-of-month window (about 5-12 bps a day), and post-earnings drift (dead in large caps since about 2006).

Convention: "bps per event" means the documented average return for one occurrence of the trade. Cost assumptions in the Inferences are mine, not from a source: SPY/IEF/TLT round trip about 1-2 bps, S&P 500 single names about 3-10 bps round trip at small size, commission-free at Alpaca.

## 1. Earnings announcement premium (EAP), and whether the bot's earnings blackout blocks it

### Takeaway
The premium for buying ahead of scheduled announcements was large and strongest in large caps before about 2004. The best recent US study finds it has disappeared since then (not statistically different from zero after 2004). The bot's default 10-session earnings blackout does block this trade, but on current evidence it gives up little expected return and avoids large gap risk. The right move is a shadow test, not removing the blackout.

### Cited Findings
- Frazzini & Lamont (NBER w13090): stock prices rise around scheduled earnings announcement dates, and the premium is tied to volume surges. Strategies earned 7% to 18% a year with Sharpe ratios above other popular anomalies. The effect is strongest in large-cap stocks and is present in data back to 1927. — [NBER w13090](https://www.nber.org/papers/w13090); [NBER PDF](https://www.nber.org/system/files/working_papers/w13090/w13090.pdf)
- Barber, De George, Lehavy & Trueman, "The Earnings Announcement Premium Around the Globe" (JFE 2013): the premium is global, and the premium for the smallest stocks exceeds that for the largest by roughly 6% a year. This cuts against the Frazzini-Lamont large-cap finding. — [SSRN 1872183](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1872183)
- Savor & Wilson, "Earnings Announcements and Systematic Risk" (JF 2016, vol. 71, DOI 10.1111/jofi.12361): firms scheduled to report earn an annualized abnormal return of 9.9%. Announcement risk is priced in the cross-section, and early (late) announcers earn higher (lower) returns. — [vLex record of JF article](https://law-journals-books.vlex.com/vid/earnings-announcements-and-systematic-855658475)
- Heitz, Narayanamoorthy & Zekhnini, "The Disappearing Earnings Announcement Premium": the premium has disappeared from US markets in recent years. Post-2004 estimates are not statistically different from zero. They link the timing to the 2004 SEC disclosure rule that increased Form 8-K filings of material news. The premium is still robust internationally, and the results fit uncertainty-based rather than limited-attention explanations. I could not open the SSRN page (HTTP 403); these points come from the abstract as shown in search results and a paperswithbacktest listing. — [SSRN 3296537](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3296537); [paperswithbacktest summary](https://paperswithbacktest.com/strategies/the-disappearing-earnings-announcement-premium)
- Contrary evidence: a 2023 Pacific-Basin Finance Journal study using 1983-2016 data finds the premium still positive and significant, but lowest in 2006-2016. — [ScienceDirect S0927538X23000951](https://www.sciencedirect.com/science/article/abs/pii/S0927538X23000951)
- Hong Liu (2024 AEA program paper, "Earnings Announcements: Ex-ante Risk Premia"): the average ex-ante earnings announcement risk premium per announcement is "highly significant at 15 basis points". Smaller firms are riskier. The sample period was not visible. — [AEA 2024 program](https://www.aeaweb.org/conference/2024/program/paper/5GGEki7i)
- Bot setting: `paper_earnings_blackout_sessions` defaults to 10. It skips new paper BUYs when an earnings report falls within 10 sessions (today included). Funds are unaffected, and an unavailable calendar does not block. Strategy rows can override it (0 = off), and the setup shadow already tests blackout values 0/10/20. — local code: `/Users/ainz_ool_gown/Documents/Trading App/artifacts/api-server/backend/app/core/config.py` (lines 314-318), `/Users/ainz_ool_gown/Documents/Trading App/artifacts/api-server/backend/app/services/earnings_calendar.py` (`earnings_blackout`), `/Users/ainz_ool_gown/Documents/Trading App/artifacts/api-server/backend/app/services/setup_shadow.py`

### Inferences
- The blackout and the EAP are direct opposites: the EAP trade is to buy in the days or month before a scheduled report, which is exactly the window the 10-session blackout removes. If the US premium were still about 15 bps per event, the blackout would cost roughly 15 bps on each blocked entry. With Heitz et al. finding zero after 2004, the expected cost is about zero, while the blackout avoids the 4-8% single-day gaps typical for large caps on report days (typical-gap figure is my assumption, not sourced).
- Per-event magnitude, if alive: about 15 bps (Liu) against about 3-10 bps of round-trip cost for single names. That is thin even before the post-2004 evidence of no premium.
- Evidence rate is the one strength: about 2,000 S&P 500 reports a year. With an assumed 5% standard deviation of the 1-2 day announcement return, detecting 15 bps at t=2 needs about (2 x 500/15)^2, roughly 4,400 events, or about 2 years of all S&P 500 reports. Clustering in earnings season lowers the effective count, so use market-adjusted returns.
- Practical recommendation: keep the blackout on the main book. Add a no-money shadow log that records the market-adjusted return from close(t-2) to close(t+1) around each S&P 500 report, using yfinance earnings dates. Backfill 2015-2026 from yfinance history if `get_earnings_dates` reaches back far enough.
- Long-only, free data: yes (yfinance earnings dates plus daily bars). Short pilot: not relevant.

### Gaps
- I could not read Heitz et al. in full (SSRN blocked), so I have no post-2004 point estimate in bps, no large-cap-only split, and no confirmation of the sample end year.
- No 2015-2026 large-cap-only US EAP estimate was found in any source.
- How far back yfinance's earnings-date history reaches per symbol was not verified in this pass. It matters for a backtest.

## 2. S&P 500 additions and deletions (announcement to effective-date drift)

### Takeaway
The classic index effect is gone on average. Additions earned 0.8% total (insignificant) and deletions -0.6% in 2010-2020. MidCap migrations, the majority of additions, earned about -2.6%. Since 2021 the announcement-day pop for direct additions has come back (about +4 percentage points on average), but S&P announces after the close, so the pop is mostly in the overnight gap a daily-bar bot cannot buy before. No source shows a positive post-announcement-open to effective-date drift for 2015 onward. Long-only rank: low.

### Cited Findings
- Greenwood & Sammon (NBER w30748, Dec 2022; published JF 2025, vol. 80(2), 657-698): the average addition abnormal return was 3.4% in the 1980s, 7.6% in the 1990s, 5.2% in the 2000s and 0.8% in 2010-2020, statistically indistinguishable from zero. Deletions: -4.6% (1980s), -16.6% (1990s), -12.3% (2000-09), -0.6% (2010-20). — [NBER w30748 PDF](https://www.nber.org/system/files/working_papers/w30748/w30748.pdf); [EconPapers JF record](https://econpapers.repec.org/RePEc:bla:jfinan:v:80:y:2025:i:2:p:657-698)
- Same paper: the announcement return (t-1 to t+1) fell to about 1% by the late 2010s. The implementation (effective-date) return was about 2% in the 1980s, about 1% in the 2000s, and close to zero by the late 2010s. The average gap between announcement and effective date is 4.8 trading days for additions and 5.8 for deletions (1980-2020 sample). Excluding Tesla, the 2020 inclusion effect was -3 bps. — [NBER w30748 PDF](https://www.nber.org/system/files/working_papers/w30748/w30748.pdf)
- Same paper: migrations from the S&P MidCap went from about 40% of additions to over 80%. By the late 2010s direct additions returned 2.2% while migrations returned -2.3%. For migrations the effect was -2.6% in both the early and late 2010s. — [NBER w30748 PDF](https://www.nber.org/system/files/working_papers/w30748/w30748.pdf)
- Same paper, explanations: a larger share of the move now happens before announcement (front-running, "selecting largest eligible firm" predicts additions better). Index desks have grown. The effective date's share of volume in the month around the change rose from 15% (1990s) to almost 30% (2010s). Index trackers buy about 7-8% of shares, yet total institutional ownership barely moves, because active institutions supply the liquidity. — [NBER w30748 PDF](https://www.nber.org/system/files/working_papers/w30748/w30748.pdf)
- Post-2021 resurgence (Goldman Sachs research via ETFdb): since 2021, new S&P 500 additions beat the equal-weight index by about 4 percentage points on announcement day on average, with nearly three-quarters beating it. 2025 additions (e.g., Block, Coinbase, DoorDash) beat it by 7.4 percentage points on announcement day. Goldman found no evidence that outperformance continued after the reshuffle. This is secondary reporting of sell-side research. — [ETFdb, Aug 2025](https://etfdb.com/news/2025/08/06/retail-revival-fuels-sp-500-index-inclusion-effect/); [Voronoi chart](https://www.voronoiapp.com/markets/The-SP-500-inclusion-effect-springboard-is-back-in-a-big-way-5981)
- After inclusion: McKinsey reports prices return to intrinsic value within about two months of inclusion or removal. Morningstar summarizes academic work showing added firms underperform matched peers afterward. Both are secondary summaries. — [McKinsey](https://www.mckinsey.com/featured-insights/week-in-charts/the-ephemeral-effect-of-stock-index-inclusion); [Morningstar](https://www.morningstar.com/funds/sp-500-bump-that-doesnt-last)
- Timing in S&P DJI press releases: quarterly-rebalance batches come out about 2-3 weeks before the effective date (Dec 1 for Dec 18, 2023; Jun 7 for Jun 24, 2024; Jun 5 for Jun 22, 2026). Changes take effect before the open, with index trades at the prior Friday's close. Ad hoc replacements (e.g., VICI for Cerner, June 2022) have their own dates. — [S&P DJI Jun 2022](https://press.spglobal.com/2022-06-03-Keurig-Dr-Pepper,-VICI-Properties-and-ON-Semiconductor-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400,-and-S-P-SmallCap-600); [S&P DJI Dec 2023](https://press.spglobal.com/2023-12-01-Uber-Technologies,-Jabil-and-Builders-FirstSource-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600); [S&P DJI Jun 2024](https://press.spglobal.com/2024-06-07-KKR,-CrowdStrike-Holdings-and-GoDaddy-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600); [S&P DJI Jun 2026](https://press.spglobal.com/2026-06-05-Marvell-Technology-and-Flex-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600)
- Older context: S&P Global data cited by Motley Fool shows 1995-99 additions beat the index by 8.3 points between announcement and effective date, versus 3.6 points for 2000-2010. — [Motley Fool, 2024](https://www.fool.com/investing/2024/09/11/history-added-sp-500-impact-palantir-stock)

### Inferences
- The tradable window for this bot is from the next open after the after-close press release to the effective date. That window excludes the overnight pop (where the post-2021 +4 points sits) and keeps only the "implementation" piece, which Greenwood-Sammon put at about zero by the late 2010s. Migrations are the common case and were negative.
- Long-only: no defensible edge. A possible use for the short pilot is shorting direct additions at the effective-date close, betting on the reported post-inclusion fade. That rests only on secondary sources and should stay a shadow idea.
- Defensive use: index changes are a useful "do not chase" flag for the news-discovery admission path, since they produce big announcement gaps that later fade.
- Evidence rate: quarterly batches typically add 2-3 names (from the press releases above) plus ad hoc changes, so roughly 20-30 additions a year (my estimate). Too few to judge forward within months.
- Data: S&P DJI press releases are free and timely (press.spglobal.com, after the close). They are not a clean API, so scrape the release title pattern "Set to Join S&P 500".

### Gaps
- No peer-reviewed estimate covers 2021-2026 for the next-open-to-effective window. The Goldman figures are announcement-day only and secondhand.
- No exact annual count of S&P 500 changes for 2015-2026 was retrieved.

## 3. Option expiration week, triple witching, and the 0DTE era

### Takeaway
Stivers & Sun found high expiration-week returns in large caps (0.45% vs 0.12% in other weeks) followed by a flat-to-negative week after, but their sample ends in 2008. Post-2015 evidence is only practitioner backtests, which say the SPY effect has deteriorated and that expiration Friday itself is poor. No study covers how 0DTE dominance since 2022 has changed this. Rank: low.

### Cited Findings
- Stivers & Sun, "Returns and option activity over the option-expiration week for S&P 100 stocks" (J. Banking & Finance 37(11), 2013, 4226-4240): weekly returns in option-expiration (third-Friday) weeks are high relative to other stocks with less option activity and to the stocks' own other weeks. Proposed mechanisms: market makers shrinking short-stock delta hedges over the week, and implied volatility falling over the week. — [IDEAS/RePEc](https://ideas.repec.org/a/eee/jbfina/v37y2013i11p4226-4240.html)
- Per CXO Advisory's summary of Stivers & Sun: for 28 large caps in 1996-2008, expiration weeks averaged 0.45% vs 0.12% in other weeks. The week after averaged -0.03% vs 0.18% in other non-expiration weeks (partial reversal). Expiration-week returns were also high for the S&P 500 index in 1983-2008, but not pre-1983 or for small caps. The option-to-stock volume filter is not knowable in advance, so only the plain week effect is tradable. — [CXO Advisory drill-down](https://cxoadvisory.com/calendar-effects/option-expiration-week-stock-return-drill-down)
- Quantified Strategies (practitioner backtest, page dated March 2026): buying SPY at the open of OPEX week and selling at the expiration-day close gives about 0.2% average weekly gain and 2% CAGR, invested about 18% of the time. The effect "has deteriorated over the last four years" (no numbers given). Expiration day itself (prior close to Friday close) is poor: compounded -23.55%, win rate 51.4%, average win 0.60%, average loss -0.77%. Neither triple witching nor 0DTE is analysed. — [Quantified Strategies](https://quantifiedstrategies.substack.com/p/the-options-expiration-week-effect-aa2)
- A separate study finds stocks with large deep in-the-money call open interest earn about -0.8% daily returns on expiration dates, attributed to selling pressure (authors and sample not confirmed). — [ScienceDirect S0927539814000231](https://www.sciencedirect.com/science/article/abs/pii/S0927539814000231)
- 0DTE share: a secondary industry source cites Cboe data putting 0DTE at about 65% of SPX option volume by May 2026. Unverified against Cboe directly. — [Lambda Finance](https://www.lambdafin.com/articles/0dte-vs-weekly-options-compared)

### Inferences
- The Stivers-Sun mechanism (hedges built up over a monthly cycle, then unwound into expiry) should weaken when most index option volume expires the same day it trades. This is a plausible reason for the reported fade, but it is unproven.
- Per-event magnitude, if alive: about 20-30 bps per OPEX week on SPY (practitioner figure, gross), against 1-2 bps of SPY round-trip cost. It is cheap to implement, but the post-2015 evidence is weak and anecdotal.
- Long-only: yes (SPY or large caps). Data: free, since expiry dates are deterministic (third Friday; Thursday when Friday is a market holiday). Triple witching is the March/June/September/December third Friday, which also coincides with the S&P quarterly rebalance.
- Evidence rate: 12 a year (4 triple witchings). Too slow to judge forward within months. A backtest on yfinance SPY 2015-2026 (about 140 OPEX weeks) is cheap and should come first.
- Worth noting: avoiding new buys on expiration Friday's close-to-close is consistent with the practitioner result, but it is a weak, unpublished pattern.

### Gaps
- No peer-reviewed post-2015 study of US OPEX-week or triple-witching returns was found.
- No study of 0DTE's effect on underlying expiration-week returns was found.

## 4. Month-end and quarter-end pension rebalancing flows, and leveraged ETF rebalancing

### Takeaway
This is the strongest published, recent, calendar-timed effect in this set. Harvey, Mazzoleni & Melone (NBER w33554, revised Jan 2026) show that when stocks have outrun bonds, calendar rebalancers sell equities around month-end. A one-standard-deviation signal predicts about -17 bps for stocks minus bonds over the next day, mostly reversing within two weeks. Their futures long-short version earns about 10% a year with a Sharpe ratio near 1 net of costs (1997-2023), and gains are largest in high-VIX periods. A long-only version is feasible with free SPY/IEF/TLT daily closes as a conditioning filter. Leveraged ETF rebalancing is a real end-of-day effect, but it is intraday, shrinking, and reverses overnight, so it does not fit a daily-bar bot.

### Cited Findings
- Harvey, Mazzoleni & Melone, "The Unintended Consequences of Rebalancing" (NBER w33554, Mar 2025, revised Jan 14, 2026; AFA 2026): the paper simulates 60/40 S&P 500 / 10-year Treasury weight deviations from daily futures returns, 1997-2023. A one-standard-deviation rise in the Threshold (Calendar) signal predicts about -16 bps (-17 bps) for equities and about +4 bps (+2 bps) for bonds over the next trading day. Pressure reverts almost entirely within two weeks. — [EDHEC-hosted paper PDF](https://www.edhec.edu/sites/default/files/2026-03/Scientific%20paper.%20ssrn-5122748.pdf); [NBER w33554](https://www.nber.org/papers/w33554)
- Same paper: Calendar-signal predictability is concentrated at month-end and absent at other times. Both signals strengthen as quarter-end approaches. Appendix: weeks 1 and 3 show positive coefficients, consistent with reversal. — [EDHEC-hosted paper PDF](https://www.edhec.edu/sites/default/files/2026-03/Scientific%20paper.%20ssrn-5122748.pdf)
- Same paper, strategy: long S&P 500 futures / short 10-year note futures, or the reverse, on the signals. Annualized return is about 10% with a Sharpe ratio above 1, versus 0.35 for equities and 0.48 for bonds over the sample. The net-of-cost Sharpe ratio stays close to 1 (Harvey et al. 2018 cost assumptions). Skewness is 5.23, and the 2008-09 GFC and March 2020 are major contributors, though the Sharpe ratio is still 0.90 after excluding them. The signals became significant in the early 2000s and work for large and small caps, not value/growth. — [EDHEC-hosted paper PDF](https://www.edhec.edu/sites/default/files/2026-03/Scientific%20paper.%20ssrn-5122748.pdf)
- Same paper, Table 2 Panel C: strategy returns are about 14.85% a year in high-VIX periods vs 5.55% in low-VIX periods, and similarly split for high/low idiosyncratic volatility, liquidity risk, MOVE and EPU. — [EDHEC-hosted paper PDF](https://www.edhec.edu/sites/default/files/2026-03/Scientific%20paper.%20ssrn-5122748.pdf)
- Same paper: estimated cost to rebalancers is above 8 bps a year, about $16 billion a year. Randomizing rebalancing dates largely removes it. — [EDHEC-hosted paper PDF](https://www.edhec.edu/sites/default/files/2026-03/Scientific%20paper.%20ssrn-5122748.pdf); [Duke Fuqua Insights](https://www.fuqua.duke.edu/duke-fuqua-insights/what-do-pensions-lose-from-rebalancing)
- Industry pushback: L&G Investment Management argues rebalancing has real utility (tracking error and cash-outflow needs). This disputes the cost framing, not the price pattern. — [L&G rebuttal, Oct 2025](https://am.landg.us.com/insights/insights-blog/a-rebuttal-of-rebalancing-downsides)
- Leveraged ETFs: Barbon, Beckmeyer, Buraschi & Moerke find LETF rebalancing causes end-of-day momentum and then mean reversion. LETF effects have decreased over time while option-gamma effects persisted, and LETF flows are perfectly predictable from the day's return. — [SSRN 3925725](https://ssrn.com/abstract=3925725); [FoFI 2022 version](https://wp.lancs.ac.uk/fofi2022/files/2022/08/FoFI-2022-027-Mathis-Moerke.pdf)
- Leveraged VIX products: late-day price impact from predictable rebalancing flows is completely reversed overnight. — [J. Banking & Finance, ScienceDirect S0378426621002363](https://www.sciencedirect.com/science/article/abs/pii/S0378426621002363)
- LETF trades run mostly in the closing auction, and the drag accrues to liquidity providers (Korean LETF working paper, 2026; not verified beyond the abstract). — [arXiv 2608.03703](https://arxiv.org/pdf/2608.03703)

### Inferences
- Long-only version (free data): in the last 3-5 sessions of each month, compute SPY vs IEF (or TLT) return since the prior month-end, or since quarter-end for the stronger quarter-end case. If stocks have outrun bonds by a lot (stocks overweight), skip or delay new SPY/large-cap buys until the first 1-3 sessions of the new month. If stocks have lagged (underweight, so rebalancers buy equities), allow or size up entries into month-end. This also refines the [-3,+3] turn-of-month window from the earlier research.
- A long-only switch into IEF/TLT when stocks are overweight captures the bond leg. That is still long-only, but the bond leg in the paper is small (+2 to +4 bps per one-standard-deviation signal).
- Per-event magnitude: about 17 bps per one-standard-deviation signal on the next day. Large signals (2-3 standard deviations, after big monthly divergences) imply roughly 35-50 bps, linear extrapolation that is my assumption. Costs on SPY/IEF are 1-2 bps, so it survives costs on ETFs. The paper's own net Sharpe ratio is about 1.
- The gains concentrate in high-volatility months, as with the 2026-11-07 finding that pre-FOMC drift survives only in high VIX. A VIX-conditioned version is consistent across both.
- Decay risk: published in 2025-26 and widely covered (Bloomberg, CFA Institute). Post-publication decay is likely (McLean-Pontiff logic, as Greenwood-Sammon note for index effects).
- Evidence rate: 12 month-ends a year (4 quarter-ends). With about 1.2% daily standard deviation for SPY minus IEF (my assumption), detecting 17 bps at t=2 needs about 200 month-ends, which is far too slow to judge forward. Rely instead on a free replication: yfinance SPY and IEF daily since 2002, about 285 month-ends, about 130 of them since 2015.
- LETF rebalancing: the closing-auction push and overnight reversal look like the overnight bounce after big down days that the RSI(2)/IBS strategies already harvest. A daily-bar bot using next-open fills cannot take the last-30-minute leg. Skip.

### Gaps
- The paper reports no separate 2015-2023 subsample statistic that I could find. The post-2015 share of the strategy's return is not quantified beyond the cumulative-return figure.
- No paper gives day-by-day bps for "last N days of month" in large-cap ETFs specifically. The coefficients are for futures, stocks minus bonds.
- No bps magnitude was found for LETF end-of-day pressure in large caps after 2015. Drafts report the effect relative to the average last-half-hour return, and the figures vary across versions.

## 5. Other macro-calendar effects: Treasury auctions, pre-holiday returns, the FOMC even-week cycle

### Takeaway
The FOMC even-week cycle has failed out of sample: it was flat or reversed in 2017-2023, so drop it. Pre-holiday returns have faded since the late 1990s and are too rare (about 9 a year) to judge. Treasury auction cycles are a bond-market effect of a few bps, with no documented equity spillover. It could apply only to IEF/TLT and is marginal after spreads.

### Cited Findings
- Cieslak, Morse & Vissing-Jorgensen (JF 2019): since 1994 the equity premium is earned entirely in even weeks (0, 2, 4, 6) of FOMC cycle time. — [Wiley JF](https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12818); [SSRN 2687614](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2687614)
- Uppal, "Does the FOMC Cycle Still Drive Stock Returns?" (Imperial College working paper, March 2025): the result does not hold out of sample and weakens as early as 2004. In-sample, average excess return per day was 13.6 bps higher in week 0 and 9.9 bps higher in weeks 2/4/6 than in odd weeks. In 2017-2023 (1,824 daily observations), the week 0 dummy is negative (insignificant) and the weeks 2/4/6 dummy is indistinguishable from zero. A combined 2014-2023 test also fails. The proposed leak mechanism (biweekly board meetings) stopped after 2004, and pre-2004 outliers drive the result. — [Uppal 2025 PDF](https://aliuppal.me/files/papers/Ali_Uppal_CB_Cycles.pdf)
- A 2025 Annual Review of Financial Economics article reports a statistically significant opposite cycle in 2017-2021, with lower returns in even weeks (from the search abstract; the page returned 403). — [Annual Reviews](https://www.annualreviews.org/content/journals/10.1146/annurev-financial-090524-120826)
- Lou, Yan & Zhang (RFS 2013): Treasury prices fall significantly in the few days before auctions and recover shortly after, even though timing and size are pre-announced. Sample 1980-2008. — [LSE Research Online](https://researchonline.lse.ac.uk/id/eprint/46329)
- Follow-ups: 10-year TIPS show an auction cycle of about 6.17 bps (2005-2017; EFMA 2020 paper). — [EFMA 2020 paper](https://www.efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2021-Leeds/papers/EFMA%202020_stage-1301_question-Full%20Paper_id-406.pdf). Somogyi, Wallen & Xu (2025) report "no inverse V-shaped pattern in long-term G10 yields over Treasury auctions", which suggests the effect is transitory or absent in their sample. — [Darden PDF](https://finance.darden.virginia.edu/wp-content/uploads/2025/12/Somogyi_Wallen_Xu_2025_What_Treasury_Auctions_Reveal_About_Investor_Demand.pdf). A New York Fed staff report gives 33 years of intraday evidence on auction-day price pressure. — [NY Fed SR1188](https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr1188.pdf)
- Equity spillover: Collin-Dufresne's discussion says equity-market price effects around auction dates "seem hard to reconcile" with issuance price pressure. I found no follow-up study of equities. — [Treasury conference discussion](https://home.treasury.gov/system/files/276/2011-session-2-yan-shocks-in-liquid-market-DISCUSSION-collin-dufresne.pdf)
- Pre-holiday: the US effect declined significantly in the late 1990s (international decline-and-reversal study), and Keef & Roush (2005) reportedly found it severely diminished after 1987. — [ResearchGate, Pre-holiday effects: decline and reversal](https://www.researchgate.net/publication/222245676_Pre-holiday_effects_International_evidence_on_the_decline_and_reversal_of_a_stock_market_anomaly)
- Pre-holiday, 2015 onward: a 2015-2025 study finds the pooled holiday effect for the S&P 500 statistically insignificant, with only a volatility effect before Christmas. — [ResearchGate 2025](https://www.researchgate.net/publication/396814315_The_Influence_of_Holidays_on_the_Yield_and_Volatility_of_United_States_Stocks). A 2026 Review of Financial Economics paper (Albertson; 2014-2024 data) reports significant positive abnormal returns around "certain" federal holidays. I could not open the full text (403), so which holidays and how large is unknown. — [Wiley RFE 2026](https://onlinelibrary.wiley.com/doi/full/10.1002/rfe.70018)
- CXO Advisory (1950-2024 daily data, Labor Day): "noise generally dominates", and recent data do not support a return anomaly. Christmas and Thanksgiving pages show modest or fading patterns. — [CXO Labor Day](https://www.cxoadvisory.com/calendar-effects/stock-returns-around-labor-day/); [CXO Christmas](https://www.cxoadvisory.com/calendar-effects/stock-returns-around-christmas/); [CXO Thanksgiving](https://www.cxoadvisory.com/calendar-effects/stock-returns-around-thanksgiving/)

### Inferences
- FOMC even weeks: do not implement. The bot's existing pre-FOMC blackout and the VIX-conditioned pre-FOMC drift from earlier research are the only FOMC-calendar uses with any post-2015 support.
- Treasury auctions: a long-only IEF/TLT "buy at the auction-day close, hold 1-3 days" version is technically possible. But the documented size is a few bps of yield (about 6 bps for 10-year TIPS; nominal Treasury price figures not retrieved), against 1-2 bps ETF spreads, and Somogyi-Wallen-Xu question whether it persists. The bot is an equity book, so this is low priority.
- Pre-holiday: about 9-10 events a year. With about 1.1% daily SPY standard deviation and a 10-20 bps effect (assumed), detection needs about 120-480 events, decades of forward data. A one-off yfinance SPY 2015-2026 check (about 100 events) is cheap, but expect noise.

### Gaps
- No bps figure for the nominal Treasury pre-auction price dip in post-2015 data was retrieved. The Lou-Yan-Zhang magnitudes were not read directly.
- The Albertson (2026) holiday results were not accessible.

## 6. Dividend effects in large caps: ex-dividend day drop and the dividend-month premium

### Takeaway
Ex-day dividend capture is dead after costs in the S&P 500 (2008-2025 out-of-sample: -1.4% to -1.9% a year net). The dividend-month premium (Hartzmark & Solomon) survives post-publication abroad (Germany 2003-2017). The one US large-cap post-2008 test, a practitioner backtest, shows the week before the ex-date beating SPY by about 10 bps per event, with a 95% interval spanning zero. It is weak but cheap to test, and events are frequent.

### Cited Findings
- Hartzmark & Solomon, "The Dividend Month Premium" (JFE 2013; US stocks 1927-2009): abnormal returns are high in months when a dividend is predicted, relative both to other firms and to the same payers in non-dividend months. This is consistent with price pressure from dividend-seeking investors and makes risk explanations unlikely. — [ScienceDirect JFE](https://www.sciencedirect.com/science/article/abs/pii/S0304405X13000585); [SSRN 1930620](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1930620)
- Post-publication, Germany (J. Asset Management 2021; April 2003-December 2017): the strategy stays profitable, with one-factor alphas of 0.13% to 0.47% a month. A related German result reports the premium turning negative when rates rise. — [Springer JAM 2021](https://link.springer.com/article/10.1057/s41260-021-00215-3)
- Nordics: firms earn significantly higher returns in dividend-payment months (Aalto thesis). — [Aalto](https://aaltodoc.aalto.fi/handle/123456789/41312)
- QuanterLab (Girgin; practitioner backtest, not peer-reviewed). Universe: point-in-time S&P 500, with a cash-dividend census for 2005-2026 of 28,964 payer events across 547 names. Out-of-sample: 18 one-year windows, 2008-2025. Median ex-date drop ratio is 0.81 for all payers (0.95 market-adjusted). Overnight dividend capture loses: high-yield list -1.88% a year net (+0.15% gross), long-record list -1.42% a year net (+0.43% gross). — [QuanterLab](https://quanterlab.com/research/dividend-capture-loses-the-week-before-the-ex-date-pays)
- Same source, week before the ex-date: gross returns per event are 0.32% (high yield) and 0.29% (long record), vs about 0.18% for an average week of the same stocks. Against SPY over the same five days, the high-yield run-up is 0.10% per event (95% interval -0.02% to +0.23%). Stated caveats: costs from a dollar-volume bps proxy, no taxes, 117 delisted names unpriced (survivorship), and in 13 of 18 windows the paired comparison favoured the unselected arm. — [QuanterLab](https://quanterlab.com/research/dividend-capture-loses-the-week-before-the-ex-date-pays)
- Theory: the Elton-Gruber clientele model predicts drop ratios below 1 when dividends are taxed above capital gains, and tick discreteness keeps the drop within a tick of the dividend. — [Applied Economics 2022 (cum-ex)](https://www.tandfonline.com/doi/full/10.1080/00036846.2022.2141450); [JFE 1997 discreteness](https://www.sciencedirect.com/science/article/pii/S0304405X9700041X)

### Inferences
- Ex-day capture: do not implement. The roughly 5% market-adjusted shortfall of the drop vs the dividend is smaller than spread plus overnight risk for large caps, and it is a tax clientele effect that does not exist for a paper account anyway.
- Week-before-ex-date (dividend-month premium, short window): about 10 bps per event over SPY gross, against about 3-10 bps round trip for single names. Net it is about zero to marginally positive. Implementable long-only with free data (yfinance `Ticker.dividends` history for past ex-dates; announced next ex-dates via yfinance calendar or Nasdaq's dividend calendar).
- It could also work as an entry tilt rather than a standalone trade: among the bot's existing long signals, prefer names with an ex-date in the next 5-10 sessions. That costs nothing extra.
- Evidence rate is the best in this set: about 1,380 S&P 500 dividend events a year (28,964 over about 21 years). Assuming about 2.5% standard deviation for 5-day market-adjusted returns, detecting 10 bps at t=2 needs about 2,500 events, about 2 years of all payers. A shadow log could give a usable read in 12-24 months. Backfilling 2015-2026 from yfinance is faster, but survivorship needs care.

### Gaps
- I did not retrieve the original Hartzmark-Solomon US magnitude (bps a month) or any peer-reviewed US post-2015 replication.
- Not verified: whether Alpaca paper trading credits cash dividends to the paper account. This matters for how any dividend-window trade's P&L would be booked. Check Alpaca docs and account activities.

## 7. Evidence rate: which events give enough occurrences to judge forward within months

### Takeaway
Only per-stock events that repeat across the S&P 500 (earnings dates, about 2,000 a year; dividend ex-dates, about 1,400 a year) give enough occurrences to judge forward in 1-2 years. Market-wide calendar events (month-ends, OPEX, holidays, FOMC cycles, index batches) come 4-30 times a year and need 10-25 years to show a 10-30 bps effect. Judge those on free historical replications (yfinance since the 1990s-2000s), not forward paper P&L.

### Cited Findings
- S&P 500 dividend census: 28,964 payer events over 2005-2026, about 1,380 a year. — [QuanterLab](https://quanterlab.com/research/dividend-capture-loses-the-week-before-the-ex-date-pays)
- Uppal's 2017-2023 out-of-sample FOMC test used 1,824 daily observations, showing the scale needed to reject even a 10-14 bps a day effect. — [Uppal 2025](https://aliuppal.me/files/papers/Ali_Uppal_CB_Cycles.pdf)
- S&P 500 change batches are quarterly, with about 2-3 adds each plus ad hoc changes. — [S&P DJI press releases](https://press.spglobal.com/2024-06-07-KKR,-CrowdStrike-Holdings-and-GoDaddy-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600)

### Inferences
Rough events needed for t=2, N = (2 x sigma / effect)^2, with my assumed per-event standard deviations:

| Effect | Events/yr | Effect per event (source) | Assumed sigma per event | Events for t=2 | Years forward |
|---|---|---|---|---|---|
| Earnings premium (S&P 500, 1-2 days) | ~2,000 | ~15 bps if alive (Liu); ~0 post-2004 (Heitz) | 5% | ~4,400 | ~2 |
| Week before ex-dividend (S&P 500) | ~1,380 | ~10 bps vs SPY (QuanterLab) | 2.5% | ~2,500 | ~2 |
| S&P 500 additions (next open to effective) | ~20-30 | ~0 to 1% (Greenwood-Sammon) | 6% | ~150+ | 5-7+ |
| Month-end rebalancing signal (SPY vs IEF) | 12 | ~17 bps per 1 SD (HMM) | 1.2% | ~200 | ~17 |
| OPEX week (SPY) | 12 | ~20-33 bps (pre-2008 / practitioner) | 2.2% | ~180-480 | 15-40 |
| Pre-holiday (SPY) | ~9 | ~10-20 bps (faded) | 1.1% | ~120-480 | 13-50 |
| FOMC even weeks | ~8 cycles | ~0 or negative post-2016 | n/a | n/a | dead |
| Treasury auction cycle (IEF/TLT) | ~60+ coupon auctions | few bps of yield | n/a | n/a | marginal |

- Practical rule: per-stock event families can run as forward shadow tests. Market-calendar families should be accepted or rejected on 2002-2026 replications, with a hold-out from 2015, before any live capital or gate change.

### Gaps
- The sigma values are my assumptions, not measured. A first job is to measure them on the bot's own universe from yfinance.
- Cross-sectional clustering (many earnings or ex-dates on the same days) lowers the effective event count. It is not modelled here.

## 8. Free data sources for the calendars

### Takeaway
Every calendar needed is available free. S&P DJI press releases (index changes, after the close), yfinance (ex-dividend history and next ex-date, earnings dates), deterministic rules (OPEX third Fridays, month-ends, holidays from the exchange calendar), and the Treasury Fiscal Data API (auction schedule and history, no key, checked live today) cover them all.

### Cited Findings
- S&P 500 changes: S&P DJI press releases at press.spglobal.com, titled "... Set to Join S&P 500 ...", give the effective date (before the open) and note that index trades happen at the prior close. Batches come about 2-3 weeks ahead of the quarterly effective date, and ad hoc changes have their own dates. — [S&P DJI June 2026 release](https://press.spglobal.com/2026-06-05-Marvell-Technology-and-Flex-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400-and-S-P-SmallCap-600); [S&P DJI June 2022 release](https://press.spglobal.com/2022-06-03-Keurig-Dr-Pepper,-VICI-Properties-and-ON-Semiconductor-Set-to-Join-S-P-500-Others-to-Join-S-P-MidCap-400,-and-S-P-SmallCap-600)
- Greenwood-Sammon took historical add/delete announcement and effective dates from Siblis Research (a commercial/paid source), which shows clean history is not free. — [NBER w30748 PDF](https://www.nber.org/system/files/working_papers/w30748/w30748.pdf)
- Treasury auctions: `https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/upcoming_auctions` (announced upcoming auctions with announcement, auction and issue dates and offering amounts) and `.../v1/accounting/od/auctions_query` (history; 11,154 records; latest returned auction 2026-10-22). Both returned JSON without an API key when queried on 2026-11-07. Third-party guides warn that endpoint paths change, so check the API Quick Guide. TreasuryDirect also publishes an upcoming-auctions feed, typically about a week ahead. — live query of the [Fiscal Data API](https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query); [data.gov upcoming auctions](https://catalog-old.data.gov/dataset/upcoming-auctions); [TFTC methodology note](https://www.tftc.io/treasury-auctions/methodology)
- Option expiry: standard monthly equity and index options expire on the third Friday, and the quarterly (March/June/September/December) ones are "triple witching". — [Quantpedia OPEX strategy page](https://quantpedia.com/strategies/option-expiration-week-effect); [CXO Advisory](https://cxoadvisory.com/calendar-effects/option-expiration-week-stock-return-drill-down)

### Inferences
- Ex-dividend calendars: yfinance `Ticker.dividends` (historical ex-dates and amounts) plus the next declared ex-date from yfinance's calendar or info fields, or Nasdaq's public dividend calendar page. These are free but unofficial, so cache and cross-check. (Not verified live in this pass.)
- OPEX dates need no data feed: compute the third Friday and roll back one day when it is an NYSE holiday (e.g., Good Friday). The bot's market calendar can supply holidays.
- S&P change announcements are free but need a small scraper of press.spglobal.com or the S&P DJI index-announcements page, polled after 17:00 ET. That is timely for a next-open trade.

### Gaps
- yfinance's coverage of forward ex-dividend dates and of historical earnings dates (how many years back) was not verified in this pass.
- Did not confirm whether S&P DJI offers an RSS feed for index announcements.

## 9. Overall ranking (long-only primary, free data, post-2015 evidence, net of costs)

### Takeaway
Nothing in this set is proven to make money after costs in post-2015 US large caps. The best candidates are (1) a month-end rebalancing-pressure filter on SPY/large-cap entries, (2) a pre-ex-dividend entry tilt, and (3) a shadow test of the earnings premium the blackout currently blocks. Index additions, OPEX, pre-holiday, Treasury auctions, LETF flows, ex-day capture and FOMC even weeks should be dropped or kept only as risk flags.

### Cited Findings
- Month-end rebalancing: Sharpe ratio about 1 net (futures long-short, 1997-2023), -17 bps next day per one-standard-deviation signal, strongest in high VIX. — [HMM paper](https://www.edhec.edu/sites/default/files/2026-03/Scientific%20paper.%20ssrn-5122748.pdf)
- Week before ex-date: +10 bps vs SPY per event, 95% interval -2 to +23 bps, S&P 500 2008-2025 (practitioner). — [QuanterLab](https://quanterlab.com/research/dividend-capture-loses-the-week-before-the-ex-date-pays)
- Earnings premium: US premium about zero after 2004. — [Heitz et al. SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3296537)
- Index effect: about 0.8% / -0.6% (insignificant) in 2010-2020, and migrations negative. — [Greenwood & Sammon](https://www.nber.org/system/files/working_papers/w30748/w30748.pdf)
- FOMC even weeks: fail in 2017-2023. — [Uppal 2025](https://aliuppal.me/files/papers/Ali_Uppal_CB_Cycles.pdf)
- Ex-day capture: -1.4% to -1.9% a year net. — [QuanterLab](https://quanterlab.com/research/dividend-capture-loses-the-week-before-the-ex-date-pays)

### Inferences
1. **Month-end/quarter-end rebalancing-pressure filter (SPY vs IEF/TLT month-to-date).** Best published post-2015-inclusive evidence, works on ETFs with 1-2 bps costs, free data, 12 events a year. Use it as a gate or sizing input on SPY/large-cap entries in the last days of the month, ideally VIX-conditioned. Risks: post-publication decay, crisis-concentrated returns, and the long-only version captures only part of the long-short spread. Next step: replicate on yfinance SPY/IEF 2002-2026 with a 2015+ hold-out.
2. **Pre-ex-dividend tilt (entries 5 sessions before the ex-date).** Weak but positive post-2008 large-cap estimate (about 10 bps vs SPY, not significant), the highest evidence rate (about 1,400 events a year), and free data. Use it as a tie-breaker tilt inside existing signals, plus a shadow log.
3. **Earnings premium shadow test.** The blackout blocks it, but US evidence says it is gone. Keep the 10-session blackout and log pre-report returns to test it. This resolves in about 2 years forward or faster with a backfill.
4. **Treasury auction cycle (IEF/TLT only).** A real but small, bond-only effect, of questionable persistence. Low priority for an equity book.
5. **OPEX week / triple witching.** Pre-2008 large-cap evidence, faded since, poor on expiration Friday, 0DTE-era unknown. Backtest only.
6. **Pre-holiday.** Faded, about 9 events a year. Backtest only, expect noise.
7. **S&P 500 additions/deletions.** No long-only edge after the overnight pop. At most a short-pilot shadow on effective-date fades and a "do not chase" news flag.
8. **LETF end-of-day rebalancing.** Intraday, shrinking, reverses overnight, and duplicates the existing RSI(2)/IBS bounce logic. Skip.
9. **Ex-dividend day capture.** Negative net. Skip.
10. **FOMC even-week cycle.** Failed or reversed out of sample. Skip.

### Gaps
- None of the top three has a peer-reviewed, large-cap, long-only, post-2015 net-of-cost estimate. All three need the bot's own replication before any live change.
- The interaction between the month-end rebalancing filter and the existing [-3,+3] turn-of-month window, the SPY-trend market gate and the VIX gate is untested.
