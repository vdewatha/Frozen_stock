# Open-Source Trading Bots and Techniques Review — 2026-10-28

Scope: what to borrow from open-source frameworks and the literature to push Frozen_stock toward an
**80% win rate while keeping positive expectancy after costs** (AAPL, MSFT, SPY, QQQ; Alpaca paper; IEX 1-min
plus yfinance daily). Read-only research. Licenses and star counts were checked against the GitHub API on 2026-10-28.

---

## 0. The core trade-off: win rate vs expectancy

Expectancy per trade = `p·W − (1−p)·L − costs`, where p is the win rate, W the average win and L the average loss.
Break-even win rate = `(L + c) / (W + L)`.

| Target exit geometry (W : L) | Break-even win rate (before costs) | Comment |
|---|---|---|
| 1 : 1 | 50% | An 80% win rate here would be an exceptional edge |
| 1 : 2 (tight TP, wide stop) | 66.7% | The usual way to "buy" a high win rate |
| 1 : 4 | 80% | An 80% win rate at this geometry makes **zero** money |
| 2 : 1 | 33% | Trend-following geometry. Low win rate, can still be profitable |

Takeaways:
- A high win rate is easy to manufacture: use a tight take-profit, a wide stop (or none), and hold losers. That moves the
  distribution of outcomes into a rare, large left tail, so the win rate rises and expectancy usually falls once
  costs and gaps are counted. Our intrabar-stop backtester already models this. Keep it honest.
- The only **legitimate** ways to raise win rate without hurting expectancy are (a) **not taking** low-quality trades
  (filtering: meta-labeling, confidence thresholds, regime and time filters) and (b) better exit timing that is
  validated out of sample. Filtering lowers trade count. With only 4 symbols, trade count is the binding
  constraint on statistical confidence. Set a minimum such as ≥ 100 out-of-sample trades before believing any
  win rate. The Wilson 95% CI at 80/100 is about 71–87%.
- Recommended acceptance gate: **win rate ≥ 80% AND expectancy net of costs > 0 AND profit factor ≥ 1.3 AND
  lower bound of bootstrap CI on expectancy > 0**, judged on the forward paper trial, not only on the backtest.

---

## 1. Ranked shortlist: techniques to implement

Ranking criterion: plausibility of raising win rate *without* destroying expectancy, given our existing
infrastructure (walk-forward/purged CV, deflated Sharpe, bootstrap CIs, paper trial, edge-decay demotion).

