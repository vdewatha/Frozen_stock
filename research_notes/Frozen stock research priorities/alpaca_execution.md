# Execution quality for Frozen_stock on Alpaca: entries at the open, exits, and backtest/live mismatch

Researched 2026-11-07. Scope: a small retail account trading liquid US large caps and ETFs on daily-bar signals, entering at the next open, on Alpaca paper (live later), with the free IEX feed, bracket orders and app-side stops. Research budget was limited (about 17 tool calls). Alpaca's own documentation and forum are the main primary sources. The academic evidence on auction cost at the open is thinner and partly dated; this is flagged where it applies.

## Alpaca order types, time in force, auction routing, bracket/OTO/OCO limits and fractional rules (paper vs live)

### Takeaway
Live `opg` orders (market-on-open, limit-on-open) go to the primary listing exchange's opening auction and must be submitted before 9:28 ET or after 19:00 ET the previous evening. They cannot be combined with brackets, stops or fractional quantities. Paper trading does NOT simulate the auction: it fills OPG orders like ordinary market orders at the opening quote, with random partial fills, and cancels them 30 seconds after the open. So paper is a poor test of an auction-entry strategy. Any opg experiment needs live (small-size) fills, or must be scored against the official opening print rather than paper fills.

### Cited Findings
**Time in force (current Alpaca docs)**
- `day`: valid only during regular hours (9:30-16:00 ET); unfilled orders are cancelled after the closing auction; orders sent after the close queue for the next trading day — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- `opg`: only for market-on-open and limit-on-open orders, which execute only in the opening auction. "OPG orders submitted after 9:28am but before 7:00pm ET will be rejected"; orders sent after 19:00 ET queue for the next day's opening auction — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- `cls`: market-on-close and limit-on-close only. "CLS orders submitted after 3:50pm but before 7:00pm ET will be rejected" — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- On-open and on-close orders are routed to the primary exchange. The docs do not list the individual exchanges, but primary-exchange routing implies the NYSE Opening Auction for NYSE listings, the Nasdaq Opening Cross for Nasdaq listings, and NYSE Arca for many ETFs — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- The TIF support table marks IOC/FOK/OPG/CLS as "Yes*" for market and limit orders only, with an asterisk saying to contact sales. Stop and stop-limit orders do not support OPG/CLS/IOC/FOK — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- Order handling standards: market and limit orders are protected on the primary exchange's opening print, subject to exchange cut-offs. For example, on Nasdaq, "market orders received before 9:28 will be filled at the Nasdaq Official Opening Price" — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- Market orders "may get filled at unexpected prices due to short-term price spikes". The docs do not describe a market-order collar — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)

**Brackets, OTO, OCO and trailing stops**
- Bracket `time_in_force` "must be `day` or `gtc`", so opg/cls entries cannot carry bracket legs. Extended hours are not supported (`extended_hours` must be false). Exit legs activate only after the entry is completely filled. Cancelling any leg cancels the rest. Take-profit must be above the stop for a buy. Each leg is sent DNR/DNC (not adjusted for dividends). The limit and stop prices can be replaced through PATCH — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- OTO: an entry plus one take-profit or one stop-loss leg; "order replacement is not supported yet". OCO: exits only, on an existing position, with `type` "limit"; the stop leg is a stop or a stop-limit — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- In brackets, OCOs and OTOs, the stop price must be at least $0.01 below the "base price" for a sell stop — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- Trailing stops take only `day`/`gtc`, cannot be bracket or OCO legs, and "will not trigger outside of the regular market hours" — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)

**Fractional shares and extended hours**
- Fractional and notional orders support only `day` TIF (market, limit, stop, stop-limit). GTC/IOC/FOK/OPG/CLS are "No". Notional orders cannot be replaced through PATCH — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- Extended hours: overnight 20:00-04:00 ET (Sunday-Friday), pre-market 04:00-09:30, after-hours 16:00-20:00. Only limit orders with `extended_hours=true` and day/gtc are accepted; everything else is rejected — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)

