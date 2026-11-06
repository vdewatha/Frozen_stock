# Self-healing: the bot keeps itself running and explains itself (2026-11-06)

Branch `claude/youthful-pascal-a5e9d8`. Paper only. Nothing here can place, change or cancel an order, touch an
exit, flip the kill switch, change a risk setting or resume paper trading.

**Question:** which routine operational stalls still wait for a person, which of them have a fix the bot can apply
safely on its own, and how does the bot tell the owner what it changed and why?

## 1. Inventory of the known stalls

| Stall | How it is detected today | Safe automatic fix? | What ships here |
|---|---|---|---|
| Meta-label model file missing after a publish (shadow mode fails open: no BUY is scored; enforce mode skips every BUY) | Only indirectly: `meta_label_shadow.resolve_runtime` logs "model unavailable" per cycle; `meta_label_learning.retrain_due` reports "no meta-label artifact"; the nightly `meta-label-retrain` beat (20:15 ET) retrains. No health check, no alert. | **Yes, off hours.** The retrain is the fix and already skips the session itself. | `meta_label_artifact`: outside open-30min..close, enqueue `meta_label_retrain_job` at once. Cooldown 6 h, cap 2/day. During the session: `waiting` (reported, acts after the close). Retraining disabled: `manual`. |
| Intraday symbols going stale when the minutely import exceeds its time budget (2026-11-06: nine symbols, runs expired) | `operations_health` warns on a slow import (> 60 s) and a stale SPY bar (> 5 min) in session; the learning scoreboard's `news_fed` check names admitted symbols with no bars this session. Both are read-only. | **Yes.** `ingest_intraday` is an idempotent upsert with a bounded request and time budget; re-running it for the stale symbols only repairs the gaps the minutely import never reached. | `stale_intraday_symbols`: in session, 20 min after the open, any traded symbol (execution universe plus news-admitted names) without a minute bar in the last 15 min while SPY is current. Up to 10 symbols per run, oldest first, inside the existing `intraday_backfill_time_budget_seconds` (25 s). Cooldown 20 min, cap 12/day. SPY itself stale: `manual` (feed or provider outage; operations health already alerts, a backfill would not help). |
| Learning-queue jobs starved by long jobs (news discovery, attribution expired behind multi-hour research tasks) | The `learning_fast` lane (PR #7) is the fix; the scoreboard's `news_ran` check warns when the latest news run is > 75 min old. No alert. | **No.** If the fast lane itself is not consumed the worker is down; enqueueing more work changes nothing. | `learning_lane_starved`: 09:25-16:55 ET, latest `news_symbol_discovery` row older than 75 min (or none). Reported only, with one warning notification raised, refreshed while it lasts and resolved when a run lands. The self-healing job runs on `learning_fast` itself, so a long learning job cannot hold back the fixes for the stalls it causes. |
| Learning scope whose worker lease expired without a result | `recover_unpublished_strategy_learning_scopes_job` requeues them every 60 s. It is on the `learning` queue, so during a long job it is exactly the job that cannot run. | **Yes.** Same steps as the recovery job (mark `lease_expired`, publish `strategy_learning_scope_job` with the dispatch id); the scope's claim fences a duplicate delivery. | `expired_learning_lease`: `running` dispatches whose lease expired more than 5 min ago (five chances for the recovery job). Up to 16 per run; a scope that already ran 3 times is left for the next batch (`manual`). Cooldown 30 min, cap 20/day. |
| Post-publish heartbeat lag (watchdog pauses on stale monitor or reconciliation heartbeats right after a deploy) | Restart grace (`stock_restart_grace.py`), the transient-pause restore (`restore_after_transient_heartbeat_pause`), auto-revalidation (`auto_revalidation.py`, capped per day) and the boot-id lease release in `_acquire_job_lock`. `operations_health` warns after 15 session minutes paused. | **No.** Resume logic is out of bounds for self-healing by rule, and the existing automation already covers the explained cases. | `heartbeat_pause`: reported in the run when recovery is not tradable with a heartbeat reason. No action, no extra notification (the pause raises its own). |
| Monitoring checks flipping the kill switch (three were detached; overnight lag deferred off hours since 2026-11-06) | `stock_monitoring` breach persistence, `OFFHOURS_DEFERRABLE_KEYS`, auto-revalidation; the daily operations summary counts watchdog pauses. | **No.** The kill switch and monitoring are never touched. | `monitor_pauses`: today's watchdog or monitor pauses, with reasons, in the run. Reported only. |
| Replit publishes dropping tables from unapplied migrations | Deployment monitor (database and schedule checks). | No (project rule: no migrations). | Nothing new; the self-healing ledger is the existing `audit_logs` table. |

## 2. What shipped

* `app/services/self_healing.py`: one `Case` per stall (detector, optional fix, cooldown, daily cap). A run
  detects every case, acts where safe, and records:
  * one `self_healing` audit row per **action** (`remediated`, `failed`, `capped`) with what was found, what was
    done, the evidence and the budget; the message is a plain sentence ("Self-healing found 2 traded symbols
    have no minute bar ... ; it re-ran the intraday backfill for SOFI, AAPL within the 25-second repair budget;
    1 of 2 came back complete.");
  * one `run` row per run with every case's outcome (`clear`, `cooldown`, `waiting`, `manual` included);
  * one warning notification per case while the job cannot act (cap spent, or a reported-only finding that
    nothing else alerts on), refreshed while it lasts and resolved when the case clears.
  A failed fix counts toward the cooldown and the cap, so a broken provider is retried at the case's pace, not
  every 15 minutes. A detector that raises is reported as a finding and does not stop the other cases.
* `self_healing_job`: every 15 minutes, 07:00-20:45 New York time on weekdays, `learning_fast` queue, 10-minute
  expiry, under the shared `_run_job` wrapper (advisory lock, failure notification). Setting
  `self_healing_enabled` (default on).
* `GET /stock-paper/self-healing` (viewer): schedule, what is never touched, every case with its remedy, budget
  and latest outcome, the last run, the last 20 actions and the open warnings.
* Weekly report `changes` section (`weekly_report.change_narrative`): one plain-language sentence per
  `strategy_governance` change (pauses, retirements, status moves, experiment-engine parameter changes and
  reverts; daily "hold" notes are skipped), per `experiment_engine` step (run, experiment, adoption with the
  verdict so far, verdict with the result), for the latest `gate_cost` report (what was scored, which gates are
  costly or earning their keep) and per self-healing action. Pure functions over payloads, no language model.

## 3. Verification

`tests/test_self_healing.py` (19 tests): clear run, disabled, stale symbols backfilled with the right symbols and
budget, cooldown then daily cap then warning then next-day reset, feed outage reported not backfilled, session
gating, failed fix, meta-label retrain off hours only with the six-hour cooldown, present model and off filter,
retraining disabled, expired lease requeued like the recovery job, exhausted attempts left alone, broker failure,
starved fast lane warning raised and resolved, heartbeat and monitor pauses reported and untouched, broken
detector isolated, summary shape, route role, beat and queue wiring, job wrapper. `tests/test_weekly_report.py`
(+3): sentences over sample payloads for every row type, thin payloads, the report carrying the week's rows.

Full backend suite (without `tests/test_scalp_research.py`): see the PR description for the count.

## 4. Not measured yet, and what stays manual

* The stale-symbol repair runs beside the minutely import (both are idempotent upserts); it does not take the
  import's Redis lease. A targeted 25-second repair shares the provider's request allowance with that minute's
  import; the per-run symbol cap and the cooldown keep that bounded. Whether a repair restores entries for the
  symbol is visible on the next run (the symbol drops out of the finding) and on the scoreboard's `news_fed`.
* A learning scope that has already run three times is not requeued again; the minutely recovery job still
  will once the learning queue is free. If that loops, the research scope itself is broken.
* Heartbeat pauses, monitoring pauses and the kill switch stay with the existing automation and the owner.
