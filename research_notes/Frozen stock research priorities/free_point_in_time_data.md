# Free, point-in-time data sources with short-horizon predictive information for US large caps (Frozen_stock)

Scope: free sources beyond price/volume/news headlines; long-only, ~45 liquid large caps/ETFs, 1-20 session holds, rule language + meta-label classifier. Research as of 2026-11-07. About 18 search/fetch calls; several sub-questions remain open (see Gaps). Anything not backed by a fetched source is labelled as an inference or a gap.

## Q1. SEC EDGAR Form 4 insider transactions (access, point-in-time handling, evidence)

### Takeaway
EDGAR is free and point-in-time if you key on the acceptance/filing timestamp (Form 3/4/5 filings accepted up to 10:00 p.m. ET are disseminated the same day), under a 10 requests/second fair-access cap with a declared User-Agent. The best-known evidence, Cohen-Malloy-Pomorski (opportunistic insiders, 82 bps/month value-weighted), uses 1986-2007 data. I found no clean post-2010 large-cap replication, and older work finds the insider signal much weaker in large firms. Expected value for a long-only large-cap book is modest but real for opportunistic or cluster *buys*.

### Cited Findings
**Access and terms**
- SEC fair-access policy: current maximum is 10 requests/second; the SEC may lower limits; scripts must declare a User-Agent with company name and admin email; download only what you need; "botnets and automated tools are not permitted to crawl the site"; no SEC technical support. — [SEC: Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- Daily, full and quarterly index files cover filings from 1994Q3 (company, form type, CIK, date filed, file name). Daily archives sit in /Archives/edgar/Feed/ and /Archives/edgar/Oldloads/. Submissions and extracted XBRL data are served as JSON REST APIs on data.sec.gov. — [SEC: Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- Indexes covering the current business day are rebuilt nightly from about 10:00 p.m. ET. Filings submitted after 5:30 p.m. ET are disseminated the next business day, except Forms 3/4/5, whose cutoff is 10:00 p.m. ET. — [SEC: Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- The SEC page names no dedicated Form 4 dataset; Forms 3/4/5 appear only in the context of the dissemination cutoff. — [SEC: Accessing EDGAR Data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
- Parser: `edgartools` is listed on PyPI as MIT-licensed (v5.3x in 2026), with Form 3/4/5 parsing (`form4.transactions`, `to_dataframe()`, transaction history by insider). I did not inspect the repo's LICENSE file directly. — [PyPI edgartools 5.31.0](https://pypi.org/project/edgartools/5.31.0/); [socket.dev edgartools](https://socket.dev/pypi/package/edgartools)

**Evidence**
- Cohen, Malloy & Pomorski (JF 2012, 67(3):1009-1043): "routine" insiders trade in the same calendar month for several years and make up about 55% of insider trades; their trades carry essentially zero abnormal return. A portfolio of "opportunistic" trades only earns **82 bps/month value-weighted abnormal return**. The most informed are local, non-executive insiders at geographically concentrated, poorly governed firms. Sample: Thomson Reuters insider filings, Jan 1986 to Dec 2007. — [NBER Digest](https://www.nber.org/digest/apr11/w16454.html); [NBER WP 16454](https://www.nber.org/papers/w16454.pdf); [EconPapers JF listing](https://econpapers.repec.org/RePEc:bla:jfinan:v:67:y:2012:i:3:p:1009-1043)
- Opportunistic traders are more likely to face SEC enforcement and trade less after enforcement waves. — [HBS Working Knowledge](https://hbswk.hbs.edu/item/decoding-inside-information)
- Size: Lakonishok & Lee (2001), as summarized by IBKR Campus, found the gap between strong insider buying and strong insider selling is "much weaker for large companies." — [IBKR Campus: corporate insider buying](https://www.interactivebrokers.com/campus/traders-insight/securities/stocks/what-corporate-insider-buying-can-tell-investors-evidence-from-academic-research/)
- Cluster purchases: an older study (dates and size mix unclear from the excerpt) reports cluster-purchase abnormal returns almost twice those of non-cluster purchases over a 21-day window. — [UGA PhD thesis, Chen 2015](https://openscholar.uga.edu/record/12198/files/chen_pei-han_201512_phd.pdf)
- Recent (unverified, second-hand): a 2026 arXiv paper cites a 2015-2025 Form 4 study reporting a net Sharpe of 1.77 at 25 bp round-trip cost. The setting is microcaps, not large caps. — [arXiv 2602.06198](https://arxiv.org/pdf/2602.06198)
- Practitioner caution: an anecdotal post (n = 4-10) argues most insider alpha is gone by the time the filing is public. — [Tommi Johnsen Substack](https://tommijohnsen.substack.com/p/most-of-the-insider-trading-alpha)

### Inferences
- PIT rule: timestamp each Form 4 by EDGAR acceptance datetime, never by transaction date. Filings accepted after the close (most of them, up to 10 p.m.) should first be tradable at the next session's open. Use the transaction date only to compute "filing lag" as a feature.
- For a ~45-name large-cap universe, Form 4 open-market buys (code "P") are rare. A cluster-buy feature (e.g., 2 or more distinct non-routine insiders buying within 30 days) will fire seldom. It suits the meta-label classifier as a sparse binary feature, or a "don't-short-term-exit" tilt, better than a standalone strategy. Backtests on the point-in-time S&P 500 universe give a larger sample.
- Classifying routine vs opportunistic needs 3+ years of each insider's trade history. EDGAR XML Form 4s exist from about 2003 onward, so this is buildable from free data (inference, not verified).
- Engineering effort: medium. Nightly pull of daily index, filter form type 4, fetch XML, parse with edgartools, map CIK to ticker, store by acceptance time. Backfill 2005-2026 for about 500 names at under 10 req/s is a multi-hour to multi-day one-off job.
- Selling is mostly diversification or liquidity driven; a long-only bot would mainly use buys (inference consistent with Lakonishok-Lee's asymmetry, not separately sourced here).

### Gaps
- No primary-source post-2010, large-cap-only replication of CMP or cluster buys was found. The key open question for this bot.
- Did not confirm the exact data.sec.gov endpoint latency (how fast submissions JSON reflects a new Form 4), or the EDGAR full-text search API's limits and history depth (full-text search is generally understood to start in 2001, but this was not verified).
- Did not verify the Form 4 two-business-day filing deadline from a primary source (widely known from SOX 2002).

## Q2. FINRA short interest (twice monthly) and daily short sale volume (Reg SHO files)

### Takeaway
Both are free and point-in-time if keyed on publication date. Daily short volume files post by 6 p.m. ET same day (with rare later "Updated" files). Short interest is published about 7 business days after the settlement date. Proprietary-data evidence (Boehmer-Jones-Zhang) is strong, and a JFE study confirms the FINRA public daily series still predicted negative returns in 2010-2015. But the FINRA daily data covers off-exchange prints only and is dominated by market-maker shorting, so the signal is noisy and mainly sits in extremes. For a long-only bot, the usable form is an avoid-or-downweight filter, or a positive tilt on unusually *low* short volume.

### Cited Findings
**Daily short sale volume (Reg SHO)**
- FINRA posts the Daily Short Sale Volume Files no later than 6:00 p.m. ET on the trade date. In rare cases a file is updated on a later day and labelled "Updated." Files exist per TRF (Nasdaq Chicago, Nasdaq Carteret, NYSE), ADF, ORF, plus a Consolidated NMS (CNMS) file. — [FINRA: Daily Short Sale Volume Files](https://www.finra.org/finra-data/browse-catalog/short-sale-volume-data/daily-short-sale-volume-files); [FINRA catalog](https://www.finra.org/finra-data/browse-catalog/reg-sho-daily)
- Layout (since 2011-02-28): pipe-delimited Date | Symbol | ShortVolume | ShortExemptVolume | TotalVolume | Market. No-data days still produce a header/trailer file. — [FINRA file layout PDF](https://www.finra.org/sites/default/files/2021-07/DailyShortSaleVolumeFileLayout.pdf)
- Archive depth: one FINRA page lists the earliest CNMS file as 2018-08-01. Older per-TRF files exist on FINRA's TRF Reg SHO yearly pages (e.g., 2014, 2018, 2019). — [FINRA daily files](https://www.finra.org/finra-data/browse-catalog/short-sale-volume-data/daily-short-sale-volume-files); [TRF Reg SHO 2014](https://www.finra.org/filing-reporting/trf/trf-regulation-sho-2014)
- Coverage caveat: FINRA figures cover off-exchange short sale transactions only, not a complete view of market-wide shorting. — [ORTEX: Why daily short volumes are misinterpreted](https://public.ortex.com/why-daily-short-volumes-are-often-misinterpreted/)
- Evidence on the public FINRA daily series: shorting flows remain a significant predictor of negative future returns in 2010-2015, when the data were published in real time (JFE, DOI 10.1016/j.jfineco.2019.05.018). A HKUST thesis finds the same for 2010-2018 with simple sorts on the short volume ratio. — [Lehigh-hosted paper PDF](https://www.lehigh.edu/~xuy219/research/Short_Final_Version.pdf); [HKUST](https://lbezone.hkust.edu.hk/rse/?p=51474)
- Zhu (2024), via Alpha Architect: the predictive content sits in extreme values, especially unusually low short volume, not in average or aggregate short volume. — [Alpha Architect: short positions](https://alphaarchitect.com/short-positions/)
- Diether, Lee & Werner (2005, pre-FINRA public data): rising short sales predict negative returns, but the strategy's trading costs wipe out profits. — [NBER conference PDF](https://users.nber.org/~confer/2005/mmf05/werner.pdf)

**Proprietary benchmark**
- Boehmer, Jones & Zhang (JF 2008, 63(2):491-527), proprietary NYSE order data 2000-2004: heavily shorted stocks underperform lightly shorted ones by 1.16% risk-adjusted over the next 20 trading days (15.6% annualized). Institutional non-program shorts are most informed (1.43%/month). Shorting was more than 12.9% of NYSE volume. — [Cochrane-hosted PDF](https://www.johnhcochrane.com/s/boehmer_jones_zhang_shorts.pdf); [IDEAS](https://ideas.repec.org/a/bla/jfinan/v63y2008i2p491-527.html)

**Short interest (Rule 4560)**
- Firms report short positions twice a month, due 6 p.m. ET on the second business day after the settlement date. FINRA publishes aggregate data 7 business days after the settlement date, and posts each year's settlement, due and publication dates in advance (e.g., 2025: settlement Jan 15, publication Jan 27). — [FINRA SR-FINRA-2026-012](https://www.finra.org/sites/default/files/2026-05/SR-FINRA-2026-012.pdf); [FINRA short interest reporting](https://www.finra.org/industry/Short%20Interest); [MarketBeat reporting dates](https://www.marketbeat.com/short-interest/short-interest-reporting-dates/)
- Scope changed: a 2021 notice said FINRA's site disseminated short interest only for OTC securities, with exchange-listed data going to the exchanges. The current FINRA catalog says it covers all exchange-listed and OTC equities. — [FINRA Reg Notice 21-19](https://www.finra.org/sites/default/files/2021-06/Regulatory-Notice-21-19.pdf); [FINRA Equity Short Interest catalog](https://www.finra.org:443/finra-data/browse-catalog/equity-short-interest)
- API: POST https://api.finra.org/data/group/otcMarket/name/EquityShortInterest, filterable by settlementDate. The document is dated; verify auth requirements. — [FINRA Equity Short Interest API PDF](https://WWW.FINRA.ORG/sites/default/files/Equity_Short_Interest_Data_File_Download_API.pdf)
- A 2026 Federal Register proposal (2026-09864) would change short interest reporting rules. Not read in full. — [Federal Register public inspection](https://public-inspection.federalregister.gov/2026-09864.pdf)

### Inferences
- PIT rule for short interest: available_date = FINRA publication date (settlement + 7 business days), never the settlement date. Using the settlement date is a roughly 1.5-week lookahead.
- PIT rule for daily short volume: available after 6 p.m. ET on day t, so usable from the t+1 open. Watch for "Updated" files. Store the first-seen version to stay strictly point-in-time.
- Feature design: short-volume ratio = ShortVolume / TotalVolume from CNMS, z-scored per symbol against its own 60-day history. Market makers' short-side hedging puts the baseline near 40-50% for most large caps (general knowledge, not sourced here). Per Zhu, the extremes matter, so a "very low SVR" tail is the long-side candidate.
- Engineering effort: low. One small text file per day. The 2018+ CNMS archive is enough for a backtest. Short interest is one API/file pull twice a month, and days-to-cover = SI / ADV comes from existing volume data.
- For mega-caps, short interest is usually a small fraction of float, and the literature's short-interest effects are concentrated in high-SI, hard-to-borrow, smaller names (inference; not quantified here). Expected large-cap value: low to moderate.

### Gaps
- No post-2010 magnitude in bps/month for large caps from the FINRA daily series was extracted (the JFE paper's tables weren't read).
- Did not find specific large-cap evidence on days-to-cover (Hong, Li, Ni, Scheinkman, Xu) at 1-month horizons; not searched within budget.
- Whether FINRA's API needs registration or a key for the short interest endpoint today is not confirmed.

## Q3. 13F institutional holdings: short-horizon value?

### Takeaway
No source in this pass supported 1-day to 1-month predictive value from 13F, and the 45-day filing lag makes it structurally slow. Treat it as low priority for this bot. Confirmation is by structure, not by a located study.

### Cited Findings
- No relevant source found within budget. A search pairing 13F staleness with short-horizon predictability returned only CFTC material.
- `ledge-edgar` (MIT) and `edgartools` both parse 13F from EDGAR at no cost. — [PyPI ledge-edgar](https://pypi.org/project/ledge-edgar/); [PyPI edgartools](https://pypi.org/project/edgartools/5.31.0/)

### Inferences
- 13F holdings are quarter-end snapshots filed up to 45 days later. Any information is at least 6 weeks old when public, versus the bot's 1-20 session hold. Use only filing date as available date.
- Possible niche use: the 13F filing-date cluster (mid-Feb/May/Aug/Nov) as a calendar feature. Speculative.
- Engineering effort: medium (CUSIP-to-ticker mapping is the main pain). Expected value: lowest of the candidates.

### Gaps
- Did not locate studies on 13F-based short-horizon predictability (e.g., "copycat" 13F returns, hedge-fund crowding) to formally confirm "low value".

## Q4. Overnight vs intraday return decomposition (free from existing OHLC)

### Takeaway
Zero cost, already in the bot's data, and trivially point-in-time. Lou-Polk-Skouras show momentum profits accrue overnight. Akbas-Boehmer-Jiang-Koch show frequent "positive overnight then negative daytime" reversals predict *lower* returns, while frequent negative daytime reversals predict outperformance for up to six months. These are monthly cross-sectional effects on broad samples (not large-cap or short-horizon specific), so treat them as meta-label features, not stand-alone 1-5 day rules. Highest value per engineering hour.

### Cited Findings
- Lou, Polk & Skouras (JFE 2019, 134(1):192-213): strong firm-level continuation in both overnight and intraday components, plus an offsetting cross-period reversal, persisting for years. All abnormal momentum returns occur overnight, while the other strategies' profits occur intraday. Institutions prefer to trade intraday and against momentum. The smoothed overnight-minus-intraday spread forecasts a strategy's close-to-close performance over time. — [LSE Research Online](https://researchonline.lse.ac.uk/id/eprint/87481); [Polk PDF](https://personal.lse.ac.uk/polk/research/TugOfWar.pdf)
- Alpha Architect summary (secondary): overnight momentum 3-factor alpha 0.95% (significant) vs intraday 0.11% (insignificant). The 2015 working paper says 9 of 14 strategies earn their premia entirely intraday; check the published version before citing counts. — [Alpha Architect](https://alphaarchitect.com/2015/02/overnight-momentum-vs-intraday-momentum/); [NBER conference draft](https://conference.nber.org/conf_papers/f78239/f78239.pdf)
- Akbas, Boehmer, Jiang & Koch (JFE 2022, 145(3):850-875; DOI 10.1016/j.jfineco.2021.09.019), US common stocks May 1993 to Dec 2017: monthly "tug-of-war" intensity (frequency of positive overnight returns followed by negative daytime reversals) relates negatively to future returns, but only for negative reversals (high opens). Stocks with abnormally frequent negative daytime reversals outperform for up to six months, without long-run reversal. Also holds for NYSE 1926-1962. — [EconPapers](https://econpapers.repec.org/RePEc:eee:jfinec:v:145:y:2022:i:3:p:850-875); [SUFE summary](https://academicnewsletter.sufe.edu.cn/info/355173); [CXO Advisory](https://www.cxoadvisory.com/calendar-effects/tug-of-war-risk-and-future-stock-returns/)
- Out-of-US replication is mixed: no significant high-vs-low intensity return difference in China (Cheema, Chiah & Man 2022), limited aggregate power in Thailand. — [IDEAS Pacific-Basin FJ](https://ideas.repec.org/a/eee/pacfin/v74y2022ics0927538x22001044.html); [Thai PMR](https://so09.tci-thaijo.org/index.php/PMR/article/download/8411/5276/48870)

### Inferences
- Features from existing yfinance OHLC: overnight return = open_t / close_{t-1} - 1; intraday return = close_t / open_t - 1; 20-day count of (overnight > 0 and intraday < 0) days; 20-day count of negative daytime reversals; 20-day cumulative overnight minus intraday spread. All known at close_t, so they are PIT for a next-open entry.
- Caveat: yfinance "Open" is the primary or consolidated opening print, and the split/dividend adjustment must be applied consistently to open and close (dividends go ex at the open, so overnight returns on ex-dates need care). Verify in code.
- Engineering effort: very low (hours). Expected value: moderate; the main risk is that effects documented in monthly cross-sections of all stocks are weak in 45 mega-caps over 1-5 days.

### Gaps
- No published magnitude found specific to large caps or to holding periods under one month for the Akbas measure (bps/month not extracted from the paper's tables).
- No post-2017 out-of-sample test in US data located.

## Q5. Options-derived signals from free sources (put/call, IV skew, volatility spread)

### Takeaway
The academic evidence is strong and short-horizon: Cremers-Weinbaum find about 50 bps/week for expensive-call minus expensive-put stocks, and the Xing-Zhang-Zhao smirk predicts for at least six months. But no free point-in-time history of single-stock implied volatility was found. CBOE's free aggregate put/call files run only Nov 2006 to Oct 2019. Free single-stock IV therefore means snapshotting option chains forward (Yahoo/yfinance, or Alpaca's options data if on the free plan) and waiting 1-2 years for usable history. Worth starting a daily collector now; payoff is deferred.

### Cited Findings
- Cremers & Weinbaum (JFQA 2010, 45(2):335-367): deviations from put-call parity (volatility spread) predict returns. Stocks with relatively expensive calls outperform stocks with relatively expensive puts by **50 bps/week**. Not explained by short-sale constraints. Mispricing was larger early in the sample and declined gradually over time. — [Cambridge Core](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/deviations-from-putcall-parity-and-stock-return-predictability/D9BA8F97580328AAFD7988B092FE5D50); [IDEAS](https://ideas.repec.org/a/cup/jfinqa/v45y2010i02p335-367_00.html)
- Xing, Zhang & Zhao (JFQA 2010, 45(3):641-662): smirk = OTM put IV minus ATM call IV. A steep smirk predicts low subsequent returns, consistent with informed options traders, and the predictability persists for at least six months. — [Cambridge Core](https://resolve.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/what-does-the-individual-option-volatility-smirk-tell-us-about-future-equity-returns/ECFD16BA9ACBDC8D577D1BD866FBEA72); [EconPapers](https://econpapers.repec.org/RePEc:cup:jfinqa:v:45:y:2010:i:03:p:641-662_00)
- Persistence: Fu, Arisoy, Shackleton & Umutlu find skew has the strongest predictive power among option-implied measures across horizons, both before and after the 2008 crisis. Doran & Krieger find skew predictability in US and international equities and in ETFs. — [Lancaster research portal](https://www.research.lancs.ac.uk/portal/en/publications/option-implied-volatility-measures-and-stock-return-predictability(2e430128-3867-4051-8b3f-6fba409a9fdb).html); [CFA Institute FAJ 2010](https://rpc.cfainstitute.org/en/research/financial-analysts-journal/2010/implications-for-asset-returns-in-the-implied-volatility-skew)
- Free CBOE data: the historical download page has total exchange volume and put/call ratio files from 2006-11-01 to 2019-10-04. From September 2019, historical equity option volume moves to the Historical Options Data page. A daily market statistics page shows the current session's ratios (equity, index, total), browsable by date via `?dt=YYYY-MM-DD`. — [CBOE historical data](https://ww2.cboe.com/us/options/market_statistics/historical_data); [CBOE daily stats (dated example)](https://www.cboe.com/us/options/market_statistics/daily/?dt=2023-06-02)
- A third-party source says CBOE retired free historical ratio files in 2019, leaving the daily stats page as the only free official source. YCharts and Alphacast list series (2006+), but access terms weren't visible. QuantShare and Wealth-Lab tools scrape CBOE. — [thetrading.tools](https://www.thetrading.tools/manuals/put-call-ratio); [Alphacast](https://www.alphacast.io/datasets/equity-put-call-ratio-38727); [Wealth-Lab CBOE provider](https://wl6.wealth-lab.com/Extensions/Details/42)

### Inferences
- The `?dt=` parameter on CBOE's daily stats page suggests post-2019 daily aggregate put/call values can be reconstructed date by date. Check CBOE's terms of use before scraping. Aggregate (market-level) put/call is a timing or regime feature, not a cross-sectional stock selector.
- Single-stock IV skew and volatility spread need current-day chains. yfinance `Ticker.option_chain()` returns a snapshot with an impliedVolatility column but no history. Start a daily end-of-day collector (45 names, a few expiries each) now, storing raw chains with the capture timestamp. This is the "collect forward" implication.
- The evidence is mostly from broad optionable samples where the edge concentrates in less liquid names. Large-cap edge is plausibly smaller, and the 2010 papers predate heavy retail options flow (inference).
- Engineering effort: low for a collector (a day), medium for IV computation and QC (Yahoo IVs are noisy and stale-quote prone). The value is deferred until 12-24 months of history exist.

### Gaps
- No post-2015 large-cap magnitude for skew or volatility spread found.
- Did not check whether Alpaca's free tier includes options chains with greeks/IV, or whether Yahoo/Alpaca terms permit storing scraped chains.
- No free historical single-stock IV source found (none may exist).

## Q6. Analyst earnings revisions and estimates: any free point-in-time source?

### Takeaway
No free point-in-time history of analyst estimate revisions was found. yfinance `eps_trend` and `eps_revisions` are rolling windows relative to "now" (7/30/60/90 days), so they cannot be backfilled. The only free path is daily snapshots stored forward.

### Cited Findings
- yfinance exposes `eps_trend` (current estimate vs 7, 30, 60, 90 days ago) and `eps_revisions` (up vs down counts over the last 7 and 30 days). — [yfinance Ticker API reference](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.html)
- Twelve Data has an EPS revisions endpoint covering the last week and month (free-tier inclusion unconfirmed). Financial Modeling Prep's estimates endpoint provides no historical revision timeline. Practitioners build revision signals from tracked snapshots. — [Twelve Data eps_revisions docs](https://twelvedata.com/docs/llms/analysis/eps_revisions.md); [FMP: earnings revision pressure signal](https://site.financialmodelingprep.com/education/calendar/build-an-earnings-revision-pressure-signal-estimate-drift-monitor)

### Inferences
- Since `eps_trend` embeds 7/30/60/90-day-ago values, a single daily snapshot already yields a usable 30/90-day revision feature from day one going forward. No backfill is possible, though, so it cannot be backtested until months of snapshots exist.
- Engineering effort: low (a daily cron of 45 calls). Yahoo's terms of service on automated use were not checked; yfinance is unofficial.

### Gaps
- Did not locate the post-2010 large-cap magnitude of estimate-revision momentum (e.g., Chan-Jegadeesh-Lakonishok follow-ups) within budget.
- Zacks and Nasdaq free limits not checked.

## Q7. Other candidates (8-K items, buybacks, macro surprises, attention data, Reddit, CFTC COT, ETF flows)

### Takeaway
8-K filings are free and timestamped via EDGAR. Post-2010 evidence shows reactions mostly on the event and filing day, with some item-level drift. CFTC COT is free and PIT (Friday 3:30 p.m. ET release of Tuesday data; a CFTC release says twice-weekly publication is coming by end-2026). Wikipedia views and Google Trends have attention-return evidence. WSB and social sentiment evidence is concentrated in meme stocks. Magnitudes for large caps at 1-20 days were mostly not located. A general caveat: anomaly returns fall about 58% after publication.

### Cited Findings
**Decay caveat (applies to every source above)**
- McLean & Pontiff (JF 2016), 97 predictors: portfolio returns are 26% lower out-of-sample and 58% lower post-publication. The authors read the 26% as an upper bound on data mining and attribute the roughly 32-point difference to investors learning from publications. — [Gwern-hosted PDF](https://Www.Gwern.net/doc/economics/2016-mclean.pdf); [CXO Advisory](https://www.cxoadvisory.com/big-ideas/effects-of-market-adaptation)

**SEC 8-K item filings**
- Lerman & Livnat (RAST 2010): some 8-K items show significant return drift after filing, and good-news 8-Ks produce larger event-day reactions than bad news. — [IDEAS RAST 2010](https://ideas.repec.org/a/spr/reaccs/v15y2010i4d10.1007_s11142-009-9114-7.html)
- Israelsen (2018), 2010-2015: significant market reaction on the event date, on the days between event and filing, and on the filing date. Institutions appear to learn of events before the 8-K is filed. — [Israelsen 2018 PDF](https://www.lehigh.edu/~jms408/Israelsen_2018.pdf)
- Item 4.02 (non-reliance) event study, 8,000+ filings 2004-2023: negative CARs vs SPY that compound over 1-20 business days. A vendor study, not peer-reviewed. — [sec-api.io](https://sec-api.io/resources/stock-price-reactions-to-item-4-02-disclosures-in-sec-form-8-k-filings)
- Investor inattention: the absolute response is 11% weaker for December 8-K filings (Chava & Paradkar WP). — [AEA 2018 program](https://benny.aeaweb.org/conference/2018/preliminary/paper/D96Bs9KR)

**Attention and social data**
- Wikipedia: a 2025 SSRN paper finds firms with surges in Wikipedia views earn higher value- and equal-weighted returns, and a long-short on view changes earns positive abnormal returns. An earlier Finance Research Letters/IRFA paper studies Wikipedia searches and returns. — [Synapse summary of 2025 SSRN](https://www.synapsesocial.com/papers/6971be50642b1836717e2f57); [IDEAS IRFA 2020](https://ideas.repec.org/a/eee/finana/v72y2020ics1057521920302076.html); [WWW 2016 companion](https://archives.iw3c2.org/www2016/WWW2016-Proceedings/companion/p591.pdf)
- WallStreetBets: a 2021 paper (via CXO) finds WSB buy recommendations beat DGTW-matched stocks by 3.92% over one month (1,878 reports, 2018-2020). For meme-stock indices, Google search sentiment predicts at 3-7 days. For GME/AMC, comment volume and Google Trends beat sentiment scores. — [CXO Advisory: WSB](https://www.cxoadvisory.com/investing-expertise/wallstreetbets-contributors-and-users-skilled); [arXiv 2507.22922](https://arxiv.org/abs/2507.22922v1); [AEA 2026 program](https://www.aeaweb.org/conference/2026/program/paper/9K6d53DN)

**CFTC Commitments of Traders**
- COT is published at 3:30 p.m. ET, typically Friday, with data as of the preceding Tuesday; holidays can delay release by 1-2 days. — [CFTC COT release schedule](https://cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm); [CFTC COT descriptions](https://cftc.gov/node/128971)
- CFTC release 9316-26 (dated 2026-10-27, per search summary; not opened) says the CFTC plans to move to two publications per week by end-2026, cutting latency from three days to two. — [CFTC press release 9316-26](https://www.cftc.gov/PressRoom/PressReleases/9316-26)

### Inferences
- 8-K: EDGAR acceptance timestamps make 8-K features PIT. Item 2.02 (earnings) overlaps the existing earnings data. Items 1.01, 5.02 (officer changes) and 8.01 can be event flags for the meta-label model (e.g., "CEO/CFO departure in last 5 days" means skip the entry). The same EDGAR pipeline as Form 4 makes this low marginal effort.
- Buybacks: no free structured buyback-announcement feed found. Text-mining 8-K/press releases is possible but medium-high effort with unverified large-cap evidence.
- Wikipedia Pageviews (Wikimedia REST API, free, daily, from mid-2015; general knowledge, not fetched) is PIT and cheap to add. Google Trends values are re-normalized per query and sampled, so backfilled series are not strictly PIT. Store snapshots forward.
- Reddit/WSB: the evidence is meme-stock centric and of little relevance to liquid large caps. Reddit API terms since 2023 restrict free bulk access (general knowledge, not verified here). Low priority.
- COT on index futures (E-mini S&P, Nasdaq) gives a weekly, market-level positioning feature, usable from the Friday close at the earliest. A regime or sizing input, not stock selection.
- ETF flows/creations: no free authoritative daily flow source located. Shares-outstanding changes can be approximated from issuer sites, but that is unverified.
- Macro surprise series: FRED already in use. A free "consensus" for surprises was not located (consensus forecasts are typically proprietary).

### Gaps
- No magnitudes located for: Item 1.01/5.02 drift, post-2010 buyback-announcement drift, Wikipedia/Trends effects in large caps at 1-20 days, COT predictive power for index returns, ETF-flow predictability, government-contract (USAspending) signals.
- Da, Engelberg & Gao (JF 2011, Google SVI predicts higher returns over 2 weeks then reversal) not retrieved in this pass.
- Did not check Wikimedia, Google Trends or Reddit terms of use.

## Ranking by expected value for Frozen_stock (synthesis; inference from the findings above)

1. **Overnight/intraday decomposition features.** Zero data cost, already PIT in existing OHLC, hours to build. JFE-level evidence (LPS 2019; Akbas et al. 2022), though monthly and broad-sample. Add to rule language + meta-label first.
2. **FINRA daily short volume (CNMS) + bi-monthly short interest/days-to-cover.** Free, small files, clear PIT timing (6 p.m. same day; settlement + 7 business days). JFE evidence the public series predicted returns 2010-2015, with the signal in extremes. Low effort, 2018+ backtest.
3. **SEC EDGAR Form 4 (opportunistic/cluster buys) + 8-K item flags (one pipeline).** Free, PIT by acceptance time, strong historical evidence (82 bps/month), but sparse and weaker in large caps, with no post-2010 large-cap replication found. Medium effort; test on the PIT S&P 500 universe for sample size.
4. **Forward-collected snapshots: option chains (IV skew / volatility spread) + yfinance eps_trend/eps_revisions.** Strongest short-horizon academic evidence (Cremers-Weinbaum 50 bps/week), but no free history. Start collectors now (low effort). Value arrives in 12-24 months.
5. **CBOE aggregate put/call, CFTC COT, Wikipedia page views.** Free, market-level or attention features. Modest, mostly unquantified evidence for large caps. Low-medium effort.
6. **13F, Reddit/WSB, Google Trends (backfilled), ETF flows, buybacks.** Too slow, wrong universe, not PIT, or no free source. Deprioritize.

Apply a McLean-Pontiff haircut (about 58% post-publication decay) to every published magnitude above before comparing it to the bot's costs.
