# Meta-labeling filter for BUY signals: 2026-10-28

Branch `phase2/meta-label`. Implements items #1 and #2 of `open-source-research-20261028.md`: triple-barrier
labels, a calibrated meta-model, and a threshold chosen to maximise net expectancy subject to win rate ≥ 80%
and a minimum trade count. The model is wired into the paper cycle in **shadow mode only**.

## Bottom line

* **The 80% target is not reachable out of sample with these primary strategies at the live 3%/3% bracket.**
  In 11 of 13 yearly folds, no threshold on the validation year reached 80% on ≥ 30 trades with positive
  expectancy, so the filter took no trades in those years. In the 2 folds where it did, it took 166
  executable trades over 2014–2026, with a 49.4% win rate and +0.04% per trade. The bootstrap CI includes
  zero.
* The model has a real but small edge. Pooled, its ranking is near random: fold AUC is 0.47–0.55, about
  0.52 on average. Only the top tail of calibrated probability is useful. Taking OOS trades with calibrated
  p ≥ 0.80 gives **62% wins, +0.61%/trade, PF 1.64, 2,200 trades** (2014–2026). This frontier is
  descriptive: those fixed cut-offs were read off the test years, not chosen in advance.
* Taking every primary BUY (the current behaviour) loses money after costs: **51.3% wins, −0.09%/trade,
  PF 0.94** over 62k executable trades. Post-2020 it is worse: 50.5%, −0.13%/trade.
* The deployed artifact's own final validation year (2025-11 → 2026-10) also cannot reach 80%. The best
  achievable rate there was 79.7%. The artifact's `threshold` is therefore `null`, and every shadow
  decision is currently `skip`, with the calibrated probability recorded. This is the honest output of
  the rule the owner asked for. Do **not** switch to `enforce`: with no threshold, it would block every BUY.

## Setup

* Universe: the 45 `paper_execution_symbols`, with yfinance adjusted daily bars from 2008-06 to 2026-10-27
  (XLRE from 2015, XLC from 2018, META from 2012, ABBV from 2013, TSLA from 2010). This is **today's**
  megacap list, so there is survivorship bias that flatters every row below.
* Primary signals: `macd_momentum`, `moving_average_crossover`, `rsi_mean_reversion`,
  `bollinger_mean_reversion`, `channel_breakout`, `trend_pullback` and `ensemble`, all at default
  parameters. Each is replayed per bar by a vectorised replica (`meta_label_primary.py`). The test suite
  checks it for exact parity with the strategy classes on prefixes.
  `model_predictive_long` is **excluded**. It refits a random forest on every call, so a per-bar replay of
  15 years is infeasible. In shadow mode its BUYs are recorded as `unsupported_strategy`.
* Events: every BUY bar, with the decision at the close of t and entry at the open of t+1. That is
  306,037 events.
* Labels (`triple_barrier.py`): take-profit +3%, stop −3% (the live bracket), and a vertical barrier at
  10 bars. Fills follow `backtester.run_backtest`: gaps fill at the open, stop-first when both levels are
  inside one bar, and the vertical exit is at the open. Costs use the backtester's `volatility_scaled`
  model on both sides plus 1 bp fee per side, on a $10k notional. Label = 1 only if the **net** return is
  > 0. Overlap is handled with AFML average-uniqueness sample weights, computed per symbol across all
  strategies.
* Features use only data up to bar t, with a lookback of at most 252 bars, so the paper path's 260-bar
  frame is enough:
  * strategy one-hot, plus primary confidence and probability_up
  * 1/5/20/60-day returns, 20/60-day volatility, ATR14%
  * distance to the 20/50/200-day SMAs, SMA50/200, RSI14 and RSI2
  * IBS, gap, 20-day volume ratio, distance to the 252-day high
  * SPY: above SMA200, distance to SMA200, 20-day volatility, vol20/vol250, 5/20-day returns
  * count of strategies saying BUY, day of week, ETF flag, sector group
* Model: `HistGradientBoostingClassifier` (seeded) with uniqueness weights, then isotonic calibration.
* CV: yearly walk-forward with test years 2014–2026. For test year Y:
  * training uses labels that end before Y−2 minus a 10-day embargo
  * the calibrator is fitted on year Y−2
  * the threshold is selected on year Y−1
  * year Y is scored once
  
  Every partition is purged so that no label crosses into a later partition (asserted by tests and listed
  in the audit).
* Threshold rule: maximise mean net return per trade on the validation year, subject to win rate ≥ 0.80,
  ≥ 30 trades and expectancy > 0. When no threshold qualifies, no trades are taken that year.
* Metrics:
  * "Executable" means non-overlapping trades per (symbol, strategy). A strategy cannot add a second lot
    while it already holds the name, which mirrors the paper path's no-pyramiding rule.
  * CIs come from a month-cluster bootstrap (2,000 draws).
  * No portfolio-level caps (sector cap, max positions) are modelled.

## Out-of-sample results (executable trades, net of costs)

| Period | Policy | Trades | Win rate (95% CI) | Expectancy/trade (95% CI) | PF |
|---|---|---|---|---|---|
| 2014–2026 | take every primary BUY | 62,361 | 51.3% (49.5–53.1) | −0.09% (−0.20, +0.03) | 0.94 |
| 2014–2026 | **meta filter, target 80%** | 166 | 49.4% (31–78) | +0.04% (−0.87, +1.53) | 1.04 |
| 2014–2026 | meta filter, target 65% | 968 | 53.4% (44–65) | +0.13% (−0.39, +0.77) | 1.10 |
| 2014–2026 | meta filter, target 55% | 5,352 | 52.2% (47–58) | −0.06% (−0.35, +0.28) | 0.96 |
| 2020–2026 | take every primary BUY | 36,870 | 50.5% (48.2–52.7) | −0.13% (−0.28, +0.01) | 0.92 |
| 2020–2026 | **meta filter, target 80%** | 41 | 95.1% (70–100) | +2.50% (+1.15, +2.85) | 17.3 |
| 2020–2026 | meta filter, target 70% | 65 | 83.1% (63–98) | +1.54% (+0.03, +2.66) | 3.91 |
| 2020–2026 | meta filter, target 55% | 1,083 | 58.4% (49–70) | +0.38% (−0.20, +1.05) | 1.33 |