| # | Technique | Expected impact | Effort | Sources |
|---|---|---|---|---|
| 1 | **Triple-barrier labeling + meta-labeling** (secondary classifier on top of existing strategy signals) | **High.** It is the most direct way to raise precision (win rate) while keeping the primary model's recall. It turns every existing strategy into a candidate primary model | M (1–2 wk) | López de Prado, *Advances in Financial ML* (2018), ch. 3; Joubert, "Meta-Labeling: Theory and Framework", JFDS 2022; Jesse docs https://docs.jesse.trade/docs/research/ml/meta-labeling ; Hudson & Thames toy example https://hudsonthames.org/?p=3043 |
| 2 | **Probability calibration + expectancy-aware threshold** (calibrate meta-model, then pick the threshold that maximises *net expectancy* subject to win rate ≥ 80% and N ≥ min trades, inside walk-forward) | **High.** It makes the "80%" a tunable, honest operating point instead of a hope | S (2–4 days) | scikit-learn `CalibratedClassifierCV` / reliability curves https://scikit-learn.org/stable/modules/calibration.html (BSD-3); FreqAI `DI_threshold` / outlier handling https://www.freqtrade.io/en/stable/freqai/ |
| 3 | **Regime filter** (trend: price vs 200-day SMA on SPY; volatility: VIX or realised-vol percentile; only trade the strategy in regimes where its OOS expectancy was positive). Also use regime as a meta-model feature | **Medium–High.** Mean-reversion long setups on index ETFs show much higher hit rates above the 200-day MA | S | Faber, "A Quantitative Approach to Tactical Asset Allocation" https://papers.ssrn.com/sol3/papers.cfm?abstract_id=962461 ; Connors & Alvarez, *Short Term Trading Strategies That Work* (2009) |
| 4 | **Short-horizon mean reversion with asymmetric but bounded exits** (e.g. RSI(2)/IBS oversold, buy above the 200-day MA, exit on first up-close or at the 5-day SMA, plus a time stop of N bars and a catastrophic stop). The structure naturally produces 65–80% win rates on SPY/QQQ | **Medium–High** as a *primary* model feeding #1. Historically one of the few setups with naturally high hit rates, though the edge has decayed since publication | S | Connors RSI(2) (book above); freqtrade ROI-table concept (time-decaying take-profit) https://www.freqtrade.io/en/stable/configuration/ |
| 5 | **Time-based exits / ROI table + trailing stop after profit offset** (freqtrade-style: take-profit target that shrinks with holding time; trailing stop only activates after +x%; hard time stop) | **Medium.** Converts stale "drifters" into small wins or scratches and raises win rate. Must be checked for expectancy because it truncates winners | S | freqtrade stoploss docs https://www.freqtrade.io/en/stable/stoploss/ (GPL-3.0: re-implement the idea, do not copy code) |
| 6 | **Protections / circuit breakers** (StoplossGuard: pause after K stop-outs in a window; CooldownPeriod after exit; LowProfitPairs: lock a symbol whose recent net profit < 0; MaxDrawdown lock) | **Medium.** Cuts loss clusters in hostile regimes. Complements our edge-decay demotion at a faster time scale | S | https://www.freqtrade.io/en/stable/plugins/#protections |
| 7 | **Time-of-day filter for intraday entries** (avoid the first 15–30 min and the last 10 min; IEX-only data is thin and noisy at the open) as a meta-model feature or hard rule | **Low–Medium.** Mostly improves fill quality and reduces false breakouts. Must be validated per strategy | S | Intraday U-shaped volume/volatility literature (e.g. Admati & Pfleiderer 1988, RFS); Alpaca IEX vs SIP caveat https://docs.alpaca.markets/docs/market-data-faq |
| 8 | **Ensemble consensus + fractional Kelly sizing** (require k-of-n strategy agreement as a primary-signal gate; size by ¼–½ Kelly using the *calibrated* meta-probability and empirical W/L, capped) | **Low–Medium** for win rate (consensus); improves risk-adjusted P&L (sizing). Kelly on miscalibrated probabilities is dangerous, so do it only after #2 | M | Thorp, "The Kelly Criterion in Blackjack, Sports Betting and the Stock Market"; López de Prado AFML ch. 10 (bet sizing from predicted probabilities) |

Supporting infrastructure (not ranked, but needed for the items above):
- **Optuna hyperparameter search inside walk-forward folds** (TPE sampler, objective = net expectancy with
  win-rate constraint; log the number of trials so the deflated Sharpe can account for them). https://optuna.org (MIT).
  freqtrade hyperopt is a good *design* reference (loss functions, `--timerange` splits), but it is GPL.
- **Probability of Backtest Overfitting (CSCV)** to complement the deflated Sharpe: Bailey, Borwein, López de Prado & Zhu,
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253 . Deflated Sharpe: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551 .
- **Sample-uniqueness weights / sequential bootstrap** for overlapping triple-barrier labels (AFML ch. 4). This matters
  because the meta-labels overlap in time.

### Implementation sketch for #1 + #2 (ideas only)
1. For each primary signal event (from the existing MA/MACD/RSI/breakout/ensemble/ML strategies), set barriers:
   upper = entry + a·σ, lower = entry − b·σ (σ = recent volatility, e.g. EWMA of returns or ATR), vertical = H bars.
   Label = 1 if the upper barrier is hit first **and the net-of-cost return > 0**; else 0. Use our intrabar logic
   and cost model so the labels match what the executor will experience.
