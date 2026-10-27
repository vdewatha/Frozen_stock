# Backtest realism and evaluation honesty — October 27, 2026

Scope: `app/services/backtester.py`, `experiments.py`, `research_simulator.py`, `forward_evaluation.py`, a new pure helper `backtest_statistics.py`, and additive optional fields on `BacktestResponse`. There were no broker calls, no orders, no reads of `.env`, and no database writes. Stored historical results were not recalculated. Every promotion and rejection gate was either left as it was or made stricter.

Paths are relative to `artifacts/api-server/backend/` unless prefixed.

## Status of the October 7 audit findings

| # | Finding | Status before this work | Status now |
|---|---|---|---|
| 1 | Same-history selection labelled as promotion (`experiments.py`) | Partly fixed already: promotion needed an untouched 20% validation tail when `apply_promotions=True`, and research fan-out could never promote. | Fixed further. The trial count and deflated Sharpe ratio are reported for in-sample and validation. Promotion now also needs a validation DSR ≥ 0.95 across the candidates that were validated. |
| 2 | No passive baselines in strategy evaluation | Open | Fixed. Every backtest reports cash, same-symbol buy-and-hold, exposure-matched buy-and-hold and SPY buy-and-hold, with excess returns and beat flags. Two new rejection gates use them. |
| 3 | Non-standard Sortino denominator | Already fixed (`downside_deviation`, RMS over all periods) | Unchanged |
| 4 | Realized and unrealized evidence mixed; profit factor hardcoded to 3.0; no exit cost at end of test | Partly fixed: an open position was realized in the ledger at end of history, but its exit cost never reached the equity curve | Fixed: liquidation costs now reach the final equity point and `total_return`. `profit_factor_capped` marks the 3.0 sentinel. Every trade has an `exit_reason` and `exit_cost_bps`. |
| 5 | Stops and targets checked only at the open; flat slippage; no capacity model | Open | Fixed for the strategy backtester: intrabar exits, a volatility- and liquidity-scaled cost model, and a participation cap. The 252-period year is unchanged. |

## What changed

### 1. Intrabar stop-loss and take-profit (`backtester.py`)
- Stop and target levels are fixed at entry, relative to the entry price after costs (the same reference as before).
- At each bar's open:
  - An open at or below the stop exits at the open (`stop_loss_gap`), never at the better stop price.
  - An open at or above the target exits at the open (`take_profit_gap`).
  - Otherwise, the max-holding rule exits at the open.
- After the open, including on the entry bar:
  - `low <= stop` exits at the stop (`stop_loss_intrabar`).
  - Otherwise, `high >= target` exits at the target (`take_profit_intrabar`).
  - If one bar touches both levels, the stop is assumed to have come first.
- After an intrabar exit there is no re-entry on that bar, because its open has already passed.
- When high and low are missing, the open–close envelope is used. `exit_model` records which range source applied.

### 2. Passive baselines (`backtester.py`)
- `baselines.cash`, `baselines.buy_and_hold`, `baselines.exposure_matched_buy_and_hold` and `baselines.spy_buy_and_hold` cover the same evaluation window and starting cash.
  - Buy-and-hold enters at the first evaluated open and is liquidated at the final close.
  - It pays the same fees and cost model as the strategy, including impact, but is not volume-capped.
  - The exposure-matched baseline holds a static `average_exposure` fraction in the stock and the rest in cash. The strategy's risk-based sizing typically keeps it about 30% invested, so this is the fair comparator.
- SPY is used when `benchmark_prices` is passed, or when the symbol is SPY. Otherwise it reports `status: unavailable`. `run_strategy_experiments` loads trusted SPY history when it is available.
- `excess_return.vs_*` and `beats_baselines.*` are reported.
- **New rejection reasons (tightening):** the strategy does not beat cash after costs, or it does not beat an exposure-matched buy-and-hold of the same symbol.

### 3. Cost realism (`backtester.py`)
- `BacktestConfig.cost_model` defaults to `"volatility_scaled"`.
  - Adverse bps per side = `slippage_bps` floor + `0.05 × σ_daily` + `1.0 × σ_daily × √(qty / ADV20)`.
  - σ and ADV20 come only from bars before the execution bar.
