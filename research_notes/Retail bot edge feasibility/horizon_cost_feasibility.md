# Retail bot edge feasibility: break-even economics of trading horizons for a small Alpaca bot in liquid US large caps and ETFs

Scope: US equities and US-listed ETFs, long-only main book (short pilot noted), ~$100k paper equity, $1-5k orders, ~45 megacap/index/sector-ETF symbols, current holding period 1-20 sessions. Researched 2026-11-07. Cost figures are in basis points (bps). "Round trip" (RT) means one buy plus one sell.

## Q1. Realistic round-trip costs today for small retail orders in S&P 500 names and liquid ETFs

### Takeaway
For $1-5k market orders in megacaps and the big ETFs, the explicit spread cost is tiny: about 0.2-1 bps quoted for SPY/QQQ, about 1-5 bps quoted for megacaps, and wholesalers give back roughly 45% of the quoted spread. That puts the spread-only round trip at about 1-5 bps. Across a representative cross-section of US stocks, measured all-in round trips at commission-free brokers ran 7-46 bps (Schwarz et al. 2025), and the average stock or anomaly portfolio pays 16-111 bps. A defensible all-in planning number for this bot is 5-10 bps RT. That covers spread plus signal-to-fill slippage and open/close timing. Use 25 bps as a stress case. Price impact at this size is effectively zero.