2. Features for the meta-model: primary signal strength, strategy id, regime (SPY vs 200-day SMA, realised-vol
   percentile, VIX level), time of day, gap size, distance to VWAP/MAs, recent strategy hit rate (edge-decay
   state), spread/volume proxies. Gradient-boosted trees or logistic regression; keep it small.
3. Purged and embargoed walk-forward (already present). Calibrate (isotonic if many samples, else Platt).
4. Choose threshold τ per fold on validation data: maximise net expectancy s.t. precision ≥ 0.80 and
   trades ≥ N_min. Report the OOS precision, expectancy, and trade count, plus the bootstrap CI.
5. Route through the existing paper trial and promotion pipeline. Demote if the live precision CI drops below 70%
   or expectancy turns negative (edge-decay module).

Caution on barrier geometry: with a = b (symmetric), any precision above 50% plus cost is genuine edge. If you choose
a < b to lift precision, the meta-model must clear the higher break-even (table in §0). Always report both.

---

## 2. Frameworks reviewed

| Framework | URL | License | Stars* | Relevance | Borrowable ideas |
|---|---|---|---|---|---|
| **freqtrade** (+ FreqAI) | https://github.com/freqtrade/freqtrade | **GPL-3.0** (ideas only, no code) | ~56k | High (design) | ROI table (time-decaying TP), trailing stop with `trailing_stop_positive_offset`, protections (StoplossGuard, CooldownPeriod, LowProfitPairs, MaxDrawdown), hyperopt loss functions, FreqAI: periodic retraining, dissimilarity-index (DI) outlier gating to skip predictions on out-of-distribution data, label-period config. Crypto-focused; no Alpaca equities |
| **Jesse** | https://github.com/jesse-ai/jesse | MIT | ~8.7k | Medium–High | Clean strategy API; docs include a meta-labeling research guide; Optuna-based optimization; walk-forward-like split discipline. Crypto-focused |
| **Microsoft Qlib** | https://github.com/microsoft/qlib | MIT | ~50k | Medium | Alpha158/Alpha360 feature sets (usable as meta-model features), rolling retraining workflow, IC/rank-IC evaluation, model zoo (LightGBM, etc.). Cross-sectional China-A focus, overkill for 4 symbols |
| **vectorbt** (open) | https://github.com/polakowo/vectorbt | Apache-2.0 + **Commons Clause** (source-available) | ~9.6k | Medium | Fast parameter sweeps and heatmaps for robustness checks (look for plateaus, not peaks). Fine to *use* as a research tool; avoid embedding in a product you sell. Open edition is maintenance-only; PRO is paid |
| **backtesting.py** | https://github.com/kernc/backtesting.py | **AGPL-3.0** | ~9.2k | Low | Simple SL/TP and optimisation heatmaps. We already exceed it. AGPL: do not vendor |
| **NautilusTrader** | https://github.com/nautechsystems/nautilus_trader | **LGPL-3.0** | ~31k | Low–Medium | Event-driven, same-code backtest/live parity, realistic order book and fill models. Too heavy to adopt. Parity is the idea to emulate |
| **LEAN / QuantConnect** | https://github.com/QuantConnect/Lean | Apache-2.0 | ~22k | Medium | Algorithm Framework separation (Alpha → Portfolio Construction → Risk Mgmt → Execution) with "Insight" objects carrying direction, magnitude, *confidence*, and duration. A clean way to plumb meta-model probability into sizing. Fill/slippage models |
| **Lumibot** | https://github.com/Lumiwealth/lumibot | **GPL-3.0** | ~2.2k | Low–Medium | Native Alpaca integration and same-code backtest/live. Reference only (GPL) |
| **Hummingbot** | https://github.com/hummingbot/hummingbot | Apache-2.0 | ~21k | Low | Market making and arbitrage on crypto venues. Not applicable to directional equity swing trades. Its "triple barrier" position executor (TP/SL/time limit) is a neat config pattern |
| **FinRL** | https://github.com/AI4Finance-Foundation/FinRL | MIT | ~17k | Low | Deep RL for portfolio allocation. Research-grade, high overfit risk, and reported results are mostly in-sample or short OOS. Turbulence-index regime switch is the one borrowable idea |
| **mlfinlab** (Hudson & Thames) | https://github.com/hudson-and-thames/mlfinlab | **Proprietary / all rights reserved** (older BSD forks exist) | — | Reference | Canonical implementations of triple barrier, meta-labeling, sample weights, and fractional differentiation. Do **not** copy. Implement from the AFML book. `mlfinpy` (https://pypi.org/project/mlfinpy/) is a clean-room open alternative (verify license and maintenance before use) |
| **Optuna** | https://github.com/optuna/optuna | MIT | — | High (tooling) | TPE plus pruning for walk-forward hyperparameter search. Multi-objective (expectancy vs win rate) Pareto front |

