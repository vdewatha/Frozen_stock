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

`SOAK_INTERRUPTION_ARGS` can record operator observations in the report
without mutating the database:

```bash
SOAK_INTERRUPTION_ARGS='--interruption feed=deferred:feed was stale \
  --interruption worker=recovered:dedicated worker restarted' \
bash scripts/run_autonomous_paper_soak.sh
```

Only use an interruption record after the corresponding controlled action and
recovery state have been observed. The report stores identifiers from the
database; it does not accept IDs supplied by the operator.

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
4. recovery and audit event IDs are present;
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