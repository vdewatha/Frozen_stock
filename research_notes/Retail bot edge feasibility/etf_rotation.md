# Long-only cross-asset ETF rotation and trend strategies as a new strategy family for Frozen_stock (evidence through November 2026)

Scope note: this file mixes (1) published or vendor evidence, each line with a source link, and (2) a quick backtest I ran in this session on yfinance adjusted closes (Jun 2007 to 6 Nov 2026). I label the backtest "OWN BACKTEST". It was not pre-registered. Script: `/Users/ainz_ool_gown/Documents/Trading App/research_notes/Retail bot edge feasibility/etf_rotation_backtest.py`. How it works: the signal is taken at the close of the signal day and executed at the next day's close. Weights drift between rebalances. Costs are 5 bps per unit of one-way turnover (sum of |dw|), with 0 and 25 bp sensitivity runs. Cash is BIL. Sharpe is the excess over BIL, from daily returns. The main window is 2015-01-01 to 2026-10-31 (11.8 years). Over that window BIL compounded 1.97%/yr.

OWN BACKTEST headline table, 2015-01 to 2026-10, net of 5 bps:

| Strategy | CAGR | Vol | Sharpe | MaxDD | 2022 | Turnover/yr |
|---|---|---|---|---|---|---|
| SPY buy and hold | 13.5% | 17.5% | 0.70 | -33.7% | -18.2% | 0 |
| 60/40 SPY/IEF (monthly) | 8.7% | 10.3% | 0.68 | -21.2% | -16.6% | 0.22 |
| Equal-weight 8 ETFs buy and hold | 6.9% | 10.0% | 0.52 | -20.6% | -13.7% | 0.32 |
| GTAA5 Faber 10-month SMA (SPY, EFA, IEF, DBC, VNQ; else BIL) | 4.6% | 6.8% | 0.41 | -9.2% | -6.0% | 4.0 |
| GTAA5 with SMA ensemble 6-12 months | 5.0% | 6.7% | 0.47 | -10.5% | -4.7% | 3.8 |
| TSMOM ensemble (1/3/6/12m vs BIL), inverse-vol, 8 ETFs, monthly | 3.8% | 4.8% | 0.39 | -8.3% | -3.8% | 4.6 |
| Same, weekly rebalance | 3.8% | 4.6% | 0.41 | -6.3% | -4.6% | 10.3 |
| GEM 12m (SPY/VEU/AGG, BIL gate) | 7.1% | 16.2% | 0.39 | -33.7% | -17.3% | 3.6 |
| Relative momentum top-3 of 8 (13612W) plus absolute filter | 6.6% | 11.8% | 0.43 | -24.9% | -4.9% | 11.0 |
| Proxy equity-reversion book (SPY RSI(2)<10 above 200d SMA, exit above 5d SMA) | 5.9% | 6.1% | 0.65 | -9.4% | -1.7% | 17.4 |
| 50% SPY + 50% BIL (cash blend benchmark) | 8.0% | n/a | 0.70 | -17.9% | n/a | n/a |

The 8 ETFs are SPY, EFA, EEM, IEF, TLT, GLD, DBC and VNQ.

## Q1. Does time-series momentum (trend following) on asset classes still work after 2015, and does a long-only ETF version keep it?

### Takeaway
Diversified long/short futures trend following has more than a century of positive results. After 2015 its record was bumpy: a weak 2010s (SG Trend Index about 1.8%/yr), a record 2022 (+27%), and the worst 12-month loss in its history to April 2025. A long-only, unlevered ETF version is a much weaker cousin. In my 2015-2026 backtest it earned about 2-3%/yr over cash (CAGR 3.8-5.0% net), with drawdowns under 11% and Sharpe 0.39-0.47. That is positive but not statistically significant, and below SPY and 60/40 on Sharpe.