**Paper vs live for opg/cls**
- Paper fills match against the NBBO using real-time quotes. Order size is not checked against quoted size. Random partial fills happen "10% of the time". Paper does not model market impact, latency slippage, queue position, price improvement, regulatory fees, dividends or borrow fees — [Alpaca: Paper Trading](https://docs.alpaca.markets/docs/paper-trading)
- Alpaca staff (22 Mar 2024): paper does not implement OPG/CLS, and such orders "do not fill at the open or close price as one would see in actual live trading". They fill at the current quote with random partial fills. Staff (28 May 2024): paper OPG orders are cancelled 30 seconds after the open, and slow processing can cut orders off. On 28 May 2024, 59.7% of paper OPG orders were cancelled (491 of 822) against a normal rate of about 1%. Live OPG partial fills are "rare", happening only with an auction imbalance — [Alpaca forum: partial fills on market OPG](https://forum.alpaca.markets/t/recently-seeing-partial-fills-on-market-opg-order-s-p500-stocks/13906)
- Staff (24 Jan 2023): paper fills all market orders at the current quote (buys at the ask, sells at the bid), and paper does not distinguish MOO/MOC from market orders — [Alpaca forum: opening and closing prices in paper](https://forum.alpaca.markets/t/opening-and-closing-market-prices-in-paper-trading/11664)
- Live MOO orders fill at the primary-exchange auction price. That price can differ from the vendor "open" (the first valid trade of the day), which is why users report "worse than the daily open" — [Alpaca forum: OPG not filled at open price (live)](https://forum.alpaca.markets/t/opg-orders-not-filled-at-open-price-live-trading/14672); [Alpaca forum: MOO worse than daily open](https://forum.alpaca.markets/t/market-on-open-order-gives-me-a-worse-price-than-daily-open/14856)

### Inferences
- A daily-close signal can be submitted as `opg` any time after 19:00 ET the evening before and up to 09:28 ET. This fits the "decide on close, enter at next open" design without depending on the bot being online at 09:30.
- An opg entry cannot carry a bracket. The bot needs a two-step flow: (1) MOO or LOO entry; (2) after the fill (about 09:30-09:31), submit an OCO (take-profit limit plus stop) or a plain stop order for the filled quantity. The position is unprotected between the auction fill and the OCO's acceptance. That gap is usually seconds, but it must be handled for partial fills and rejections.
- Fractional positions cannot use opg or gtc. If sizing produces fractional quantities, round down to whole shares for opg entries, which matches the existing one-share floor.
- The "Yes*" / "contact sales" marking on OPG/CLS conflicts with forum reports of retail users placing OPG orders. It may mean availability depends on account type, or the note may be outdated. Verify with a single live order before building on it.
- Paper slippage on opg entries is a quote-based artefact: buys fill at the ask at about 09:30. It is not the auction price. Exclude paper opg fills from the cost-model calibration, or tag them separately. Otherwise the calibrator learns paper's simulator rather than the market.

### Gaps
- No Alpaca doc found on whether limit-on-open orders that miss the auction convert to day orders or are cancelled. Forum evidence says paper cancels after 30 s. Live behaviour for an unfilled LOO is not documented here. Test it.
- Whether the docs' OPG cut-off (09:28) is stricter than the exchange cut-offs for every listing venue (for example the NYSE Arca opening auction for ETFs) was not verified.
- The "Paper MOC orders partially fill, remainder expires" thread (June 2026) was seen only as a title.

## Opening auction vs continuous-market cost at the open (first 5-30 minutes; U-shaped cost curve)

### Takeaway
The available evidence agrees on the direction: spreads and volatility are highest in the first minutes after the open and narrow quickly. Call auctions improve opening-price efficiency, and the first-trade effective spread is lower in an auction open. But no recent (2020-2026) study surfaced that gives bps costs per minute bucket for S&P 500 names. Frozen_stock's own fills, bucketed by time of day, are the best evidence available to it.

### Cited Findings
- Nasdaq after introducing its opening and closing calls (2004): the U-shaped intraday volatility pattern in half-hour data "is largely driven by the first two minutes after the open and the final minute prior to the close", and opening/closing volatility fell appreciably after the calls began — [CFS working paper on Nasdaq call auctions (gfk-cfs.de)](https://gfk-cfs.de/media//08_45.pdf) (as summarised in search results; full text not read)
- NYSE vs Nasdaq opens (2001 working paper, before the Nasdaq Opening Cross): the first trade's effective spread is substantially lower on the NYSE's call-auction open, with larger savings for bigger firms — [Darden working paper "Open 2001"](https://www.darden.virginia.edu/sites/default/files/inline-files/Open_2001_WP.pdf) (dated; Nasdaq now runs its own cross)
- Opening call auctions significantly increase the informational efficiency of opening prices compared with a standalone continuous market, and liquidity spills over into the continuous session that follows (experimental or market study) — [gfk-cfs.de paper](https://gfk-cfs.de/media//08_45.pdf) (as summarised in search results)
- NYSE spreads show a U-shaped intraday pattern (Wood and McInish 1992, cited in) — [Review of Financial Economics 2004 (IDEAS)](https://ideas.repec.org/a/wly/revfec/v13y2004i4p341-356.html)
- ETF spreads are highest in the morning near the open (Alpha Architect summary of ETF trading-time research) — [Alpha Architect: best times ETF investors trade](https://alphaarchitect.com/best-times-etf-investors-trade/) (page returned 403; seen only as a search summary)
- Opening auction volume is much smaller than closing auction volume — [Cambridge paper on auction price impact](https://www.cambridge.org/core/product/0F72910A79C5B42CF6E85F55164CE846/core-reader) (search summary)
- NYSE closing-auction data: the volume-weighted average cost (VWAC) for S&P 500 stocks peaks at about $0.03 around 15:58 (closing, not opening) — [NYSE Data Insights: Closing Auction transaction cost part 2](https://www.nyse.com/data-insights-blog/closing-auction-immediate-market-impact-price-drift-and-transaction-cost-of-trading-part-2-eqjcqvixj8-partial)

### Inferences
- A market order at 09:30:00-09:35 in continuous trading probably pays the widest quoted spreads of the day plus a volatility cost. A MOO order pays no spread (it fills at the single cross price) but carries the auction's price risk. For a few hundred to a few thousand dollars, the order adds no meaningful imbalance, so a MOO in liquid names is likely the cheapest way to get the "next open" price that backtests assume.
- Backtests built on consolidated daily bars use an "open" that is the official opening print or the first qualifying trade. The auction print is the closest live analogue, so opg entries should shrink backtest/live mismatch compared with 09:30 continuous market orders. This is an inference; measure it.
- Delaying entry to 09:45-10:00 likely gives tighter spreads, but it changes the strategy's price (drift from the open). That is a signal-quality question as well as a cost question, so test it as a separate variant, not as a cost fix.

### Gaps
- No recent NYSE or Nasdaq study was retrieved that gives effective spreads in bps by minute for the first 30 minutes for large caps.
- No Nasdaq Opening Cross or NYSE Opening Auction white paper with retail-size auction costs was retrieved.
- The opening-auction dynamics papers above are pre-2010 or not read in full; treat them as directional only.

## Limit-on-open and marketable-limit vs market orders: fill rate, price improvement and adverse selection

### Takeaway
No source was retrieved that quantifies this trade-off for daily mean-reversion and momentum signals. The mechanism is standard: passive limits fill more often on losers and miss winners. It should be measured directly with paired experiments. Paper cannot answer it, because it models no queue position and gives non-marketable limits idealised fills.

### Cited Findings
- Paper non-marketable limits fill as soon as the limit touches the NBBO, with no queue-position modelling. This makes paper limit fill rates optimistic — [Alpaca: Paper Trading](https://docs.alpaca.markets/docs/paper-trading)
- On Nasdaq, market and limit orders received before 09:28 are protected at the Nasdaq Official Opening Price, so a limit-on-open is either filled at the cross price or not filled — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- Live OPG partial fills are rare and come from auction imbalances (Alpaca staff) — [Alpaca forum](https://forum.alpaca.markets/t/recently-seeing-partial-fills-on-market-opg-order-s-p500-stocks/13906)

### Inferences
- A limit-on-open (LOO) priced at the prior close plus or minus k × ATR fills at the cross price whenever the cross is within the limit. Price improvement relative to a MOO is therefore zero when filled. The only effect is to skip entries where the open gaps beyond the limit. For mean reversion (buying weakness), skipping big gap-downs may avoid falling knives or may miss the best rebounds. For momentum or breakouts, skipping gap-ups probably discards the strongest signals. This is a signal filter, not a cost saving, and must be judged on P&L of the skipped trades, as the "missed winners" counterfactual.
- Marketable limits in continuous trading (for example the ask plus 5-10 bps) cap the worst fill in a spike. With small size in liquid names they usually fill near the quote. Their cost is the rare non-fill in fast markets.

### Gaps
- No academic estimate was found for adverse selection on non-filled limits for daily-horizon retail strategies.
- No Alpaca live fill-rate data was found for LOO orders.

## Alpaca retail order routing: PFOF, Rule 606/605 and paper vs live fills

### Takeaway
Alpaca receives PFOF and routes non-directed orders through wholesaler smart order routers (Citadel, Virtu, Jane Street, GTS). FINRA cited its 606 disclosures as deficient in 2025. Academic work finds large, persistent differences in execution quality across retail brokers that are driven by wholesalers, not by PFOF levels. So Alpaca's live costs must be measured, not assumed from paper.

### Cited Findings
- Alpaca's 606(a)(1) reports for 2026 Q1/Q2 name Virtu, Citadel Securities (CDRG), Jane Street (JNST) and GTS (GTSM) as venues paying for order flow — [Alpaca 606 2026Q1](https://files.alpaca.markets/disclosures/library/SEC+606a1+-+2026Q1.pdf); [Alpaca 606 2026Q2](https://files.alpaca.markets/disclosures/library/SEC+606a1+-+2026Q2.pdf)
- Alpaca's Form CRS (via a white-label partner) says Alpaca receives PFOF on some or all transactions — [Form CRS (gugu.fund copy)](https://www.gugu.fund/legal/document/FormCRS.pdf)
- Alpaca's conflicts-of-interest chart says it relies on the smart order routers of Jane Street, Citadel and Virtu, reviewed annually on price, speed, certainty, cost, liquidity and market impact — [Alpaca conflicts chart (Canada)](https://files.alpaca.markets/disclosures/library/ca/Alpaca+Canada+Conflicts+of+Interest+Chart.pdf)
- FINRA letter SD-2436 (March 2025): Alpaca failed to disclose specific PFOF arrangement details in its Rule 606(a)(1) report and had failures related to Rule 606(b)(1) — [FINRA SD-2436](https://finra.org/sites/default/files/2025-03/SD-2436-Alpaca-03-06-2025.pdf)
- Quarterly 606 reports are listed at [Alpaca 606 reports](https://alpaca.markets/disclosures/606-reports) and the [Alpaca disclosures library](https://alpaca.markets/disclosures). The two listings disagree on which quarter is the latest.
- "The 'Actual Retail Price' of Equity Trades" (Schwarz, Barber, Huang, Jorion, Odean; Journal of Finance 2025, vol. 80(5), pp. 2507-2541): 85,000 simultaneous market orders across six accounts at five brokers. The mean account-level round-trip cost ranged from 0.07% to 0.46% (7-46 bps), excluding commissions. Dispersion comes from wholesalers giving different prices to different brokers, and differences in PFOF do not explain it — [IDEAS/RePEc](https://ideas.repec.org/a/bla/jfinan/v80y2025i5p2507-2541.html); [SSRN 4189239](https://papers.ssrn.com/abstract=4189239); [WashU Olin news](https://olin.washu.edu/about/news-and-media/news/2022/10/study-finds-astonishing-differences-in-brokers-pricing-of-stock-trades.php)
- Follow-up, "Who Is Minding the Store?" (Huang, Jorion, Lee, Schwarz 2024): uses 150,000 actual trades. Its results are inconsistent with perfect price competition among wholesalers — [Huang Routing July 2024](https://foster.uw.edu/wp-content/uploads/2024/07/Huang_Routing-July-2024.pdf)
- Paper: no price improvement is modelled; fills are at the NBBO — [Alpaca: Paper Trading](https://docs.alpaca.markets/docs/paper-trading)

### Inferences
- Paper fills bias costs in two directions. They overstate cost for continuous market orders, because there is no wholesaler price improvement. They understate cost for size and latency. For orders of a few hundred shares in large caps, live continuous-market fills may actually beat paper. Keep paper-calibrated and live-calibrated cost models separate.
- The Schwarz et al. range (7-46 bps round trip) is a useful prior for a retail broker. The paper did not single out Alpaca in the parts retrieved. Use the bot's own live fills to place Alpaca within that range.
- Auction orders (opg/cls) go to the primary exchange, not to a wholesaler, so PFOF and price improvement do not apply to them.

### Gaps
- The 606 PDFs could not be parsed (font-encoded). Order-type mix, venue percentages and PFOF rates per 100 shares for Alpaca were not extracted. Read them manually.
- No Alpaca Rule 605 execution-quality statistics (price improvement, effective-to-quoted spread) were found. Wholesalers publish 605 reports, but none were retrieved.
- SEC Rule 605 amendments (adopted 2024) compliance status as of November 2026 was not confirmed.
- The specific Barber et al. Robinhood execution paper was not retrieved. Only the JF 2025 multi-broker study was covered.
- Market-structure change to watch: the 2024 Reg NMS amendments (half-penny ticks for tick-constrained stocks, lower access-fee caps) had a November 2025 compliance date, later pushed to November 2026. Recent sources say it was extended again to November 2027, but this is unconfirmed — [MoFo (June 2026)](https://www.mofo.com/pdf/resources/insights/260612-sec-proposes-landmark-rollback-of-core-regulation); [FlexTrade](https://flextrade.com/resources/secs-broader-reg-nms-overhaul-could-delay-access-fee-tick-sizes/); [SIFMA May 2026 letter](https://www.sifma.org/advocacy/letters/supplemental-request-for-immediate-extension-of-tick-size-and-access-fee-compliance-dates). If the change goes live, quoted spreads for many mega-caps would narrow, and older cost calibrations should be refreshed.

## IEX vs SIP: coverage, bar differences, effect on stops and backtests, and free-plan access

### Takeaway
IEX is about 3% of all US equity volume (about 6% of exchange-only volume, about 11% intraday exchange share in S&P 500 names, Q4 2025). IEX-only bars therefore have different opens, closes, highs and lows, and some intervals have no bar at all. The free plan can query historical SIP bars whose end time is at least 15 minutes old. Daily signals and backtests should use SIP daily bars (free with the 15-minute delay), not IEX bars.

### Cited Findings
- Basic (free) plan: real-time equities data from IEX only; 30 WebSocket symbols; 200 historical calls/min; historical data limited to the "latest 15 minutes". Algo Trader Plus costs $99/month and covers all exchanges — [Alpaca: About Market Data API](https://docs.alpaca.markets/docs/about-market-data-api)
- "The `end` parameter must be at least 15 minutes old to query SIP data without a subscription". The "latest" endpoints need a subscription for SIP. `feed` selects iex or sip, and the default is the "best" feed for the subscription — [Alpaca: Market Data FAQ](https://docs.alpaca.markets/docs/market-data-faq)
- Paper-only accounts are entitled only to IEX market data — [Alpaca: Paper Trading](https://docs.alpaca.markets/docs/paper-trading)
- Example scale: AAPL on 2023-09-29 had 12,630 trades and 923,134 shares on IEX, against more than 535,136 trades and 51,861,083 shares across all venues (about 1.8% of shares) — [Alpaca: Market Data FAQ](https://docs.alpaca.markets/docs/market-data-faq)
- Bar construction: built from trades using the SIP timestamp under CTA/UTP/FINRA condition rules. Some conditions (for example odd lots `I`, cash sales `C`) update volume but not prices. No bar is generated if an interval has no qualifying trades. Daily bars are stamped at midnight New York time — [Alpaca: Market Data FAQ](https://docs.alpaca.markets/docs/market-data-faq)
- IEX share: 3.2% of total US volume including off-exchange (3.8% intraday); 6.5% exchange-only; 10.9% intraday exchange share in S&P 500 names (Q4 2025, IEX-reported). Records: 3.4% in November and 3.6% in December 2025 — [IEX news](https://iex.io/news); [Markets Media: Outlook 2026, Bryan Harkins](https://www.marketsmedia.com/outlook-2026-bryan-harkins-iex-group/). Independent Cboe data: about 3.3% in September 2025 (via Barron's) — [Barron's via itiger](https://www.itiger.com/news/2568412468)
- Off-exchange trading exceeded 50% of US equity volume in Q4 2024 (SIFMA, as cited) — [Markets Media](https://www.marketsmedia.com/?p=149910)
- Alpaca's broker-side stop election uses the consolidated tape: a sell stop elects only on "a trade on the consolidated tape at or lower than your stop price", and only if that trade is not outside the NBBO — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)

### Inferences
- The IEX open is not the official open. IEX's first trade can come seconds after 09:30 at a different price from the primary auction. IEX highs and lows sample about 3% of prints, so they understate the true range. Backtests on consolidated daily bars therefore see more stop and target touches than an IEX-driven bot would.
- App-side stops driven by IEX trades will trigger later (or never) relative to SIP. They can also trigger on IEX-only prints. Broker-side stops elect on the consolidated tape within the NBBO, so they match SIP-based backtests better. This argues for server-side stops as the primary protection, with app-side logic as a backup.
- For slippage measurement, use the 15-minute-delayed SIP minute bars or trades as the benchmark (for example the official open or the 09:30 SIP minute bar), fetched after the fact, rather than the IEX quote at decision time. This is free.
- Frozen_stock can get consolidated daily OHLC for signals at no cost by querying `feed=sip` after 16:15 ET. It should check that signals and backtests use the same feed.

### Gaps
- No quantitative study comparing IEX-only and SIP daily OHLC differences in bps for large caps was found. This can be computed locally from Alpaca's `feed=iex` and `feed=sip` historical bars.
- The `delayed_sip` feed value, which exists in some Alpaca SDKs, was not confirmed in the docs retrieved.

## Stops: stop-market vs stop-limit at gaps and in fast markets; broker-side vs app-side

### Takeaway
On Alpaca a stop becomes a market order on election, so it fills through a gap at the open. A stop-limit can sit unfilled below a gap ("remains active as a limit order"). No empirical study of retail stop slippage was found. Gap-through losses are a modelling issue for backtests that assume fills at the stop price.

### Cited Findings
- Alpaca stop orders: "Once the order is elected, the stop order becomes a market order"; sell stops are not converted to stop-limits; the fill price is not guaranteed. Stop-limit orders become limit orders when triggered. On a gap through the limit, the order "will remain active as a limit order until it is executable or cancelled" — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- Stops cannot use opg/cls, so a stop cannot be an auction order. Trailing stops do not trigger outside regular hours — [Alpaca: Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca)
- Stop-loss rules with overnight gaps (Arratia and Dorador 2019, Quantitative Finance): simple fixed-percentage stops can improve risk-adjusted returns in rising markets. This is a modelling study and does not measure fills — [IDEAS](https://ideas.repec.org/a/taf/quantf/v19y2019i11p1857-1873.html)
- Practitioner sources describe the same trade-off: stop-market fills at the open after a gap, beyond the stop; stop-limit may not fill at all — [Kotak Neo](https://www.kotakneo.com/stockshaala/swing-trading-strategies/gaps-and-overnight-risk/); [MQL5 blog (opinion)](https://www.mql5.com/en/blogs/post/775565)

### Inferences
- Backtests on daily bars should fill a stop at min(stop, open) when the open gaps through it (long positions), not at the stop. If the cost model already does this, the residual live slippage is the spread and momentum at the open.
- A stop-limit protects against flash prints but can leave a position open after a gap. Given the bot's small size and liquid names, stop-market plus an app-side "gap-through" check (for example a MOO sell if the pre-market is far below the stop) is the more robust combination. That is an inference, not a tested result.
- Broker-side stops survive app outages and use consolidated-tape election. App-side stops on IEX data can fire on thin IEX prints. Keep broker-side as the primary stop.

### Gaps
- No documented magnitudes in bps were found for retail stop-order slippage on liquid large caps.
- No Alpaca-specific data on stop fills at the open (for example whether stops elect on the opening auction print) was found.

## Documented retail execution cost magnitudes for liquid large caps and ETFs (bps)

### Takeaway
The best recent primary estimate is 7-46 bps per round trip across retail brokers for market orders (Schwarz et al., JF 2025). The spread is driven by broker and wholesaler. Frozen_stock's live fills should be benchmarked against it.

### Cited Findings
- Mean account-level round-trip cost of 0.07%-0.46% across six accounts at five brokers, excluding commissions; 85,000 simultaneous market orders — [IDEAS/RePEc](https://ideas.repec.org/a/bla/jfinan/v80y2025i5p2507-2541.html); [AAII summary](https://www.aaii.com/journal/article/23536-execution-prices-vary-significantly-for-commission-free-trades); [CXO Advisory](https://www.cxoadvisory.com/individual-investing/actual-stock-trading-frictions-by-broker)
- Each 1 bp of execution-price difference costs retail traders about $2.8 billion a year (media estimate) — [WashU Olin news](https://olin.washu.edu/about/news-and-media/news/2022/10/study-finds-astonishing-differences-in-brokers-pricing-of-stock-trades.php)
- Secondary summaries disagree on sign and units (round trip vs per trade). Read the paper before quoting exact figures — [CXO Advisory](https://www.cxoadvisory.com/individual-investing/actual-stock-trading-frictions-by-broker)

### Inferences
- The suggested experiments below use the existing per-fill slippage logging and the symbol-class × time-of-day calibration. Each compares arms within the same symbols and dates so that market conditions cancel out.
  1. **MOO vs 09:30-09:31 market (live, small):** randomly assign each entry (coin flip per signal) to `opg` market or `day` market sent at 09:30:05. Benchmark both against the official open (SIP, fetched later). Expected result: MOO slippage near 0 against the official open, with lower variance.
  2. **LOO vs MOO:** LOO priced at the prior close ± k × ATR (for example k = 0.5, 1.0). Track the fill rate, and log the counterfactual P&L of skipped signals ("missed winners") separately for mean-reversion and momentum strategies.
  3. **Delayed entry:** market or marketable limit at 09:45 and 10:00 vs MOO. Measure spread cost and drift from the open, both against SIP bars.
  4. **Marketable limit collar:** a `day` limit at the ask plus 5 or 10 bps vs plain market at the same time. Measure fill rate and how often the collar saves cost in a spike.
  5. **Exit leg:** after opg fills, submit OCO (take-profit plus stop-market) vs OCO with stop-limit (limit = stop − x bps). Log gap-through events and their fill vs stop. Also measure the protection gap between entry fill and OCO acceptance.
  6. **Feed sanity check:** daily job that compares IEX and SIP daily OHLC for the traded symbols. Log how often IEX lows or highs miss SIP stop or target touches, to size the backtest/live mismatch.
  7. **Calibration hygiene:** tag fills as paper or live and auction or continuous. Exclude paper opg/cls fills (quote-based simulation) from the calibrated cost model.

### Gaps
- No SEC 2021-2025 retail execution-quality report with bps by stock class was retrieved.
- No ETF-specific retail cost figures for the open were retrieved (the Alpha Architect summary was blocked).
- No figures were found that separate Alpaca's own live execution quality from the multi-broker study.