\*Stars from the GitHub API on 2026-10-28, rounded.

License rule for our codebase: **MIT / Apache-2.0 / BSD code may be adapted with attribution. GPL-3.0, AGPL-3.0
and LGPL code (freqtrade, Lumibot, backtesting.py, NautilusTrader) must not be copied. Re-implement ideas from docs or
papers.** vectorbt's Commons Clause and mlfinlab's proprietary license also rule out copying.

---

## 3. Red flags: claims to distrust

1. **"80–95% win rate" bots and strategies (YouTube, GitHub READMEs, marketplace strategies).** Almost always
   (a) a tight TP with a wide or no stop, (b) averaging down / martingale / DCA "safety orders" (common in crypto
   grid/DCA bots), or (c) in-sample results. Always demand the average win/loss ratio, max drawdown, trade count,
   and an out-of-sample period. Win rate alone is meaningless.
2. **Stock-prediction repos showing "accuracy 90%" or "price prediction" LSTM charts.** Typical leakage: predicting
   price *levels* (a persistence model looks perfect), scaling with full-sample statistics, shuffled train/test
   splits, or features computed with future bars. Directional accuracy on daily returns above ~55% out of sample is
   rare. Treat higher claims as leakage until proven otherwise.
3. **Hyperopt / Optuna "best result" screenshots.** Selection bias from thousands of trials. Without the deflated
   Sharpe or PBO accounting for the trial count, the top result is expected to be overfit. freqtrade docs themselves
   warn about hyperopt overfitting.
4. **FinRL / deep-RL papers with very high Sharpe.** Often short test windows, no realistic costs, or a single seed.
   Reproductions frequently fail to beat buy-and-hold.
5. **Backtests without intrabar stop ambiguity handling.** When TP and SL are both inside one bar, optimistic
   ordering inflates win rate. Our backtester handles this. Do not relax it to hit 80%.
6. **Published high-win-rate rules (Connors RSI(2), IBS).** These are genuine historically, but the edge has decayed
   since publication (McLean & Pontiff, "Does Academic Research Destroy Stock Return Predictability?", JF 2016,
   https://onlinelibrary.wiley.com/doi/10.1111/jofi.12365). Validate on post-2015 data only, net of costs.
7. **Small-sample win rates.** 8/10 or even 16/20 wins is statistically compatible with a 55% true rate. Use
   Wilson CIs and our bootstrap. With 4 symbols and filtering, expect few trades per month, so the paper trial
   needs months, not days.
8. **IEX-only data.** Alpaca's free feed is IEX (a few % of consolidated volume). Intraday signals and stops computed
   on IEX bars can differ from SIP prints, so time-of-day and breakout results may not transfer.

---

## Bottom line

The realistic path to "8 of 10 trades profitable and net positive" is **a high-recall primary signal (existing
strategies, especially short-horizon mean reversion in an uptrend regime) plus a calibrated meta-labeling filter
with an expectancy-constrained threshold**, combined with time stops and protections. All of it is judged on
purged walk-forward results plus a forward paper trial with enough trades for the CI to exclude a coin flip. Do not
reach 80% by widening stops. Every candidate should report win rate, payoff ratio, expectancy, and trade count together.