- Entries are capped at `max_participation = 1%` of the prior 20-bar average volume. `cost_model.participation_capped_entries` counts how often the cap applied.
- `cost_model="flat"` reproduces the legacy fixed-bps behaviour.

### 4. Multiple-testing honesty (`backtest_statistics.py`, `experiments.py`, `research_simulator.py`)
- The new pure helpers are:
  - probabilistic Sharpe ratio (Bailey & López de Prado 2012);
  - expected maximum Sharpe under the null for N trials;
  - deflated Sharpe ratio (2014), which uses the variance of the trial Sharpes, plus skew and kurtosis;
  - a moving-block bootstrap.
- `run_strategy_experiments` runs every candidate before persisting anything, then records `paper_result_summary.multiple_testing`:
  - `in_sample`: N = current + candidates;
  - `validation`: N = validated candidates;
  - the minimum DSR.
- `_decision` will not return `promoted` unless the validation DSR is at least 0.95. A missing DSR fails closed and returns `needs_more_data`.
- `simulate_research_matrix` adds `multiple_testing`: the trial count, the best case by return, that case's DSR and bootstrap CI, and how many cases beat buy-and-hold. `simulate()` adds `excess_return` and `beats_benchmarks` against its existing cash and buy-and-hold benchmarks.
- A single backtest reports `statistics.probabilistic_sharpe` with `trials=1`, and says that callers must deflate for their own trial count.

### 5. Statistical confidence
- Each backtest reports `statistics.bootstrap`: a moving-block bootstrap with block length ≈ n^(1/3), 1,000 samples and a fixed seed, giving 95% CIs for mean daily return and annualized Sharpe.
- Below 20 observations the status is `insufficient_observations`.
- **New rejection reasons:** too few observations for a CI, or a mean-return CI that does not exclude zero.
- `forward_evaluation.py` adds a roundtrip P&L bootstrap CI to each strategy summary and a `statistical_confidence` section. These are descriptive only: the pre-registered frozen checks are unchanged.

### API compatibility
- No existing field was removed or renamed.
- `BacktestResponse` gains optional fields: `exit_model`, `cost_model`, `average_exposure`, `profit_factor_capped`, `baselines`, `excess_return`, `beats_baselines` and `statistics`. Before this change, the Pydantic response model would have dropped them from `/backtests`.
- Trade dicts gain `exit_reason` and `exit_cost_bps`.

## How metrics move on fixtures

### Existing unit-test fixtures
These all pass:
- the next-open causality test (entry still 100.05);
- the initial-capital drawdown and Sharpe cases;
- the end-of-history realization test.

Two tests needed explicit config so they still test what they were written for:
- **Zero-cost arithmetic tests** now pass `cost_model="flat"`. With the default model, the volatility component charged about 20 bps when liquidating after a 100→80 move, and drawdown became 0.202 instead of 0.2.
- **End-of-history test** now passes `take_profit_pct=0.5`. Its final 100→110 bar crosses the default 6% target intrabar, so the trade honestly exits as `take_profit_intrabar`.

One experiment test was changed because the gate is stricter: `test_promotion_requires_validation_improvement` now supplies a passing validation DSR.

### Deterministic random-walk fixtures
Fixtures: 420 bars, daily σ≈1.5%, seeds 1–3. Defaults throughout. Generated by a scratch script that was not committed.