### Cited Findings
- Moskowitz, Ooi and Pedersen, "Time Series Momentum", Journal of Financial Economics 104(2), 2012, pp. 228-250. They document time-series momentum in equity index, currency, commodity and bond futures across 58 liquid instruments. Returns persist for 1 to 12 months and partially reverse over longer horizons — [EconPapers](https://econpapers.repec.org/RePEc:eee:jfinec:v:104:y:2012:i:2:p:228-250); [SSRN](https://papers.ssrn.com/abstract=2089463)
- Hurst, Ooi and Pedersen, "A Century of Evidence on Trend-Following Investing" (AQR, Fall 2014 update; published in JPM 2017):
  - The strategy is an equal-weighted mix of 1-, 3- and 12-month TSMOM across 67 markets (29 commodities, 11 equity indices, 15 bond markets, 12 currency pairs), Jan 1880 to Dec 2013. It is vol-scaled to a 10% target using a rolling 3-year covariance.
  - Full sample: 14.9%/yr gross and 11.2%/yr net of simulated 2-and-20 fees, 9.7% vol, net Sharpe 0.77. Correlation is 0.00 to US equities and -0.04 to US 10-year bonds.
  - It was positive in 8 of the 10 largest 60/40 drawdowns. Those drawdowns averaged about 15 months peak to trough, which gives trend time to reposition.
  - An 80/20 mix of 60/40 and TSMOM (1880-2013) improved on 60/40 alone: return 8.5% vs 7.8%, vol 8.8% vs 10.8%, max DD -50.2% vs -62.3%, Sharpe 0.54 vs 0.38.
  - The authors note that average pairwise correlations across markets rose after 2007, leaving fewer independent trends. Even if future Sharpe is only 0.4 net, a 20% allocation would still have helped a 60/40.
  - Source: [Hurst/Ooi/Pedersen PDF](https://www.trendfollowing.com/whitepaper/Century_Evidence_Trend_Following.pdf); [CBS research portal](https://research.cbs.dk/en/publications/a-century-of-evidence-on-trend-following-investing/)
- AQR, "Trend-Following: Why Now? A Macro Perspective" (4Q 2022), data to 30 Sep 2022 — [AQR PDF](https://www.aqr.com/-/media/AQR/Documents/White-Papers/Trend-Following-Why-Now-A-Macro-Perspective.pdf?sc_lang=en):
  - SG Trend Index annualized return: 8.2% (2000-09), 1.8% (2010-19), 17.9% (2020 to Sep 2022), 6.4% full sample.
  - SG Trend Index vol: 16.1%, 11.3%, 11.0%, 13.6%. Global 60/40 return: 2.5%, 7.5%, 0.2%, 4.4%.
  - Correlation of SG Trend to Global 60/40: -0.2, +0.2, -0.3, -0.1. Sharpe since 2000 is 0.4 for SG Trend vs 0.3 for 60/40.
  - AQR blames the lean 2010s on benign macro shocks, low growth and inflation, and central banks held at the lower bound, which together gave unusually few large market moves.
  - There was no tendency to underperform after strong 12-month periods: median next-12-month returns ran 3.5%-6.1% across trailing-return quintiles. The best calendar year before 2022 was +26.1% in 2002.
- SG Trend Index finished 2022 up 27.3% — [Top Traders Unplugged, Dec 2022](https://www.toptradersunplugged.com/trend-following-performance-report-december-2022/). SG CTA Index rose 20.1% in 2022, its best year since 2000 — [Alpha Week](https://www.alpha-week.com/2022-cta-index-performance-review)
- Critic's view: Price Action Lab puts trend CTAs' "lost decade" at 0.4% annualized with a 21.8% max drawdown, using different dates from AQR. It argues CTAs failed to beat a simple momentum strategy — [Price Action Lab](https://www.priceactionlab.com/Blog/2023/02/trend-ctas-momentum/)
- Two practitioner sources describe hard trend periods. Archive Capital names 2011-2013 and 2016-2018 as notably difficult, low-trend periods — [Archive Capital](https://www.archivecapitaladvisors.ie/post/how-trend-following-can-enhance-your-portfolio). TTU describes a May 2015 to Feb 2019 drawdown for its own trend model, which is a different product from the SG index — [TTU](https://www.toptradersunplugged.com/trend-following-performance-report-december-2022/)
- 2025: per Virtus/AlphaSimplex, the SG Trend Index returned -18.7% for the year ended 30 Apr 2025, the worst 12-month return in its 25-plus-year history — [Virtus report via mebfaber.com](https://mebfaber.com/wp-content/uploads/2025/05/the_patience_premium_5028-1747850090.pdf). A second Virtus document calls the 14 May 2025 low the second-largest drawdown in the index's history, so the sources disagree on the ranking — [Virtus](https://www.virtus.com/assets/files/95f/the_patience_premium_5028.pdf). TTU reports SG Trend +2.39% for full-year 2025 — [TTU Dec 2025](https://www.toptradersunplugged.com/trend-following-performance-report-december-2025/). Meketa: SG Trend 4.9%/yr annualized from Jan 2000 to May 2025, with 13.4% vol — [Meketa, Dec 2025](https://meketa.com/wp-content/uploads/2025/12/MEKETA_Trend-Following.pdf)
- Baltas and Kosowski attribute weak post-2008 TSMOM performance to higher pairwise correlations — [SSRN 2140091](https://papers.ssrn.com/abstract=2140091)
- Faber's long-only rule: at each month-end, hold an asset if its price is above its 10-month SMA, otherwise move it to cash — [PortfolioWiser summary](https://www.portfoliowiser.com/knowledge-hub/meb-faber-gtaa). Aggregator backtest figures, which are secondary and inconsistent across pages:
  - GTAA 13 since 1980: 9.4% CAGR, -12.5% max DD, Sharpe 0.69 — [PortfolioDB](https://www.portfoliodb.com/portfolios/global-tactical-asset-allocation-13-gtaa-13-meb-faber)
  - One GTAA 5 page shows both a 9.2% and a 4.8% 10-year return — [PortfolioDB GTAA5](https://www.portfoliodb.com/portfolios/global-tactical-asset-allocation-5-gtaa-5-meb-faber)
- Live, real-money evidence: Cambria Global Momentum ETF (GMOM), a trend/momentum tactical fund launched 3 Nov 2014 — [SEC N-CSR FY2024](https://www.sec.gov/Archives/edgar/data/1529390/000199937124008322/cmba-ncsr_043024.htm); [SEC N-CSR FY2022](https://www.sec.gov/Archives/edgar/data/1529390/000121390022038176/s138554_ncsr.htm):
  - NAV returns to 30 Apr 2024: 1 year 3.98%, 3 years 1.47%, 5 years 5.08%, since inception 3.68%/yr. Its benchmark, the S&P Balanced Equity and Bond Moderate index, returned 6.42%/yr since inception. Expense ratio 1.11%.
  - As of 30 Apr 2022, since-inception returns were 5.43% vs 6.94% for the benchmark.
  - StockAnalysis shows a later, undated since-inception average of 5.90% — [StockAnalysis](https://stockanalysis.com/etf/gmom/)

### Inferences
- OWN BACKTEST, long-only trend on ETFs, 2015-01 to 2026-10:
  - GTAA5 (10-month SMA) made 4.6%/yr, Sharpe 0.41, max DD -9.2%. The SMA-ensemble version made 5.0%, Sharpe 0.47, max DD -10.5%. TSMOM ensemble with inverse-vol weights made 3.8%, Sharpe 0.39, max DD -8.3%.
  - That is 1.8-3.0%/yr over BIL (1.97%). Expectancy after costs was positive but small.
  - On Sharpe, every version lost to SPY (0.70), 60/40 (0.68) and even a plain 50% SPY / 50% BIL mix (0.70, -17.9% DD).
  - With Sharpe around 0.4 over 11.8 years, the t-stat is about 1.3-1.6. Reaching t = 2 at that Sharpe would take roughly 18-27 years of data. The post-2015 sample cannot prove an edge, so the case rests on the long history (Hurst et al.) plus crisis behaviour.
- OWN BACKTEST calendar years show the cost of being defensive:
  - 2019: GTAA5 7.3%, TSMOM 7.8%, SPY 31.2%.
  - 2022: GTAA5 -6.0%, TSMOM -3.8%, SPY -18.2%.
  - 2023: GTAA5 1.8%, TSMOM 2.6%, SPY 26.2%.
  - 2024: GTAA5 5.1%, TSMOM 5.0%, SPY 24.9%.
  - The trend lane earns its keep in slow bear markets such as 2022 and lags badly in V-shaped recoveries (2023) and in strong single-asset bull runs.
- Long-only trend cannot short bonds or commodities, so it misses the short side that produced much of managed futures' +27% in 2022. In 2022 it went to cash rather than profiting: my long-only versions lost 4-6%. Its realistic role is drawdown control, not crisis alpha.
- The live GMOM record (about 3.7%/yr since inception to Apr 2024, trailing a moderate balanced benchmark, after a 1.11% fee) is consistent with my backtest. A self-run version on Alpaca avoids the 1.11% fee, so expect roughly 4-5%/yr gross of tax in a similar regime.

### Gaps
- I could not get calendar-year SG Trend Index returns for 2015-2021, 2023 or 2024 from a primary source. The SG factsheet is the right place to confirm them. The 2010-19 decade figure (1.8%/yr) is from AQR.
- I found no AQR post-2017 update of the century study with 2015-2026 data. AQR's 2022 note only runs to Sep 2022.
- I did not find Faber's own updated GTAA paper figures for 2015-2026. Aggregator pages conflict with each other.
- GMOM's exact rule set is not in the SEC filing, so it is only a loose live proxy for Faber's rules.

## Q2. Does relative (cross-sectional) momentum across asset-class ETFs (GEM, PAA, DAA, VAA, BAA, AAA) hold up after publication, and what do critics say?

### Takeaway
Out-of-sample results after publication are mediocre. Dual momentum (GEM) trailed SPY by about 5 points a year and roughly matched 60/40 since 2014. Its protective value came mainly from slow crashes such as 2008, not fast ones such as 2020. Results are very sensitive to lookback and rebalance day. In my backtest, a top-3 relative-momentum rule's 2015-2026 CAGR ranged from 4.9% to 10.6% depending only on which day of the month it traded. Keller-family strategies have good real-time tracking per Allocate Smartly, but they were built partly by minimizing drawdown on history, and that practice invites overfitting.

### Cited Findings
- GEM out of sample: quant4free tested 2014-2026 (the book came out in 2014), using 12-month signals on SPY, VEU, AGG and BIL with monthly checks. It did not state costs — [quant4free](https://quant4free.com/analysis/dual-momentum/):
  - GEM: CAGR 8.4%, Sharpe 0.70, max DD -20%.
  - SPY: CAGR 13.6%, Sharpe 0.94, max DD -24%.
  - Static 60/40: CAGR 9.1%, Sharpe 0.95, max DD -20%.
  - GEM switched 30 times in 18 years and was in US stocks about two-thirds of the time. Whipsaws in 2011 and 2015-16 cost it the recoveries.
- GEM taken apart: Quanterlab ran 18 one-year walk-forward windows (July 2008 to July 2026) with 10 bps one-way costs — [Quanterlab](https://quanterlab.com/research/antonacci-s-dual-momentum-taken-apart-gate-relative-leg-and-blends-across-four-l):
  - At 12 months: GEM 9.28% CAGR, Sharpe 0.63, DD -33.7%. Absolute-momentum gate only (no international leg): 10.70%, 0.73, -33.7%. Relative momentum only, no gate: 9.80%, 0.56, -51.0%. SPY: 10.15%, 0.59, -47.9%. 60/40: 8.80%, 0.77, -30.1%.
  - The international leg cost 1.4-3.2 points a year at every lookback tested.
  - CAGR across lookbacks forms a U shape: 8.9% (3m), 6.7% (6m), 5.7% (9m), 9.3% (12m).
  - The gate helped in slow crashes such as 2008 but not fast ones; every book fell about 34% in 2020.
  - The authors flag that the best cells are inflated by selection across 18 configurations.
- Newfound Research, lookback fragility of GEM — [Flirting with Models](https://blog.thinknewfound.com/2019/01/fragility-case-study-dual-momentum-gem/):
  - The 9-month variant returned 43.1% total vs 146.1% for the 10-month variant.
  - In 2010 alone the 10-month variant made +12.2% and the 9-month variant -9.31%, about a 2,150 bp spread.
  - In Q4 2018 GEM signalled defensive only in early January 2019, after the S&P was already about 14% off its peak.
  - Newfound recommends equal-weighting all lookback variants, which brings results closer to the median outcome, with higher Sharpe and lower max DD than the typical single variant. Specification gaps are described as random but permanent, not mean-reverting.
- From 2014 to 2022, GEM underperformed buy-and-hold S&P 500 because shallow corrections in a US-led bull market left the gate idle. Defenders argue this is expected behaviour — [Reblnc](https://reblnc.com/insights/dual-momentum)
- Antonacci's own extended backtest calls 1973-74 out-of-sample validation. This is a self-assessment — [Antonacci on Medium](https://medium.com/@garyantonacci_30463/extended-backtest-of-global-equities-momentum-dual-momentum-eb12902612e0)
- Keller and Keuning's Defensive Asset Allocation (DAA, 2018) — [SSRN 3212862](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3212862); [CXO Advisory](https://www.cxoadvisory.com/strategic-allocation/multi-class-momentum-portfolio-with-canary-crash-protection/):
  - The "canary universe" (VWO and BND, i.e. EEM and AGG) was picked using Dec 1926 to Dec 1970 data with the S&P 500 as the only risky asset.
  - In-sample is Dec 1970 to Dec 1993; out-of-sample is Dec 1993 to Mar 2018.
  - Per CXO, DAA beat VAA on CAGR in 3 of 4 universes but had larger max drawdowns in all 4.
- Allocate Smartly says Keller's strategies remain among the top performers it tracks in real-time out-of-sample trading. It also warns that BAA is a mashup of PAA, VAA and DAA ideas, which raises overfitting risk, and that designing strategies to minimize max drawdown increases the opportunity to overfit and the chance of disappointing out-of-sample results — [Allocate Smartly, BAA](https://allocatesmartly.com/bold-asset-allocation/)
- Allocate Smartly notes that the month-end advantage in these backtests could partly reflect overfitting to monthly data, and that it has flattened in recent years — [Allocate Smartly, day of month](https://allocatesmartly.com/diving-deeper-does-the-day-of-the-month-matter/)
- Allocate Smartly's review of tactical asset allocation (TAA) in the 2022 bear, via a secondhand summary — [the7circles](https://the7circles.uk/?p=41965):
  - Aggregate TAA max DD was 3% in 2000-02 and 7% in 2007-08. 60/40 lost 17% in 2022.
  - TAA moved risk-off as designed, but long-duration Treasuries were the wrong defensive asset.
  - The third of strategies with the least rising-rate exposure had an 8% max DD. The most-exposed third had 17%, the same as 60/40.
  - Strategies that required defensive assets to show positive momentum, with cash as the backstop, did much better.
- Morningstar: tactical-allocation funds were more likely to close than other allocation funds. The tactical funds with the best 10-year returns going into 2022 fell furthest — [Morningstar, Rekenthaler](https://www.morningstar.com/columns/rekenthaler-report/have-tactical-asset-allocation-funds-earned-their-keep)
- General post-publication decay: McLean and Pontiff (Journal of Finance, 2016) studied 97 published cross-sectional predictors and concluded that investors learn about mispricing from academic publications, so predictability weakens afterwards. Press coverage of an earlier version reported about 35% post-publication decay — [McLean and Pontiff PDF](https://Www.Gwern.net/doc/economics/2016-mclean.pdf); [Fortune](https://fortune.com/2012/11/02/academics-academia-destroys-stock-picking-strategies/)

### Inferences
- OWN BACKTEST, 2015-01 to 2026-10:
  - GEM 12m made 7.1%/yr, Sharpe 0.39, max DD -33.7%, with a monthly correlation of 0.83 to SPY. That matches Quanterlab's -33.7% DD. quant4free's -20% DD probably comes from monthly-sampled drawdowns.
  - GEM is essentially a US-equity lane with a slow exit. It would not add independent bets to an equity book. Its 2022 result (-17.3%) shows the bond (AGG) defensive leg failing.
- OWN BACKTEST: a top-3-of-8 relative-momentum rule (13612W weighting plus absolute filter) made 6.6%/yr, Sharpe 0.43, max DD -24.9%, with turnover of 11x a year. Moving the rebalance day changed its 2015-2026 CAGR from 4.9% to 10.6% and its max DD from -15.6% to -28.5%. That much dispersion means any single backtest of a concentrated rotation rule is mostly noise. Pre-registration must fix the rebalance day, or better, average across days in tranches.
- The universes and canaries in published rotation strategies (EEM/AGG canaries, chosen ETF lists) were picked by authors with hindsight. The bot should treat any published universe as already in-sample and judge a strategy only on 2015-2026 (or later) results, with the universe fixed before testing.
- For this bot, relative momentum adds concentration (top 1-3 assets) and timing luck without improving Sharpe over absolute trend. If it is used at all, it should be a minority sleeve.

### Gaps
- I found no out-of-sample evidence for Butler et al.'s Adaptive Asset Allocation, and no source was retrieved for it.
- I found no consolidated post-publication scorecard for PAA, VAA, DAA or BAA. Allocate Smartly's tracking data is behind a subscription, and only its qualitative statement was retrieved.
- McLean and Pontiff's published abstract is widely quoted as "26% lower out-of-sample, 58% lower post-publication". I could not verify those exact numbers in this session, and the paper covers stock-level anomalies, not asset-class rotation.

## Q3. Risk parity and volatility targeting as alternatives or overlays: what is their record after 2015, including 2022?

### Takeaway
Stock-bond risk parity relies on bonds hedging stocks. 2022 broke that: the stock-bond correlation turned positive and levered bond exposure produced roughly 20-30% losses, though sources conflict on the exact figure. Trend following tended to help in exactly that regime. For a long-only bot, the useful pieces are inverse-volatility weighting and a cash (BIL/SGOV) defensive asset instead of long Treasuries. Inverse-vol weighting lowered drawdown in my backtest but did not raise Sharpe.

### Cited Findings
- LongTail Alpha (Bhansali et al., "Risk Parity with Trend-Following", 14 Mar 2025). The authors sell hedging products, so treat this as vendor research — [LongTail Alpha PDF](https://www.longtailalpha.com/wp-content/uploads/2025/09/LongTail-Alpha-Risk-Parity-With-Trend-Following-1.pdf):
  - Stock-bond risk parity worked from the early 1990s to 2020 because of negative stock-bond correlation, positive returns on both, and cheap leverage.
  - In 2022 the correlation turned positive "for the first time in over a decade" and bond volatility spiked.
  - Mean-reversion strategies, including volatility selling, suffered in 2022 while trend did well.
  - Adding a simple trend index (Credit Suisse 15% vol) to stock-bond risk parity, Jan 1999 to Nov 2023, raised CAGR from 8.46% to 9.49%, Sharpe from 0.56 to 0.63 and Sortino from 0.89 to 1.14, and cut skew from -0.90 to -0.19. This is levered and long/short, not long-only.
- Figures for the 2022 risk-parity drawdown conflict across secondary sources:
  - RPAR 2022 year-to-date -28.4% — [Picture Perfect Portfolios](https://pictureperfectportfolios.com/rpar-risk-parity-etf-review/)
  - Risk parity fell 30.3% from Dec 2021 to Sep 2022, called its worst fall in 61 years — [Thor FT](https://thorft.com/insights/risk-parity); [AssetWhisper](https://assetwhisper.com/risk-parity-volatility-targeting/)
  - A third source reports RPAR at -11.8% for 2022, which looks inconsistent with the others — [Money365](https://www.money365.market/articles/risk-parity-portfolio)
  - All are secondary and need checking against fund data.
- One secondary source gives stock-bond correlation regimes of +0.29 (1965-1999), -0.34 (2000-2020) and +0.50 (2021-2026). The same source says volatility targeting cut the 2007-09 loss from about 50% to 30% but its Sharpe gain did not persist — [AssetWhisper](https://assetwhisper.com/risk-parity-volatility-targeting/)
- In Hurst et al., the TSMOM strategy is vol-scaled. Each position is sized by its own volatility, and the portfolio targets 10% ex-ante vol — [Hurst/Ooi/Pedersen PDF](https://www.trendfollowing.com/whitepaper/Century_Evidence_Trend_Following.pdf)
- Allocate Smartly's 2022 review: long Treasuries as the default defensive asset failed. Momentum-filtered defensive assets with a cash backstop did much better — [the7circles summary](https://the7circles.uk/?p=41965)

### Inferences
- OWN BACKTEST, SPY-IEF daily correlation by year:
  - 2015 through 2020: -0.35, -0.39, -0.36, -0.39, -0.49, -0.51.
  - 2021 through 2026 YTD: -0.09, +0.19, +0.04, +0.07, -0.09, +0.29.
  - The bond hedge that powered 2000-2020 risk parity has been unreliable since 2021. Any rotation design should keep T-bills, not IEF, TLT or AGG, as the unconditional fallback, and hold bonds only when they themselves trend up.
- OWN BACKTEST: inverse-vol, long-only TSMOM over 8 ETFs had the lowest volatility (4.8%) and drawdown (-8.3%) of all rules, but the lowest CAGR (3.8%). Vol-weighting makes it a low-risk sleeve. Without leverage, which a retail long-only bot should not use, it cannot match equity-like returns.
- Levered risk parity (RPAR-style or futures) falls outside the "long-only, cash allowed" constraint and has a poor 2022 record. I would not recommend it as the bot's new lane.

### Gaps
- I found no primary 2015-2026 performance series for a published risk-parity index. RPAR's 2022 return conflicts across sources and was not verified against the fund's own reports.
- I did not retrieve primary research on equity vol targeting after 2015 (e.g., Moreira and Muir, or Harvey et al. 2018).

## Q4. What do costs and taxes do to monthly-rebalanced ETF strategies at small size?

### Takeaway
At about $100k with commission-free execution, spreads on the core ETFs are a fraction of a basis point to a few basis points, so trading costs barely matter for monthly trend rules (about 4x a year turnover, roughly 1 bp a year of drag per bp of cost). They start to matter for weekly or concentrated rotation (10-11x a year). The real drags are fund fees, if you buy a packaged fund such as GMOM at 1.11%, and in a taxable account the short-term gains from monthly switching.

### Cited Findings
- Average ETF spreads, ETF.com. The search summary converted these to bps incorrectly; 0.01% = 1 bp. Fixed-income ETFs as a group average 0.25% (median 0.14%), but that includes many illiquid funds — [ETF.com spreads ranking](https://www.etf.com/sections/news/etfs-highest-lowest-trading-spreads); [ETF.com on why spreads matter](https://www.etf.com/sections/news/why-trading-spreads-matter-etfs):
  - SPY about 0.0032% (about 0.3 bp)
  - GLD about 0.0065-0.0086% (about 0.7-0.9 bp)
  - TLT about 0.0084% (about 0.8 bp)
- Quanterlab's GEM study assumed 10 bps per one-way traded dollar — [Quanterlab](https://quanterlab.com/research/antonacci-s-dual-momentum-taken-apart-gate-relative-leg-and-blends-across-four-l)
- Baltas and Kosowski: better volatility estimators (range-based, e.g. Yang-Zhang) and a trend rule that skips trades when no significant trend exists can cut turnover by more than a third, without significant performance loss. Turnover falls by about two-thirds compared with a sign-of-return rule. Costs scale with turnover — [SSRN 2140091](https://papers.ssrn.com/abstract=2140091); [CME summary](https://www.cmegroup.com/education/files/demystifiing-time-series-momentum-strategies.pdf)
- GMOM charges a 1.11% expense ratio — [SEC N-CSR FY2024](https://www.sec.gov/Archives/edgar/data/1529390/000199937124008322/cmba-ncsr_043024.htm)
- In Hurst et al., simulated 2-and-20 fees cut 14.9% gross to 11.2% net — [Hurst/Ooi/Pedersen PDF](https://www.trendfollowing.com/whitepaper/Century_Evidence_Trend_Following.pdf)

### Inferences
- OWN BACKTEST, CAGR 2015-01 to 2026-10 at 0 / 5 / 25 bps per one-way turnover:
  - GTAA5: 4.83% / 4.62% / 3.79%
  - TSMOM ensemble monthly: 3.99% / 3.75% / 2.81%
  - TSMOM ensemble weekly: 4.35% / 3.82% / 1.70%
  - Top-3 relative momentum: 7.17% / 6.58% / 4.25%
  - GEM: 7.30% / 7.11% / 6.35%
- At realistic costs for SPY, EFA, IEF, TLT, GLD and BIL (under 2-5 bps all-in at this size), monthly rules lose about 0.2%/yr. Weekly rebalancing doubles turnover and did not improve net results, so weekly is not justified on this evidence.
- DBC, EEM, VNQ, HYG and LQD have wider spreads than SPY. Using limit or marketable-limit orders near the close keeps costs near the 5 bp assumption. That is an inference; I did not measure Alpaca fills.
- Taxes: monthly switching produces mostly short-term gains, taxed as ordinary income in a taxable US account. This does not affect the paper bot, but it matters before any live capital. No source was retrieved, so treat this as standard tax treatment to be confirmed.

### Gaps
- I did not retrieve current spreads for IEF, BIL, SGOV, DBC, VNQ or EEM, or Alpaca's execution quality for ETFs at the close.
- I retrieved no quantitative source on the tax drag of monthly TAA.

## Q5. How would a rotation or trend lane interact with the bot's short-horizon equity book: return correlation, and do the drawdowns coincide?

### Takeaway
Cross-asset ETFs give more independent bets than the bot's equity universe. Across 8 cross-asset ETFs the average weekly correlation was 0.28, about 3.6 effective bets, against 0.59 and about 2.3 effective bets across 9 sector ETFs. Long-only trend has only moderate correlation to SPY (0.49-0.60 monthly) and lost far less in SPY's worst months. It held up in the equity-reversion proxy's worst months too. But a 50/50 blend did not raise Sharpe. It mostly cut drawdown, much as adding cash would, so the gain is risk reduction, not more return per unit of risk.

### Cited Findings
- SG Trend Index correlation to Global 60/40: -0.1 since 2000, +0.2 in the 2010s, -0.3 for 2020 to Sep 2022 — [AQR 4Q 2022](https://www.aqr.com/-/media/AQR/Documents/White-Papers/Trend-Following-Why-Now-A-Macro-Perspective.pdf?sc_lang=en)
- Century TSMOM: correlation 0.00 to US equities and -0.04 to 10-year bonds, 1880-2013 — [Hurst/Ooi/Pedersen PDF](https://www.trendfollowing.com/whitepaper/Century_Evidence_Trend_Following.pdf)
- In 2022 mean-reversion strategies, including volatility selling, suffered while trend did well — [LongTail Alpha PDF](https://www.longtailalpha.com/wp-content/uploads/2025/09/LongTail-Alpha-Risk-Parity-With-Trend-Following-1.pdf)
- A trading-systems text reports that the low correlation between trend-following and mean-reversion systems smoothed equity drawdowns in a combined portfolio — [O'Reilly, Mechanical Trading Systems](https://www.oreilly.com/library/view/mechanical-trading-systems/9780471730972/chapter09-sec003.html)
- Graham Capital describes trend and mean reversion as opposites that can still line up at times, depending on timescale. An arXiv paper finds that markets trend over the medium term and mean-revert over longer horizons, with the sign changing around 2 years. So the diversification depends on horizon — [Graham Capital](https://www.grahamcapital.com/quant-post/diversification-by-model-when-opposites-attract); [arXiv 1711.04717](https://ar5iv.labs.arxiv.org/html/1711.04717)

### Inferences
- OWN BACKTEST, monthly return correlations 2015-01 to 2026-10. The proxy book is SPY RSI(2), a stand-in for the bot's book, not the book itself.

  | Strategy | Corr. to SPY | Corr. to RSI(2) proxy |
  |---|---|---|
  | GTAA5 | 0.58 | 0.43 |
  | GTAA5 ensemble | 0.60 | 0.45 |
  | TSMOM ensemble | 0.49 | 0.29 |
  | Top-3 relative momentum | 0.47 | 0.21 |
  | GEM | 0.83 | 0.31 |
  | 60/40 | 0.96 | 0.21 |

  Long-only trend is far from the near-zero correlation of long/short managed futures, because it is long equities during uptrends.
- OWN BACKTEST, in SPY's worst 10% of months (15 months), mean monthly returns were:
  - SPY -7.08%, 60/40 -4.31%, GEM -6.20%
  - GTAA5 -1.67%, TSMOM ensemble -0.92%, RSI(2) proxy -0.89%
- In the proxy book's worst 10% of months, mean returns were:
  - Proxy itself -2.08%, SPY -3.26%, 60/40 -1.91%, GEM -3.54%
  - GTAA5 -1.41%, TSMOM ensemble -0.98%
  - The trend lane's bad months only partly overlap with the reversion proxy's bad months.
- OWN BACKTEST, 50/50 daily-rebalanced blends, 2015-01 to 2026-10:

  | Portfolio | CAGR | Sharpe | Max DD |
  |---|---|---|---|
  | RSI(2) proxy alone | 5.85% | 0.65 | -9.4% |
  | Proxy + 50% TSMOM ensemble | 4.85% | 0.66 | -6.7% |
  | Proxy + 50% GTAA5 | 5.31% | 0.63 | -7.2% |
  | Proxy + 50% 60/40 | 7.43% | 0.80 | -12.1% |
  | SPY alone | 13.51% | 0.70 | -33.7% |
  | SPY + 50% TSMOM ensemble | 8.86% | 0.69 | -19.4% |
  | 50% SPY + 50% BIL | 8.00% | 0.70 | -17.9% |

  Over this window the trend lane is mainly a drawdown reducer. It added about 0.9%/yr over a cash blend at about the same Sharpe and drawdown. It did not lift portfolio Sharpe.
- OWN BACKTEST, breadth measured on weekly returns, 2015-2026:
  - 8 cross-asset ETFs: average pairwise correlation 0.28, effective number of bets (eigenvalue participation ratio) 3.6 of 8.
  - 9 SPDR sector ETFs: 0.59 and 2.3 of 9.
  - In 2022: 0.33 and 3.5 for cross-asset, against 0.64 and 2.0 for sectors.
  - Cross-asset ETFs give roughly 1.5x the independent bets of a US-equity universe. With monthly rebalancing that still means only about 12 decisions a year over 3-4 effective bets. It is a slow evidence engine, not a fix for the bot's problem of collecting little independent evidence per week.
- The bot should repeat the correlation and drawdown-overlap test on its own realized daily P&L, not on the RSI(2) proxy. The proxy is one SPY-only rule and understates the bot's breadth across 45 names.

### Gaps
- I found no published study that directly measures the correlation between long-only ETF TAA and short-horizon single-stock mean-reversion books.
- The proxy book is a stand-in. The bot's actual book, with its correlated same-day stop-outs, may have a different crash profile.

## Q6. How should lookbacks and other parameters be chosen without overfitting?

### Takeaway
The literature agrees that trend signals are closely related to each other: TSMOM and moving-average crossovers are equivalent in general form. Single-parameter choices (9- vs 10-month, rebalance day) produce large, random, permanent differences. Published timing-rule performance carries real data-mining bias. Best practice is to fix a simple ensemble in advance: 1/3/6/12-month TSMOM or 6-12-month SMAs. Average across rebalance days by running weekly tranches, prefer lower-turnover signal designs, and judge only on data the rules have not seen.

### Cited Findings
- Levine and Pedersen, "Which Trend Is Your Friend?" (FAJ 2016): TSMOM and moving-average crossovers are closely linked, and in their most general forms equivalent. Both also cover HP and Kalman filters. On 58 futures, 12-month windows worked better than 1- or 3-month windows — [CFA Institute](https://rpc.cfainstitute.org/research/financial-analysts-journal/2016/which-trend-is-your-friend); [CBS working paper](https://research-api.cbs.dk/ws/files/60084063/lasse_heje_pedersen_et_al_which_trend_is_your_friend_publishersversion.pdf)
- Zakamulin, papers on moving-average timing:
  - Journal of Asset Management 2014: reported performance of such rules usually contains considerable data-mining bias and ignores market frictions. Out-of-sample tests with realistic costs show performance is "highly overstated" — [SSRN 2242795](https://papers.ssrn.com/abstract=2242795); [EconPapers](https://econpapers.repec.org/RePEc:pal:assmgt:v:15:y:2014:i:4:d:10.1057_jam.2014.25)
  - 155-year study: no statistically significant outperformance in the second half of the sample. Neither the weighting shape nor the estimation scheme improved results. Rules tend to outperform in bear states and generate many false signals — [SSRN 2677212](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2677212)
  - A further paper tests 300 weighting shapes out of sample, 1870-2010 — [SSRN 2585056](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2585056); [Alpha Architect](https://alphaarchitect.com/2016/04/the-moving-average-research-king-valeriy-zakamulin/)
- Baltas and Kosowski: range-based vol estimators and a trend rule that ignores weak trends cut turnover substantially without significant performance loss — [SSRN 2140091](https://papers.ssrn.com/abstract=2140091)
- Hurst et al. deliberately average 1-, 3- and 12-month signals to avoid arbitrary choices — [Hurst/Ooi/Pedersen PDF](https://www.trendfollowing.com/whitepaper/Century_Evidence_Trend_Following.pdf)
- Newfound: equal-weight ensembles across lookbacks land near the median outcome with better Sharpe and drawdown than a typical single variant. Diversify across what, how and when; specification risk can be controlled, style risk cannot — [Flirting with Models](https://blog.thinknewfound.com/2019/01/fragility-case-study-dual-momentum-gem/)
- Allocate Smartly: minimizing max drawdown in design invites overfitting, and month-end effects in monthly backtests may be overfit — [Allocate Smartly, BAA](https://allocatesmartly.com/bold-asset-allocation/); [Allocate Smartly, day of month](https://allocatesmartly.com/diving-deeper-does-the-day-of-the-month-matter/)
- Quanterlab found that blends added after the main walk-forward did not beat the best single cells, and explicitly flagged the risk of selection inflation — [Quanterlab](https://quanterlab.com/research/antonacci-s-dual-momentum-taken-apart-gate-relative-leg-and-blends-across-four-l)

### Inferences
- OWN BACKTEST: the GTAA5 SMA ensemble (6-12 months) beat the single 10-month SMA on Sharpe in both windows: 0.47 vs 0.41 from 2015, 0.46 vs 0.37 from 2008, with similar CAGR. This matches the ensemble literature, though the edge is small and not significant.
- OWN BACKTEST, timing luck: shifting the monthly rebalance 0-15 trading days before month-end changed 2015-2026 max drawdown a lot.

  | Strategy | CAGR range | Max DD range |
  |---|---|---|
  | GTAA5 | 4.56-4.82% | -9.2% to -20.4% |
  | TSMOM ensemble | 3.68-4.39% | -8.3% to -13.5% |
  | Top-3 relative momentum | 4.86-10.57% | -15.6% to -28.5% |
  | GEM | 7.07-9.97% | -29.1% to -33.7% |

  A pre-registered test should report the distribution across rebalance days, not one day. Running four weekly tranches, each rebalancing monthly on a different week, is the practical fix.
- With roughly 12 years of post-2015 data and Sharpe around 0.4, no parameter grid can be validated statistically. Parameters should come from the long-history literature (1/3/6/12-month TSMOM, 10-month SMA family) and be frozen. The 2015-2026 data should only confirm sign and risk properties.

### Gaps
- I could not retrieve full-text numbers from Baltas and Kosowski (exact Sharpe and turnover tables) or Zakamulin's ETF-era results.

## Q7. Which concrete designs should the bot backtest under a pre-registered rule, and what historical evidence supports each?

### Takeaway
Recommend one primary lane and one alternate. Primary (A): a long-only, inverse-volatility TSMOM ensemble over about 8 diversified ETFs with T-bills as the fallback, run as four weekly tranches. Alternate (B): a Faber-style GTAA5 with a 6-12-month SMA ensemble. Expect about 2-3%/yr over cash, Sharpe around 0.4-0.5, max DD around 10%. Judge them as a portfolio drawdown reducer and diversifier, not as an alpha source that clears the bot's profit gate on its own. Rotation rules (GEM, top-k relative momentum) should not be the primary lane: they correlate highly with SPY (GEM) or carry heavy timing luck (top-k).

### Cited Findings
- Design A's construction follows Hurst et al. (1/3/12-month TSMOM averaged, vol-scaled), extended here with a 6-month window — [Hurst/Ooi/Pedersen PDF](https://www.trendfollowing.com/whitepaper/Century_Evidence_Trend_Following.pdf)
- Using T-bills as the fallback rather than bonds is supported by Allocate Smartly's 2022 review — [the7circles summary](https://the7circles.uk/?p=41965)
- Lookback ensembles and spreading trades across time to cut specification and timing risk come from Newfound — [Flirting with Models](https://blog.thinknewfound.com/2019/01/fragility-case-study-dual-momentum-gem/)
- Equivalence of TSMOM and moving-average signals, so there is no need to search across signal families: Levine and Pedersen — [CFA Institute](https://rpc.cfainstitute.org/research/financial-analysts-journal/2016/which-trend-is-your-friend)
- Design B follows Faber's 10-month SMA timing rule — [PortfolioWiser](https://www.portfoliowiser.com/knowledge-hub/meb-faber-gtaa)
- Live real-money check on the family: GMOM, about 3.7%/yr since Nov 2014 to Apr 2024 after a 1.11% fee — [SEC N-CSR FY2024](https://www.sec.gov/Archives/edgar/data/1529390/000199937124008322/cmba-ncsr_043024.htm)
- The bond and international legs hurt GEM after 2008 (international leg -1.4 to -3.2 points a year) — [Quanterlab](https://quanterlab.com/research/antonacci-s-dual-momentum-taken-apart-gate-relative-leg-and-blends-across-four-l)

### Inferences
- **Design A, primary: "XA-TSMOM-IV".**
  - Universe, fixed in advance: SPY, EFA, EEM, IEF, TLT, GLD, DBC, VNQ. Optional additions TIP and LQD would need to be pre-declared.
  - Cash: BIL, or SGOV after 2020.
  - Signal per asset: the fraction of {21, 63, 126, 252}-trading-day total returns that beat BIL's total return over the same window. Values are 0, 0.25, 0.5, 0.75 or 1.
  - Weight: inverse 63-day realized vol, normalized across all 8 assets, times the signal. The remainder goes to cash. No leverage, long-only.
  - Rebalance: monthly, in 4 weekly tranches of 25% each, trading at the next close after the signal.
  - OWN BACKTEST, single tranche at month-end, 2015-01 to 2026-10 net of 5 bp: CAGR 3.8%, vol 4.8%, Sharpe 0.39, max DD -8.3%, 2022 -3.8%, turnover 4.6x a year, average cash weight 43%. From 2008-07: CAGR 4.1%, Sharpe 0.54, max DD -8.3%.
  - Rationale: the most diversified, lowest drawdown and lowest correlation to the RSI(2) proxy (0.29). Weak on absolute return. A higher-return variant would scale the risky sleeve to a fixed vol target, say 8-10%, capped at 100% invested.
- **Design B, alternate: "GTAA5-ENS".**
  - 20% each in SPY, EFA, IEF, DBC and VNQ. Each sleeve is held in proportion to the fraction of SMA lengths {126, 147, ..., 252} trading days (6 to 12 months) for which the price is above its SMA. The rest goes to BIL. Monthly, in tranches as in Design A.
  - OWN BACKTEST 2015-01 to 2026-10: CAGR 5.0%, Sharpe 0.47, max DD -10.5%, 2022 -4.7%, turnover 3.8x a year. From 2008: 4.6%, Sharpe 0.46, max DD -12.7%.
  - Rationale: the longest published track record (Faber's original paper is from 2007), simple, and the best Sharpe of the rules tested here. But it is more equity-correlated (0.60 to SPY).
- **Not recommended as primary:**
  - GEM: monthly correlation 0.83 to SPY, -33.7% DD, -17.3% in 2022.
  - Top-k relative momentum: 11x a year turnover and a 4.9-10.6% CAGR spread from rebalance day alone.
  - Levered risk parity: breaks the long-only constraint and has a poor 2022 record.
- **Suggested pre-registered gate for this family.** The bot's existing gate likely expects stand-alone profit. This family's evidence supports a portfolio-level gate instead:
  1. Net CAGR above BIL by at least 1.5%/yr over the full 2008-2026 history and over the 2015-2026 sub-period.
  2. Max DD no more than 15% for the median across rebalance-day offsets.
  3. A 50/50 blend with the bot's actual equity-book daily P&L shows lower max DD and Sharpe no lower than the book alone.
  4. Results hold at 25 bp costs.
  - Designs A and B pass 1, 2 and 4 in my backtest. Criterion 3 passed only on drawdown against the RSI(2) proxy (Sharpe flat: 0.66 vs 0.65 for A, 0.63 vs 0.65 for B). It must be tested on the real book.
- **Expectation management.** Over 2015-2026 the lane would have earned about 4-5%/yr. It would have lagged SPY by 8-9 points a year, cut 2022 losses from about -18% to about -4% to -6%, and given back relative performance in 2023-2024. It will not clear a "beat SPY" bar. Its value rests on long-history crisis evidence (positive in 8 of 10 large 60/40 drawdowns for long/short TSMOM) and on adding 1-2 effective bets beyond the equity book.

### Gaps
- I did not test the vol-targeted variant of Design A, TIP or LQD additions, or SGOV in place of BIL. These would need pre-declaring before any run.
- My backtest uses yfinance adjusted closes as a total-return proxy. It does not model dividend timing, Alpaca fill quality or intramonth stops, and it was not run on the bot's own P&L.
- I found no published out-of-sample record specific to a long-only, inverse-vol, multi-lookback TSMOM ETF portfolio. Evidence for Design A is inferred from the long/short futures literature plus my own backtest.
