# Bounded autonomous paper soak

This runbook demonstrates the governed stock-paper loop without changing its
policy, fabricating forward observations, or enabling live trading. It is
repeatable and bounded. The JSON report is the durable review artifact; the
isolated PostgreSQL schema is retained for lineage review unless the operator
explicitly removes it.

## Contract

The soak must use:

- a disposable PostgreSQL schema on the configured PostgreSQL server;
- a fresh Redis database on a local, non-persistent Redis process;
- one API process;
- exactly one Celery beat process holding the Redis lease;
- one worker listening only to `intraday_market_data`;
- one general worker listening to `default,market_data,learning,paper_trading,risk`;
- the independent stock watchdog.

The existing risk limits, model policy, holdout policy, feed entitlement, and
cost assumptions are not changed. A cycle may end as `deferred`, `blocked`, or
`awaiting_forward_evidence`; those are valid fail-closed outcomes.

The minimum forward-evidence policy remains the application policy: 20 regular
sessions, 30 closed trades, and at least 90% decision coverage. A bounded run
must report incomplete forward evidence when that window has not elapsed. It
must never call the run profitable or live-trading ready.

## Unattended operation

The paper loop can run session after session without an operator. Four
controls make that possible; each keeps the existing bounds, audit chain and
fail-closed behaviour.

### Standing paper mandate

An admin records one standing mandate (`POST /api/stock/learning-cycles/paper-mandate`):
symbol universe, exposure caps (`max_allocated_notional`, `max_risk_per_trade`),
loss cap, allowed triggers, remaining-position policies, sessions per cycle,
total sessions and expiry. The mandate row is immutable and audited; a later
mandate supersedes it and `POST .../paper-mandate/{id}/revoke` withdraws it.

At handoff the scheduler mints the cycle's exact paper-run approval from the
mandate. The minted approval carries the cycle's own runtime bounds, never the
mandate's caps, and lists `paper_mandate:<id>` among its approving actors, so
the approval projection and the dispatch-time recheck are unchanged.

A handoff still defers, with the reason shown on the cycle and in the
mandate panel's "next auto-minted approval", when:

- no mandate exists, or it is pending, expired, revoked or out of sessions;
- the cycle trades a symbol outside the mandate universe;
- any exposure or loss bound exceeds the mandate cap;
- the cycle needs more sessions than the per-cycle or remaining total limit;
- the trigger or remaining-position policy is not covered.

Revoking or expiring a mandate withdraws approvals it minted for cycles that
have not yet started (status `mandate_withdrawn`), on the scheduled handoff and
on operator trial starts alike. Minted approvals hash exactly like operator
approvals, so the order-time approval fence accepts them unchanged. A warning alert fires
`PAPER_MANDATE_EXPIRY_WARNING_HOURS` (default 48) before expiry.

### Explained-halt auto-resume

`explained_halt_auto_resume_job` (every 5 minutes) resumes only halts with an
explained technical cause:

| Class | Cause |
| --- | --- |
| `stale_heartbeat` | Watchdog stale-heartbeat pause, or monitor worker/scheduler breach |
| `transient_broker` | Broker/transport or reconciliation breach that later reconciles clean |
| `feed_gap_repaired` | Data freshness/provenance breach after the feed is repaired |
| `risk_drawdown_reset` | Daily-loss/drawdown pause, only on a later New York session |

Every class also requires: the pause recorded the prior state as armed or
resumable with the kill switch off; cooldown has elapsed; a reconciliation
newer than the pause with a fresh heartbeat; verified accounting with no
residual; no in-flight orders (open positions are allowed); and clear fresh
monitoring. The automation records the same revalidation evidence a human
resume does, under the actor `stock_explained_halt_automation`.

These stay with a human and raise a critical alert: unexplained accounting
residuals, uncertain order outcomes, incomplete broker pagination, model
performance breaches, missing (not stale) heartbeats, manual pauses, and
legacy pauses without recorded prior state. Set `PAPER_AUTO_RESUME_ENABLED=false`
to disable the automation.

### Edge-decay re-admission

A strategy suspended for edge decay is quarantined for
`STRATEGY_READMISSION_QUARANTINE_HOURS` (default 72), then
`strategy_readmission_job` schedules a new governed learning cycle (fresh
holdout, new challenger, new trial). The strategy is re-admitted only when
that cycle is promoted through the normal gates. A trained but unpromoted
cycle counts as a failure and backs off by quarantine ×
`STRATEGY_READMISSION_BACKOFF_FACTOR`^attempts; after
`STRATEGY_READMISSION_MAX_ATTEMPTS` failures the strategy is retired. A cycle
blocked before training is not counted and retries shortly after the next
regular-session open, so an out-of-session block cannot repeat forever. Pending
re-admissions appear in the edge-decay projection.

### External alerts

Warning and critical notifications are queued in a delivery outbox and sent by
`alert_delivery_job`, outside the request transaction, with retry and a
dedupe window. A daily digest is queued after the close. Channels are enabled
only when configured:

