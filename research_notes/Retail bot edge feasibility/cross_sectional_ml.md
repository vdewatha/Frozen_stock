# Cross-sectional ML return prediction for Frozen_stock (US large caps, free data, 1-day to 1-month horizons, post-2015)

Scope note: research as of 2026-11-07. Primary sources read in full text this session: Gu, Kelly & Xiu (NBER w25398 version of the RFS 2020 paper) and Jiang, Kelly & Xiu (JF 2023, version of record). Everything else comes from abstracts, publisher or institute listings, or secondary summaries, and is flagged that way. Bot facts (11 paper trades at -184 bps against a +27 bps backtest, the free data stack, ~$100k equity) come from the assignment brief, not from an external source.

## 1. Gu, Kelly & Xiu (2020) and Avramov, Cheng & Metzker (2023): how much of the ML alpha is in small, illiquid, or distressed stocks, and what is left after costs

### Takeaway
GKX's monthly ML forecasts fit large stocks as well as or better than small stocks, measured by R². But their headline portfolio Sharpe ratios are gross of costs, long-short, rely on 94 characteristics (some fundamental), turn over about 110-130% a month, and come from a test sample (1987-2016) that ends before the period this bot cares about. Avramov, Cheng & Metzker show that the portfolio profits come largely from hard-to-arbitrage stocks: payoffs fall 62-80% once microcaps, non-rated firms, or distressed firms are excluded, and fall further after reasonable trading costs.