| Strategy / seed | Trades before→after | Total return before→after | Sharpe before→after | Max DD before→after | Same-symbol B&H | Excess vs exposure-matched B&H | Sharpe 95% CI |
|---|---|---|---|---|---|---|---|
| MA crossover / 1 | 8→17 | −4.62%→−5.99% | −0.85→−1.10 | 5.4%→8.2% | −28.9% | −4.3% | [−2.90, 0.83] |
| MA crossover / 2 | 18→26 | −9.74%→−10.19% | −1.31→−1.31 | 10.2%→10.7% | −16.9% | −7.6% | [−2.55, 0.16] |
| MA crossover / 3 | 22→34 | **+8.28%→−0.03%** | 0.93→0.03 | 5.6%→10.1% | +66.7% | −11.4% | [−1.87, 1.79] |
| RSI mean reversion / 1 | 11→15 | −6.20%→−9.23% | −1.01→−1.48 | 10.7%→12.8% | −28.9% | −5.6% | [−2.73, −0.19] |
| RSI mean reversion / 2 | 9→10 | −2.26%→−0.29% | −0.40→−0.04 | 7.0%→5.7% | −16.9% | +0.9% | [−1.64, 1.68] |
| RSI mean reversion / 3 | 8→10 | +0.26%→+1.36% | 0.07→0.26 | 3.7%→3.4% | +66.7% | −3.1% | [−1.41, 1.77] |

How to read the table:
- **Trade counts rise.** Intrabar stops and targets close positions sooner, which frees capital for more entries. Most exits are now `stop_loss_intrabar`.
- **The one apparently profitable configuration (MA/3, +8.3%) was an artefact.** With intrabar exits and flat costs it falls to +3.5%; with scaled costs it falls to −0.03%. Over the same window, buy-and-hold made +66.7%.
- **Path dependence shows up.** Changing exit or cost timing changes later entries, so results can move in either direction (RSI/2 and RSI/3 improved). Higher costs also shift the stop and target levels, so MA/1 loses less under scaled costs (−6.0%) than under intrabar exits with flat costs (−9.0%).
- **Costs.** Average exit cost under the scaled model was 13–16 bps per side, against a flat 5. No entries hit the participation cap: fixture volume was 0.2–2M shares.
- **No configuration has a Sharpe CI that excludes zero on the positive side.** All six were rejected before and after. They now carry the baseline and bootstrap reasons as well.

### Expected operational effect
`trade_candidates.py` and `candidate_evidence.py` call `run_backtest` with default settings. Expect noticeably more `needs_more_evidence` candidates, because the new rejection reasons appear as blockers. That is the intended outcome of the stricter gate. Those files were not edited.

## Tests
- New tests: 13 in `test_backtester.py`, 6 in `test_backtest_statistics.py`, 3 in `test_experiments.py`, 2 in `test_research_simulator.py`, 1 in `test_backtest_response_schema.py`, and 1 extended forward-evaluation test.
- Each one was checked to fail against the pre-change implementation, either on behaviour or on a missing field.
- Focused suites pass: backtester, statistics, experiments, research simulator and matrix, execution stress, forward evaluation, experiment and launcher, and the schema test.
- Full-suite results are in the final agent summary.

## Remaining limitations
- **Fill-order and bar-path assumptions.** Daily bars cannot show the order of events within a bar. Stop-first is conservative for stops but pessimistic for targets. Target fills assume a resting limit at the level with the same adverse cost.
- **Cost model inputs are assumptions, not measurements.** The volatility fraction (0.05) and impact coefficient (1.0) are not calibrated to this account's fills. Spread is not estimated from quotes. Exits are not volume-capped; only entries are.
- **Buy-and-hold baselines are uncapped.** In illiquid names they may be unachievable at full size.
- **The exposure-matched baseline is a static mix.** It is not a volatility-matched or beta-matched benchmark.
- **DSR is approximate.** It uses only the trials this code ran: earlier research sessions and other agents' searches are not counted, so the true N is larger and DSR is optimistic. The trial-Sharpe variance is noisy with only 3–4 trials.
- **The bootstrap is descriptive.** The block length is a heuristic. Overlapping or autocorrelated returns can still understate uncertainty.
- **Annualization still assumes 252 periods.**
- **The research simulator keeps flat spread and slippage**, with its existing participation cap. Volatility scaling was not ported to its Decimal ledger.
- **`paper_cost_sensitivity.py` and `execution_stress.py` were not changed.** They remain illustrative, fixed-bps scenarios.
- **The frozen checks in `forward_evaluation.py` were left unchanged on purpose**, to preserve pre-registration. The new CI is reported, but it does not gate.
- **The validation tail in `experiments.py` is about 84 bars.** Combined with the 30-trade and DSR gates, promotions will now be very rare. That is accurate, not a defect.
