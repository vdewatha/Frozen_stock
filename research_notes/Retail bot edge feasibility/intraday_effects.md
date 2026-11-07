# Intraday return effects in liquid US large caps and ETFs: post-2015 survival, retail costs, and fit for a free-IEX-data bot

Scope note: researched 2026-11-07. Universe is the bot's ~45 liquid symbols (megacaps, SPY/QQQ/IWM/DIA, sector ETFs). Where a finding comes only from an abstract, search snippet or secondary summary, that is stated. Two context facts come from the assignment and earlier project research and were not re-verified here: IEX is roughly 3% of US equity volume, and Alpaca paper trading does not simulate the opening auction.

## 1. Intraday momentum: first half-hour predicts last half-hour (Gao, Han, Li & Zhou 2018) and hedging-demand momentum (Baltussen, Da, Lammers & Martens 2021)

### Takeaway
The effect is well documented in-sample (SPY 1993-2013; more than 60 futures markets 1974-2020). But the direct post-publication evidence on SPY/SPX is mixed to negative. A 2026 out-of-sample test of the best-known net-of-cost SPY version found the Sharpe ratio fell to 0.39 (May 2024-Mar 2026). A 2022-2026 SPX session study found the unconditional effect flat in every year. Whatever remains seems to depend on the sign of dealer gamma, and the bot cannot observe that with free data.

