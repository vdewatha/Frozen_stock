# Strategy evaluation audit — October 7, 2026

Scope: static review of the strategy backtester, parameter experiments, feature pipeline, research trainers v1–v3, development walk-forward evaluation, and offline simulator; focused local tests. No broker calls, execution enablement, database changes, historical result rewrites, or profitability certification.

## Implemented correction

`artifacts/api-server/backend/app/services/backtester.py:114–125` now includes starting capital before computing periodic returns and the equity high-water mark. Previously, the first marked value became the starting point: a fully invested account falling from $100,000 to $80,000 on its first evaluated bar reported **0% maximum drawdown**, even if it subsequently recovered only to $81,000. Both paths now report **20%**. Sharpe also includes the first loss. The returned dated equity curve, total return, annualization period, and fill decisions are unchanged; composite scores and rejection outcomes can change because risk statistics are corrected. Existing stored results are not recalculated.

This follows the initial-capital convention in [Quantopian Empyrical's max_drawdown implementation](https://github.com/quantopian/empyrical/blob/master/empyrical/stats.py), which inserts starting wealth before accumulating the peak.

Evidence: newly added `test_risk_metrics_include_initial_capital` cases failed twice before the fix (`0.0 != 0.2`), with three tests passing. After the correction, the focused suite passed **32 tests**. Cases cover a single losing bar, loss followed by partial recovery (including independently calculated Sharpe), flat equity, and no evaluated bars. The existing next-open causality test also passes.

## Remaining findings

1. **High priority — same-history selection is labeled promotion.** `app/services/experiments.py:79–104` evaluates current and candidate parameters on the same 420-bar history. Lines 45–55 mark a candidate `promoted` when its score improves by 0.03 and other gates pass. Lines 163 onward can apply the highest-scoring parameters when `apply_promotions=True` (default is false). There is no independent evaluation period in this function. The old parameter set is a comparator, not an out-of-sample control. [QuantConnect's parameter optimization documentation](https://www.quantconnect.com/docs/v2/writing-algorithms/optimization/parameters) explicitly warns about evaluating optimized parameters on the optimization period. Treat these records as exploratory selection; require a frozen future or untouched validation period before promotion. No promotion was invoked in this audit.

2. **Medium priority — strategy evaluation lacks passive baselines.** `backtester.py:132–150` reports raw returns and a heuristic score without cash or buy-and-hold comparisons. `experiments.py` compares only current versus proposed parameters. Consequently, a positive score cannot establish market outperformance. In contrast, `research_simulator.py:134–135` runs cash and buy-and-hold with the same simulation cost and capacity configuration. Porting a baseline requires an explicit matching capital/exposure convention, so it was not bundled into the arithmetic fix.

3. **Medium priority — Sortino has a nonstandard denominator.** `backtester.py:123–124` uses the sample standard deviation of negative returns alone, returning zero when there are fewer than two negative observations or their standard deviation is zero. It does not use downside root-mean-square deviation over all periods. [Empyrical's downside_risk implementation](https://github.com/quantopian/empyrical/blob/master/empyrical/stats.py) clips excess returns at zero, squares, averages over all periods, and takes a square root. This metric should not be compared directly with conventional Sortino figures until corrected and its zero-downside convention is documented.

4. **Medium priority — profitability statistics mix realized and unrealized evidence.** `backtester.py:111–131` includes open-position mark-to-market P&L in total return while win rate and profit factor cover only closed trades. With wins and no losses, profit factor is hardcoded to 3.0 rather than reported as undefined/unbounded. End-of-test exit costs are not charged to an open position. These conventions need explicit presentation before treating the outputs as realized profits; the composite score is a heuristic, not an estimated probability of profitability.

5. **Simulation limits remain.** The strategy backtester checks stops/take-profits at opens only (`backtester.py:66–81`); intrabar high/low crossings do not trigger them. It assumes a 252-observation year (`:121–124`) and fixed slippage without capacity modeling. The offline simulator caps opening fills using the minimum of previous and realized current bar volume (`research_simulator.py:83`); although it never increases capacity from future volume, this is still an ex-post liquidity assumption, not a fully observable opening-auction model. No assertion that either simulation reproduces actual fills is warranted.

## Causality and research controls that held up

- `backtester.py:57–65` supplies only prior bars to the strategy and fills on the next open. This agrees with [Backtrader's documented market-order timing](https://www.backtrader.com/docu/order-creation-execution/order-creation-execution/). The passing existing regression checks both signal history and fill price.
- `research_training_v2.py:54–62` constructs cost-adjusted next-open labels. Its split plan purges training labels ending at or after the evaluation boundary (`:79,88`). v3 independently purges development/calibration boundaries (`research_training_v3.py:17–26`) and fits calibration before final evaluation. This implements time-ordered evaluation with horizon-aware separation, consistent with the purpose of [scikit-learn TimeSeriesSplit and its gap parameter](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html).
- v2 and v3 report training-prevalence probability baselines. The development walk-forward evaluator also includes calibration-prevalence baselines (`research_walkforward.py:38–53`), excludes original calibration/final partitions, and explicitly reports `new_untouched_test=False` (`:90–94`). These are probability diagnostics, not executable portfolio returns.
- Passing tests include final-label perturbations that must not change selection/calibration, strictly purged boundaries, and later-label perturbations that must not change earlier fold predictions. These checks support the inspected paths, not a guarantee against every form of data leakage.
- Research manifests explicitly warn that repeated overlapping final tests are not independent evidence and retain trading-ineligible status. Caller source provenance remains unverified; the audit did not certify point-in-time universes, adjusted data vintages, or statistical significance across repeated experiments.

## Verification

Run from the repository root:

```sh
PYTHONPATH=artifacts/api-server/backend .venv/bin/python -m pytest artifacts/api-server/backend/tests/test_backtester.py artifacts/api-server/backend/tests/test_research_simulator.py artifacts/api-server/backend/tests/test_research_walkforward.py artifacts/api-server/backend/tests/test_research_training_v2.py artifacts/api-server/backend/tests/test_research_training_v3.py -q
```

Result: **32 passed, 165 warnings in 14.54s**. Warnings were scikit-learn `extmath.py:205` divide-by-zero, overflow, and invalid-value warnings during matrix multiplication in research tests. The trainer already documents prediction-time library warnings and checks returned probabilities for finiteness and bounds. These warnings remain a numerical investigation item; passing tests do not prove numerical health for every dataset. `git diff --check` passed.

All repository paths above, unless fully prefixed, are relative to `artifacts/api-server/backend/`. Remaining findings were documented rather than changing research selection policy or persisted metric contracts in this focused fix.
