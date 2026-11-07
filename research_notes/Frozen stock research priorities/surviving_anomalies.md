# Surviving Stock-Return Anomalies: Post-Publication, Post-2015, Net of Costs, Large-Cap US, 1-Day to 1-Month Horizons

Research notes for Frozen_stock (long-only, Alpaca paper, yfinance daily OHLCV, PIT S&P 500 universe + index/sector ETFs, 1-20 session holds). Compiled 2026-11-07. 19 tool calls. Sources were read mainly through abstracts, search snippets and secondary summaries; few full PDFs were opened (flagged where it matters).

---

## 1. Open Source Asset Pricing (Chen & Zimmermann): post-publication and recent returns, large caps, data access and licence

### Takeaway
OSAP replicates essentially the whole cross-sectional predictor zoo (319 characteristics in the CFR paper). Its own documentation and companion papers show large post-publication decay. Weighting matters a great deal: a majority of anomalies fail a simple t >= 1.96 test once they are value-weighted with NYSE breakpoints, which is the setting closest to "liquid large caps". The data is publicly downloadable (Hugging Face, Python and R packages) with a request to cite. I found no explicit licence.

### Cited Findings
- Chen & Zimmermann, "Open Source Cross-Sectional Asset Pricing", Critical Finance Review 2022 vol. 11 issue 2, releases data and code reproducing nearly all cross-sectional predictors, drawing on 319 characteristics. — [IDEAS/RePEc](https://ideas.repec.org/a/now/jnlcfr/104.00000112.html); [SSRN](https://papers.ssrn.com/abstract=3604626); [Fed FEDS page](https://www.federalreserve.gov/econres/feds/open-source-cross-sectional-asset-pricing.htm)
- Large-cap and weighting sensitivity. Under NYSE breakpoints with value-weighted returns, 65.4% of anomalies fail |t| >= 1.96. With NYSE-AMEX-NASDAQ breakpoints and equal weights ("microcaps run amok"), the failure rate falls to 43.1%. The search summary attributes this to the OSAP paper's discussion; the figures are the Hou-Xue-Zhang-style replication numbers that OSAP engages with, and the primary attribution should be checked. — [Chen & Zimmermann paper PDF, CFR](https://cfr.ivo-welch.info/published/papers/chen2021open.pdf)
- Chen & Zimmermann's earlier publication-bias paper estimates that publication-bias-adjusted returns are only about 12% smaller than in-sample returns. It concludes that bias is too small to explain post-publication deterioration, which points to mispricing that is arbitraged away. — [Fed FEDS 2018-033](https://www.federalreserve.gov/econres/feds/files/2018033pap.pdf)
- Examples of decay in the OSAP data documentation: the accruals post-publication t-stat fell from 2.37 to 1.43 after a definition change; coskewness post-publication t-stats fell from about 1.6 to about 1.2. — [OSAP documentation (Google Doc export)](https://docs.google.com/document/d/1sV6CE71tlliLJJFpXINMVVQzU_7I77VX/export?format=pdf)
- Data access. openassetpricing.com offers "test asset returns and signals replicated from the academic asset pricing literature", downloadable through Hugging Face, a Python package or an R package. It asks users to "cite our paper". The page states no licence or terms of use and no price. The latest release is the October 2026 data update (dated 2026-10-28), which migrates to CRSP v2 (CIZ). — [openassetpricing.com](https://www.openassetpricing.com/)
- Code and portfolio files are also on GitHub (OpenSourceAP/CrossSection) and the Fed page. — [Fed FEDS page](https://www.federalreserve.gov/econres/feds/open-source-cross-sectional-asset-pricing.htm)

### Inferences
- Signals built from CRSP/Compustat (most OSAP predictors) cannot be reconstructed for live trading from free data. OSAP's free *portfolio return* files can still serve as an offline research oracle: check whether a candidate theme (for example short-term reversal, seasonality or industry momentum) has held up post-publication and post-2015 before Frozen_stock spends trials on it. This also cuts the deflated-Sharpe penalty.
- Because many signals are derived from CRSP, the **signal-level** files may carry WRDS redistribution constraints even though the portfolio returns are public. Treat redistribution and commercial use as unresolved until the licence is confirmed.

### Gaps
- I did not retrieve an OSAP table of post-publication or post-2015 returns split by size (for example a "large-cap only" or "price > $5, top-NYSE-quintile" variant). I could not confirm which predictors are strongest among large caps. The OSAP portfolio files include alternative implementations (VW, NYSE breakpoints, ex-price-<$5 and similar), which would answer this directly with a download.
- No explicit licence text was found (the GitHub repo licence file was not checked).

---

## 2. McLean & Pontiff, Hou-Xue-Zhang, Jensen-Kelly-Pedersen: decay and which themes survive by size group

### Takeaway
Post-publication decay is real and large: about 50% on average, and about 72% when restricted to post-2005 data. JKP (2023, JF) argue most factor *themes* still replicate (about 82-85% under their Bayesian method). Seasonality, Leverage and Size are the weak themes, and JKP report replication holds across the size distribution. HXZ-style value-weighted replications are much harsher. For a large-cap, long-only, 1-20 day trader, the cross-sectional factor zoo is mostly irrelevant or cost-dominated.

### Cited Findings
- Chen & Velikov replicate McLean-Pontiff and find gross anomaly returns decay by roughly 50% post-publication. Restricting to post-2005 data raises the decay to 72%. — [Chen & Velikov, Fed FEDS 2020-039](https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf); [JFQA 2023](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/zeroing-in-on-the-expected-returns-of-anomalies/945133D5A3ECEEAF466AEE91551FD225)
- Jensen, Kelly & Pedersen, "Is There a Replication Crisis in Finance?", Journal of Finance 78(5), 2465-2518 (2023). Bayesian model over 153 factors in 93 countries, grouped into 13 themes: Accruals, Debt Issuance, Investment, Leverage, Low Risk, Momentum, Profit Growth, Profitability, Quality, Seasonality, Size, Skewness, Value. — [Alpha Architect summary](https://alphaarchitect.com/is-there-a-replication-crisis-in-finance/); [NBER w28432](https://nber.org/system/files/working_papers/w28432/w28432.pdf)
- JKP: the replication rate exceeds 75% in 10 of 13 themes. The exceptions are **Seasonality, Leverage and Size**. — [Alpha Architect](https://alphaarchitect.com/is-there-a-replication-crisis-in-finance/)
- Reported replication rates differ across summaries: 84.9% of 153 factors ([CXO Advisory](https://www.cxoadvisory.com/big-ideas/re-examining-equity-factor-research-replicability/)), about 85% versus CAPM, and 82.4% Bayesian in other summaries ([Alpha Architect](https://alphaarchitect.com/is-there-a-replication-crisis-in-finance/)). The differences likely reflect paper versions or definitions.
- JKP report that factors replicate across the size distribution, so size and liquidity critiques look largely unfounded. Their factors use tercile sorts with breakpoints from **non-micro** stocks and capped value weights. CXO cautions that de-emphasising the largest stocks may not represent real investor experience. — [CXO Advisory](https://www.cxoadvisory.com/big-ideas/re-examining-equity-factor-research-replicability/); [Alpha Architect](https://alphaarchitect.com/is-there-a-replication-crisis-in-finance/)
- JKP data: the R package `tidyfinance::download_data_jkp` exposes "factors", "portfolios" (low/mid/high legs), "industry", "nyse_cutoffs" and "return_cutoffs". The documentation PDF is at jkpfactors.s3.amazonaws.com/documents/Documentation.pdf (not opened). One guide notes access "may require registration". — [rdrr.io tidyfinance docs](https://rdrr.io/cran/tidyfinance/man/download_data_jkp.html); [quinfer datasets guide](https://quinfer.github.io/financial-data-science/resources/datasets.html)
- Characteristic counts conflict: 153 factors in the paper versus "406 characteristics" quoted by a third-party comparison site (low reliability). — [findmymoat (low reliability)](https://www.findmymoat.com/vs/global-factor-data-jkp-factors-vs-portfolioslab)

### Inferences
- From prior knowledge, **not re-verified this session**: McLean & Pontiff (JF 2016, 97 predictors) report returns about 26% lower out-of-sample and about 58% lower post-publication. Hou, Xue & Zhang (RFS 2020) find about 65% of 452 anomalies fail |t| >= 1.96 under NYSE breakpoints and VW returns, with trading-frictions anomalies failing worst. The 65.4% VW/NYSE failure figure in Section 1 is consistent with this.
- JKP's surviving themes (Value, Momentum, Quality, Profitability, Low Risk, Investment) are monthly-to-annual-rebalance cross-sectional *long-short* factors. Frozen_stock captures them only as tilts on long-only entry filters, for example preferring high-quality or low-vol names when choosing among RSI(2) candidates. They are not standalone 1-20 day trades.
- JKP flag "Seasonality" as a weak theme. That directly lowers the prior for Heston-Sadka-type same-month seasonality (Section 3).

### Gaps
- I did not retrieve the JKP mega/large/small breakdown (the paper has a size-group figure; NBER w28432 should contain it). I did not open the JKP documentation PDF or confirm its terms of use.
- HXZ 2020 primary text was not opened this session.

---

## 3. Short-horizon effects (1 day to 1 month): post-2015 evidence, large-cap evidence, cost sensitivity

### Takeaway
The strongest candidates for Frozen_stock are (1) **industry-adjusted or residual short-term reversal** (revived in Blitz et al. 2023 with "no decay" through 2022, but about 2,400% annual turnover, gross of costs) and (2) **short-horizon mean reversion on liquid index/sector ETFs**, which is consistent with the documented collapse of short-term *trend* signals since about 2009 (Kurth, Eisler, Rej & Bouchaud 2026). Turn-of-month on SPY has faded post-2015 under the classic [0,+3] window, though a [-3,+3] window still shows about 5-12 bps/day. The pre-FOMC drift largely disappeared after 2015 except in high-uncertainty regimes. PEAD in non-microcap stocks has been essentially dead since about 2006. Overnight/intraday decomposition is a useful lens, but I found no post-2015 large-cap evidence.

### Cited Findings

**Short-term reversal (1-week / 1-month) vs industry momentum**
- Blitz, van der Grient & Honarvar (2023), "Reversing the Trend of Short-Term Reversal": generic STR (raw last-month return) is fading. Industry-adjusted STR (last-month return minus industry return) and residual STR (vol-scaled 3-factor residual) "more than doubled Generic STR's gross Sharpe ratios, with no decay". Sample: MSCI World stocks, Dec 1985-Dec 2022, about 1,745 stocks/yr, monthly quintile long-short, **gross of costs, about 2,400% annual turnover**. — [CXO Advisory summary](https://www.cxoadvisory.com/technical-trading/reviving-short-term-reversal/); [Robeco (author firm, marketing summary)](https://www.robeco.com/en-us/insights/2023/10/reversing-the-trend-of-short-term-reversal)
- Robeco: "enhanced reversal strategies offer double the risk-adjusted return and remain effective over time". This is the authors' firm's claim and should be checked against the SSRN paper. — [Robeco](https://robeco.com/en-int/insights/2023/10/reversing-the-trend-of-short-term-reversal)
- Da, Liu & Schaumburg (2012) decompose reversal into across-industry and within-industry parts on relatively large, liquid stocks (about 75% of US market cap, Jan 1982-Mar 2009). This is pre-2010 evidence. — [Da, Liu & Schaumburg PDF (NUS)](https://web2-bschool.nus.edu.sg/wp-content/uploads/media_rp/publications/BwXb81392625636.pdf)
- Over 1963-2006, industry-based STR beat market-wide STR (1.46% vs 1.05% per month). This is pre-2010 evidence. — [CXO Advisory](https://www.cxoadvisory.com/?p=15912)
- A Quantpedia/IBKR summary of a momentum-and-reversal study covering 2010-2020 says reversal remains highly significant. Sector and industry momentum disappeared at 12 months but "intensified" as 1-month reversals. Exact numbers were not retrieved. — [IBKR Campus / Quantpedia](https://www.interactivebrokers.com/campus/ibkr-quant-news/an-important-analysis-of-stock-momentum-and-reversal-factors/)
- STR is widely interpreted as compensation for supplying liquidity (Hameed, Kang & Viswanathan 2010; Nagel 2012). It may therefore not show up in broad VW indexes, and it shrinks as market structure lowers liquidity-provision rents. — [Columbia STR paper](https://business.columbia.edu/sites/default/files-efs/pubfiles/11568/str1.pdf)

**Short-horizon trend vs mean reversion on index/sector instruments**
- Kurth, Eisler, Rej & Bouchaud (arXiv 2607.01550, 2 July 2026), "Is Trend Still Your Friend?": "short-term trends have ceased to deliver reliable returns" since about 2009. Post-2008 trend PnL "has collapsed on small-tick contracts across all signal horizons", while large-tick contracts remain "essentially intact". The proposed mechanism is HFT liquidity withdrawal interacting with trend order flow. The abstract does not address mean reversion or name equity ETFs. — [arXiv](https://arxiv.org/abs/2607.01550)
- Time-series momentum / trend following (monthly to 12-month) underperformed in the 2010s. AQR (Babu et al.) attribute this to a lack of large risk-adjusted moves, not a loss of ability to profit from trends. Hutchinson & O'Brien find trend is weak for about 4 years after financial crises. — [Quantpedia](https://quantpedia.com/why-did-trend-following-underperform-in-the-last-decade/?a=6080); [ETF.com / Swedroe](https://www.etf.com/sections/index-investor-corner/swedroe-trend-following-works-weakest-after-crises); [Alpha Architect](https://alphaarchitect.com/2020/04/trend-following-reality-you-need-trends-to-trend-follow/)

**Overnight vs intraday ("tug of war")**
- Lou, Polk & Skouras (JFE 2019, 134:192-213): across 14 strategies, profits accrue either entirely overnight (momentum and a variety of others, plus reversal per the abstract summary) or entirely intraday, consistent with institutions trading intraday against momentum. — [LSE PDF](https://personal.lse.ac.uk/polk/research/TugOfWar.pdf); [RePEc](https://econpapers.repec.org/RePEc:eee:jfinec:v:134:y:2019:i:1:p:192-213)
- Follow-up: Lou, Polk & Skouras, "The Day Destroys the Night, Night Extends the Day" (2022 version), applies the clientele view to the equity premium. The 2024 work characterises overnight clientele as household-like and intraday clientele as institutional. — [NBER conference PDF](https://conference.nber.org/conf_papers/f78239/f78239.pdf)
- Momentum profits accrue overnight (CXO summary). — [CXO Advisory](https://www.cxoadvisory.com/calendar-effects/momentum-happens-at-night/)

**Turn of the month (TOM)**
- QuantSeeker (EODHD ETF data, full ETF histories): the classic [0,+3] TOM window shows no statistically significant effect on US equity ETFs in recent data. The effect "disappeared over the past decade"; QQQ's strong early-2000s effect faded to about zero; IWM and many sectors show an inverse effect. The [-3,+3] window still shows a significant effect of about **5-12 bps/day** above other days for most equity ETFs, though it is declining. A long-only US-ETF TOM strategy (enter close day -4, exit close day +3, 5 bp one-way cost) lowers CAGR with similar or lower Sharpe, but reduces drawdowns and improves skew. — [QuantSeeker](https://www.quantseeker.com/p/turn-of-the-month-strategies-do-they)
- Earlier literature: Chen et al. (1996-2012) found 15-20 bps/day in the TOM window versus about 0 otherwise (pre-2015). — [QuantSeeker](https://www.quantseeker.com/p/turn-of-the-month-strategies-do-they)
- Conflicting view: a 2022 Norwegian thesis (summarised by Harbourfront Quant) concludes the effect is still present in US equities. That sample ends around 2022. — [Harbourfront Quant](https://harbourfrontquant.substack.com/p/do-calendar-anomalies-still-work)

**Pre-FOMC announcement drift (Lucca & Moench 2015)**
- Kurov, Wolfe & Gilbert (Finance Research Letters 2021) extend the Lucca-Moench sample (Sep 1994-Mar 2011) through Dec 2019. The pre-FOMC drift "essentially disappeared after 2015", both with and without press conferences. — [Kurov et al. PDF](https://www.skidmore.edu/economics/documents/KurovWolfeGilbert-TheDisappearingPre-FOMC-Announce-Drift-200914.pdf); [IDEAS](https://ideas.repec.org/a/eee/finlet/v40y2021ics1544612320315956.html)
- Ben Dor & Rosa (2019) reportedly find no change after 2015 (as described by Kurov et al.). — [Kurov et al. PDF](https://www.skidmore.edu/economics/documents/KurovWolfeGilbert-TheDisappearingPre-FOMC-Announce-Drift-200914.pdf)
- Martello & Ribeiro: the drift is concentrated in high-uncertainty periods. It averages 109 bps when implied vol is above its prior median versus 9.7 bps below, and the moderation reflects time variation also present earlier. — [Quantpedia](https://quantpedia.com/fomc-equity-drift-occurs-in-periods-of-high-uncertainty/)
- Vissing-Jorgensen: excess returns are higher in even weeks of FOMC cycle time, holding in 2014-2016. — [Keynote slides](https://acfr.aut.ac.nz/__data/assets/pdf_file/0012/222024/Keynote-2-vissingjorgensen.pdf)

**Post-earnings-announcement drift (PEAD) in large caps**
- Martineau, "Rest in Peace Post-Earnings Announcement Drift" (CFR 2022): for large stocks, PEAD has been non-existent since 2006, and since 2006 analyst-based surprises fail to positively predict post-announcement returns. Microcap PEAD vanished only recently. — [CFR PDF](https://cfr.ivo-welch.info/published/papers/martineau2021rest.pdf); [Now Publishers](https://nowpublishers.com/article/Details/CFR-0122)
- Non-microcap drift began disappearing in 2001 and was gone by 2006, which Martineau attributes to decimalisation and 2005 regulation enabling HFT arbitrage. — [UCLA Anderson Review](https://anderson-review.ucla.edu/is-post-earnings-announcement-drift-a-thing-again)
- Subrahmanyam (2025 WP), Feb 2001-Dec 2024: the earnings-drift factor has t = 2.18 on all stocks but **t = 1.43 excluding microcaps** (bottom 20% NYSE cap, about 3% of market value). Microcaps drive the apparent drift. — [UCLA Anderson Review](https://anderson-review.ucla.edu/is-post-earnings-announcement-drift-a-thing-again)
- Dickerson, Julliard & Mueller (JFE, in press) argue earnings drift remains a major factor, using an announcement-return-based factor with no microcap filter. Hirshleifer, Peng & Wang (RFS 2025) report drift t of about 14, also unfiltered. — [UCLA Anderson Review](https://anderson-review.ucla.edu/is-post-earnings-announcement-drift-a-thing-again)

**Return seasonality (Heston & Sadka)**
- Heston & Sadka (JFE 2008), NYSE/AMEX 1963-2002: stocks with high returns in a given calendar month tend to have high returns in the same month in later years. — [UMD Smith](https://rhsmith.umd.edu/news/seasonality-stock-market-returns)
- Keloharju, Linnainmaa & Nyberg (JF), "Return Seasonalities": a same-calendar-month long-short earns about 13%/yr over 1963-2011. — [NBER w20815](https://www.nber.org/papers/w20815.pdf)
- Large-cap figure: 0.69%/month for the 12-months-ago sort, 1965-2002. This is pre-2010 evidence only. — [Quantpedia screener](https://quantpedia.com/Screener/Details/125)
- JKP classify Seasonality as one of 3 themes below 75% replication. — [Alpha Architect](https://alphaarchitect.com/is-there-a-replication-crisis-in-finance/)

### Inferences
- **Priority 1: residual or industry-adjusted short-term reversal within the S&P 500, long-only leg.** Buy names whose 1-5 day or 1-month return is low *relative to their sector ETF*, ideally in an uptrend. This is the closest documented survivor to Frozen_stock's existing "explore" candidates and needs only daily OHLCV plus sector mapping (SPDR sector ETFs as the industry proxy). The caveat is cost: 2,400%/yr turnover means net viability depends on spread and slippage. Alpaca has no commission, but S&P 500 half-spreads of a few bps times 2 legs times about 100 trades/yr matter against a +40-65 bps gross edge.
- **Priority 2: short-horizon mean reversion on index/sector ETFs.** Kurth et al.'s finding that short-term trend collapsed after 2009 indirectly supports fading short-term moves in liquid index instruments. This matches Frozen_stock's own +40-65 bps, about 70% win-rate explore results. No primary paper tested ETF reversion post-2015 in this session.
- **Priority 3: [-3,+3] turn-of-month as a sizing or entry-timing overlay** rather than a standalone strategy. At about 5-12 bps/day it is below costs as a solo trade but can tilt the timing of mean-reversion entries.
- **Conditional FOMC: test pre-FOMC long only when VIX is above its trailing median** (Martello-Ribeiro). Frozen_stock already has VIX and a Fed-calendar blackout (PR #12). The current BUY blackout around Fed events may be forgoing the one surviving sliver of this effect, which is worth one trial.
- **Avoid PEAD in S&P 500 names.** Large-cap evidence says it has been dead since about 2006, and the 2025 "revival" papers depend on microcaps. This matches Frozen_stock's own "PEAD blocked by survivorship" result.
- **De-prioritise Heston-Sadka seasonality.** All large-cap evidence is pre-2011, and JKP flag the theme as weak. It needs 10-20 years of per-stock history, which the PIT S&P 500 universe can supply, so it is cheap to test, but the prior is low.
- **Overnight/intraday:** yfinance daily Open/Close allows an overnight vs intraday split. One possible use is as a signal, for example buying names with strong overnight but weak intraday returns, since the institutional intraday tug of war fades momentum. Primary post-2015 large-cap tests were not found, so this is exploratory.

### Gaps
- No post-2015 primary evidence was retrieved for: **pre-holiday effect, earnings-announcement premium (buy before the announcement), 52-week-high (George-Hwang), MAX/lottery (Bali-Cakici-Whitelaw)**, or ETF-specific short-horizon mean reversion. These remain untested priors. From prior knowledge (not verified): MAX is a monthly long-short that is mainly short-leg and small-cap; 52-week-high is a 6-12 month horizon; the earnings-announcement premium (Frazzini-Lamont; Barber et al.) is a few-day long-only effect that would be implementable with free earnings dates, so a post-2015 large-cap check is worth doing.
- Blitz et al. 2023 exact numbers (Sharpe by variant, post-2010 subperiod, large-cap or MSCI-only, net returns) were behind paywalls. The SSRN PDF should be pulled.
- Lou-Polk-Skouras sample end dates and any post-2015 replication were not found.
- QuantSeeker's SPY-specific table values were in images and could not be read.

---

## 4. Net-of-cost estimates (Novy-Marx & Velikov 2016; Chen & Velikov 2023)

### Takeaway
Net of spreads, post-publication decay and modern-era trading, the average anomaly earns about 4 bps/month (Chen-Velikov). The best earn at most about 10 bps, about 20 bps combined, and most dispersion is luck. Novy-Marx-Velikov's rule: costs eat about 1% of monthly one-sided turnover, so anomalies with turnover above 50%/month rarely survive. Every 1-20 day strategy Frozen_stock runs is a high-turnover strategy, so edge must come from trade selection, not cross-sectional factor premia.

### Cited Findings
- Chen & Velikov (JFQA 2023, 58(3):968-1004), 204 anomalies: accounting for effective bid-ask spreads, post-publication effects and the post-early-2000s trading-technology era, the average anomaly's net expected return is "a measly 4 bps per month". An earlier 2020 WP reported 8 bps. The strongest anomalies net at most about 10 bps after data-mining controls, and combinations reach about 20 bps. — [JFQA](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/zeroing-in-on-the-expected-returns-of-anomalies/945133D5A3ECEEAF466AEE91551FD225); [Fed FEDS 2020-039](https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf); [RePEc](https://econpapers.repec.org/RePEc:cup:jfinqa:v:58:y:2023:i:3:p:968-1004_2)
- Sorting on in-sample net Sharpe, the top quartile averages 21 bps/month net in post-publication, post-2005 data. Most dispersion in net returns in that sample is due to luck. — [Fed FEDS 2020-039](https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf)
- Chen-Velikov estimates exclude price impact and shorting costs, so they are upper bounds. — [Fed FEDS 2020-039](https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf)
- Novy-Marx & Velikov (RFS 2016, 29(1):104-147): most anomalies with monthly one-sided turnover under 50% keep significant net spreads when designed to mitigate costs; few higher-turnover ones do. A buy/hold spread (stricter entry than retention thresholds) is the most effective mitigation. — [NBER w20721](https://www.nber.org/papers/w20721.pdf); [Rochester PDF](https://mysimon.rochester.edu/novy-marx/research/ToAatTC.pdf)
- Rule of thumb: transaction costs reduce VW long-short returns by about 1% of monthly one-sided turnover. Momentum costs about 48.4 bps/month versus about 5.5-5.7 bps for size and value. Size, value and profitability have the greatest capacity. — [Alpha Architect](https://alphaarchitect.com/2017/05/trading-costs-destroy-factor-investing/)

### Inferences
- These estimates are for institutional long-short portfolios across all stocks, including small caps. For a long-only retail account trading S&P 500 names and SPY/sector ETFs on Alpaca (no commission), effective spreads are much smaller (about 1-3 bps for SPY-class ETFs and mega caps, from general knowledge, not verified here). Short-horizon strategies with +40-65 bps gross per trade can therefore survive costs where academic STR long-short does not. Slippage from market-on-close and open fills should be modelled explicitly.
- A buy/hold hysteresis band (Novy-Marx-Velikov) maps directly onto Frozen_stock exit rules: enter at RSI(2) < 5, hold until RSI(2) > 70 rather than exiting at 50. This reduces round trips.

### Gaps
- No retail-scale (sub-$100k, zero-commission) net-of-cost study for these specific short-horizon effects was found.
- Novy-Marx-Velikov net-return tables for individual anomalies, notably STR and industry momentum, were not retrieved.

---

## 5. Implementability: free daily OHLCV plus earnings dates vs fundamentals (and free point-in-time fundamentals)

### Takeaway
The surviving short-horizon candidates (industry-adjusted reversal, ETF mean reversion, TOM, conditional pre-FOMC, overnight/intraday splits, seasonality) need only daily OHLCV, sector ETF mappings, a calendar, and VIX, all available free. PEAD and the earnings-announcement premium need earnings dates (free via yfinance or Alpaca news, quality varies). Quality, profitability and value tilts from JKP themes need point-in-time fundamentals. I did not verify SEC XBRL coverage or point-in-time properties in this session.

### Cited Findings
- Industry-adjusted STR is defined purely from returns (last-month return minus industry return), and residual STR from 3-factor residuals. Both are computable from price data plus factor and industry returns. — [CXO Advisory](https://www.cxoadvisory.com/technical-trading/reviving-short-term-reversal/)
- TOM tests in QuantSeeker use only daily ETF prices. — [QuantSeeker](https://www.quantseeker.com/p/turn-of-the-month-strategies-do-they)
- The FOMC conditional test uses implied volatility (VIX) splits. — [Quantpedia](https://quantpedia.com/fomc-equity-drift-occurs-in-periods-of-high-uncertainty/)
- JKP provide free factor and industry return files (useful as benchmarks or for residualising), via the tidyfinance R wrapper. — [rdrr.io](https://rdrr.io/cran/tidyfinance/man/download_data_jkp.html)

### Inferences
- **OHLCV only:** industry- or sector-relative 1-5 day reversal in S&P 500 names; RSI(2)/IBS on sector ETFs; [-3,+3] TOM overlay; overnight vs intraday return split (Open/Close); Heston-Sadka same-month seasonality; conditional pre-FOMC with a VIX filter (FOMC dates are public).
- **Earnings dates required:** earnings-announcement premium (buy 1-5 days before the report); PEAD (avoid for large caps).
- **Fundamentals required:** quality, profitability, value and investment tilts. Free option: SEC EDGAR XBRL "companyfacts"/"frames" APIs. From prior knowledge, not verified this session: these carry filing dates, which allows as-of reconstruction, but they start around 2009-2011 and need restatement handling. That caps fundamental backtests to about 2010+.
- Residualising against Fama-French daily factors (Ken French library, free) is a cheap upgrade over sector-ETF adjustment.

### Gaps
- SEC XBRL point-in-time suitability, coverage start and restatement behaviour were not researched in this session.
- Quality of yfinance historical earnings dates (survivorship, missing timestamps for before/after market) was not assessed.

### Summary priority lists (inference; synthesised from the findings above)

**Test first (documented survival or strong indirect support, OHLCV-only):**
1. Sector- or industry-adjusted short-term reversal in S&P 500 names, long-only leg, with a buy/hold hysteresis exit (Blitz et al. 2023; Novy-Marx-Velikov).
2. Short-horizon mean reversion on index/sector ETFs (Kurth et al. 2026, the inverse of the trend collapse; Frozen_stock's own explore results).
3. Pre-FOMC long conditional on high VIX (Martello-Ribeiro); revisit the Fed BUY blackout.
4. [-3,+3] TOM as a timing or sizing overlay (QuantSeeker).

**Exploratory (thin or pre-2015 evidence):** overnight-vs-intraday signals (Lou-Polk-Skouras); earnings-announcement premium in large caps; Heston-Sadka seasonality (pre-2011 large-cap evidence only; JKP flag the theme as weak).

**Avoid or deprioritise:** PEAD in non-microcaps (dead since about 2006; Martineau, Subrahmanyam); unconditional pre-FOMC drift (gone after 2015; Kurov et al.); classic [0,+3] TOM on SPY or IWM (faded or inverted); short-term trend or breakout on index products (Kurth et al.; 2010s TSMOM weakness); generic raw STR (fading; Blitz et al.); most monthly-rebalanced cross-sectional factors as standalone trades (Chen-Velikov net of about 4 bps/month on average).
