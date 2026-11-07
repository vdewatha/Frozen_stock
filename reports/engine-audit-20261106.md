# Engine audit, 2026-11-06

Branch `claude/upbeat-diffie-43ece4`, the next item after `self-evolution-roadmap-20261106.md` once the bot adopts
changes on its own (`experiment_engine.py`: lessons -> replay -> `decide` -> adopt -> `review_changes` verdict).
**Question:** the engine's replay gains are in-sample claims, and each experiment tries several candidate values.
How far do its adopted changes fall short of what their replays promised, how much of the adoption record is luck,
and what should the engine do about it on its own?

**Goal used:** total profit after costs, as everywhere in the engine. The audit never adopts, reverts or touches a
strategy row; it sets one number the pre-registered rule already has room for (a margin a candidate's gain must
cover), and only when the engine's own track record says its expectations have run ahead of reality.

## What was built

`app/services/engine_audit.py`, paper only. Audit rows only, no migration.

1. **Expected vs realised per change** (`change_record`). Every `change_adopted` row carries the baseline and the
   adopted replay rows per window (`expected`) and the last 20 trades before the change (`recent_before`); every
   `change_verdict` row carries the forward record on the live trades after it (`after`) and the alternatives'
   shadow record (`shadow`, PR #28). For each change: expected gain = adopted minus baseline replay expectancy over
   the 2020+ window (falling back to 2015+ and all when a window is missing, and saying which); realised gain =
   after minus before over the live trades once they reach the verdict's `min_trades`, otherwise the adopted value
   against the value it replaced over the same shadow decisions (the `setup_shadow` record is scored on the same
   counterfactual trades, so this delta is the gate decision alone); shortfall = expected minus realised; ratio =
   realised over expected (only when the expected gain is positive); forward trade count = the live trades, or the
   shadow overlap when the shadow is the source; `judged` = a ratio exists and the forward trades reach
   `min_trades`. A change still watching gets its shadow record live from `setup_shadow.shadow_track_record`.
2. **Engine hit rate** (`hit_rate`). Across all `experiment` rows: experiments run, candidates tried (setups minus
   the baseline), eligible, adopted; verdict counts `working` / `not_helping` / `inconclusive` / `watching`;
   the median realised-over-expected ratio with a 95% percentile-bootstrap interval (2000 resamples, fixed seed)
   over the judged changes, reported from 5 of them; the mean shortfall in bps. Multiple testing: candidates tried
   per adoption, and the share of adoptions luck alone would produce at that count as a Bonferroni-style expected
   false-positive count (candidates tried x 0.05, over the adoptions). This is the stated choice over the deflated
   Sharpe helper in `backtest_statistics.py`: the replay rows store expectancies and their cluster-bootstrap CI,
   not per-period Sharpe ratios, so the DSR formula has no input here. The count is the honest, conservative
   version: it treats every candidate test as an independent chance at a false positive at 5%.
3. **Automatic tightening** (`next_factor`, `record_engine_audit`). When the median ratio over at least 5 judged
   changes is below 0.5, the audit writes an `engine_audit` row with a calibration factor in [0.5, 1.0]
   (max(0.5, median)). The engine reads the latest row's factor before its weekly run
   (`experiment_engine._calibration_factor`, 1.0 without a row or when the ledger is unreadable) and `decide`
   multiplies it into each candidate's expected gain: the deflated gain must cover the optimism's share of the
   engine's own revert margin, `factor x gain >= (1 - factor) x 10 bps`. At 0.5 a candidate must therefore beat
   the baseline by 10 bps per trade in every window; at 0.8 by 2.5 bps; at 1.0 by nothing more than before. The
   factor returns to 1.0 once the median recovers above 0.8; between 0.5 and 0.8 it follows the median while
   tightened and stays 1.0 if it was never tightened (hysteresis). The `RULE` text, the other four tests in
   `decide` (trades per window, total profit, drawdown, the largest minimum gain wins) and the ranking are
   unchanged; at factor 1.0 the comparison is exactly `expectancy not lower`. The factor is documented
   everywhere it acts: in `decide`'s result (`calibration`), the `experiment` payload, the `change_adopted`
   payload (`expected.calibration_factor`, `expected.required_gain_bps`), the weekly run summary
   (`calibration_factor`) and each failed candidate's reason.
4. **Verdicts carry the comparison.** `review_changes` adds `expected_window`, `expected_gain_bps`,
   `realised_gain_bps`, `realised_source`, `shortfall_bps`, `realised_over_expected` and
   `calibration_factor_at_adoption` to every verdict payload (additive; unreadable means absent).
5. **Surface.** `GET /stock-paper/engine-audit` (viewer): the table of changes, the hit rate, the factor in force
   and the one this audit would set, the last stored row, the thresholds, the rule and the method. The weekly
   report and the learning scoreboard each gain an `engine_audit` section with the same table, hit rate and
   factor. `engine_audit_job` (Saturday 14:30 New York time, `learning` queue, after the 14:07 weekly report and
   before the Sunday noon engine run) recomputes and stores the row.

## Why the margin, not a plain multiplier

Multiplying a candidate's gain by a factor below one never changes the sign of the gain, so on its own it could
not change a rule that asks for `expectancy not lower`. The rule needs a hurdle for the factor to bite. The hurdle
chosen is the engine's existing revert margin (10 bps per trade, the amount by which a change must fall short to be
reverted): the deflated gain must cover the share of that margin the engine has been missing, so an adoption that
then realises only `factor` of its promise is still not an immediate revert candidate. No new constant.

## Current result

Nothing has run on the host yet; the ledger holds no adoption and no verdict. The audit reports `waiting` with a
factor of 1.0, the engine runs under the unchanged rule, and the first real numbers come after the first verdicts
(20 live trades per change, or sooner on shadow evidence). The thresholds (5 judged changes, 0.5 / 0.8, floor 0.5)
are fixed in code, before any data.

## Tests

`tests/test_engine_audit.py` (12): expected and realised from sample payloads (live source, shadow source while
the live trades are too few, the hand-over to live, window fallback, exit templates without a shadow); the verdict
subset; the hit rate with verdict counts, median, bootstrap interval, mean shortfall and the multiple-testing
view; the bootstrap's reproducibility; the tightening threshold, floor, hysteresis and recovery as pure rules and
through stored rows; `decide` applying the factor without changing the rule; the weekly engine run reading a stored
factor, rejecting a 7 bps gain at 0.5, adopting a 11 bps gain and recording the factor; a real verdict carrying the
comparison; the route, weekly report and scoreboard sections; the job, queue, beat and role wiring. Full backend
suite: see the PR.

## Not done

* The luck share is a Bonferroni-style expected count, not a deflated Sharpe ratio; if the replay rows ever store
  per-period returns, `backtest_statistics.deflated_sharpe_ratio` can replace it in `multiple_testing`.
* Exit templates (`stop_width`, `time_stop`) have no shadow record, so their realised gain waits for the live
  trades.
* The factor acts on the expectancy test only; the total-profit test keeps its plain `not lower`.
