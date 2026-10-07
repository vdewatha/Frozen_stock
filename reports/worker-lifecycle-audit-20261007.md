# Worker and paper execution lifecycle audit — October 7, 2026

Scope: source inspection and isolated tests. No production worker inspection,
deployment, broker request, order placement, or kill-switch change was performed.
Existing runtime evidence in `research-worker-audit-20261007.md` is historical;
this audit does not establish current worker health.

## Comparison with current open-source patterns

- [Freqtrade multiple-instance guidance](https://docs.freqtrade.io/en/stable/advanced-setup/)
  separates independent bots' databases and ports. This application instead
  intentionally shares one paper ledger among role-specific workers, so its
  account locks and durable order identities are essential. Replicated learning
  workers are intentional capacity, not inherently duplicate bot instances.
- [Celery task guidance](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
  ties late acknowledgement and worker-loss redelivery to idempotent work.
  Learning scopes and intraday ingestion opt into redelivery. Execution does
  not gain blanket automatic retries from this change.

## Findings

1. **Fixed: invisible Redis lease cleanup failures and incomplete client
   lifecycle.** `_run_job` silently swallowed Redis release errors, hiding
   expiry or lost ownership. `_acquire_job_lock` also left its client open on
   contention and acquisition failure, and successful jobs did not explicitly
   close the lease client. Cleanup now closes clients, logs job name and error
   type without exception details, and preserves both successful results and
   original work failures. Redis's token-checked release is retained; no raw
   key deletion or retry of completed work was added. The log is diagnostic,
   not a fencing mechanism or a durable alert.

2. **Existing protection: duplicate execution.** `deploy/paper/compose.yaml`
   assigns distinct worker names and queues, uses concurrency one for execution
   and time-sensitive collectors, and starts beat through
   `scripts/run_beat_with_lease.py`. `_run_job` holds PostgreSQL advisory locks
   on dedicated connections across work-session commits. Selected cadence jobs
   also use Redis leases; intraday/IEX hard limits precede their lease expiry.
   SQLite does not provide the PostgreSQL lock layer. Other Redis-locked jobs
   have a finite 15-minute lease without renewal or a matching hard limit, so
   the lease alone is not sufficient proof against overlap in those deployments.

3. **Remaining research durability gap.** `strategy_learning_batch_job`
   publishes individual scopes without a durable outbox/dispatch record and
   catches publication failures in its return payload. A crash midway through
   fan-out can omit remaining scopes until a later scheduled batch; failed
   publication does not create the per-scope incident consumed by the retry
   job. Conversely, advisory locks only exclude concurrent scopes: sequential
   duplicate deliveries can rerun experiments. Do not describe this path as
   exactly-once. A durable batch/scope identity and recoverable publishing state
   would be a separate, larger improvement. `StockTrainingJob` already has a
   stronger durable claim, heartbeat, and recovery design to follow.

4. **Paper execution safety retained.** `stock_paper_ledger.py` derives stable
   client IDs from idempotency keys, serializes reservation with account locks,
   rejects conflicting key reuse, and commits `submitting` before the broker
   call. An ambiguous response becomes `unknown` and halts the account for
   reconciliation rather than blindly repeating a POST. Reservation/dispatch
   checks cover kill switch, account reconciliation, approval, market freshness,
   and exposure as applicable. Existing explicitly governed recovery-flatten
   exceptions were not changed. Legacy simulator jobs remain quarantined;
   learning scopes continue with `apply_promotions=False`.

5. **Remaining scheduler review item.** Beat's initial lease is acquired before
   optional startup publishing, and the child-exit restart branch spawns a new
   child without first confirming lease ownership. Long startup/restart delays
   warrant a separate leadership-loss fault test. The job and account locks
   remain necessary downstream protections; the wrapper alone is not a proof
   of exclusive scheduling under every failure.

## Validation

New tests cover contention, connection/acquisition errors, successful work,
failed work, expired/lost lease ownership, close failures, secret-safe logs,
and database session closure. All use mocks or isolated databases and never
contact a broker.

Final isolated run: **199 tests and 28 subtests passed**, with two dependency
warnings. Files: `test_job_redis_cleanup.py`, `test_job_failure_transactions.py`,
`test_celery_app.py`, `test_paper_execution.py`, `test_live_safety_contract.py`,
`test_stock_paper_ledger.py`, `test_paper_transport_recovery.py`, and
`test_stock_forward_trial.py`.

Run from `/tmp` to prevent loading the workspace `.env`, using the repository's
`.local/paper-runtime/bin/python -m pytest -q --tb=short` with absolute test
paths, `PYTHONPATH` pointing to `artifacts/api-server/backend`,
`DATABASE_URL=sqlite://`, `REDIS_URL=redis://127.0.0.1:1/15`, and
`ALLOW_LIVE_TRADING=false`. Earlier runs from the workspace inherited paper
account/feed configuration and failed fixture assumptions (11 forward-trial
failures also reproduced with the original worker module). The clean-directory
run resolved those failures without modifying execution gates or test fixtures.

This is not a full backend, PostgreSQL concurrency, or process-death test run.
Existing concurrent changes to the backtester and its audit were left untouched.