### Cited Findings
- GKX data: about 30,000 US stocks, 1957-2016, 94 stock characteristics plus interactions with macro variables. The test sample is 1987-2016, so it contains almost no post-2015 data. — [Gu, Kelly & Xiu, NBER w25398](https://www.nber.org/papers/w25398.pdf)
- Monthly out-of-sample R² (%), Table 1, full model estimated on all stocks:
  - All stocks: GBRT 0.34, RF 0.33, NN3 0.40.
  - Top 1,000 stocks by market equity: GBRT 0.52, RF 0.63, NN3 0.70, NN4 0.67.
  - Bottom 1,000: GBRT 0.32, RF 0.35, NN3 0.45.
  - OLS with all predictors: -11.28 in the top 1,000.
  - The authors say this "provides reassurance that machine learning is not merely picking up small scale inefficiencies driven by illiquidity."
  — [GKX, NBER w25398](https://www.nber.org/papers/w25398.pdf)
- Deep learning adds little at the monthly horizon: NN4 and NN5 do not improve on NN3. — [GKX](https://www.nber.org/papers/w25398.pdf)
- Value-weighted 10-1 decile spread from NN4: 2.3% a month, 20.1% annualized volatility, out-of-sample Sharpe 1.35. The linear benchmark gets 0.61 value-weighted and 0.83 equal-weighted. Equal-weighted NN4 gets 2.45, and 1.69 after excluding stocks below the 20th NYSE size percentile. So micro- and small caps inflate the equal-weighted figure substantially. — [GKX](https://www.nber.org/papers/w25398.pdf)
- Turnover: NN1-NN5 portfolios turn over "consistently between 110% and 130% per month"; trees and penalized regressions turn over slightly more. For comparison, a short-term reversal decile spread turns over 172.6% a month and a size decile spread 22.9%. GKX attribute the high turnover to "the large role of price trend predictors." — [GKX](https://www.nber.org/papers/w25398.pdf)
- Variable importance groups, in order:
  1. Price trends: 1-month reversal, 12-month momentum, momentum change, industry momentum, max return, 36-month reversal.
  2. Liquidity: turnover, market equity, dollar volume, Amihud illiquidity, zero-trade days, bid-ask spread.
  3. Risk: total and idiosyncratic volatility, beta, beta squared.
  4. Valuation and fundamentals, last: E/P, S/P, asset growth, earnings increases.
  — [GKX](https://www.nber.org/papers/w25398.pdf)
- Avramov, Cheng & Metzker, *Management Science* 69(5), 2023, pp. 2587-2619. Excluding microcaps, distressed stocks, or episodes of high market volatility "considerably attenuates profitability." Performance "further deteriorates in the presence of reasonable trading costs because of high turnover and extreme positions." ML methods "would struggle to achieve statistically and economically meaningful risk-adjusted performance in the presence of reasonable transaction costs." — [EconPapers/RePEc record](https://econpapers.repec.org/RePEc:inm:ormnsc:v:69:y:2023:i:5:p:2587-2619); [SSRN 3450322](https://papers.ssrn.com/abstract=3450322)
- Avramov et al., from a secondary summary by CUHK Business School, not the paper:
  - Sample 1987-2017.
  - Monthly value-weighted risk-adjusted returns of 0.75%-1.88%.
  - Payoffs fall 62% excluding microcaps, 68% excluding non-rated firms, and 80% excluding distressed firms around rating downgrades.
  - ML strategies remain profitable in long positions and in the post-2001 period, and do well in downturns.
  - Note: secondary sources disagree slightly (1.87% vs 1.88%). — [CUHK Business Knowledge summary](https://cbk.bschool.cuhk.edu.hk/the-limitations-of-using-ai-to-pick-stocks/)
- Follow-up, Azevedo, Hoegner & Velikov, "The Expected Returns on Machine-Learning Strategies":
  - Nine ML strategies earn net out-of-sample returns of up to 1.42% a month despite turnover above 50%.
  - Trading costs cut performance by 13-40%.
  - Common cost-mitigation techniques lower turnover but do not improve net performance; only a two-month holding period helps, and only marginally.
  - Part of the favorable result is attributed to the post-decimalization period of lower trading costs.
  - Their one-hidden-layer model has an expected gross (net) Sharpe of 0.94 (0.84) after adjusting for post-publication decay.
  — [Quantpedia summary](https://quantpedia.com/the-expected-returns-of-machine-learning-strategies/); [AEA 2025 program](https://www.aeaweb.org/conference/2025/program/paper/TihddefB)
- Follow-up, Leung, Lohre, Mischlich, Shea & Stroh, *Journal of Financial Data Science* 3(2), 2021: ML models were "somewhat more effective" than linear models over the prior decade at predicting one-month cross-sectional returns. Their cost and universe results were not retrieved. — [Lancaster eprint record](https://eprints.lancs.ac.uk/id/eprint/160329)

### Inferences
- GKX's large-stock R² (NN3 0.70% monthly in the top 1,000) is the strongest published evidence that ML predictability is not purely a microcap effect. But a monthly R² of 0.7% means more than 99% of the variance is unexplained. It is also a pooled fit statistic, not a net-of-cost portfolio result, and the large-stock portfolio Sharpe ratios after costs are not in the main text.
- The two papers can both be right. GKX show that predictability exists in large stocks. ACM show that the profits of the trading strategy, and especially its extreme positions, come mostly from stocks that are hard to arbitrage. A long-only S&P 500 book captures the first and gives up most of the second.
- GKX's top predictor groups (price trends, liquidity, volatility) can be computed from free OHLCV data. Several liquidity features (zero-trade days, bid-ask spread, Amihud) barely vary inside the S&P 500, so they matter less in this universe.
- Every headline sample above ends between 2016 and 2019. None measures 2015-2026 large-cap performance directly.

### Gaps
- GKX's appendix tables of portfolio Sharpe ratios for the top 1,000 stocks only (if they exist) were not checked. Only R² by size is in the main text.
- The exact Avramov et al. numbers (by model, net-of-cost tables, turnover) came from a secondary summary. The full paper was not retrieved.
- The Azevedo, Hoegner & Velikov universe (whether microcaps are included) and their sample period were not verified.
- I found no peer-reviewed 2023-2026 paper that re-runs GKX-style ML on S&P 500 or Russell 1000 stocks only, over 2015-2026, net of costs.

## 2. Short-horizon ML from price and volume only: JKX images, Fischer & Krauss LSTM, Krauss, Do & Huck

### Takeaway
Price- and volume-only ML has the most impressive gross results at weekly horizons (JKX). But in the 500 largest US stocks the weekly strategy's gross spread is about the same size as its own trading cost assumption, and the S&P 500 daily-horizon ML papers (Fischer & Krauss; Krauss, Do & Huck) show profits decaying to roughly zero after costs from about 2010. The surviving opportunity, if any, sits at monthly or longer horizons with low turnover, where gross Sharpe ratios are much lower.

### Cited Findings
- JKX setup: a CNN on 5-, 20-, and 60-day OHLC-and-volume chart images, trained as a binary up/down classifier. Training and validation data come from before 2001; the test sample is 2001-2019, with the model "held fixed for the entire 2001 to 2019 test sample." — [Jiang, Kelly & Xiu, JF 2023](https://economics.yale.edu/sites/default/files/2023-11/The%20Journal%20of%20Finance%20-%202023%20-%20JIANG%20-%20Re%25E2%2580%2590%20Imag%20in%20ing%20Price%20Trends_0.pdf)
- JKX full universe, weekly holding period (Table I):
  - Equal-weighted H-L Sharpe ratios: 7.15 (I5/R5), 6.75 (I20/R5), 4.89 (I60/R5). Weekly reversal gets 2.84 and TREND 2.92.
  - Value-weighted Sharpe ratios: 1.49, 1.74, 1.44, against 0.77 for weekly reversal.
  - Monthly turnover: 690% equal-weighted and 758% value-weighted for I5/R5.
  - The paper's maximum turnover is 200%/M, where M is the holding period in months. For a weekly strategy that is about 800% a month, so 700%+ means nearly the whole book is replaced every week.
  — [JKX](https://economics.yale.edu/sites/default/files/2023-11/The%20Journal%20of%20Finance%20-%202023%20-%20JIANG%20-%20Re%25E2%2580%2590%20Imag%20in%20ing%20Price%20Trends_0.pdf)
- JKX, 500 largest stocks only (Table IV):
  - Equal-weighted H-L: 0.12% a week with Sharpe 1.02 (I5/R5), 0.09%/0.78 (I20/R5), 0.11%/1.08 (I60/R5).
  - Value-weighted H-L: 0.17%/1.29 (I5/R5), 0.12%/0.96, 0.13%/1.03.
  - Turnover stays at 638%-722% a month.
  - In the same top-500 universe, weekly short-term reversal (STR, WSTR) earns negative H-L returns.
  - The value-weighted top-500 long decile earns 0.16% a week, against a decile average of about 0.08%.
  — [JKX](https://economics.yale.edu/sites/default/files/2023-11/The%20Journal%20of%20Finance%20-%202023%20-%20JIANG%20-%20Re%25E2%2580%2590%20Imag%20in%20ing%20Price%20Trends_0.pdf)
- JKX costs: profits "survive standard trading cost adjustments (10 basis points for stocks exceeding the 80th size percentile of the NYSE, and 20 basis points otherwise)." Net Sharpe ratios reach 4.0, 1.5, and 0.9 for weekly, monthly, and quarterly equal-weighted strategies, but these are for the full universe, not the top 500. The authors add that the weekly strategy "is mostly accessible to investors who behave as market makers." — [JKX](https://economics.yale.edu/sites/default/files/2023-11/The%20Journal%20of%20Finance%20-%202023%20-%20JIANG%20-%20Re%25E2%2580%2590%20Imag%20in%20ing%20Price%20Trends_0.pdf)
- JKX lower frequencies:
  - Monthly equal-weighted H-L Sharpe reaches 2.4, and quarterly 1.3.
  - Quarterly strategies turn over about 60% a month, similar to monthly momentum.
  - Most of the monthly strategy's return comes in days 1-5, but days 6-20 alone still give a Sharpe of 1.2.
  — [JKX](https://economics.yale.edu/sites/default/files/2023-11/The%20Journal%20of%20Finance%20-%202023%20-%20JIANG%20-%20Re%25E2%2580%2590%20Imag%20in%20ing%20Price%20Trends_0.pdf)
- Fischer & Krauss, *EJOR* 270(2), 2018: an LSTM on S&P 500 constituents, 1992-2015, earns 0.46% a day with a Sharpe of 5.8 before costs (working-paper figures). Outperformance is "very clear from 1992 to 2009, but as of 2010, excess returns seem to have been arbitraged away with LSTM profitability fluctuating around zero after transaction costs." — [EconPapers record](https://econpapers.repec.org/RePEc:eee:ejores:v:270:y:2018:i:2:p:654-669); [FAU working paper](https://iwf.rw.fau.de/files/2015/12/11-2017.pdf)
- A 2026 arXiv replication ("Cross-Sectional Heterogeneity in LSTM Networks for Financial Time Series") reports that after 2008 all three models stay below 0.1% average daily return, and that the original headline returns came from the 1990s. This was seen only in a search snippet and not verified. — [arXiv 2608.05755](https://arxiv.org/pdf/2608.05755)
- Krauss, Do & Huck, *EJOR* 259(2), 2017:
  - Deep nets, gradient-boosted trees, random forests, and an ensemble, trained on lagged returns of S&P 500 members after removing survivor bias, generating daily signals for 1992-2015.
  - The ensemble earns more than 0.45% a day for k=10 before costs; the working paper reports about 0.25% a day after costs.
  - "Profits are declining in recent years."
  — [EconPapers record](https://econpapers.repec.org/RePEc:eee:ejores:v:259:y:2017:i:2:p:689-702); [FAU working paper 03-2016](https://www.iwf.rw.fau.de/files/2016/03/03-2016.pdf)

### Inferences
- Rough cost check for JKX in the top 500, using its own cost assumption. Replacing about 90% of a book each week costs about 2 × 0.9 × the one-way cost per leg per week, roughly 18 bps a week at 10 bps one-way. The top-500 H-L gross return is 9-17 bps a week, so at JKX's own 10 bps the weekly large-cap CNN strategy is about breakeven or negative. At 2-3 bps one-way, which is plausible for $100k orders in S&P 500 names but not verified, it could stay positive. The answer depends on real execution cost. Alpaca paper fills cannot settle this.
- The long-only version this bot would run (top-500 long decile minus universe) has about 8 bps a week of gross edge in 2001-2019. That margin is thin enough that turnover control matters more than model choice.
- The daily-horizon S&P 500 ML literature is the closest match to the bot's previous approach, and it is the most clearly decayed (around zero after costs since about 2010). A daily-rebalanced S&P 500 ML ranker should be treated as dead on arrival.
- In large caps, weekly reversal (WSTR/STR) has negative H-L returns in JKX's top-500 table. A naive "buy the 1-week losers" feature should not be expected to work in this universe even though it ranks high in GKX's full universe.

### Gaps
- JKX's Internet Appendix net-of-cost results restricted to the top 500 were not retrieved, so the cost check above is my own approximation.
- No JKX-style result covers 2020-2026, and I did not find a published out-of-sample update of the image CNN after 2019.
- The arXiv 2026 replication's details (universe, costs) were not verified.

## 3. Large-cap-specific US evidence and the "virtue of complexity" debate

### Takeaway
The direct evidence for ML cross-sectional profits in US large caps, net of costs, after 2015, is thin to absent in what I could find. Large-cap gross predictability is documented through about 2016-2019, but nothing I found tests net-of-cost performance over 2015-2026. The Kelly-Malamud-Zhou "virtue of complexity" result is about timing the aggregate market, not ranking stocks. A prominent 2025 critique argues the result is an artifact (volatility-timed momentum). It should not be used to justify a large model for this bot.

### Cited Findings
- GKX: trees and neural nets are "especially successful among large stocks," with monthly R² of 0.52%-0.70% in the top 1,000. The test period ends in 2016. — [GKX](https://www.nber.org/papers/w25398.pdf)
- JKX: H-L Sharpe ratios of about 0.8-1.3 gross in the 500 largest stocks at weekly horizons, test period 2001-2019. — [JKX](https://economics.yale.edu/sites/default/files/2023-11/The%20Journal%20of%20Finance%20-%202023%20-%20JIANG%20-%20Re%25E2%2580%2590%20Imag%20in%20ing%20Price%20Trends_0.pdf)
- Kelly, Malamud & Zhou, "The Virtue of Complexity in Return Prediction," *JF* 2024: they show theoretically that simple models understate return predictability compared with "complex" models whose parameter count exceeds the number of observations. — [SFI listing](https://www.sfi.ch/en/publications/the-virtue-of-complexity-in-return-prediction); [Alpha Architect summary](https://alphaarchitect.com/complexity-is-a-virtue-in-return-prediction/)
- Nagel, "Seemingly Virtuous Complexity in Return Prediction," NBER w34104, 2025: the random-Fourier-feature strategy's performance "stems not from its ability to extract predictive signals from the training data, but from the fact that a volatility-timed momentum strategy happened to perform well in historical data." On artificial data with reversals instead of momentum, the method still builds volatility-timed momentum and does poorly. — [NBER w34104](https://www.nber.org/papers/w34104); [BFI working paper PDF](https://bfi.uchicago.edu/wp-content/uploads/2025/08/BFI_WP_2025-104.pdf)
- Kılıç, Federal Reserve FEDS 2025-089 ("Virtue or Mirage?"): in FX forecasting, the gains from complexity are limited, fragile, and rarely statistically significant. — [FEDS 2025-089](https://www.federalreserve.gov/econres/feds/files/2025089pap.pdf)
- A search for S&P 500 ML replications over 2015-2024 net of costs turned up mainly index-timing studies. For example, an Olson, Nusair & Galariotis study of S&P 500 index ML over 1970-2024 (as summarized on Substack) finds that transaction costs erased most abnormal returns after 1987. That is market timing, not cross-sectional ranking. — [Harbourfront Quant summary](https://harbourfrontquant.substack.com/p/evaluating-machine-learning-models)

### Inferences
- The best large-cap evidence (GKX top 1,000; JKX top 500) ends in 2016-2019 and is gross of costs or based on institutional cost assumptions. Fischer & Krauss and Krauss, Do & Huck, the two papers that run ML on S&P 500 members at daily horizons, both show decay. A reasonable prior is that large-cap ML signals exist but are small, and that net-of-cost edge after 2015 is unproven.
- The complexity debate is about time-series timing of the market factor, not the cross-sectional ranker in scope here. Its main lesson for this bot is caution: an apparently good complex-model backtest can be one lucky implicit strategy (Nagel), which is exactly the overfitting risk the bot has already hit.

### Gaps
- No post-2015 US S&P 500 or Russell 1000 net-of-cost cross-sectional ML study was found or verified. This is the central evidence gap for the build decision.
- Blitz, Hoogteijling, Lohre & Messow (2023), "How can machine learning advance quantitative asset management?", came up in search ([Lancaster eprint PDF](https://eprints.lancs.ac.uk/199224/1/BlitzHoogteijlingLohreMessow2023_SSRN.pdf)) but was not read. It may contain large-cap and practitioner evidence.

## 4. Turnover against the cost budget, and cost-aware training (Jensen, Kelly, Malamud & Pedersen)

### Takeaway
Pure return-forecast ML rankers trade too much: about 110-130% a month at monthly rebalance (GKX) and 640-760% a month at weekly rebalance (JKX). Cost-aware approaches that learn portfolio weights with a net-of-cost objective are where the recent literature points (Jensen-Kelly-Malamud-Pedersen, in press at RFS), and they report that cost-blind ML frontiers can "bend downward," meaning they are not implementable.

### Cited Findings
- GKX neural-network portfolios turn over 110-130% a month at monthly rebalance. The value-weighted table shows 121-127% for NN1-NN5; the OLS-3 baseline turns over 58%. — [GKX](https://www.nber.org/papers/w25398.pdf)
- JKX weekly CNN strategies turn over 619-758% a month, quarterly about 60%. — [JKX](https://economics.yale.edu/sites/default/files/2023-11/The%20Journal%20of%20Finance%20-%202023%20-%20JIANG%20-%20Re%25E2%2580%2590%20Imag%20in%20ing%20Price%20Trends_0.pdf)
- Jensen, Kelly, Malamud & Pedersen, "Machine Learning and the Implementable Efficient Frontier":
  - Strategies should be judged by net-of-cost return at each risk level.
  - ML return forecasts that ignore trading costs lead to "excessive reliance on fleeting small-scale characteristics, resulting in poor net returns."
  - Superior net performance comes from "learning directly about portfolio weights using an economic objective."
  - The method also yields "economic feature importance."
  - Some benchmark frontiers bend downward after costs.
  - Listed as in press at RFS; SSRN version from August 2022, revised 2024.
  — [SFI publication page](https://sfi.ch/en/publications/machine-learning-and-the-implementable-efficient-frontier); [SSRN 4187217](https://papers.ssrn.com/abstract=4187217)
- Azevedo, Hoegner & Velikov: standard cost-mitigation methods reduce turnover and costs but do not improve net performance, except a two-month holding period, which helps marginally. — [Quantpedia summary](https://quantpedia.com/the-expected-returns-of-machine-learning-strategies/)
- Avramov et al.: ML performance deteriorates after costs "because of high turnover and extreme positions." — [EconPapers record](https://econpapers.repec.org/RePEc:inm:ormnsc:v:69:y:2023:i:5:p:2587-2619)

### Inferences
- At $100k in S&P 500 names, market impact is negligible. The relevant cost is half-spread plus slippage at the open or close. JKX's 10 bps one-way for large stocks is an institutional-scale assumption and probably conservative for this bot. But Alpaca paper fills likely understate real costs, so backtests should stress-test at 5 and 10 bps one-way.
- A full Jensen-Kelly-Malamud-Pedersen implementation (learning weights against a price-impact model) is too heavy for a minimal test. The cheap proxies are:
  - a longer label horizon (20 days),
  - a hold band (buy at rank ≤ N, sell only when rank falls past 3N),
  - trading a position only when the change in predicted rank-spread exceeds a cost threshold.
  Each proxy should be pre-registered, because each extra variant adds to the trial count.
- Rough turnover budget for a long-only book. With a gross top-bucket edge of about 8 bps a week (JKX top-500, 2001-2019) and a 5 bps one-way cost, the book can replace no more than about 80% of itself per week before costs eat the edge (2 × 0.8 × 5 = 8 bps). Realistically the book should target weekly replacement well below 40%, which means banding is mandatory.

### Gaps
- Jensen-Kelly-Malamud-Pedersen's numbers (net Sharpe ratios, assumed AUM, sample, universe) were not retrieved because SSRN returned 403. Their published net-of-cost frontier figures should be checked before the report quotes any number.
- JKX's Internet Appendix net-of-cost Sharpe ratios for the top 500 were not retrieved.

## 5. Free-data features that suffice, versus those that need fundamentals; Qlib Alpha158; SEC XBRL

### Takeaway
GKX's most important predictor groups are price trends, liquidity, and volatility, all computable from free daily OHLCV. Fundamentals rank last. An Alpha158-style price/volume feature set plus earnings-date features is enough for a first honest test. Qlib's published benchmarks are China A-shares (CSI300/CSI500) only and say nothing about US large caps. SEC Financial Statement Data Sets give free, as-filed XBRL fundamentals from 2009 onward, but point-in-time use needs care.

### Cited Findings
- GKX importance ranking: price trends (1-month reversal, 12-month momentum, momentum change, industry momentum, max return, 36-month reversal), then liquidity, then risk, then valuation and fundamentals (E/P, S/P, asset growth, earnings increases) last. Trees and NNs draw on a "more democratic" spread of characteristics than linear models. — [GKX](https://www.nber.org/papers/w25398.pdf)
- JKX: the CNN signal is approximated best by conventional signals including dollar volume, size, reversal, and Amihud illiquidity. Price/volume patterns alone produce the results. — [JKX](https://economics.yale.edu/sites/default/files/2023-11/The%20Journal%20of%20Finance%20-%202023%20-%20JIANG%20-%20Re%25E2%2580%2590%20Imag%20in%20ing%20Price%20Trends_0.pdf)
- Qlib benchmarks cover China's A-share market only (CSI300 main, CSI500 partial), with no US results. Alpha158 is a hand-engineered price/volume feature set; Alpha360 is raw price/volume.
  - CSI300 with Alpha158, mean over 20 seeds:
    - LightGBM: IC 0.0448, Rank IC 0.0469, annualized return 9.01%, information ratio 1.02, max drawdown -10.4%.
    - XGBoost: IC 0.0498, IR 0.91.
    - MLP: IR 1.14.
    - LSTM and GRU: IR 0.52-0.56.
    - Transformer: IR 0.40.
    - DoubleEnsemble is best, at IR 1.34.
  - The page notes that the backtest changed as of version 0.8.0, and it does not state the cost settings.
  — [Qlib benchmarks README](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md)
- SEC Financial Statement Data Sets: numeric data from the face financial statements, extracted from XBRL filings and presented "without change from the 'as filed' financial reports."
  - Coverage January 2009 through September 2026, updated quarterly, public downloads.
  - The SEC does not guarantee accuracy.
  - As of December 2024, only primary-statement numeric data is included, with a new segments field.
  — [SEC Financial Statement Data Sets](https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets)

### Inferences
- A sufficient first feature set from free data, all ranked cross-sectionally each date:
  - Returns over 1, 5, 10, 20, 60, 120, and 252 days, plus 12-1 momentum.
  - Realized volatility over 5, 20, and 60 days, and max daily return.
  - Distance from the 52-week high.
  - Volume relative to its 20- and 60-day average, dollar volume, and Amihud illiquidity.
  - Beta and idiosyncratic volatility against SPY.
  - Overnight gap and intraday range statistics.
  - Days to and since the last earnings date, and the stock's earnings-day return/gap, a price-based proxy for post-earnings drift that avoids needing earnings-surprise (SUE) data.
  - Industry or sector momentum, if a free point-in-time sector map is available.
- Market-level variables (VIX, FRED macro, SPY trend) do not change cross-sectional ranks unless they are interacted with stock features. In a minimal pooled ranker they are better used for sizing or gating outside the model.
- Qlib is a useful feature recipe and pipeline reference (Alpha158 is price/volume only). Its CSI300 numbers come from a more retail-dominated market and should not anchor expectations for the S&P 500. Expect a lower US large-cap IC than CSI300's 0.04-0.05.
- Fundamentals from SEC data are a second-phase option only, and only if each value is stamped with its filing or acceptance date. GKX ranks fundamentals last, so they are not needed for a first test.

### Gaps
- Whether the SEC dataset's submission table carries filing or acceptance timestamps was not confirmed from the page fetched. I believe it does (a `filed` / `accepted` field), but this must be checked in the SEC's documentation PDF before claiming point-in-time usability. Restatement handling was also not confirmed.
- Qlib's exact backtest settings (top-k, drop rate, open/close costs) were not on the README page.

## 6. Failure modes and validation: per-symbol vs pooled models, leakage, overfitting, turnover, survivorship

### Takeaway
The bot's paused per-symbol random forest (+27 bps a trade in backtest, -184 bps over 11 paper trades) fits the textbook failure pattern. Per-symbol models have tiny effective samples and learn market direction instead of relative value. The literature's successful designs are pooled panel models across all stocks and dates, trained on cross-sectionally normalized inputs and evaluated strictly out of sample with fixed models or expanding windows. The main residual risks for this bot are survivorship gaps in yfinance, label overlap leaking into validation, and the multiple-testing load from the bot's many prior strategy searches.

### Cited Findings
- GKX estimate one pooled model on all stocks and evaluate subsets (top and bottom 1,000) from that full model. Their train/validation/test design uses 18 years of training (1957-1974), 12 years of validation (1975-1986), and 30 years of test (1987-2016). — [GKX](https://www.nber.org/papers/w25398.pdf)
- In GKX, OLS with all predictors gets an out-of-sample R² of -3.46% (all stocks) and -11.28% (top 1,000). Unregularized flexible fits destroy out-of-sample performance, especially in large caps. — [GKX](https://www.nber.org/papers/w25398.pdf)
- JKX train on pre-2001 data and hold the model fixed for the whole 2001-2019 test, which removes the temptation to tune on the test period. — [JKX](https://economics.yale.edu/sites/default/files/2023-11/The%20Journal%20of%20Finance%20-%202023%20-%20JIANG%20-%20Re%25E2%2580%2590%20Imag%20in%20ing%20Price%20Trends_0.pdf)
- Krauss, Do & Huck explicitly remove survivor bias from S&P 500 constituents before training. — [EconPapers record](https://econpapers.repec.org/RePEc:eee:ejores:v:259:y:2017:i:2:p:689-702)
- Fischer & Krauss: S&P 500 ML profits fluctuate around zero after costs from 2010, which looks like the edge decaying or being arbitraged away. — [EconPapers record](https://econpapers.repec.org/RePEc:eee:ejores:v:270:y:2018:i:2:p:654-669)
- Nagel: a complex model's backtest can be driven by one implicit strategy that "happened to perform well in historical data." — [NBER w34104](https://www.nber.org/papers/w34104)
- Qlib's CSI300 benchmarks are averaged over 20 random seeds and show large spreads between architectures on the same data (Transformer IR 0.40 vs LightGBM 1.02 on Alpha158). Model and seed choice is itself a source of selection bias. — [Qlib benchmarks README](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md)
- From the assignment brief: the bot's per-symbol random forest made 11 paper trades averaging -184 bps against +27 bps in backtest. A +27 bps backtest edge per trade is within plausible cost and slippage noise.

### Inferences
- Why per-symbol models fail: a single stock over 2000-2026 has about 6,500 daily rows with heavily overlapping multi-day labels, and maybe a few hundred independent observations at a 20-day horizon. Its target is dominated by market and sector moves. A pooled model over about 500 names gets roughly 500 times the rows. Ranking or demeaning the labels cross-sectionally removes the market component, so the model learns relative value, which is what a ranker trades.
- Required validation for this bot:
  1. An expanding-window walk-forward with annual (or semi-annual) retraining.
  2. A purge of training rows whose label window overlaps the test start, plus an embargo of at least the label horizon (5 or 20 sessions).
  3. Hyperparameters fixed before the test period, or tuned only on pre-2015 data.
  4. A single pre-registered 2015-2026 out-of-sample run.
  5. Every variant counted toward a deflated-Sharpe or multiple-testing hurdle, using the bot's existing trial library and adding to it rather than starting fresh.
- Survivorship is the largest data risk. The point-in-time S&P 500 list helps only if yfinance has price history for removed or acquired members on each date. Before modelling, measure per-date coverage (the fraction of members on that date with data). Report results only on dates with coverage of 95% or more, and say how missing names are handled. Earlier work in this project found PEAD blocked by survivorship.
- Execution-time leakage: features must use data through the close on day t only, and the first tradable price must be the open (or close) of day t+1. Results should be stable under a one-day execution delay. If not, the backtest is probably harvesting bid-ask bounce or close-price artifacts.
- Paper-trading validation cannot statistically confirm a weekly information ratio of about 0.5 within months; that takes years. A live shadow period checks implementation (fills, slippage, how closely live returns track the backtest), not alpha.

### Gaps
- Primary sources for purged and embargoed cross-validation and for the deflated Sharpe ratio (López de Prado; Bailey & López de Prado) were not fetched this session. These are standard practice, but the report should cite them from their original publications if it names them.
- I found no well-documented retail replication of a pooled cross-sectional ML ranker on S&P 500 members over 2015-2026 with free data, net of costs. Practitioner write-ups found were index-timing or unverified.

## 7. Recommendation: build or not, the minimal honest design, and what result justifies continuing

### Takeaway
Run a cheap, time-boxed, offline research test, not a live strategy. A pooled cross-sectional ranker is the one ML design with real academic support, and the bot already has the data. But the evidence says to expect a small large-cap edge (a few percent a year gross for a long-only top bucket in 2001-2019) that turnover can wipe out. There is no published proof of net-of-cost profits in US large caps after 2015. Proceed only under pre-registered kill criteria. If the test fails, stop; don't tune the model until it passes.

### Cited Findings (basis for the design)
- Large-stock predictability exists in pooled ML models: GKX top-1,000 NN3 R² of 0.70% monthly, and JKX top-500 weekly H-L Sharpe of about 1.0-1.3 gross. — [GKX](https://www.nber.org/papers/w25398.pdf); [JKX](https://economics.yale.edu/sites/default/files/2023-11/The%20Journal%20of%20Finance%20-%202023%20-%20JIANG%20-%20Re%25E2%2580%2590%20Imag%20in%20ing%20Price%20Trends_0.pdf)
- Profits shrink sharply outside hard-to-arbitrage stocks and after costs. — [Avramov, Cheng & Metzker record](https://econpapers.repec.org/RePEc:inm:ormnsc:v:69:y:2023:i:5:p:2587-2619); [CUHK summary](https://cbk.bschool.cuhk.edu.hk/the-limitations-of-using-ai-to-pick-stocks/)
- Daily-horizon S&P 500 ML profits decayed to about zero after costs from 2010. — [Fischer & Krauss record](https://econpapers.repec.org/RePEc:eee:ejores:v:270:y:2018:i:2:p:654-669)
- Cost-aware objectives beat cost-blind return forecasts net of costs. — [Jensen, Kelly, Malamud & Pedersen, SFI page](https://sfi.ch/en/publications/machine-learning-and-the-implementable-efficient-frontier)
- Deeper models add little at monthly horizons (NN3 ≥ NN4/NN5), and gradient-boosted trees are competitive (CSI300 Alpha158 LightGBM IR 1.02 vs Transformer 0.40). — [GKX](https://www.nber.org/papers/w25398.pdf); [Qlib benchmarks](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md)

### Inferences: minimal honest design
- **Universe:** point-in-time S&P 500 members on each date, with yfinance coverage checked per date and a minimum price and dollar-volume filter. No small caps: the question is whether the edge exists where this bot can trade it.
- **Model:** a single pooled LightGBM, as either regression on cross-sectionally ranked forward returns or LambdaRank. Keep the trees small, regularize heavily (for example 31 leaves or fewer, a large minimum leaf size, subsampling), and average over five or more seeds. Do not use a per-symbol model, an LSTM, a CNN, or a Transformer in round one.
- **Baselines** (the ML must beat all of them net of costs):
  1. Ridge or linear regression on the same features.
  2. A 12-1 momentum sort.
  3. A simple equal-weighted composite of momentum, low volatility, and 52-week-high distance.
  4. The equal-weighted universe.
- **Label:** a pre-registered primary horizon of 20 sessions (forward return from the open of t+1, cross-sectionally ranked), with a secondary 5-session label reported but not used for selection. The 20-day choice follows the turnover evidence: weekly signals are the strongest but cost the most, and JKX days 6-20 remain significant.
- **Features:** the free OHLCV and earnings-date set from section 5, all cross-sectionally ranked each date. No fundamentals, macro, or VIX inside the model in round one.
- **Rebalance and portfolio:**
  - Weekly scoring.
  - Long-only top N (about 20-25 names, equal-weighted, which fits $100k).
  - Hysteresis band: enter at rank ≤ 25, exit when rank falls past 75.
  - Hold periods of 5-20 sessions, matching the bot's 1-20 session range.
  - Optional: a short pilot on the bottom bucket, reported separately.
- **Cost-aware piece:** apply a turnover penalty or hold band in portfolio construction, and use costs of 5 bps one-way as base and 10 bps as stress. Learning weights directly (Jensen et al.) is phase two only if phase one passes.
- **Validation:**
  - Expanding-window training from 2000.
  - Retrain annually.
  - Purge plus an embargo of at least 20 sessions.
  - Hyperparameters frozen using pre-2015 data only.
  - One out-of-sample run over 2015-01 to 2026-10.
  - Log every variant into the trial library.
  - Run a one-day execution-lag robustness check.
- **Pass criteria (all required to continue; my proposed hurdles, not from a source):**
  1. Mean weekly cross-sectional Spearman IC on the 20-day label is above 0 with a Newey-West t-stat of 3 or more over the out-of-sample period.
  2. The long-only top-N book's net active return against the equal-weighted universe is positive at 10 bps, with an annualized information ratio of 0.5 or more at 5 bps.
  3. Net active return is positive in at least 2 of 3 subperiods (2015-2019, 2020-2022, 2023-2026).
  4. The model beats the ridge and momentum baselines net of costs. If it doesn't, ship the simpler baseline instead.
  5. The deflated Sharpe probability exceeds 0.95 given the cumulative trial count.
  6. Average weekly book replacement is below about 40%.
- **If it passes:** run a live shadow for 13 weeks or more, scoring weekly without trading, to measure how closely live results track the backtest and to model real fills. Only then allocate a small paper sleeve.
- **If it fails:** record it as a documented negative result and do not iterate on architecture. The most useful fallback is to use the ranker's score only as an input to the existing meta-label filter (vetoing rule signals for stocks in the bottom half of the cross-sectional rank). That adds no turnover of its own, but it inherits the meta-label filter's own lack of out-of-sample proof.
- **Base-rate expectation:** the most likely outcome is a statistically real but small IC whose long-only, net-of-cost information ratio sits near the 0.3-0.5 border. In effect that is a smoothed nonlinear blend of momentum, low volatility, and trend. It is worth one honest test because the infrastructure cost is low and the result decides a whole branch of the research tree. It is not worth weeks of model tuning.

### Gaps
- The pass hurdles above are my proposals. I did not find a sourced standard for retail-scale thresholds.
- Real Alpaca execution costs (half-spread plus slippage at the open or close for S&P 500 names at about $5k per order) were not researched. They decide whether the weekly variant is viable and should be measured from the bot's own fill logs.
- Post-2015 large-cap, net-of-cost ML evidence remains the key missing piece. The bot's own pre-registered test would be new evidence rather than a replication.
