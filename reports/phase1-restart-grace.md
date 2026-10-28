# Phase 1: restart grace and the fix for Resume being undone

Branch: `phase1/restart-grace`. Paper only. There are no broker calls and nothing new can place orders.

## 1. How pauses happen today

**Independent watchdog** (`scripts/run_stock_watchdog.py` → `stock_recovery.run_stock_watchdog`, runs every 30 s)
- Checks `account.last_reconciled_at` and `state.last_monitor_heartbeat_at`. Each one can be missing, future-dated, or stale (older than `HEARTBEAT_TIMEOUT` = 10 min).
- If any issue is found and the account is not already halted, it calls `enter_stock_recovery`. The pause happens on the first observation; nothing needs to persist.

**Continuous monitor** (`scripts/run_stock_monitor_loop.py` → `stock_monitoring.run_stock_monitoring`, runs every 60 s)
- Runs the checks, and `_persist_breach` keeps a counter for each check key.
- "Persistent" means `consecutive_count >= 2`. The previous breach observation must be within `PERSISTENCE_WINDOW` (20 min).
- A persistent check with `action_scope == "safety"` calls `_pause_stock_path` → `enter_stock_recovery`. A persistent model check demotes the model instead.

**`enter_stock_recovery` does the following:**
- sets state to `cooldown` with `cooldown_until = now + 15 min`
- turns the kill switch on
- halts the account (`_halt`)
- writes a recovery event, a ledger event and an audit record

**State changes after a pause:**
- `cooldown`, then `revalidation_required`. That second step happens through reconciliation, but only for watchdog or transient-provider halts.
- To get back to `resumable`, an operator must call `POST /api/stock-paper/resume` (`resume_stock_paper_after_revalidation`). It requires:
  - the cooldown has expired
  - a reconciliation was done
  - the accounting gates pass
  - there are no orders in flight
  - `_fresh_monitoring_is_clear`
- On success it turns the kill switch off and marks the account reconciled.
- The automatic actor is explicitly refused at this step.

**Entry gates:**
- `readiness.py` accepts recovery status `resumable` or `not_required`.
- Autonomous reservations (`reserve_stock_paper_order` → `evaluate_launch_admission_prerequisites`) require the persisted status `armed`, plus fresh monitor and watchdog heartbeats.

## 2. Restart grace (implemented)

New module: `app/services/stock_restart_grace.py`.

**Boot marker.** `production-server.ts` creates `PAPER_BOOT_ID` and `PAPER_BOOT_AT` once per gateway process. It sets `PAPER_WORKERS_STARTED_AT` the first time workers start, and passes `PAPER_BUILD_ID` through. A worker that the supervisor respawns keeps the same marker, so a crash loop cannot reopen or extend the window. A process without the marker (local dev, tests, manual scripts) gets no grace.

**Opening the window.** The first watchdog or monitor evaluation for a boot records one `restart_grace` recovery event (plus an audit record), with status `opened` or `denied`. Later evaluations reuse that decision. Grace is denied when any of these is true:
- the recovery state was not `armed` or `resumable` (it was already paused)
- the previous generation has no watchdog heartbeat, or that heartbeat is older than `stock_restart_grace_max_downtime_seconds` (default 30 min) before the worker start. That counts as an outage, not a deploy.
- the marker is future-dated
- more than `stock_restart_grace_max_boots_per_hour` (default 3) windows were opened in the last hour (restart-storm guard)

The window runs from the worker start for `stock_restart_grace_seconds` (default 300).

**What grace defers:**
- Watchdog: only `... heartbeat is stale` where the heartbeat was last written before the worker start. The watchdog returns `warming_up`, records a `deferred` event, and does not pause. Missing or future-dated heartbeats still pause.
- Monitor: only persistent breaches that the restart explains:
  - `data.freshness_provenance`, when every failing symbol's `failure_class` is `stale_data` or `incomplete_data`, or is `entitlement` with a verification time from before the restart. `configuration`, `availability` and exceptions are never deferred.
  - `health.reconciliation`, only when the account is `reconciled`, `reconciliation_required` is false, there is no residual, and `last_reconciled_at` is from before the restart.
  - `health.worker_scheduler`, only when there are no `recent_unresolved_failures`.
- Every other breach pauses in the same evaluation, exactly as before. That includes drawdown and other risk checks, `health.broker` (halted account), reconciliation mismatch or residual, and fill divergence.

**New entries are blocked while warming up:**
- `recovery_status()` reports a derived `status: "warming_up"`. It also returns `persisted_status` and `restart_warmup`. Nothing new is persisted and the DB constraint is unchanged. Readiness therefore blocks entries.
- The launch-admission `recovery_state` gate fails with a "warming up" reason.

