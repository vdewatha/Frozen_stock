# Operational hardening runbook

## Required checks

Run the deployment probe after migrations and before enabling unattended paper
promotion:

```bash
PYTHONPATH=. python3.11 scripts/verify_operational_hardening.py
```

The probe checks PostgreSQL advisory-lock support, the Redis scheduler lease,
worker lease expiry, the tamper-evident audit chain, backup tooling, and the
live-trading guard. A `breach` fails the command. An `unknown` requires
operator review and must not be treated as healthy.

## Incident procedure

1. Stop new paper entries with the paper kill switch.
2. Do not retry uncertain broker submissions.
3. Preserve the deployment/configuration digest and audit-chain report.
4. Reconcile Alpaca account, positions, fills, and in-flight orders.
5. Run the backup/restore probe against a disposable restore target.
6. If model state is implicated, cancel/flatten through the recovery endpoint
   and roll back to the recorded last-known-good binding.
7. Revalidate monitoring, broker, feed, worker, and scheduler evidence.
8. Wait for the recovery cooldown, then resume only with operator approval.

## Backup and restore proof

Set `DATABASE_URL` to the source database and provide a disposable
`RESTORE_DATABASE_URL` with `RESTORE_TARGET_DISPOSABLE=true`:

```bash
RESTORE_TARGET_DISPOSABLE=true \
  python3.11 scripts/verify_postgres_backup_restore.py
```

The script does not print either database URL. The restore target must be
disposable because `pg_restore --clean` replaces objects in that database.

## Chaos scenarios

- Kill the Celery worker: the independent watchdog must continue to heartbeat
  and pause the paper path when monitoring/reconciliation evidence becomes
  stale.
- Kill the scheduler: the Redis lease expires; a replacement beat may acquire
  it, while a second live scheduler is rejected.
- Drop broker connectivity during cancellation or submission: the account
  remains halted and the order remains pending/unknown until reconciliation.
- Restart the API and worker: restart reconciliation must import broker truth
  before any resume decision.
- Corrupt an audit row in a disposable database: the audit-chain check must
  report `breach`; production audit rows are append-only at the database layer.