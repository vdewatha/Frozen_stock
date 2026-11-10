# Local preview authentication

The API always requires four distinct, random role keys of at least 32
characters: `AUTH_VIEWER_KEY`, `AUTH_RESEARCHER_KEY`, `AUTH_OPERATOR_KEY`,
and `AUTH_ADMIN_KEY`. This applies to local preview and production.

Store these values only in Replit Secrets. Never put them in browser
environment variables, source files, logs, or documentation. Live trading
remains disabled by default.

## Stock-paper reservation concurrency tests

The stock-paper ledger tests use an isolated file-backed SQLite database. They
exercise idempotency and reservation accounting, but SQLite does not implement
PostgreSQL row-level `FOR UPDATE` locking. Do not treat those tests as proof of
multi-process locking behavior. A PostgreSQL locking test must run only against
an explicitly provisioned, isolated test schema/database; never point it at the
application or production database.
## CI test shards

GitHub Actions (`.github/workflows/ci.yml`) runs the backend suite as four
parallel shards plus two separate jobs for the Celery/IEX crash-recovery
integration tests, which start their own `redis-server` and take minutes each.
The required "Backend tests" check is a gate job that passes only when every
shard and crash-recovery job passed.

Shards are cut by recorded test durations (`pytest-split`) stored in
`tests/.test_durations`. A test missing from that file is assumed to take the
average recorded duration, so new tests need no action. Refreshing shard
balance (when one shard runs noticeably longer than the others) means running
the non-crash suite once with `--store-durations` and committing the file:

```bash
PYTHONPATH=. python -m pytest -q -p no:warnings --store-durations \
  --durations-path tests/.test_durations tests \
  --ignore=tests/test_celery_worker_crash.py \
  --ignore=tests/test_iex_lease_crash.py \
  --ignore=tests/test_job_lease_previous_boot.py
```