**Ending the window:**
- A clean monitor run (no restart-explainable key in breach) records `complete` and lifts the block early.
- If the window ends while findings are still stale, nothing is deferred any more. The persistence counters were never reset, so the next evaluation pauses through the unchanged code path.

## 3. Resume being undone by the monitor (fixed; coordinator priority)

**Production, 2026-10-28:**
- Resume at 15:19:41Z.
- Automatic pause at about 15:19:47Z on `health.broker, health.reconciliation`.

**Cause.** `_persist_breach` counted breach observations made while the account was halted. A halted account is a `health.broker` breach by definition. The first evaluation after resume (or one that read the halted account just before the resume committed) then became the second consecutive observation and paused again.

**Fix: a persistence floor in `stock_monitoring._persistence_floor`.**
- The floor is `max(last_revalidation_at, current worker start)`.
- A previous observation counts toward persistence only if it was made after the floor.
- The recovery row is re-read with `populate_existing()`, so a resume that commits during an evaluation is seen.
- After a resume, a breach needs two new observations, made after the resume, before it pauses. A genuine new breach still pauses after about 2 minutes, as it would from a clean start.

## 4. Automatic revalidation (deliberately not implemented)

I did not implement automatic revalidation for pauses caused purely by restart staleness. Reasons:

- Once grace exists, a restart-staleness pause can only happen when one of these is true:
  - staleness lasted past the grace window, so the restart no longer explains it
  - grace was denied: long outage, restart storm, already paused, or no marker
- Clearing either of those automatically would reverse the rule "behave exactly as today past grace".
- The codebase also has a tested invariant that the final resume is done by an operator. `resume_stock_paper_after_revalidation` refuses `AUTOMATIC_RECOVERY_ACTOR`, and `attempt_automatic_stock_recovery` stops at `awaiting_operator_revalidation`. Removing that would weaken a protection that applies to every pause reason.

## 5. Tests

New file: `tests/test_stock_restart_grace.py` (21 tests).
- Watchdog within grace → `warming_up`, no pause, kill switch off, readiness status `warming_up`, launch admission fails.
- Same boot past grace → paused, cooldown, kill switch on, account halted.
- Grace is not applied and the path pauses for each of these: no boot marker; long outage; already paused before boot; restart storm; future-dated heartbeat; halted/required reconciliation.
- Monitor stale data within grace → `restart_warmup`, no pause. Past grace → pause on `data.freshness_provenance`.
- Entitlement proof that aged across the restart → deferred. `configuration`, `availability` and unknown failure classes → pause.
- Drawdown during grace → pause on `risk.daily_loss`, while freshness is still deferred. Unexplained residual during grace → pause.
- A clean run completes warm-up early. A later stale finding in the same boot pauses.
- Regression for section 3: breaches while halted → pause; resume; an evaluation that started before the resume and the first one after it → no pause; then a genuine drawdown seen twice → pause.
- An observation from the previous generation plus one new observation is not counted as persistent.

Results:
- New file: 21 passed.
- Related suites (grace, stock_monitoring, stock_paper_ledger, stock_learning_cycle, paper_recovery, monitor_security, production_operations_certification, live_risk_recovery_certification): 157 passed, 16 skipped.
- Full backend suite: 1643 passed, 32 skipped, 206 subtests passed, 0 failed (6m51s).
- `tsc --noEmit` and an esbuild bundle of `production-server.ts` both succeed.

## 6. Deploy notes

- No migration. Events reuse `stock_paper_recovery_events` with `action = "restart_grace"`.
- Settings (environment variables, defaults shown):
  - `STOCK_RESTART_GRACE_ENABLED=true`
  - `STOCK_RESTART_GRACE_SECONDS=300` (allowed range 0–900; 0 disables grace)
  - `STOCK_RESTART_GRACE_MAX_DOWNTIME_SECONDS=1800`
  - `STOCK_RESTART_GRACE_MAX_BOOTS_PER_HOUR=3`
- `.replit` does not need to change. Setting `PAPER_BUILD_ID` per deploy is recommended for the audit trail, but grace does not require it.
- `/api/startup` now also reports `boot_id`, `boot_at` and `workers_started_at`.
- Grace only takes effect once the new `production-server.ts` is running, because the boot marker comes from the gateway.

## 7. Observation, not changed

- `readiness.py` treats the recovery status `resumable` as ready.
- Launch admission (`stock_learning_cycle.evaluate_launch_admission_prerequisites`) and the trial handoff accept only `armed`.
- Nothing ever moves the status from `resumable` back to `armed`.
- So after any operator resume, autonomous reservations may still fail launch admission. Please confirm against production before relying on Resume to restart autonomous trading.