### Cited Findings
- Gao, Han, Li & Zhou (JFE 2018): the first half-hour return on SPY, measured from the previous day's close, predicts the last half-hour return. Sample: Feb 1993 to Dec 2013 high-frequency SPY data. The authors report predictability both in sample and out of sample. — [Market intraday momentum, JFE (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351); [author PDF](https://assets.super.so/e46b77e7-ee08-445e-b43f-4ffd88ae0a0e/files/ee7dac49-530b-4950-b5d0-e0b5eee08f2e.pdf)
- The same paper reports the pattern in ten other heavily traded domestic and international ETFs. Its proposed mechanisms are late-informed traders and institutions that delay trading to the first or last half-hour for liquidity and to avoid overnight risk. — [SSRN working-paper version (mirror)](https://www.smallake.kr/wp-content/uploads/2015/01/SSRN-id2440866.pdf); [Alpha Architect summary](https://alphaarchitect.com/attention-prop-traders-the-first-half-hour-of-trading-predicts-the-last-half-hour/)
- International evidence is mixed. One study finds significant first-half-hour to last-half-hour predictability in 12 of 16 developed markets. Another finds no significant intraday momentum for Europe and Canada. — [Li, Sakkas & Urquhart, Intraday time series momentum: global evidence (accepted version)](https://centaur.reading.ac.uk/95566/1/Accepted-Version.pdf); [Lancaster FoFI 2020 working paper](http://wp.lancs.ac.uk/fofi2020/files/2020/04/FoFI-2020-092-Zeming-Li.pdf)
- A Markov-switching extension of the Gao et al. work reports that predictability disappears in its out-of-sample period. This comes from a search snippet; the sample dates were not retrieved, and the paper tests an overnight-return variant. — [Understanding intraday momentum strategies (ResearchGate)](https://www.researchgate.net/publication/363384228_Understanding_intraday_momentum_strategies)
- Baltussen, Da, Lammers & Martens (JFE 2021, vol. 142, pp. 377-403) use intraday returns on more than 60 futures (equities, bonds, commodities, currencies) from 1974 to 2020. They find strong market intraday momentum everywhere: the rest-of-day return positively predicts the last-30-minute return, and the effect reverses over the next days. — [EconPapers listing](https://econpapers.repec.org/RePEc:eee:jfinec:v:142:y:2021:i:1:p:377-403); [preprint, nd.edu](https://www3.nd.edu/~zda/intramom.pdf)
- Baltussen et al. attribute the effect to short-gamma hedging by option market makers and leveraged-ETF rebalancing. Momentum is much stronger on negative-gamma days and largely absent on positive-gamma days (as summarized by Alpha Architect). — [Alpha Architect summary](https://alphaarchitect.com/hot-topic-does-gamma-hedging-actually-affect-stock-prices/); [EUR repository](https://pure.eur.nl/en/publications/hedging-demand-and-market-intraday-momentum/)
- Zarattini, Aziz & Barbon, "Beat the Market: An Effective Intraday Momentum Strategy for SPY" (SFI Research Paper 24-97; first version May 2024, revised Feb 2025): uses 1-minute IQFeed SPY and VIX data from May 2007 to Apr 2024. Reported net of costs: 1,985% total return, 19.6% a year, Sharpe ratio 1.33. — [SFI listing](https://www.sfi.ch/de/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy); [authors' page](https://concretumgroup.com/beat-the-market-an-effective-intraday-momentum-strategy-for-sp500-etf-spy/)
- Secondhand only: the paper's cost assumptions are reported as $0.0035/share commission plus $0.001/share slippage, with a max drawdown of about -25%. — [CXO Advisory summary](https://www.cxoadvisory.com/momentum-investing/complex-intraday-time-series-momentum-strategy-applied-to-spy/)
- The paper won 4th place in the 2025 Quantpedia Awards. — [Univ. St. Gallen FSI news](https://fsi.unisg.ch/de/newsdetail/news/quantpedia-awards-2025-fsis-barbon-and-team-secure-4th-place-with-intraday-momentum-strategy/)
- Out-of-sample test (Paz, SSRN 2026): the in-sample replication of the Zarattini-Aziz-Barbon strategy held up, but out-of-sample performance "deteriorates substantially" to a Sharpe ratio of 0.39 (about May 2024 to Mar 2026). Explanations offered: changing microstructure, the spread of 0DTE options, and ordinary drawdown variation. This is from the search snippet only; SSRN returned 403 on fetch. — [SSRN 7290621](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7290621); [Swedroe commentary](https://larryswedroe.substack.com/p/when-beat-the-market-meets-the-real)
- Critique: the "improved" ZAB version was developed on the same historical data it is judged on, so it is not truly out of sample. — [Swedroe Substack](https://larryswedroe.substack.com/p/when-beat-the-market-meets-the-real)
- A practitioner analysis of 1,085 SPX sessions (2022-2026) found the unconditional first-to-last half-hour effect flat overall and in every year, with alternating signs. It found a positive interaction effect that rests on few sessions. This is a blog analysis, not peer reviewed. — [dev.to / gex.live](https://dev.to/firmtape/intraday-momentum-is-dead-in-the-0dte-era-we-measured-it-on-1085-spx-sessions-43g0)

### Inferences
- The two papers that hold up best (Gao et al. and Baltussen et al.) both end their samples before or right around the 0DTE boom (2022 onward). Every direct test after 2022 that turned up here is weak or flat. Treat the unconditional SPY signal as decayed, not proven dead.
- If the hedging-demand mechanism is right, the signal should now be conditional on dealer gamma sign. Gamma exposure needs options open-interest data, which the bot does not have for free. Alpaca Basic options data is the "indicative" feed with a 15-minute historical lag, per the data plan below.
- Per-trade magnitude is a few basis points on SPY (inferred from a published R² that is small; the exact figure was not retrieved). Gross edge per trade is close to the cost of one round trip for anything other than SPY/QQQ-class spreads.
- Data fit is good for the Gao version. The predictor (prior close to 10:00 return) is known by about 10:15 even from the 15-minute-delayed SIP feed. The trade is entered at about 15:30 and exited at the close. Only the 15:30 entry needs a live quote, and for SPY the IEX quote is a reasonable proxy.

### Gaps
- Exact in-sample magnitudes for Gao et al. (R², timing-strategy annual return and Sharpe) were not retrieved this session. They are commonly cited as an R² of about 1.6% and a timing return of about 6-7% a year. Not verified.
- No peer-reviewed post-2018 replication on SPY specifically was found. The Paz paper's full text (cost assumptions, exact dates) could not be fetched.
- No source found that tests the effect on single megacap stocks (vs ETFs/futures) after 2015.

## 2. Overnight vs intraday return split: overnight drift (Boyarchenko, Larsen & Whelan 2023), tug of war (Lou, Polk & Skouras 2019), and close-to-open capture by a long-only bot

### Takeaway
The futures "overnight drift" (2-3 a.m. ET) has averaged close to zero since 2021, according to the original authors (NY Fed, July 2026). A naive SPY "buy at close, sell at open" strategy loses money after costs. After-cost profits appear only for subsets with strong, persistent past overnight bias, traded through the auctions. Alpaca paper trading cannot validate auction fills.

### Cited Findings
- Boyarchenko, Larsen & Whelan ("The Overnight Drift", RFS 2023; NY Fed Staff Report 917): in S&P 500 E-mini futures, almost all of the US equity premium was earned in the 2:00-3:00 a.m. ET window around the European open. The proposed mechanism is dealer inventory: sell-offs produce strong positive overnight reversals, while reversals after rallies are much weaker. — [NY Fed Staff Report 917](https://www.newyorkfed.org/research/staff_reports/sr917); [Liberty Street Economics 2021](https://libertystreeteconomics.newyorkfed.org/2021/05/the-overnight-drift-in-us-equity-returns); [CEPR DP14462](https://cepr.org/publications/dp14462)
- "The Disappearing Overnight Drift" (same authors, Liberty Street Economics, July 1, 2026): the 2-3 a.m. window earned about 3.7% a year before 2021 and has averaged close to zero since. The post tests three inventory-risk channels: dispersion of closing order imbalances, return variance, and dealer risk-bearing capacity. — [Liberty Street Economics, 2026](https://libertystreeteconomics.newyorkfed.org/2026/07/the-disappearing-overnight-drift/); [IDEAS listing](https://ideas.repec.org/p/fip/fednls/103479.html)
- An Alpha Architect review of SPY close-to-open, 1993-2020: cumulative return of -32% with transaction costs vs +717% without. It also notes that market-on-open and market-on-close orders add slippage. From the search snippet; the page returned 403 on fetch. — [Alpha Architect: Trading costs wipe out the overnight return anomaly](https://alphaarchitect.com/trading-costs-wipe-out-the-overnight-return-anomaly/)
- Lachance (Review of Financial Economics, 2023): overnight trading can reduce costs by trading in the single-price opening and closing auctions. Even so, a positive after-cost answer is limited to stocks with significant positive past overnight bias; reported after-cost alpha is 10.36% for that group. — [Night trading: lower risk but higher returns? (Wiley)](https://onlinelibrary.wiley.com/doi/full/10.1002/rfe.1180)
- "ETFs' high overnight returns: The early liquidity provider gets the worm" (Journal of Financial Markets): with regression forecasts of each ETF's overnight bias, 35% of ETFs in the sample had distortions large enough to make close-to-open profitable after commissions. The mechanism is early liquidity provision at the open. — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S138641812030032X)
- Lou, Polk & Skouras, "A tug of war: overnight versus intraday expected returns" (JFE 2019): decomposes returns of well-known cross-sectional strategies. Momentum profits are earned overnight; most other anomaly profits (for example value-type strategies) are earned intraday. The authors read this as opposing investor clienteles trading at different times of day. Taken from the published abstract as previously known; the PDF fetched this session was not machine-readable, so figures were not re-checked. — [Paper PDF (LSE)](https://personal.lse.ac.uk/polk/research/TugOfWar.pdf)
- A 2025 arXiv paper asks whether overnight news explains overnight returns (relevant as a mechanism test; not read in full). — [arXiv 2507.04481](https://arxiv.org/html/2507.04481)
- Alpaca `opg` orders (market/limit on open) execute only in the opening auction. They are rejected if submitted between 9:28 a.m. and 7:00 p.m. ET and are cancelled if unfilled. `cls` (market/limit on close) orders execute only in the closing auction and are rejected between 3:50 p.m. and 7:00 p.m. ET. Both route to the primary exchange; neither supports fractional shares or extended hours. — [Alpaca docs: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)

### Inferences
- For a long-only bot, a pure "buy MOC, sell MOO" on SPY/QQQ is not supported by post-2015 evidence net of costs. Its main academic driver (the futures 2-3 a.m. drift) has faded since 2021. Even pre-2021, the naive SPY version was negative after costs.
- The surviving form is conditional. Buying at the close after sharp sell-offs fits the inventory story (BLW's asymmetric reversals) and the leveraged-ETF close-pressure reversal in Section 5. It can be tested cheaply on daily open/close data. Daily bars normally carry the official auction open and close; whether Alpaca's SIP minute bars include the auction prints is unconfirmed (see Section 7 Gaps).
- Validation problem: the paper environment does not simulate the opening auction (project context), so paper results for the `opg` leg will be optimistic or uninformative. Auction prints in the historical SIP bars are a better backtest reference than paper fills.
- The Lou-Polk-Skouras finding has a design implication for the existing daily strategies. Momentum-type signals earn overnight, so holding them only intraday (enter at open, exit at close) would give up their return. Mean-reversion/value-type signals are the reverse.

### Gaps
- No post-2015 test of close-to-open on SPY/sector ETFs net of realistic 2020s auction costs was found. The Alpha Architect sample ends in 2020, and the ETF paper's sample period was not retrieved.
- No measurement found of MOC/MOO slippage vs the official auction price for small retail orders. For small orders this should be near zero, since they fill at the auction price; the real cost is the auction price itself vs the pre-close midpoint. Not sourced.

## 3. Opening-range breakout (ORB) and gap effects on large caps and ETFs

### Takeaway
The only rigorous post-2015 academic-style ORB evidence found (Zarattini, Barbon & Aziz 2024, US stocks 2016-2023) gets its net profitability from "Stocks in Play": names with unusual relative volume from news or earnings, chosen each day from more than 7,000 stocks. This is not the bot's fixed 45-symbol megacap/ETF universe, and it needs whole-market real-time volume screening. No academic support was found for gap trading on large caps/ETFs after 2015.

### Cited Findings
- Zarattini, Barbon & Aziz, "A Profitable Day Trading Strategy for the U.S. Equity Market" (SSRN 4729284; SFI Research Paper 24-98, 2024): over 7,000 US stocks, 2016-2023. Tests a 5-minute ORB against 15-, 30- and 60-minute ranges. Restricting trades to Stocks in Play (unusually high activity from company news or earnings) gives a large benefit after transaction costs. The top-20 Stocks in Play portfolio reports total net performance over 1,600%, Sharpe 2.81, and annualized alpha of 36%. — [EconPapers / IDEAS listing](https://ideas.repec.org/p/chf/rpseri/rp2498.html); [authors' page](https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/); [CXO Advisory summary](https://www.cxoadvisory.com/individual-investing/intraday-trading-of-overactive-stocks-via-opening-range-breakout/)
- A QuantConnect community replication reports a Sharpe ratio of about 2.4 vs about 0.84 for buy-and-hold SPY, on a different setup from the paper. — [QuantConnect research](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/)

### Inferences
- The edge appears to come from selection (relative volume or news) rather than from ORB as a rule. A fixed megacap/ETF list is rarely "in play" except on its own earnings days. The bot would get at most a few such days per symbol per year, too few to estimate an edge quickly.
- Data fit is poor. Ranking "in play" names needs real-time consolidated volume across the whole market at 9:35. IEX's roughly 3% share gives noisy relative-volume readings and may have no IEX trade at all in the first minutes for less active names. The 15-minute SIP delay makes a 5-minute opening range stale by about 9:50.
- Earlier project research (memory index: "gap fill dead") already found the gap-fill rule unprofitable in this bot's own trial library. Nothing found here contradicts that.

### Gaps
- No peer-reviewed (journal) post-2015 ORB study on large caps or ETFs was found. The 2024 paper is a working paper. Older academic ORB work (for example on futures) was not searched.
- Whether ORB on SPY/QQQ alone (not Stocks in Play) is profitable after 2015 was not found in a primary source.
- No academic post-2015 study of opening-gap continuation or reversal for large caps/ETFs was found.

## 4. Intraday seasonality / periodicity in the cross-section (Heston, Korajczyk & Sadka 2010)

### Takeaway
The periodicity is real and recently re-explained as a footprint of institutional VWAP and market-on-close order flow (Haendler, Heston, Korajczyk & Sadka, Nov 2025). Exploiting it is a cross-sectional, half-hour long-short trade with per-interval magnitudes far smaller than retail round-trip costs. Not exploitable by this bot.

### Cited Findings
- Heston, Korajczyk & Sadka (JF 2010): over 13 half-hour intervals per day, returns at lags that are exact multiples of a trading day predict returns, and the effect lasts at least 40 trading days. Short-term reversal comes from bid-ask bounce and liquidity imbalances lasting under an hour. Volume, order imbalance, volatility and spreads show similar patterns but do not explain the return pattern. — [arXiv 1005.3535](https://arxiv.org/pdf/1005.3535); [Kellogg listing](https://www.kellogg.northwestern.edu/academics-research/research/detail/2010/intraday-patterns-in-the-cross-section-of-stock-returns)
- Bogousslavsky: the periodicity is consistent with an infrequent-rebalancing model in which some agents trade once a day at a set time. — [Bogousslavsky working paper (EPFL)](https://memento.epfl.ch/public/upload/files/Paper_Bogousslavsky.pdf)
- Haendler, Heston, Korajczyk & Sadka, "The Intra-Day Stock Return Periodicity Puzzle" (SSRN 5749704, Nov 2025): their explanatory variables jointly explain all of the open and midday periodicity and up to 30% of the closing-interval periodicity. Open periodicity is tied to VWAP trading and close periodicity to market-on-close trading. — [SSRN 5749704](https://papers.ssrn.com/ab_id=5749704); [Kellogg listing](https://www.kellogg.northwestern.edu/academics-research/research/detail/2025/the-intra-day-stock-return-periodicity-puzzle/?p=1)

### Inferences
- The 2025 paper suggests the effect persists in recent data, though the sample end was not retrieved, and that it is mechanical (predictable institutional order flow). That is a plausible reason it survives: arbitraging it means trading many names every half-hour.
- For this bot it is infeasible. It needs a cross-section much larger than 45 names, two legs (long and short), and a half-hour holding period. Each round trip costs at least one spread plus impact, which almost certainly exceeds a per-interval return premium of a few basis points or less. That magnitude was not retrieved, so it is an inference.
- The only practical use is defensive. Knowing that open and close intervals carry predictable VWAP/MOC flow can guide execution timing for the existing daily signals (for example, avoid the first minutes after the open).

### Gaps
- Per-interval return magnitudes (bps) and any post-2015 subsample results were not retrieved from either paper.
- No independent out-of-sample replication on other markets or on a large-cap-only subsample was found.

## 5. End-of-day and closing-auction effects: who trades at the close, leveraged-ETF rebalancing, and month-/quarter-end flow

### Takeaway
Closing auctions are now a large share of volume, driven by passive/ETF rebalancing. Close prices carry temporary pressure that reverses fast: by half within about 20 minutes after the close, and from LETF and option-hedging flows at the next open. The leveraged-ETF channel has weakened over time, while option gamma effects persist. A long-only bot could at most "provide liquidity" on down days (buy at the close after large declines, sell at the open). This is a marginal, testable idea, not an established retail edge.

### Cited Findings
- Bogousslavsky & Muravyev, "Who trades at the close? Implications for price discovery and liquidity" (Journal of Financial Markets, vol. 66, 2023; earlier titled "Should We Use Closing Prices? Institutional Price Pressure at the Close"): closing auctions set US closing prices and account for about 7.5% of daily volume. The paper studies a "new regime" of very high closing volume. — [IDEAS listing](https://ideas.repec.org/a/eee/finmar/v66y2023ics1386418123000502.html); [AEA 2021 conference entry](https://aeaweb.org/conference/2021/preliminary/paper/H9T4hef7)
- Workshop slides from the same authors (secondary to the paper): closing-auction volume is strongly associated with ETF and passive mutual fund ownership and institutional rebalancing, not active fund ownership. Closing price deviations reverse by half in the first twenty minutes after the close. Liquidity migrates toward the close ("liquidity begets liquidity"), worsening intraday liquidity. Bogousslavsky is quoted as saying the auction's huge volume distorts the closing price. — [Workshop slides (CBR)](https://www.cbr.ru/Content/Document/File/141789/press_3.pdf); [Bloomberg write-up](https://www.bloombergquint.com/markets/noise-for-sale-in-giant-u-s-auctions-used-to-end-stock-sessions)
- Barbon, Beckmeyer, Buraschi & Moerke, "Liquidity Provision to Leveraged ETFs and Equity Options Rebalancing Flows: Evidence from End-of-Day Stock Prices" (SFI WP 22-40, 2022): LETF rebalancing and dealers' option delta-hedging are distinct, economically significant liquidity demands that cause end-of-day momentum and mean reversion. The effects revert at the next day's open, which points away from an information explanation. The LETF effect is economically larger but has decreased over time; gamma effects persist through the sample. A large negative (positive) aggregate gamma imbalance relative to dollar volume produces end-of-day momentum (reversal). — [SFI listing](https://www.sfi.ch/en/publications/n-22-40-liquidity-provision-to-leveraged-etfs-and-equity-options-rebalancing-flows-evidence-from-end-of-day-stock-prices); [SSRN 3925725](https://papers.ssrn.com/abstract=3925725); [St. Gallen PDF](https://www.alexandria.unisg.ch/server/api/core/bitstreams/5a99db31-0d37-4f86-9502-8cb0f3bff4fe/content)
- Baltussen et al. (2021) also name leveraged-ETF rebalancing as a contributor to last-30-minute market momentum, which reverts over the following days. — [preprint](https://www3.nd.edu/~zda/intramom.pdf)
- A BlackRock industry perspective on the growth of market-on-close activity exists (July 2020; not read in full). — [BlackRock ViewPoint](https://BlackRock.com/corporate/literature/whitepaper/viewpoint-a-global-perspective-on-market-on-close-activity-july-2020.pdf)

### Inferences
- The tradable form for a long-only, small-order bot is to buy into the close after a large down day, when LETFs and short-gamma dealers sell into the auction, and sell at the next open. This lines up with three separate findings: BLW's "sell-offs reverse overnight", Barbon et al.'s "reverts at next open", and Bogousslavsky-Muravyev's fast post-close reversal. It also overlaps with the overnight idea in Section 2, so they should be tested as one hypothesis, not two.
- Two cautions. The LETF channel is weakening over time. Gamma sign, which determines whether the close shows momentum or reversal, is not observable with free data.
- Mechanically, Alpaca's `cls` cutoff is 3:50 p.m. ET (Alpaca docs, Section 2). The decision must use prices as of about 3:45-3:50. IEX real-time quotes for SPY/QQQ are adequate for that. The delayed SIP feed (3:30-3:35) is too stale on volatile days.
- Month-end and quarter-end flows: this is a known practitioner theme, but no primary post-2015 source was retrieved this session (see Gaps).

### Gaps
- Cheng & Madhavan (2009) on leveraged-ETF rebalancing size was not retrieved. The formula for LETF rebalancing demand, roughly proportional to L(L-1) times the daily return times AUM, is from prior knowledge and is not sourced here.
- No primary source found on month-end or quarter-end closing flow effects after 2015 in SPY/sector ETFs.
- Full text of Bogousslavsky-Muravyev (published estimates, robustness) was not retrieved; findings above come from the abstract and slides.
- No source found on how large the close-pressure reversal is in basis points for megacaps/ETFs after costs.

## 6. Costs and execution for small orders: spreads by time of day, MOC/MOO on Alpaca, IEX vs SIP signal slippage, and pattern-day-trader status

### Takeaway
Alpaca supports market/limit on open (`opg`) and on close (`cls`) with fixed cutoffs: 9:28 a.m. and 3:50 p.m. ET. The docs page marks these as needing a sales contact for some order types, so check that the paper/live account accepts them. The PDT rule was removed: SEC approval April 14, 2026, effective June 4, 2026. Firms may phase in through Oct 20, 2027, so a given broker may still enforce PDT-like limits in Nov 2026. Per-time-of-day spread data for the universe was not found in a primary source.

### Cited Findings
- Alpaca `opg`: valid only for the opening auction. Rejected if submitted after 9:28 a.m. and before 7:00 p.m. ET; submissions after 7:00 p.m. queue for the next day's open. Unfilled orders are cancelled. Market and limit types are allowed; stop and stop-limit are not. No fractional shares, no extended hours. — [Alpaca docs: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- Alpaca `cls`: valid only for the closing auction. Rejected if submitted after 3:50 p.m. and before 7:00 p.m. ET. Unfilled orders are cancelled. Market and limit types are allowed; stop and stop-limit are not. API v2 only. No fractional shares. Auction orders route to the primary listing exchange and follow its auction rules. — [Alpaca docs: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- The docs page's order-type table marks market and limit for `opg`/`cls` as "Yes*". The asterisk was read in this session as "contact sales for that TIF" (from the fetched summary; verify on the live page). The docs page does not say how paper trading fills auction orders. — [Alpaca docs: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- Pattern day trader rule: the SEC approved FINRA's amendments to Rule 4210 on April 14, 2026 (Exchange Act Release No. 105226; 91 Fed. Reg. 20731, April 17, 2026). The amendments remove the "pattern day trader" definition, its account equity requirements (the $25,000 minimum) and day-trading buying power. They are replaced by an intraday margin standard: firms check for intraday margin deficits in any margin account on days with an "IML-reducing transaction", whether or not the customer day trades. Firms can block trades in real time or issue end-of-day calls. Deficits must be met "as promptly as possible" and are outstanding until met or the 15th business day. Repeated failures trigger a 90-day credit freeze. The standard does not apply to good-faith or portfolio margin accounts, and maintenance margin under 4210(c) is unchanged. — [WilmerHale client alert, Apr 23, 2026](https://www.wilmerhale.com/en/insights/client-alerts/20260423-sec-approves-amendments-to-finra-rule-4210-replacing-day-trading-margin-requirements-with-a-modernized-intraday-margin-standard)
- Effective date June 4, 2026, with an optional 18-month phase-in for firms ending Oct 20, 2027 (FINRA Regulatory Notice 26-10, per WilmerHale). King & Spalding gives the approval date as April 15 and notes that the separate day-trading account approval and risk-disclosure rules (FINRA Rules 2130 and 2270) remain. — [WilmerHale](https://www.wilmerhale.com/en/insights/client-alerts/20260423-sec-approves-amendments-to-finra-rule-4210-replacing-day-trading-margin-requirements-with-a-modernized-intraday-margin-standard); [King & Spalding](https://www.kslaw.com/insights/articles/finra-adopts-sweeping-changes-to-margin-requirements-for-day-trading); [E*TRADE explainer](https://us.etrade.com/knowledge/library/margin/pattern-day-trading-rule-change)
- Intraday momentum backtests used $0.0035/share commission plus $0.001/share slippage on SPY (secondhand). — [CXO Advisory](https://www.cxoadvisory.com/momentum-investing/complex-intraday-time-series-momentum-strategy-applied-to-spy/)

### Inferences
- For the $100k paper account, PDT was never binding (above $25k). It matters only for a future small live account. As of Nov 2026, check whether Alpaca has already adopted the intraday-margin standard or is still phasing in. That status was not found.
- For SPY/QQQ the quoted spread is typically one cent (about 0.15-0.2 bp). Spreads in single names and smaller sector ETFs are widest in the first minutes after 9:30 and narrowest late in the day. This is standard microstructure knowledge but was not sourced here. Intraday effects worth a few bps per trade survive costs only in the tightest-spread ETFs and megacaps.
- MOC/MOO orders from small accounts should fill at the official auction price with no extra impact, so the "slippage" is the auction price vs the pre-auction quote. The documented post-close reversal (half within 20 minutes) implies that buying at the close on heavy-selling days gets a favorable, not unfavorable, auction price. This is a hypothesis, not measured here.
- IEX vs SIP signal slippage: an IEX-only last trade for SPY/QQQ/megacaps is usually within the NBBO because IEX's displayed quotes track the national best. For thin sector ETFs, IEX-only bars can be stale or missing in quiet minutes. Signals there should use IEX quotes (bid/ask midpoint) rather than IEX trade bars, or use the delayed SIP when 15 minutes of staleness is acceptable.

### Gaps
- No primary source on spreads by time of day for this universe after 2015 was retrieved.
- Alpaca's current (Nov 2026) PDT/intraday margin implementation and the exact meaning of the "Yes*" on `opg`/`cls` were not confirmed.
- How Alpaca paper trading fills `cls`/`opg` orders (at the official auction print, or a simulated quote) was not documented on the fetched page. Project context says the opening auction is not simulated.

## 7. Data sufficiency: free IEX real-time + 15-minute delayed SIP minute bars vs consolidated real-time

### Takeaway
The free Alpaca Basic plan gives IEX-only real-time data (30-symbol websocket cap, 200 REST calls/min) and full SIP history except the latest 15 minutes. That is enough for signals whose inputs are at least 15 minutes old (Gao first-half-hour, daily close/open) plus a live SPY/QQQ quote for entry. It is not enough for opening-range or relative-volume signals that need consolidated real-time prints at 9:30-9:45. Full real-time consolidated data costs $99/month (Algo Trader Plus), outside the project's $0 data budget.

### Cited Findings
- Alpaca Basic (free): equities real-time from IEX only, websocket limited to 30 symbols, historical data back to 2016 but excluding the latest 15 minutes, 200 historical API calls per minute. Options: indicative pricing feed, 200 websocket quotes, historical limited by the same 15-minute rule. — [Alpaca docs: About Market Data API](https://docs.alpaca.markets/docs/about-market-data-api)
- Alpaca Algo Trader Plus ($99/month): real-time from all US exchanges (CTA + UTP, 100% of market volume), unlimited websocket symbols, unrestricted history, 10,000 API calls/min, OPRA options feed. — [Alpaca docs: About Market Data API](https://docs.alpaca.markets/docs/about-market-data-api)
- The data-plan page does not say how IEX bars handle minutes without IEX trades, or how opening/closing auction prints appear in bars. — [Alpaca docs: About Market Data API](https://docs.alpaca.markets/docs/about-market-data-api)

### Inferences
- The 30-symbol websocket cap on the free plan is below the ~45 stored symbols. Live streaming of all symbols needs REST polling (within 200 calls/min) or a rotating subscription.
- IEX's roughly 3% volume share (project context) means the IEX "open" bar at 9:30 is not the primary-exchange opening auction price. Any signal built on the open (gap size, opening range, first-half-hour from the open) should take the open from SIP-delayed bars or daily data, available by 9:45, not from the IEX first bar.
- Signal-by-signal fit:
  - Gao first-half-hour to last-half-hour: inputs (prior close, 10:00 price) are available from delayed SIP by 10:15. Entry at 15:30 needs a live quote; IEX is fine for SPY/QQQ. Feasible on free data.
  - Baltussen rest-of-day to last-30-minutes: needs the 15:30 price live (IEX is fine for ETFs) and ideally gamma sign (not available free). Partly feasible.
  - ZAB noise-area strategy: needs live prices at 30-minute checkpoints and a trailing VWAP. IEX VWAP is a biased sample of the day's volume, so the consolidated VWAP is only available 15 minutes late. Marginal.
  - Close-to-open / down-day close reversal: needs only daily close/open and a pre-3:50 live quote. Feasible.
  - ORB / Stocks in Play: needs market-wide real-time consolidated volume. Infeasible on free data.
  - HKS periodicity: needs cross-sectional half-hour returns (delayed SIP is fine for research) but execution every half-hour. Data feasible, economics infeasible.

### Gaps
- No primary measurement found of missing-bar frequency in IEX minute bars for megacaps vs sector ETFs. The bot's own stored bars could measure this directly.
- Not confirmed whether Alpaca's SIP historical minute bars include the opening and closing auction prints in the 9:30 and 16:00 bars, or exclude them by trade-condition filtering.

## 8. Verdict per effect for this bot (feasible / marginal / infeasible)

### Takeaway
None of the intraday effects is a proven, post-2015, net-of-cost edge for a small long-only retail bot on free data. Two are worth a cheap shadow or backtest test because the data fits and the mechanism is plausible: (a) the conditional down-day close-to-open reversal, and (b) the Gao/Baltussen last-half-hour signal on SPY/QQQ. Both should start from a decay prior. The rest are infeasible for this universe or data.

### Cited Findings
- Intraday momentum, unconditional SPY: in-sample support in [Gao et al. 2018](https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351) and [Baltussen et al. 2021](https://www3.nd.edu/~zda/intramom.pdf); out-of-sample deterioration (Sharpe 0.39, 2024-2026) per [Paz 2026](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7290621); flat in 2022-2026 SPX sessions per [gex.live blog](https://dev.to/firmtape/intraday-momentum-is-dead-in-the-0dte-era-we-measured-it-on-1085-spx-sessions-43g0). Verdict: **marginal**. Data feasible; edge likely decayed; gamma conditioning not available free.
- Overnight drift (futures 2-3 a.m.) and naive SPY close-to-open: near zero since 2021 per [NY Fed 2026](https://libertystreeteconomics.newyorkfed.org/2026/07/the-disappearing-overnight-drift/); negative after costs 1993-2020 per [Alpha Architect](https://alphaarchitect.com/trading-costs-wipe-out-the-overnight-return-anomaly/). Verdict: **infeasible** as an unconditional strategy.
- Conditional close-to-open (persistent overnight-bias names, or after sell-offs): supported by [Lachance 2023](https://onlinelibrary.wiley.com/doi/full/10.1002/rfe.1180), the [ETF overnight paper](https://www.sciencedirect.com/science/article/abs/pii/S138641812030032X), [BLW asymmetric reversals](https://www.newyorkfed.org/research/staff_reports/sr917) and [Barbon et al. next-open reversion](https://www.sfi.ch/en/publications/n-22-40-liquidity-provision-to-leveraged-etfs-and-equity-options-rebalancing-flows-evidence-from-end-of-day-stock-prices). Verdict: **marginal**. Worth a backtest on SIP/daily auction prints; paper fills on `opg` are not trustworthy.
- ORB / Stocks in Play: strong backtest only with market-wide selection per [Zarattini, Barbon & Aziz 2024](https://ideas.repec.org/p/chf/rpseri/rp2498.html). Verdict: **infeasible** for a fixed 45-symbol universe on IEX data.
- Gap effects on large caps/ETFs: no post-2015 academic support found (Section 3 Gaps). Verdict: **infeasible / unsupported**.
- HKS half-hour periodicity: real and mechanically explained per [Haendler et al. 2025](https://papers.ssrn.com/ab_id=5749704). Verdict: **infeasible**, because cross-sectional half-hour long-short costs exceed the edge.
- Closing-auction pressure / LETF rebalancing: real but weakening (LETF) per [Barbon et al.](https://www.sfi.ch/en/publications/n-22-40-liquidity-provision-to-leveraged-etfs-and-equity-options-rebalancing-flows-evidence-from-end-of-day-stock-prices); fast reversal per [Bogousslavsky & Muravyev 2023](https://ideas.repec.org/a/eee/finmar/v66y2023ics1386418123000502.html). Verdict: **marginal**. Same test as the conditional close-to-open; long-only can take only the down-day side.

### Inferences
- Suggested order of cheap tests:
  1. Backtest "buy at the official close after large down days (for example SPY/QQQ/sector ETF daily return below a volatility-scaled threshold), sell at the official next open" on SIP-delayed historical bars from 2016, with a conservative cost of about 1-2 bp per side for ETFs. Report results before and after 2021 separately, given the NY Fed decay finding.
  2. Run the Gao signal (sign of prior close to 10:00 return, trade 15:30 to close on SPY/QQQ) as a live shadow log first. Use IEX quotes for entry and the official close for exit. It needs no auction simulation for entry, and the MOC exit can be marked at the official close price.
- Because Lou-Polk-Skouras-style findings tie momentum returns to the overnight session, the bot's existing daily momentum/breakout entries may benefit from MOC entry rather than next-day market entry. This can be checked in existing backtests without new data.
- The PDT removal (effective June 2026, phase-in to Oct 2027) removes the main regulatory barrier to intraday round trips in a small live margin account. Costs and data, not regulation, are the binding constraints.

### Gaps
- No single source compares all these effects on the same post-2015 sample with consistent retail costs. The verdicts above combine separate studies with different samples and cost assumptions.
- Net-of-cost magnitudes in bps per trade for each effect in this specific universe were not available from sources and would need to be measured in-house.