- webhook: `ALERT_WEBHOOK_URL`, optional `ALERT_WEBHOOK_SIGNING_SECRET`;
- Telegram: `ALERT_TELEGRAM_BOT_TOKEN`, `ALERT_TELEGRAM_CHAT_ID`;
- email: `ALERT_EMAIL_SMTP_HOST`, `ALERT_EMAIL_SMTP_PORT`,
  `ALERT_EMAIL_SMTP_USERNAME`, `ALERT_EMAIL_SMTP_PASSWORD`, `ALERT_EMAIL_FROM`,
  `ALERT_EMAIL_TO`.

With no channel configured, rows are recorded as `unconfigured` and the
notification center says so. Delivery errors are stored redacted.

## Run

From `artifacts/api-server/backend`, with a PostgreSQL `DATABASE_URL` already
configured:

```bash
PYTHONPATH=. \
SOAK_DURATION_SECONDS=300 \
SOAK_REPORT=reports/paper-soak-$(date -u +%Y%m%dT%H%M%SZ).json \
bash scripts/run_autonomous_paper_soak.sh
```

The script creates a unique schema, starts ephemeral Redis, runs migrations,
then starts the API, watchdog, dedicated intraday worker, general worker, and
leased beat through `run_local_stack.sh`. It submits one scheduled challenger
task through Celery and waits for the bounded window. The task is allowed to
defer when the market session, feed entitlement, paper ledger, or scheduler
evidence is not valid.

The command exits non-zero when continued-paper readiness is blocked. Inspect
the JSON report and the retained schema before deciding whether that is an
expected fail-closed result or an operational defect.

`SOAK_INTERRUPTION_RECORDS` can record operator observations in the report
without mutating the database. Use one `NAME=STATUS:REASON` record per line so
reasons may contain spaces:

```bash
SOAK_INTERRUPTION_RECORDS=$'feed=deferred:feed was stale\nledger=blocked:paper ledger requires reconciliation\nworker=recovered:dedicated worker restarted\nbeat_lease=blocked:beat lease expired\nredis=recovered:coordination resumed' \
bash scripts/run_autonomous_paper_soak.sh
```

Only use an interruption record after the corresponding controlled action and
recovery state have been observed. The report stores identifiers from the
database; it does not accept IDs supplied by the operator. The five names are
fixed: `feed`, `ledger`, `worker`, `beat_lease`, and `redis`. A report with a
missing, duplicated, or invalid matrix entry is blocked from continued-paper
readiness.

## Controlled interruption matrix

Perform each interruption only on the isolated stack and only one at a time.
Stop the soak if a broker submission is uncertain; do not retry that order.

| Interruption | Controlled action | Expected result before retry |
| --- | --- | --- |
| Feed unavailable or stale | Stop intraday ingestion or revoke only the disposable feed process | New dataset/cycle admission is `deferred` or `blocked`; no new trial decision is accepted |
| Ledger residual | Mark the disposable paper account reconciliation as requiring review through the existing recovery path | New decisions stop; account remains halted and recovery evidence/audit rows exist |
| Worker termination | Send `TERM` to the general or intraday worker | Watchdog continues; cycle remains queued/deferred and no duplicate binding, trial, or order appears |
| Missed beat lease | Stop beat and wait at least 15 seconds | Scheduler health is blocked; safeguards/watchdog remain independent; a replacement beat must acquire the lease |
| Redis restart | Stop only the soak Redis process and restart it on the same disposable port | Coordination jobs fail closed; no uncoordinated market-data or reconciliation work runs; retry only after `PING` and leases recover |

After each interruption, allow the watchdog and monitoring cadence to persist
evidence, then restore the prerequisite and run the scheduled handoff and
promotion jobs through Celery. Do not call cycle lifecycle endpoints. Verify:

1. the prior active paper binding ID is unchanged unless a fully passing
   automatic paper decision is recorded;
2. one source cycle has at most one binding and one scheduled trial;
3. order/client IDs remain unique and uncertain orders are not retried;
4. each interruption record includes the bounded-soak recovery-event and audit
   identifier projection; these IDs are captured from the database and cannot
   be supplied as operator input;
5. scheduled learning pause/resume affects only scheduled admission/promotion,
   while monitoring, recovery, and manual learning remain available.

## Review checklist

The JSON report contains cycle, dataset, training job, model, binding, trial,
readiness-report, promotion-decision, monitoring, recovery, cycle-event, and
audit identifiers. Review `checks.all_cycle_stages_observed`,
`checks.duplicate_lineage`, `checks.recovery_prior_binding`, and
`readiness.blockers`.

`readiness.ready_for_continued_paper_operation` describes runtime safety only.
`forward_evidence.complete` is separate and must be true before any paper
promotion claim. `readiness.live_trading_ready` and
`readiness.profitability_claim` are always false by contract.

The live activation contract is documented separately in
`docs/live-safety-contract.md`. The soak cannot advance that state machine:
paper evidence, a paper binding, or a passing paper readiness report never
authorizes live orders.