The post-2020 "80%" row is **one fold**. All 41 trades come from 2020, the only post-2020 year whose
validation year (2019) passed the constraint, and they fall in 4 calendar months. Six later years took no
trades. That is not evidence of a repeatable 80% filter.

### Win-rate / expectancy frontier (fixed calibrated-probability cut-offs, OOS, executable)

| p ≥ | 2014–2026 trades | win | exp/trade | PF | 2020–2026 trades | win | exp/trade | PF |
|---|---|---|---|---|---|---|---|---|
| 0.50 | 35,983 | 52.1% | −0.03% | 0.98 | 18,614 | 52.6% | +0.01% | 1.01 |
| 0.60 | 8,268 | 55.7% | +0.20% | 1.17 | 3,183 | 56.8% | +0.30% | 1.25 |
| 0.70 | 4,700 | 57.3% | +0.34% | 1.31 | 1,901 | 57.3% | +0.31% | 1.26 |
| 0.80 | 2,200 | 62.1% | +0.61% | 1.64 | 393 | 63.6% | +0.71% | 1.70 |
| 0.90 | 1,863 | 64.1% | +0.71% | 1.80 | 270 | 67.8% | +0.89% | 2.01 |

The best achievable win rate with a usable trade count is about **62–68%** at about +0.6–0.9% per trade.
That is profitable on paper, but these cut-offs were picked with hindsight, and the calibrated probability
overstates the realised win rate by about 20 points in the tail: isotonic regression fitted on one year
does not transfer cleanly. A frontier policy chosen in advance would need to be validated on new paper
data first.

### Sensitivity (same walk-forward, 80% target unless noted)

* Logistic regression instead of gradient boosting: 28 executable trades at target 80%, 53.6% wins.
  At p ≥ 0.80 it reaches only 50.9%, which is weaker than gradient boosting.
* Platt instead of isotonic calibration: no fold was feasible after 2014, and the 70% target lost money
  (−0.36%/trade).
* Volatility-scaled symmetric barriers (1σ·√10 each side): taking every BUY goes positive (54.0%,
  +0.19%/trade, PF 1.11). The filter's 80% target is never feasible, and the best target (60%, objective
  "total") gives 55.1% and +0.31%/trade. This is a different exit geometry from the live bracket, so it is
  not deployed.
* All runs used the same 13 folds and are reported here, so nothing was selected from these sensitivity
  runs.

### Per-fold threshold audit (gradient boosting + isotonic, target 80%)

Feasible folds: 2014 (validation 37 trades at 89%) and 2020 (validation 42 trades at 95%). In the other
years the best validation win rate with ≥ 30 trades was 47–71%. Every fold's training labels end before
its calibration window, which starts on 1 January of Y−2. For example, fold 2026 trains on labels ending
by 2023-12-21, calibrates on 2024 and selects on 2025.

## What would move the needle

1. Better primary signals. Short-horizon mean reversion in an uptrend (RSI(2)/IBS, research item #4) is
   being built by the scorecard agent. The meta-model only needs a strategy column and a retrain to use
   them.
2. Collect shadow records. Each paper BUY now logs the calibrated probability, so a forward reliability
   curve can confirm or reject the tail edge above without hindsight.
3. Revisit exits only with symmetric or wider geometry, and never by shrinking take-profits.

## Shadow integration

* `stock_paper_autotrader.run_stock_paper_signal_cycle` resolves `meta_label_shadow.resolve_runtime()` and
  passes it to the batched signal pass. For every BUY, `stock_paper_signal_batch` computes features once per
  symbol from the trusted 260-bar frame and SPY's frame. This takes about 11 ms per symbol. The record
  contains `mode, status, probability, threshold, decision, reason, model_sha256, feature_date` and is
  stored in three places:
  * the persisted `StrategySignal.features["meta_label"]`
  * the outcome dict, and the cycle `results[i]["meta_label"]`
  * an INFO log line
  
  The cycle result also carries a `meta_label` summary: mode, sha, counts of would-take and would-skip.
* `paper_meta_label_mode` accepts `off`, `shadow` (default) or `enforce`.
  * `shadow` never changes an order. A test asserts identical reserve/dispatch calls versus `off`.
  * A missing or corrupt artifact makes `shadow` behave as `off` (fail open).
  * `enforce` skips BUYs not marked `take`, and skips every BUY when the artifact is unavailable or a
    signal cannot be scored (fail closed). Sells and protective stops are never filtered.
* Artifact: `artifacts/api-server/backend/stock_training_artifacts/meta_label/meta_label_model.joblib`
  (about 200 KB) plus a `.json` manifest with sha256, the training windows, the policy and the validation
  frontier. The path can be overridden with `PAPER_META_LABEL_MODEL_PATH`; a relative path resolves
  against the backend directory. The loader verifies the digest.
* Reproduce it from `artifacts/api-server/backend`:
  `PYTHONPATH=. python scripts/train_meta_label.py --data-dir <cache> --download [--walk-forward-json wf.json]`.
  Training is deterministic: two runs on the same data snapshot gave the same sha256 (`a731ce5b…`).
