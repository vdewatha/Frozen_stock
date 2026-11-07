# Drift-triggered retraining: a model breach starts a governed challenger at once (2026-11-06)

Branch `claude/keen-darwin-4b92b8`. Paper only. Nothing here promotes, demotes, binds or trades: the drift
trigger only *creates* a learning cycle, and that cycle runs the unchanged gates (fresh holdout, challenger
training, validation, forward paper trial, about 15 promotion gates). Demotion stays with `stock_monitoring`.

**Question:** the learning-loop review of 2026-10-27 listed "retraining is calendar driven, not mistake or
drift driven" as the first remaining gap. `stock_monitoring` detects prediction-distribution drift,
input-feature drift and calibration or realised-performance degradation (`model.*` checks), and
`_demote_active_model` demotes the champion when one stays persistent, but nothing started a challenger. The
daily `scheduled_stock_challenger_retraining_job` (18:30 New York) was the only retraining path. What makes a
breach retrain the model at once, with every gate intact and without a retraining storm?

## 1. What shipped

* **Trigger `drift`** (`app/services/stock_learning_cycle.py`): `VALID_TRIGGERS` gains `drift`, and
  `AUTOMATED_TRIGGERS = {scheduled, drift}` replaces every `trigger == "scheduled"` branch: unknown
  prerequisite evidence defers a drift cycle instead of blocking it, training completion puts it in
  `awaiting_admission`, the handoff and automatic-promotion jobs pick it up, and admission and promotion accept
  it. `create_stock_training_job` accepts the trigger and gives it the same capacity and holdout deferrals as a
  scheduled challenger. The standing paper mandate's `scheduled` trigger covers drift cycles (`mandate_fit`), so
  no mandate needs re-minting; the operator route still creates only `manual` or `scheduled` cycles, so a drift
  cycle cannot be hand-made around the guard rails. The `trigger` column has no check constraint: no migration.
* **`app/services/drift_retraining.py`**: `evaluate_drift_trigger` reads the latest monitoring snapshot and the
  `model.*` breach rows; the trigger fires when a model breach is `persistent` (the monitor marks the second
  consecutive observation persistent, so "observed twice in a row" is the same condition) and belongs to that
  snapshot (within 5 minutes). Monitoring evidence older than 6 hours is not a reason to retrain. The cycle is
  created through `create_learning_cycle(trigger="drift")` with the launch universe, cutoff today, horizon 5,
  a seed derived from the snapshot id and breach keys, and a `lineage` carrying the monitoring snapshot id, the
  breach keys, the breach *episodes* (key plus the start of its consecutive run), the observed values and
  thresholds, and the attempt number. The lineage is part of the hashed immutable request and is visible in the
  cycle's `evidence.request`. One `drift_retrain` audit row per decision and an info notification per cycle.
* **Guard rails** (all derived from cycle rows and audit rows; no new table):
  * one drift cycle at a time: an open drift cycle means `waiting`. A cycle deferred at preflight or training
    without a model is treated as ended ("not evaluated": nothing retries those), a hard-blocked cycle after
    training as failed, and an open cycle older than 45 days as abandoned, so one stuck cycle cannot hold the
    slot forever;
  * cooldown `drift_retrain_cooldown_hours` (48) after a drift cycle ends, whatever the outcome, and after a
    creation that raised;
  * exponential back-off like `strategy_readmission.py`: cooldown × `drift_retrain_backoff_factor`
    (2) ^ (consecutive failures − 1), capped at 2^6; a promotion resets the count; a not-evaluated cycle neither
    counts nor resets;
  * a breach episode that a promoted drift cycle already answered never re-triggers; the breach must clear and
    come back (a new `first_observed_at`) to retrain again;
  * daily cap `drift_retrain_max_per_day` (1, New York day);
  * the operator pause of scheduled learning pauses drift retraining; `drift_retrain_enabled` (default on).
* **`drift_retrain_job`**: every 15 minutes on the `learning` queue (10-minute expiry) under the shared
  `_run_job` wrapper. The 18:30 calendar retrain stays as the fallback.
