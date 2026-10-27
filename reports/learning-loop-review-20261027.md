# Learning-loop review — 2026-10-27

Branch `improve/learning-loop`. Scope: the bot's ability to find its own mistakes,
learn from them, and change behaviour only through the governed gates.
All paths below are relative to `artifacts/api-server/backend/`.

## 1. End-to-end map (as found)

There are two loops. Only the second one can affect paper execution.

### A. Legacy loop (quarantined, display only)
`paper_trading.run_paper_signal` → `PaperTrade` → `paper_trading._update_strategy_memory`
(`StrategyMemory` per strategy/symbol/regime) → `governance.evaluate_strategy_governance`
(pause / retire / promote) → `reactivation.review_strategy_reactivation` (human) →
`learning.propose_parameter_experiments` / `strategy_improvement_queue`.
Decision journal (`decision_journal.py`) and memory replay (`memory_replay.py`) score
scanner decisions against later prices.

The scheduled jobs for this loop are quarantined in `app/tasks/jobs.py`
(`nightly_strategy_learning_job`, `paper_trade_reconciliation_job`,
`memory_replay_gate_monitor_job`, `strategy_promotion_job`, `risk_monitor_job`), and
`update_strategy_memory_from_journal` and `candidate_activation.review_candidate_activation`
return `quarantined`. However `governance.evaluate_strategy_governance` and
`reactivation.review_strategy_reactivation` were still reachable from the API
(`app/api/routes.py:109`) and could change `Strategy.current_status`.

### B. Governed stock-paper loop (the real one)
1. **Retrain**: `scheduled_stock_challenger_retraining_job` (daily, `jobs.py`) →
   `stock_learning_cycle.create_learning_cycle` → training job with purged walk-forward
   and a one-use final holdout.
2. **Re-validation**: `_automatic_validation_gate`, `_model_comparison_gate`
   (challenger vs baseline and incumbent).
3. **Forward trial**: `scheduled_stock_paper_trial_handoff_job` →
   `stock_forward_trial.start_trial` / `observe_trial` (one decision per completed bar,
   `StockPaperTrialDecision`) / `execute_pending_decisions` (creates
   `StockPaperTrialLot` linked to `entry_decision_id`) → exits (stop/horizon) →
   `evaluate_trial` → `StockPaperTrialMetric`.
4. **Promotion**: `stock_promotion_readiness.evaluate_promotion_readiness` →
   `automate_paper_promotion` (about 15 gates; paper only, `live_authorized=False`).
5. **Demotion**: `stock_monitoring._demote_active_model` on persistent model drift or
   calibration breaches. Trial-level auto-pause applies only on the equity drawdown limit.
6. **Execution gate**: `stock_paper_ledger._validate_signal_buy` requires
   `Strategy.current_status == "paper_trading_active"` and
   `StockPaperStrategyEvidence.status == "approved"` and not expired. The evidence is
   refreshed by `stock_forward_trial._evidence_allows_trade` before each trade.

**What was missing from loop B:** nothing attributed *why* a closed lot lost money.
Nothing demoted a trial strategy whose realised forward edge decayed, short of the
2% equity drawdown stop. Retraining runs on a calendar schedule and is not triggered by
mistakes or decay.

## 2. Defects found (ranked)