### Cited Findings
**Commission-free broker execution (Schwarz, Barber, Huang, Jorion, Odean, JF 2025)**
- The authors placed 85,000 simultaneous market orders through six accounts at five brokers. Mean round-trip cost excluding commissions ranged from 0.07% to 0.46% (7-46 bps) depending on the broker. The dispersion comes from off-exchange wholesalers giving different brokers different prices for identical orders. PFOF differences across brokers do not explain it. — [SSRN abstract](https://papers.ssrn.com/abstract=4189239); [JF 80(5) 2507-2541 listing](https://ideas.repec.org/a/bla/jfinan/v80y2025i5p2507-2541.html)
- Design details from the working-paper version: 74,801 small market orders of $100 or $1,000 in 128 stocks priced over $1, chosen to be representative of US common stocks. Buys and sells were paired within 30 minutes, 9:40-15:50 ET, from 2021-12-21 to 2022-06-09. Costs were measured against the prevailing NBBO. TD Ameritrade was best and Interactive Brokers worst. Alpaca was not in the sample. — [CXO Advisory summary](https://www.cxoadvisory.com/individual-investing/actual-stock-trading-frictions-by-broker)
- Broker ranking from best to worst: TD Ameritrade, Fidelity, E*Trade, Robinhood, then two IBKR platforms. Average price improvement was 3-8 cents per share. The researchers lost about $23,000 on the experiment. The average retail trade is about $8,000. — [WashU Olin news, 2022](https://olin.washu.edu/about/news-and-media/news/2022/10/study-finds-astonishing-differences-in-brokers-pricing-of-stock-trades.php)

**Price improvement in S&P 500 names (Dyhrberg, Shkilko, Werner, "The Retail Execution Quality Landscape")**
- In the July 2023 NBER version, retail orders in an average S&P 500 stock get price improvement of 44% of the quoted spread at wholesalers. On exchanges the figure is 6% (3% in the full sample). The average retail order across the full sample gets 24%. — [NBER conference paper](https://conference.nber.org/conf_papers/f187847/f187847.pdf)
- The December 2023 AEA version reports 47% of the quoted spread for S&P 500 stocks and 5% on exchanges. It gives the average S&P 500 quoted spread as 11.7 cents and wholesaler price improvement in S&P 500 names as more than 2 cents. — [AEA 2024 program paper](https://aeaweb.org/conference/2024/program/paper/5Gtsa7ra)

**ETF and large-cap spread levels**
- SPY's average bid/ask spread was 0.21 bps in March 2024, against 0.42 for IVV and 0.45 for VOO. It was 0.27 bps in June 2024 (IVV 0.54, VOO 0.50). — [SSGA SPY Capital Markets Perspective Q1 2024](https://ssga.com/library-content/products/fund-docs/etfs/us/insights-investment-ideas/spy-capital-markets-perspective-q1-2024.pdf); [Q2 2024](https://ssga.com/library-content/products/fund-docs/etfs/us/insights-investment-ideas/spy-capital-markets-perspective-q2-2024.pdf). SSGA is the issuer, so this is marketing-adjacent, though consistent with the next source.
- ETF.com tables list SPY at about 0.003-0.004% (0.3-0.4 bps) and QQQ at 0.004-0.007% (0.4-0.7 bps). Sector SPDRs such as XLK and XLV are "slightly wider". The two ETF.com tables disagree slightly, and the measurement period is unstated. — [ETF.com](https://www.etf.com/sections/news/etfs-highest-lowest-trading-spreads)
- For mega caps the minimum spread is about 1 bp, and the optimal spread in bps rises as market cap falls. — [Global Trading (Mackintosh)](https://www.globaltrading.net/?p=30712)
- NYSE example: a $50 stock trading 5 million shares a day averages a 4.28 bps spread. — [NYSE data insights](https://nyse.com/data-insights/stock-price-trading-dynamics-and-splits)
- For S&P 1500 stocks from 2003 to 2017, the average TAQ effective spread is about 12 bps. — [Federal Reserve FEDS 2019-051](https://www.federalreserve.gov/econres/feds/files/2019051pap.pdf)

**Cross-section and anomaly portfolios (where costs actually bite)**
- In 2014, NYSE effective spreads had a mode near 5 bps and a mean of 16 bps, and about 20% of NYSE stocks had effective spreads above 20 bps. Academic anomaly portfolios paid a mean of 67 bps in 2014, more than 4x the NYSE average. Over the post-publication period, the average spread paid by anomaly strategies was 111 bps. The paper notes that a 1-cent tick on a $20 stock is only 5 bps, but costs are extremely right-skewed and anomalies load on the expensive tail. — [Chen & Velikov, FEDS 2020-039 working paper](https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf)
- Low-frequency spread estimators are biased upward by 25-50 bps after 2003. Chen and Velikov say this means the low-frequency costs used by Novy-Marx & Velikov (2016) overstate costs going forward. — [Chen & Velikov WP](https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf)
- Round-trip costs for typical value-weighted anomaly strategies average more than 50 bps (sample to 2013). Equal-weighted strategies cost 2-3x more. — [Novy-Marx & Velikov 2016 (RFS) working paper](https://mysimon.rochester.edu/novy-marx/research/ToAatTC.pdf)

**Institutional comparison (Frazzini, Israel, Moskowitz, "Trading Costs")**
- The study uses $1.7 trillion of live AQR executions across 21 developed markets over 19 years. Mean market impact is about 9 bps (median just over 6 bps), and about 15 bps when dollar-weighted. The average spread at order arrival was about 21 bps, but patient limit-order execution rarely paid even half of it. About 85% of impact is permanent. Live costs were roughly an order of magnitude below the earlier literature's estimates. — [SSRN](https://papers.ssrn.com/abstract=3229719); [Alpha Architect summary](https://alphaarchitect.com/the-best-research-paper-ever-written-on-trading-costs/); [ETF.com Swedroe summary](https://www.etf.com/sections/index-investor-corner/swedroe-trading-cost-truths)

**Alpaca specifics**
- Alpaca's Rule 606 report for Q2 2026 (April 2026 month) covers non-directed S&P 500 orders. Of market orders, 33.5% went to Jane Street, 30.5% to Virtu, 29.4% to Citadel Securities and 6.6% to GTS. Net PFOF on market orders was about 73-95 cents per 100 shares, depending on venue. Virtu and Jane Street pay 12% of the spread per share, capped at 5 cents, for marketable core-session orders. Opening and closing auction fills receive no rebate. — [Alpaca SEC 606(a)(1) 2026Q2](https://files.alpaca.markets/disclosures/library/SEC+606a1+-+2026Q2.pdf)
- A FINRA letter (SD-2436, March 2025) states that Alpaca failed to disclose specific PFOF arrangement details in its 606(a)(1) report. — [FINRA SD-2436](https://finra.org/sites/default/files/2025-03/SD-2436-Alpaca-03-06-2025.pdf)
- Alpaca paper trading fills against live NBBO quotes: buys at the ask, sells at the bid. It gives no price improvement and does not model market impact, latency slippage or queue position. MOO/MOC orders are not differentiated from regular orders. — [Alpaca forum: paper vs real slippage](https://forum.alpaca.markets/t/slippage-paper-trading-vs-real-trading/2801); [Alpaca support: paper vs live](https://alpaca.markets/support/difference-paper-live-trading); [TradersPost summary of Alpaca docs](https://blog.traderspost.io/article/alpaca-paper-trading); [QuantConnect Alpaca docs](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/brokerages/alpaca). These are staff forum replies and third-party summaries, not the canonical doc page.

### Inferences
- **Spread-only round-trip cost by bucket, assembled from the findings above:**

  | Bucket | Quoted spread | Effective RT after ~45% price improvement |
  |---|---|---|
  | SPY, QQQ, IVV/VOO | ~0.2-0.7 bps | ~0.2-0.8 bps |
  | Sector SPDRs, other liquid sector/index ETFs | "slightly wider", likely 1-3 bps (not verified) | ~1-3 bps |
  | Megacaps (AAPL/MSFT/NVDA-type, high price, 1-cent tick) | ~1 bp | ~1-2 bps |
  | Average S&P 500 name | 11.7 cents, roughly 3-8 bps depending on price | ~4-10 bps |
  | Broad NYSE / anomaly-style universes | 16 bps mean | 30-110+ bps RT paid by anomaly portfolios |

- **Alpaca's average spread on S&P 500 flow.** PFOF of 0.73-0.95 cents per share at "12% of spread" implies an average quoted spread on Alpaca's S&P 500 market-order flow of about 6-8 cents. That is consistent with Dyhrberg et al.'s 11.7-cent average quote.
- **Where Alpaca sits in the Schwarz range is unknown.** Its flow goes to the same wholesalers whose broker-specific pricing created the 7-46 bps range. Assume mid-range until the bot's own fills are measured.
- **For this bot, timing costs probably exceed the spread.** Spread is not the dominant cost for $1-5k orders in megacaps and ETFs. Implementation shortfall from acting on a daily-close or IEX-minute-bar signal at a later fill likely matters more. It hurts most at the open, when spreads and volatility are highest. This is unmeasured, so carry 5-10 bps RT all-in as a base case and 25 bps as a stress case.
- **Paper fills mislead in both directions.** They overstate spread cost, because they get no price improvement. They understate latency and auction slippage, which they do not model.
- **The cost gap to anomaly portfolios is large.** By staying in megacaps and ETFs the bot pays roughly 10-50x less per round trip than the academic anomaly portfolios behind the 4-10 bps/month net figures. The catch is that most anomaly gross returns live in the expensive names (see Q4).

### Gaps
- No public study measures Alpaca's actual execution quality: no Rule 605 effective/quoted ratio and no Schwarz-style audit. The bot's own fill log against the NBBO midpoint at decision time is the only way to get it.
- I found no clean 2023-2026 effective spreads in bps by market-cap decile from TAQ. The figures above are pieced together from 2014 (NYSE), 2003-2017 (S&P 1500) and 2024 (SPY) sources.
- No verified sector-SPDR spreads in bps for 2024-2026.
- Opening-auction and first-five-minute spread multiples for megacaps are unsourced.
- The status and first data from the amended SEC Rule 605 (2024 adoption) were not verified.

## Q2. Break-even arithmetic: gross edge and turnover needed for a net Sharpe of 0.5

### Takeaway
Cost per round trip is fixed, while the return a trade must earn for a given Sharpe grows linearly with holding period. Costs therefore dominate at 1-5 days and are close to irrelevant beyond 60 days. At 10 bps RT, a fully invested strategy that turns over daily pays about 25% a year in costs. At a 5-day hold it pays about 5% a year, at 20 days about 1.3%, at 60 days about 0.4%, and at 250 days about 0.1%. With realistic per-trade volatility, costs absorb 50-85% of the required gross edge at h=1 day and 30-50% at h=5 days. They absorb about 10-25% at h=20 days and under 10% from h=60 days.

### Cited Findings
- Novy-Marx & Velikov rule of thumb: transaction costs reduce realized spreads by more than 1% of monthly one-sided turnover. A long side turning over 20% a month loses at least 20 bps a month. — [Novy-Marx & Velikov WP](https://mysimon.rochester.edu/novy-marx/research/ToAatTC.pdf)
- Chen & Velikov decomposition: the return lost to costs ≈ 2-sided turnover × average spread paid. A typical anomaly turns over 15% of the long side plus 15% of the short side each month (30% total), and 30% × 111 bps ≈ 32-33 bps a month lost. — [Chen & Velikov WP](https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf)

### Inferences
**Method (my own calculation, not from a source).**
- Assume back-to-back, non-overlapping trades of length h trading days, always fully invested. That gives n = 252/h round trips a year.
- Per-trade volatility is σ·√(h/252). The net mean needed per trade for an annual net Sharpe S is S·σ·h/252, spread across K independent concurrent bets.
- Gross needed per trade = c + S·σ·h/(252·√K). Annual cost drag = c·252/h.
- The win rate is the normal approximation Φ(gross/per-trade vol).
- S = 0.5 throughout. For a long-only bot, read σ as the volatility of the bet relative to the benchmark (SPY). A single megacap's total volatility is about 25%. A diversified active tilt has a tracking error of about 10%.

**Table A: single position, σ = 25% (one megacap at a time), K = 1**

| h (days) | Round trips/yr | Per-trade vol | Net needed/trade | Gross/trade @ c=4 | c=10 | c=25 | c=50 | Cost share of gross @ c=10 | Annual cost drag @ c=10 | Win rate needed @ c=10 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 252 | 1.57% | 5.0 bps | 9.0 | 15.0 | 30.0 | 55.0 | 67% | 25.2%/yr | 53.8% |
| 5 | 50.4 | 3.52% | 24.8 bps | 28.8 | 34.8 | 49.8 | 74.8 | 29% | 5.0%/yr | 53.9% |
| 20 | 12.6 | 7.04% | 99 bps | 103 | 109 | 124 | 149 | 9% | 1.26%/yr | 56.2% |
| 60 | 4.2 | 12.2% | 298 bps | 302 | 308 | 323 | 348 | 3% | 0.42%/yr | 60.0% |
| 250 | 1.0 | 24.9% | 1,240 bps | 1,244 | 1,250 | 1,265 | 1,290 | 1% | 0.10%/yr | 69.2% |

**Table B: diversified active tilt, tracking error σ = 10%, K = 1 (closest to a long-only sleeve vs SPY)**

| h (days) | Per-trade vol | Net needed/trade | Gross/trade @ c=4 | c=10 | c=25 | c=50 | Cost share @ c=10 | Gross annual edge needed @ c=4 / 10 / 25 / 50 |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.63% | 2.0 bps | 6.0 | 12.0 | 27.0 | 52.0 | 83% | 15.1% / 30.2% / 68% / 131% per yr |
| 5 | 1.41% | 9.9 bps | 13.9 | 19.9 | 34.9 | 59.9 | 50% | 7.0% / 10.0% / 17.6% / 30.2% |
| 20 | 2.82% | 39.7 bps | 43.7 | 49.7 | 64.7 | 89.7 | 20% | 5.5% / 6.3% / 8.2% / 11.3% |
| 60 | 4.88% | 119 bps | 123 | 129 | 144 | 169 | 8% | 5.2% / 5.4% / 6.1% / 7.1% |
| 250 | 9.96% | 496 bps | 500 | 506 | 521 | 546 | 2% | 5.0% / 5.1% / 5.3% / 5.5% |

**Table C: 10 independent concurrent bets, σ = 25% each.** Diversification shrinks the risk term but not the cost term.

| h | Gross/trade @ c=10 | Cost share @ c=10 |
|---|---|---|
| 1 | 11.6 bps | 86% |
| 5 | 17.8 bps | 56% |
| 20 | 41.4 bps | 24% |
| 60 | 104 bps | 10% |
| 250 | 402 bps | 2% |

The bot's 45 symbols are highly correlated (megacaps plus index and sector ETFs), so effective K is probably 3-5, not 45.

**Table D: annual cost drag when always invested**

| h | c=4 | c=10 | c=25 | c=50 |
|---|---|---|---|---|
| 1 day | 10.1% | 25.2% | 63% | 126% |
| 5 days | 2.0% | 5.0% | 12.6% | 25.2% |
| 20 days | 0.5% | 1.26% | 3.2% | 6.3% |
| 60 days | 0.17% | 0.42% | 1.05% | 2.1% |
| 250 days | 0.04% | 0.10% | 0.25% | 0.5% |

**Table E: turnover budget.** Maximum round trips per year (≈ annual one-sided turnover as a multiple of equity) for costs to take no more than 1/3 of a given gross annual edge.

| Gross annual edge | c=4 | c=10 | c=25 |
|---|---|---|---|
| 1% | 8 | 3 | 1 |
| 2% | 17 | 7 | 3 |
| 4% | 33 | 13 | 5 |
| 8% | 67 | 27 | 11 |

**Reading the tables**
- **At h = 1-5 days the requirement is a per-trade gross edge of about 12-35 bps** in megacaps (c=10), with costs taking 30-85% of it. The bot needs win rates of 54-58% on symmetric payoffs, sustained after decay. Any modest overestimate of edge or underestimate of slippage flips the sign.
- **At h = 20 days costs take about 10-25% of the required gross.** A 4% gross edge supports about 13 round trips a year at 10 bps (Table E), roughly a 20-day average hold. This is the point where feasibility starts.
- **At h ≥ 60 days costs are second-order.** The binding question becomes whether any gross edge exists at all. Required gross per trade rises to 1-3% per 60-day trade for a 0.5 Sharpe, and that is a statistical-power problem rather than a cost problem.
- **Novy-Marx & Velikov's "under 50%/month one-sided turnover" survival line is about a 2-month average hold.** It was derived at anomaly-portfolio costs of 50-110 bps RT. At megacap costs of 5-10 bps, a cost-equivalent line would sit about 5-10x higher, near a 1-2 week hold. But the gross edge per trade available in megacaps is also much smaller (Q3/Q4), so the economic optimum still lands in the weeks-to-months range.

### Gaps
- The bot's realized per-trade volatility, effective K (cross-position correlation), and fraction of time invested are not known here. All three shift Tables A-C, and the bot's own trade log should be substituted.
- The formula assumes independent, non-overlapping trades and normal returns. Reversal-type trades are short-volatility (negative skew), so the win-rate figures understate the tail risk.

## Q3. Where post-2015, cost-surviving effects concentrate by horizon

### Takeaway
The cost-surviving evidence clusters at low turnover: annually or monthly rebalanced value, profitability and size-style tilts, ideally combined into one portfolio with netted trades and buy/hold bands. Medium- to long-horizon trend (1-12 month lookbacks, about monthly trading) also survives costs, but it is mainly a diversified-futures result. Its net information ratio fell to about 0.2-0.3 in 2009-2019 and recovered after 2020. High-turnover effects (weekly reversal, daily reversal, intraday momentum, overnight drift, event drifts such as pre-FOMC) either fail costs in the broad literature or have no verified post-2015 net evidence. Even the "survivors" average only single-digit bps per month net post-2005.

### Cited Findings
**Average anomaly, net and post-2005 (Chen & Velikov, JFQA 2023)**
- Across 120 anomalies (working-paper version), the gross in-sample return is 66 bps a month. Net of costs it falls to 38 bps, post-publication net to 13 bps, and post-publication and post-2005 net to 8 bps. The strongest anomalies offer 10-20 bps after data-mining adjustments. Short-sale costs of 10-20 bps a month would wipe out the remainder. — [Chen & Velikov WP](https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf)
- The published version (204 anomalies) reports 4 bps a month for the average anomaly, about 10 bps for the strongest, and about 20 bps for combinations. — [JFQA 2023 (Cambridge)](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/zeroing-in-on-the-expected-returns-of-anomalies/945133D5A3ECEEAF466AEE91551FD225)
- In the post-publication, post-2005 sample (2006-2016), size, B/M and momentum net -26, +33 and +16 bps a month respectively. They netted 30-70 bps in 1998-2013. Monthly standard errors for 15-year samples are about 40 bps. Turnover is the only reliable out-of-sample predictor of net returns: the top turnover-sorted quartile delivers about 14 bps a month. Value-weighted-only implementations lose most predictability. — [Chen & Velikov WP](https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf)

**Turnover tiers (Novy-Marx & Velikov, RFS 2016; sample 1963-2013)**
- Low-turnover (annually rebalanced) strategies cost under 10 bps a month to trade. Mid-turnover strategies (monthly rebalanced, 14-35% turnover per side) cost 20-57 bps a month, often more than half the gross spread. High-turnover strategies (more than 90% a month) always cost over 1% a month when built naively, and only 2 of them had positive net returns: the high-frequency combo and low-volatility industry-relative reversals. — [Novy-Marx & Velikov WP](https://mysimon.rochester.edu/novy-marx/research/ToAatTC.pdf)
- SMB and HML cost about 5.5-5.7 bps a month to trade. UMD costs about 48 bps a month. Short-term reversal turnover is almost 3x momentum's. A buy/hold spread is the single most effective cost mitigation and cut turnover by 41% on average. — [Novy-Marx & Velikov WP](https://mysimon.rochester.edu/novy-marx/research/ToAatTC.pdf)

**Model comparison and portfolio combination**
- Detzel, Novy-Marx & Velikov (JF 2023), 1972-2017: after costs, the annually rebalanced Fama-French five-factor model beats the monthly rebalanced q-factor and Barillas-Shanken models, and cash-profitability variants do better still. Higher turnover (monthly reformation) causes greater degradation. — [EconPapers JF 78(3)](https://econpapers.repec.org/RePEc:bla:jfinan:v:78:y:2023:i:3:p:1743-1775); [CXO summary](https://www.cxoadvisory.com/equity-premium/impacts-of-frictions-on-factor-models-of-stock-returns/)
- DeMiguel, Martín-Utrera, Nogales & Uppal (RFS 2020): with transaction costs, 15 characteristics are jointly significant versus 6 without. Combining characteristics nets trades and cuts the marginal cost of trading a characteristic by about 65%. — [EconPapers RFS 33(5)](https://econpapers.repec.org/RePEc:oup:rfinst:v:33:y:2020:i:5:p:2180-2222.); [EDHEC summary (65%)](https://climateinstitute.edhec.edu/portfolio-perspective-multitude-firm-characteristics)
- Jensen, Kelly, Malamud & Pedersen, "Machine Learning and the Implementable Efficient Frontier" (RFS, in press May 2026): ML return forecasts that ignore costs lean on small-scale, short-lived characteristics and deliver poor net returns. Learning portfolio weights with a cost-aware objective gives a superior net frontier. — [SFI](https://www.sfi.ch/en/publications/machine-learning-and-the-implementable-efficient-frontier); [SSRN](https://papers.ssrn.com/abstract=4187217)
- Jensen, Kelly & Pedersen (JF 2023): 153 factors in 93 countries replicate at about 83-85% (CAPM-alpha significance, Bayesian). Post-publication decay is in line with Bayesian shrinkage expectations. This is gross of trading costs. — [Alpha Architect summary](https://alphaarchitect.com/is-there-a-replication-crisis-in-finance/); [NBER w28432](https://nber.org/system/files/working_papers/w28432/w28432.pdf)

**Short horizon (1 day to 1 month)**
- de Groot, Huij & Zhou (JBF 2012): restricting reversal to large caps cuts costs enough to leave 30-50 bps a week net. Their sample is pre-2010. — [ERIM abstract](https://www.erim.eur.nl/research/events/detail/1992/); [EconPapers](https://econpapers.repec.org/RePEc:eee:jbfina:v:36:y:2012:i:2:p:371-382)
- Medhat & Schmeling (RFS 2022), via Alpha Architect: short-term reversal earns 0.84% a month in large caps and 1.41% in small caps. Its average monthly transaction cost of 1.94% subsumes the gross return. — [Alpha Architect](https://alphaarchitect.com/short-term-momentum/)
- Reversal is effectively a short-volatility strategy. Returns are concentrated in the weeks after large market declines. — [Quantpedia](https://quantpedia.com/strategies/short-term-reversal-in-stocks)
- The pre-FOMC drift (Lucca-Moench, 1994-2011) "essentially disappeared after 2015", per Kurov, Wolfe & Gilbert (sample to 2019). Martello & Ribeiro argue the drift is time-varying and concentrated in high-uncertainty periods. — [Kurov, Wolfe & Gilbert](https://www.skidmore.edu/economics/documents/KurovWolfeGilbert-TheDisappearingPre-FOMC-Announce-Drift-200914.pdf); [Quantpedia on Martello-Ribeiro](https://quantpedia.com/fomc-equity-drift-occurs-in-periods-of-high-uncertainty/)

**Intraday**
- Gao, Han, Li & Zhou: in SPY 1993-2013, the first half-hour return predicts the last half hour (out-of-sample R² 1.2%), and the authors say it survives reasonable costs. I found no dedicated post-2013 cost-inclusive replication. — [working paper copy](https://c.mql5.com/forextsd/forum/173/intraday_momentum_-_the_first_half-hour_return_predicts_the_last_half-hour_return.pdf). This is a non-academic host for the PDF.
- Aleti, Bollerslev & Siggaard (Management Science 2025) build intraday predictors from lagged high-frequency cross-sectional returns. They report sizeable out-of-sample Sharpe ratios after costs for simple strategies in highly liquid ETFs. — [IDEAS](https://ideas.repec.org/a/inm/ormnsc/v71y2025i9p7731-7751.html)
- An Alpha Architect guest post is titled "Trading costs wipe out the overnight return anomaly" (2020). — [Alpha Architect](https://alphaarchitect.com/trading-costs-wipe-out-the-overnight-return-anomaly/)

**Trend / time-series momentum (1-12 month signals)**
- Hurst, Ooi & Pedersen, "A Century of Evidence on Trend-Following Investing": time-series momentum had positive average returns in every decade since 1880, with low correlation to traditional assets. — [paper PDF](https://fairmodel.econ.yale.edu/ec439/hurst.pdf)
- A secondary summary says AQR reported actual trading costs much lower than the estimates used in the study, and that the published results assume 2/20 fees. — [ETF.com Swedroe](https://www.etf.com/node/93905.md)
- Aspect Capital describes the decade after the GFC as a "CTA Winter" with information ratios of about 0.2-0.3, since moving back toward a historical average near 0.8 after 2020 (SG CTA Index basis). — [AIMA journal / Aspect](https://www.aima.org/article/a-trend-following-deep-dive-the-dynamics-of-dispersion.html)
- AQR attributes the weak 2010s to fewer large market moves, not to a lost ability to profit from trends. — [AQR "You Can't Always Trend When You Want"](https://www.aqr.com/Insights/Research/White-Papers/You-Cant-Always-Trend-When-You-Want)
- CTA indices had a Sharpe ratio above 1 in 2022. — [Alpha-Week](https://alpha-week.com/2022-cta-index-performance-review)
- Baltas & Kosowski document poor time-series momentum performance after 2008, linked to rising pairwise correlations. — [Imperial/SSRN 2140091](https://spiral.imperial.ac.uk/bitstream/10044/1/41472/4/SSRN-id2140091.pdf)
- Huang, Li, Wang & Zhou (JFE 2020), via Alpha Architect: asset-by-asset regressions show little time-series predictability, both in- and out-of-sample. — [Alpha Architect "An empirical challenge for trend following"](https://alphaarchitect.com/an-empirical-challenge-for-trend-following/)
- Cross-sectional stock momentum appears to have faded over the decade to 2017. — [Cambridge Associates 1Q18](https://publishedresearch.cambridgeassociates.com/wp-content/uploads/2018/02/Investment-Publications-Highlights-1Q18.pdf)

### Inferences
**Feasibility map for this bot** (c = 5-10 bps RT, long-only, megacaps/ETFs):

| Horizon band | Turnover (round trips/yr) | Cost drag @10 bps | Documented effects | Post-2015 cost-surviving evidence in large caps | Verdict |
|---|---|---|---|---|---|
| Intraday | 250-1000+ | 25%+/yr | Intraday momentum (SPY), HF cross-sectional ETF predictors, overnight drift | Unverified post-2013 for Gao et al.; Aleti et al. needs HF data the bot lacks; overnight is cost-killed | Infeasible with free IEX data |
| 1-5 days | 50-250 | 5-25%/yr | Short-term reversal, IBS/RSI(2)-type mean reversion, pre-event drifts | Large-cap reversal net-positive only in pre-2010 samples; pre-FOMC gone after 2015; broad reversal cost-killed | Mostly infeasible; only event trades with ≥30 bps OOS gross per trade |
| 5-20 days | 13-50 | 1.3-5%/yr | Weekly reversal, industry-relative reversal, short-term momentum, regime-gated mean reversion | Novy-Marx & Velikov survivors were industry-relative and low-vol reversals (to 2013); the bot's own sector-relative reversal test failed | Marginal; needs ≥20-50 bps gross per trade |
| 20-60 days (1-3 months) | 4-13 | 0.4-1.3%/yr | Monthly-rebalanced momentum and trend on index/sector ETFs; combined multi-signal tilts with bands | Trend survives costs but is weak in equity-only form; combined-signal netting helps (DeMiguel) | Feasible on costs; edge is small but real |
| 60-250 days | 1-4 | 0.1-0.4%/yr | Value, profitability, low-turnover quality/size tilts; annual rebalance | Low-turnover factors survive costs (Novy-Marx & Velikov; Detzel et al.), but post-2005 net premia are small and noisy (Chen & Velikov) | Feasible on costs; low Sharpe, slow to verify |

- The seven failed searches all sit at 1-20 day turnover with long-only megacaps. Weak gross effects plus a 5-10 bps RT cost is the expected outcome there, not bad luck.
- The literature's best cost-aware results come from portfolio construction rather than new signals: combining signals, netting trades, buy/hold bands and cost-aware weights (Novy-Marx & Velikov; DeMiguel et al.; Jensen-Kelly-Malamud-Pedersen). The bot runs separate strategies that each open and close their own positions, which is the opposite of trade netting.
- **Shorting.** Chen & Velikov's 10-20 bps a month short-sale cost estimate applies to the broad anomaly cross-section. For megacaps borrow is probably cheap, but that is unverified here. Shorting matters mainly because long-short anomaly spreads assume a short leg. A long-only book captures only part of the premium. The share captured by the long leg was not verified in this research.

### Gaps
- No verified post-2015 cost-inclusive performance for: intraday momentum in SPY; IBS or RSI(2) mean reversion on ETFs; single-stock or equity-only time-series momentum; long-only megacap factor tilts.
- I did not access the JKMP or JKP full texts. No net-Sharpe numbers from those papers are reported here.
- The long-leg versus short-leg split of post-2015 anomaly returns is unverified.

## Q4. Retail-scale versus institutional-scale capacity

### Takeaway
Tiny size eliminates price impact but not the effective spread, and the spread is what kills high-turnover anomalies for small traders. The capacity-constrained effects a retail bot could in principle harvest live in high-turnover reversal strategies and in small and microcaps. Their spreads are tens to hundreds of bps, and the high-turnover survivors' post-2005 net evidence is weak. Size is therefore not a meaningful edge for a $100k megacap bot. Its real advantage is low cost per trade in megacaps and ETFs, which only pays off if a gross edge exists there.

### Cited Findings
- Novy-Marx & Velikov's main results are explicitly for small, marginal traders paying effective spreads with no price impact. Capacity, the new capital before marginal profit hits zero, falls with turnover:
  - size and profitability: more than $100bn per side
  - value: about half that
  - momentum: about $5bn per side
  - the few high-frequency strategies that survive spreads: only "a few hundred million dollars".
  — [Novy-Marx & Velikov WP](https://mysimon.rochester.edu/novy-marx/research/ToAatTC.pdf)
- Market capitalization is by far the strongest cross-sectional correlate of estimated price impact (Kyle's lambda). — [Novy-Marx & Velikov WP](https://mysimon.rochester.edu/novy-marx/research/ToAatTC.pdf)
- Microcaps are about 3% of market cap but about 60% of stocks. With NYSE breakpoints and value weighting, 65% of 452 anomalies fail |t| ≥ 1.96, and 82% fail a 2.78 hurdle. — [Hou, Xue & Zhang RFS 2020](https://ideas.repec.org/a/oup/rfinst/v33y2020i5p2019-2133..html); [NBER conference version](https://conference.nber.org/conf_papers/f102921/f102921.pdf)
- Anomaly spread distributions match the all-stock distribution and sit well left of the Russell 2000. Academic implementations "naively trade stocks that are too illiquid", but avoiding illiquid stocks also gives up predictability. — [Chen & Velikov WP](https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf)
- Mutual funds' all-in implementation costs are 2.6-4.1% a year for value and about zero for market and size. Funds earn low returns to value and none to momentum after costs. — [Patton & Weller JFE 2020](https://econpapers.repec.org/RePEc:eee:jfinec:v:137:y:2020:i:2:p:515-549); [working paper](https://public.econ.duke.edu/~ap172/Patton_Weller_MF_28nov18.pdf)
- Institutional live costs (impact about 9 bps mean, about 15 bps dollar-weighted) come from a sample skewed toward large, liquid stocks. A CXO summary notes price impact may not be the dominant cost for small traders in illiquid stocks. — [Alpha Architect FIM summary](https://alphaarchitect.com/the-best-research-paper-ever-written-on-trading-costs/); [CXO](https://cxoadvisory.com/?p=31529)
- McLean & Pontiff find post-publication declines are larger for anomalies in easier-to-trade stocks, consistent with arbitrage capital flowing to liquid names, as cited by Novy-Marx & Velikov. — [Novy-Marx & Velikov WP](https://mysimon.rochester.edu/novy-marx/research/ToAatTC.pdf); [McLean & Pontiff 2016](https://Www.Gwern.net/doc/economics/2016-mclean.pdf)
- Falck, Rej & Thesmar: the year of publication explains about 30% of cross-factor Sharpe decay, and decay grows about 5 percentage points per later publication year. Arbitrage-capital variables have only marginal predictive power. Overfitting variables add about 15% more explanatory power. — [arXiv 2105.01380](https://arxiv.org/pdf/2105.01380)

### Inferences
- **What size does and does not buy.** For $1-5k orders, the relevant costs are the Novy-Marx & Velikov / Chen & Velikov effective-spread numbers, which are already "small trader" costs. Being small spares the bot the impact costs that limit funds (Patton & Weller), but every net-of-spread figure in Q3 already assumes a small trader. Small size does not rescue those numbers.
- **Capacity-constrained strategies cost more per trade.** The ones institutions cannot scale (high-frequency reversal combos, capacity of a few hundred million dollars) are exactly the high-turnover ones. A retail bot pays the full effective spread on every trade in them, and their post-2005 net evidence is weak.
- **Small and microcaps are a different cost regime.** At $1-5k a bot could trade small caps without impact, but spreads there are likely 20-100+ bps RT (anomaly portfolios averaged 67 bps in 2014). Under Table A that pushes the feasible hold to at least 20-60 days anyway. It would also break the bot's 45-symbol liquid-universe design and the $0-data constraint, since free spread and borrow data for small caps is poor.
- **Decay is driven more by overfitting and publication timing than by capital crowding** (Falck et al.). So "institutions can't trade it" is a weak argument for an effect surviving at retail scale.

### Gaps
- I found no study directly measuring net returns of capacity-constrained anomalies for $1-10k orders post-2015.
- No verified data on 2024-2026 small-cap effective spreads in bps.

## Q5. What practitioners with free data report as realistic net Sharpe at each horizon

### Takeaway
Rigorous evidence says to expect live or out-of-sample Sharpe ratios around 25-50% of backtest values. Post-2005 net anomaly returns run single-digit bps per month, and fewer than 1-3% of retail day traders are reliably profitable net of costs. I found no audited, horizon-by-horizon net-Sharpe survey of free-data retail systematic strategies. Treat Quantpedia, Alpha Architect and vendor backtest Sharpe figures as gross, in-sample and marketing-adjacent unless a source states otherwise.

### Cited Findings
- 215 bank-sponsored alternative-beta strategies had a median backtest Sharpe of 1.20 against 0.31 live, a median haircut of about 73%. 65 of the 215 had negative live Sharpe, and only 18 beat their backtest. The most complex strategies took a haircut more than 30% larger than simple ones. — [Alpha Architect summary of Suhonen, Lennkh & Perez](https://alphaarchitect.com/looking-at-alternatives-avoid-complexity-and-magical-backtests/); [CXO](https://www.cxoadvisory.com/big-ideas/live-performance-of-alternative-beta-products/). Summaries differ on 15 vs 17 sponsoring banks.
- Published anomalies keep about 50% of in-sample performance out of sample (72 anomalies, 1963-2018). — [Falck, Rej & Thesmar, Quantitative Finance 2022](https://econpapers.repec.org/RePEc:taf:quantf:v:22:y:2022:i:11:p:1955-1969); [arXiv](https://arxiv.org/pdf/2105.01380)
- Returns are 26% lower out of sample and 58% lower after publication (97 predictors). — [McLean & Pontiff JF 2016](https://Www.Gwern.net/doc/economics/2016-mclean.pdf)
- Average anomaly expected return post-publication, post-2005, net of costs: 4-8 bps a month. — [Chen & Velikov](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/zeroing-in-on-the-expected-returns-of-anomalies/945133D5A3ECEEAF466AEE91551FD225)
- Taiwan: fewer than 1% of day traders (about 1,000 of 360,000) are predictably profitable net of costs. — [Barber, Lee, Liu & Odean](https://faculty.haas.berkeley.edu/odean/papers/day%20traders/Day%20Trading%20Skill%20110523.pdf)
- Brazil: about 97% of people who day-traded mini-Ibovespa futures on more than 300 days lost money after fees (Chague, De-Losso & Giovannetti). This comes via a secondary summary, and details vary across summaries. — [True Wealth summary](https://www.truewealth.ch/en/blog/day-trading-how-many-traders-lose-money)
- The most active US households trailed the market by about 6.5 percentage points a year (Barber & Odean), via the same secondary summary. — [True Wealth summary](https://www.truewealth.ch/en/blog/day-trading-how-many-traders-lose-money)
- Trend following: information ratio about 0.2-0.3 in 2009-2019 and nearer 0.8 since 2020, for diversified futures CTAs. — [AIMA / Aspect](https://www.aima.org/article/a-trend-following-deep-dive-the-dynamics-of-dispersion.html)

### Inferences
**Realistic net excess Sharpe (vs SPY) by horizon for a free-data, long-only megacap/ETF bot.** This is my synthesis, not a sourced estimate:

| Horizon | Realistic net excess Sharpe | Basis |
|---|---|---|
| Intraday | ≤ 0 expected | Retail day-trader evidence, 25%+ cost drag, no HF data |
| 1-5 days | about 0 ± 0.3 | Costs absorb most of any gross edge; reversal-type effects decayed post-2010 |
| 5-20 days | about 0-0.3 | Feasible only for strong event-type signals |
| 1-3 months | about 0.1-0.4 | Combined trend/momentum/quality tilts with bands; equity-only trend Sharpe is lower than diversified-futures CTA figures |
| 3-12 months | about 0.1-0.3 | Low-turnover factor tilts; costs negligible, premia small and noisy post-2005 |

- **Haircut rule for backtests.** If a backtest shows Sharpe X, plan on 0.25-0.5X live (73% median haircut for bank alt-beta; 50% out of sample for academic anomalies). The bot's discovery gates should require backtest net Sharpe of at least 1.0-1.5 to expect about 0.3-0.5 live.
- **Benchmark against SPY.** SPY buy-and-hold has historically delivered about 0.4-0.6 total-return Sharpe at near-zero cost (not sourced here). For a long-only bot, the relevant target is the net Sharpe of excess return over SPY, or improvement of the combined portfolio. Total-return Sharpe is the wrong yardstick.

### Gaps
- I found no audited or peer-reviewed survey of net Sharpe for retail or free-data systematic strategies by horizon.
- Quantpedia and Alpha Architect strategy pages report backtest statistics, and I did not verify any live-track Sharpe from them.
- I did not fetch the Chague et al. primary paper.
- I did not access the post-2015 SG Trend Index year-by-year figures.

## Q6. Recommendation: horizon band and turnover budget

### Takeaway
Move slower. Target an average holding period of about 20-60 sessions (1-3 months), monthly rebalancing with buy/hold bands, and an annual one-sided turnover budget of about 4-8x equity, or roughly 30-70% a month. That keeps cost drag at about 0.2-0.8% a year at 5-10 bps RT, and under 2% a year even at 25 bps. Keep at most a small, separately gated short-horizon (1-5 day) sleeve for event-type signals that show at least 30 bps out-of-sample gross per trade, at least 3x the measured round-trip cost. Do not move to intraday.

### Cited Findings
- Anomalies with less than 50% monthly one-sided turnover mostly survive costs when designed for cost mitigation, and few with higher turnover do. A buy/hold spread (for example a 10%/20% entry/exit rule) is the most effective single mitigation. — [Novy-Marx & Velikov](https://mysimon.rochester.edu/novy-marx/research/ToAatTC.pdf)
- Turnover is the only reliable out-of-sample predictor of post-2005 net returns. — [Chen & Velikov WP](https://www.federalreserve.gov/econres/feds/files/2020039pap.pdf)
- Lower-turnover factor models (annual reformation) degrade least after costs. — [Detzel, Novy-Marx & Velikov](https://econpapers.repec.org/RePEc:bla:jfinan:v:78:y:2023:i:3:p:1743-1775)
- Combining characteristics into one portfolio nets trades and lowers marginal cost by about 65%. — [DeMiguel et al. via EDHEC](https://climateinstitute.edhec.edu/portfolio-perspective-multitude-firm-characteristics)
- Cost-aware portfolio learning beats cost-blind ML forecasts, which favor short-lived signals. — [Jensen, Kelly, Malamud & Pedersen](https://www.sfi.ch/en/publications/machine-learning-and-the-implementable-efficient-frontier)

### Inferences
**Recommended targets** (synthesis from Tables A-E and the literature above):

| Parameter | Recommended target |
|---|---|
| Median holding period | 20-60 sessions; current 1-20 day band shifts to 20-60 |
| Rebalance cadence | Monthly, with daily risk checks only (stops and regime gates), not daily signal trading |
| Annual one-sided turnover | 4-8x equity (≈ 33-67% a month), with a hard cap of 10x |
| Cost drag budget | ≤ 0.5%/yr at a measured c; ≤ 1/3 of expected gross edge (Table E) |
| Entry/exit rule | Buy/hold band, e.g. enter at top-decile score, exit only below median |
| Signal combination | One combined target-weight portfolio with netted trades, replacing separate strategies that each open and close positions |
| Short-horizon sleeve | ≤ 10-20% of risk; admit a rule only if OOS gross per trade ≥ max(30 bps, 3 × measured RT cost) and net Sharpe ≥ 0.5 after a 50% haircut |
| Intraday | Do not pursue: 25%+/yr cost drag at 10 bps, IEX-only data, no verified post-2015 net evidence accessible with free data |

- **Decision rule for horizon.** If the bot's measured all-in RT cost (fills vs decision-time midpoint) is c bps and the best out-of-sample gross per-trade edge at horizon h is g bps:
  - keep horizon h only if g ≥ 3c and the annualized net excess Sharpe after a 50% haircut is at least 0.3;
  - if g ≥ 3c fails everywhere below 20 days, the horizon question is settled in favor of moving slower.
- **Measure costs before any further search.** Log, for every live or paper fill, the NBBO midpoint at signal time, at order submit and at fill, and compute realized RT cost by symbol bucket and time of day. The 5-10 bps assumption is the single most important unverified input.
- **Re-baseline the gates.** Express the bot's discovery gate as net excess return over SPY after the measured cost and a 50% decay haircut. Give every candidate a turnover statistic so Table E can veto high-turnover winners automatically.
- **Shorting.** Megacap shorts mostly add the short leg of long-short anomaly spreads. This is likely useful for 1-3 month tilts, but borrow costs and recall risk must be measured. It does not change the horizon conclusion, because the cost arithmetic is symmetric.
- **Set expectations.** Even in the feasible 1-3 month band, the literature implies small edges: single-digit to low-double-digit bps a month net for the average to best anomalies. Moving slower makes costs irrelevant but does not guarantee profit. It moves the bottleneck to finding gross edge and having the statistical power to verify it, which at monthly cadence takes years of out-of-sample data.

### Gaps
- The recommendation depends on the bot's measured all-in cost, cross-position correlation and time-in-market, none of which were available here.
- No verified post-2015 evidence specific to long-only megacap/ETF multi-signal tilts at 1-3 month horizons. This is the main empirical gap to fill with the bot's own pre-registered tests.