* **Status surface**: `GET /stock/learning-cycles/schedule-control` (viewer) gains an additive
  `drift_retraining` field: policy, the current trigger evidence, the creation window, the open and last
  drift cycle with its outcome, consecutive failures, cooldown, today's count, answered episodes and the last
  recorded decision.
* `tests/test_drift_retraining.py` (16): the trigger from breach states (observed once, twice, persistent,
  cleared, safety breach, older snapshot, stale evidence), the cycle with trigger `drift` and lineage through
  the real `create_learning_cycle` and the fake, the training-job and mandate validation, admission, handoff and
  promotion accepting drift cycles, every guard rail (open cycle, dead or abandoned cycles, cooldown and
  back-off, answered episode, daily cap, session window, disabled, paused, blocked creation), the route field,
  and the beat and queue wiring.

## 2. One deviation from the brief, and why

The brief asked for drift cycles to be created outside the regular session, after the close like the discovery
job. That would make the feature dead on arrival:

* `evaluate_cycle_prerequisites` returns `verified_feed = unknown` outside a regular session ("regular-session
  verified feed preflight is required"), so an automated cycle created after the close is `deferred` at
  `preflight` with no training job, and nothing retries a preflight deferral (the handoff job only looks at
  cycles with a training job). `test_scheduled_cycle_is_visible_as_deferred_and_does_not_claim_coverage` pins
  exactly this behaviour.
* `create_stock_training_job` marks a dataset whose cutoff precedes its capture date `binding_eligible=False`,
  so even a cycle retried the next morning would be admitted with a research-only snapshot and blocked at
  admission ("Dataset snapshot is not eligible for paper binding").

`strategy_readmission.py` and `parameter_application.py`, the two existing automatic cycle creators, both create
their cycles inside a session for this reason (readmission retries a not-evaluated cycle at the next open plus
15 minutes). The drift trigger therefore follows them: a cycle is created only inside a regular session, at
least 15 minutes after the open and more than 30 minutes before the close. Outside that window the trigger is
still evaluated every 15 minutes and recorded as `deferred` with the next window's opening time, and the first
tick inside the window creates the cycle. The window is two constants in the module (`OPEN_OFFSET`,
`CLOSE_MARGIN`); switching to after-close creation is a two-line change once a preflight retry exists.

## 3. What this means for the calendar fallback (follow-up, not changed here)

By the same two facts the 18:30 `scheduled_stock_challenger_retraining_job` creates cycles that stay
`deferred` at `preflight`: every cycle it creates after the close has `verified_feed = unknown`. The cycles are
visible as deferred and claim no coverage, so nothing unsafe happens, but the "daily retrain" has not been
training. Moving that beat entry to inside the session (for example 10:00 New York, after the feed settles) or
adding a preflight retry inside the session would make the fallback real. That is a one-line beat change plus
a check of the production cycle list, and belongs in its own change because it alters the existing schedule.

## 4. How to read it

* `GET /stock/learning-cycles/schedule-control` → `drift_retraining.trigger` says whether a model breach stands
  and which; `open_cycle` / `last_cycle` say what the trigger did; `last_decision` is the latest audit row
  (`scheduled`, `waiting`, `cooldown`, `capped`, `deferred`, `answered`, `blocked`).
* `audit_logs` where `event_type = 'drift_retrain'`: `schedule_cycle` rows carry the cycle id, breach keys,
  episodes and snapshot id; `evaluate` rows are written only when the decision or the breach set changes, so a
  48-hour cooldown is one row, not 192.
* A drift cycle looks like a scheduled one everywhere else (`trigger = 'drift'` on the cycle and its training
  job; `requested_by = 'drift_retraining'`).

## 5. Tests

`tests/test_drift_retraining.py`: 16 passed. Neighbouring suites (`test_stock_learning_cycle`,
`test_unattended_paper`, `test_celery_app`, `test_paper_operations`, `test_loss_feedback_loop`,
`test_stock_monitoring`, `test_kill_switch_triggers`): 174 passed, 16 skipped. Full backend suite
(ignoring `tests/test_scalp_research.py`): 2749 passed, 33 skipped, 0 failed.