| # | Severity | Where (original line) | Defect |
|---|----------|-----------------------|--------|
| 1 | High | `services/governance.py:151-159` | `evaluate_single_strategy` could set **any** status, including `paused`, `retired`, and `research`, straight to `paper_trading_active` when the memory thresholds were met. That skipped the holdout and forward-trial gates. It is reachable via the API even though the scheduled job is quarantined. |
| 2 | High | `services/reactivation.py:56-72,118` | Human reactivation `paused → paper_trading_active` checked only legacy `StrategyMemory`. It did not check for fresh forward-trial evidence. |
| 3 | High | `services/stock_forward_trial.py:143-180` | There was no edge-decay demotion. The pre-trade evidence check refreshed `expires_at` on every observation regardless of realised performance, so a strategy losing steadily but slowly kept trading until it hit the 2% equity drawdown. |
| 4 | Medium | `services/stock_forward_trial.py:178` | `_evidence_allows_trade` raised `AttributeError` (`evidence.verified_drawdown` on `None`) when the trial already had a `strategy_id` but no evidence row. |
| 5 | Medium | `services/stock_forward_trial.py:152` | The consecutive-loss streak used **gross** outcome. A trade that was profitable only before fees counted as a win. |
| 6 | Medium | `services/governance.py:119`, `reactivation.py:80,106`, `strategy_improvement.py:119` | Lifecycle decisions used only the **most recently updated** memory slice (one symbol/regime). Losses on other symbols were invisible: a form of survivorship in the evaluated symbols. |
| 7 | Medium | `services/governance.py:139-150` | A `retired` strategy that hit a pause condition was moved to `paused`, which is a reviewable state. That is effectively an un-retire. |
| 8 | Medium | `services/decision_journal.py:217`, `memory_replay.py:344,379` | Partial-horizon follow-through (fewer bars than the predicted horizon) was returned as `return` and scored as a realised outcome (good/bad approval, replay gate samples). |
| 9 | Low | `services/decision_journal.py:203-209`, `memory_replay.py:363-369` | Entries older than the loaded price window were scored from the first bar in the window, which is a different and later date than the decision. |
| 10 | Low | `services/model_tracking.py:121-161` | Predictions whose `prediction_date` has no exact bar (holiday, missing data) are never realised. They are re-checked every hour forever and silently drop out of calibration evidence (survivorship in calibration). **Not fixed**: needs an `unscorable` status column (migration). |
| 11 | Low | `services/paper_trading.py:622` (legacy) | `StrategyMemory.avg_drawdown` is the worst single-trade return, not a drawdown. It is legacy and quarantined, so this is documented only. |
| 12 | Info | `services/stock_forward_trial.py:1481` | The stop check hard-codes `0.98` instead of `lot.stop_fraction`. They are equal today (2%), but they will diverge if the policy changes. **Not fixed**: the paper execution path is owned by another agent. |
| 13 | Info | `services/stock_forward_trial.py:1864` | `evaluate_trial` counts wins on gross outcome while expectancy uses net. **Not changed**: changing it would alter persisted readiness metrics and report hashes. |

Race conditions in the jobs: the new demotion job and the forward-trial pre-trade check
can run at the same time. Both only move state toward paused/suspended (monotonic), so
interleaving cannot produce a looser state. The new job is also Redis-lease locked.
`scheduled_stock_paper_trial_handoff_job` and `scheduled_stock_paper_promotion_job`
rely only on Postgres advisory locks, which are separate per job name, so on SQLite
they are not serialised. Promotion takes `_lock_lifecycle_admission` plus
`SELECT … FOR UPDATE` on the binding state, which is adequate on Postgres. No change was made.

## 3. Changes

1. **Post-trade mistake classifier**: new `app/services/trade_mistakes.py`.
   - `classify_trade` (pure) assigns these labels: `oversized`, `cost_dominated`,
     `regime_mismatch`, `bad_entry`, `stop_slippage`, `premature_stop`,
     `gave_back_gains`, `signal_decay`, `overconfident_signal`, `unexplained_loss`, plus a
     primary mistake (by priority) and a lesson.
   - `trial_lot_trades` / `trial_mistake_report` link every fully closed lot back to its
     `entry_decision_id`. They use broker fills (net of known fees; unknown fees are
     never treated as zero) and MFE/MAE from 1-minute bars between entry and exit.
   - `aggregate_mistakes` / `strategy_mistake_report` aggregate per strategy: counts,
     share of losses, top mistake. Lessons are proposals only
     (`auto_apply: False`, `requires_fresh_holdout_and_forward_trial: True`).
   - Read-only routes: `GET /stock/forward-trials/learning/mistakes` and
     `GET /stock/forward-trials/{trial_id}/mistakes`. These fall back to the default
     (admin) role. Mapping them to `viewer` in `core/security.py` is optional.
2. **Rolling out-of-sample edge-decay demotion**: new `app/services/strategy_edge_decay.py`.
   - `assess_edge_decay` uses the last 20 closed trial lots (minimum 8) and per-lot net
     returns. It returns `decayed` when the one-sided 95% Student-t upper bound of the
     mean is below the baseline (0). Other states are `decaying`, `healthy`, and `insufficient`.
   - `suspend_strategy_for_decay` sets the strategy to `paused` and the evidence to
     `suspended`, then writes an audit log and a notification. It is idempotent and
     **one way**: there is no automatic re-enable. A new governed cycle (new holdout plus
     forward trial, which creates a new trial strategy) is the only route back.
   - Hooked into `stock_forward_trial._evidence_allows_trade`. This rejects with
     `strategy_paused`, `strategy_evidence_suspended`, `missing_strategy_evidence`, or
     `edge_decay_detected`, and fixes defects 3, 4 and 5.
   - The scheduled `strategy_edge_decay_job` (every 15 min, Redis-locked;
     `jobs.py`, `celery_app.py`) reviews every trial strategy. It is read-only and
     visible at `GET /stock/forward-trials/learning/edge-decay`.
   - `_lot_outcomes` now also returns `lot_id`, `symbol` and `entry_cost`. These keys are additive.
3. **Promotion gate tightening**: `automate_paper_promotion` has a new
   `strategy_edge` gate (`stock_learning_cycle._automatic_strategy_edge_gate`). A canary
   whose trial strategy is paused or suspended can never be promoted.
4. **Governance/reactivation tightening** (`governance.py`, `reactivation.py`):
   - Governance can only demote. When thresholds are met for a non-active strategy it
     now returns `promotion_review_required` and does not change the status (defect 1).
   - A retired strategy is never moved to another status by governance (defect 7).
   - `aggregate_strategy_memory` judges all symbol/regime slices: weighted rates and
     worst drawdown (defect 6). It is shared by reactivation.
   - Reactivation additionally requires approved, unexpired `StockPaperStrategyEvidence`.
     Suspended evidence blocks it (defect 2).
5. **Outcome timing**: partial horizons now return `return: None` plus an additive
   `partial_return`. Entries older than the price window return `missing_entry_date`
   (defects 8 and 9).

No DB migrations. API and schema changes are additive only. No broker calls, no order
paths changed, and no live or kill-switch settings touched.

## 4. Tests

New: `tests/test_learning_loop_feedback.py` (32 tests). It covers classifier labels and
aggregation, decay statistics and the rolling window, forward-trial pause and suspension
(one way), missing-evidence handling, net-loss streaks, the scheduled review and the
read-only projection, mistake attribution linked to decisions, the promotion edge gate,
the governance and reactivation tightening, journal timing, scheduling, and route ordering.
Against the original code the module fails at collection, for two reasons: the new
services do not exist yet, and with only the tracked-file changes reverted,
`governance.aggregate_strategy_memory` is missing. The behavioural tests (decay pause,
missing-evidence crash, net-loss streak, governance and reactivation, journal timing,
scheduling) each target a fixed defect. The pure classifier and statistics tests cover
new functionality.

Full backend suite: 1478 passed, 9 failed, 32 skipped before the change (on the
original code). After the change: 1510 passed, the same 9 failed, 32 skipped. The 9
pre-existing failures are in `test_scalp_research.py` (8) and `test_readiness_scheduler.py` (1),
outside this scope.

## 5. Remaining gaps

- **Retraining is calendar driven, not mistake or drift driven.**
  `stock_monitoring` demotes on drift but does not enqueue a challenger. The daily
  schedule bounds staleness. A drift-triggered cycle should reuse
  `create_learning_cycle(trigger="scheduled")` so that all gates still apply.
- **Mistake profiles do not yet feed the challenger design.** Lessons are surfaced
  through the API only. The next step is to pass the top mistake into the training
  request as a hypothesis tag, so it is evaluated only on the fresh holdout and forward trial.
- `validated_regimes` / `market_regime` are not yet written into trial decision lineage.
  `regime_mismatch` therefore only fires when an upstream lineage supplies them.
- The model-tracking unscorable-prediction survivorship (defect 10) needs a migration.
- `premature_stop` needs `post_exit_return`. The trial adapter does not compute it yet,
  because it requires bars after the exit up to the planned horizon.
- The legacy `StrategyMemory` metrics (defect 11) remain misleading on dashboards.
