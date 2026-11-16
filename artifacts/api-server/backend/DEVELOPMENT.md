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
multi-process locking behavior. Locking behaviour is tested on PostgreSQL by
the tests in the next section.

## PostgreSQL concurrency tests

Production runs on PostgreSQL; the sharded suite runs on SQLite, which has no
row locks, no advisory locks and serializes every writer. Tests that need the
real engine carry the `postgres` marker and run in the CI job
`backend-postgres` ("Backend tests PostgreSQL") against `postgres:16` and
`redis:7` service containers. The required "Backend tests" gate needs that job.

What the marker covers:

- `tests/test_postgres_account_locks.py`: broker reconciliation holding the
  account row against the recovery watchdog, the automatic resume checks (the
  2026-11-10 deadlock: they must answer `RecoveryAccountBusy`, not queue) and
  `dispatch_reserved_order`.
- `tests/test_postgres_worker_races.py`: two workers claiming one learning
  dispatch, concurrent `write_audit_log` writers and the hash chain, concurrent
  intraday bar and no-trade upserts, and the `_run_job` advisory lock.
- The older dialect-gated tests: probe reservations, learning-cycle approval
  and handoff races, the job advisory connection, and three migrations.

Rules:

- A test that needs PostgreSQL gets `@pytest.mark.postgres` (or
  `pytestmark = pytest.mark.postgres`; for one parameter,
  `pytest.param("postgresql", marks=pytest.mark.postgres)`). On SQLite it is
  skipped. Without the marker it never runs in CI.
- The job runs `-m postgres --require-postgres`. With that flag a skipped
  `postgres` test fails the job, and so does a selection that contains none.
- A known, unfixed defect is recorded as
  `@pytest.mark.xfail(strict=True, raises=KnownDeadlock, reason=...)`. It still
  runs every time, and the job fails the day it starts passing, so the marker
  is removed with the fix.
- Use the `postgres_schema` fixture (`tests/conftest.py`): it creates a
  throwaway schema with every table, points `search_path` at it and drops it
  afterwards. Never point these tests at the application or production
  database. Advisory locks are server-wide, so use a unique job name per test.
- Make races deterministic: hold one side inside its work with an event, wait
  until the server reports the other side blocked (`wait_until_blocked` in
  `tests/postgres_support.py`), then release. Do not rely on sleeps.

Run them locally with the validation stack (PostgreSQL 16 and Redis 7 on an
internal network, no host ports, repository mounted read-only). The `tests`
image is built on `frozen-stock-paper:local`; build that first as described in
`deploy/paper/README.md`. From the repository root:

```bash
docker compose -f deploy/paper/compose.test.yaml build tests
docker compose -f deploy/paper/compose.test.yaml run --rm tests \
  python -m pytest -q -p no:cacheprovider -rsx -m postgres --require-postgres tests
docker compose -f deploy/paper/compose.test.yaml down -v
```

Or against any disposable PostgreSQL and Redis you already have:

```bash
DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/paper_tests \
REDIS_URL=redis://localhost:6379/0 PYTHONPATH=. \
  python -m pytest -q -p no:warnings -rsx -m postgres --require-postgres tests
```

Each test creates about a hundred tables. A PostgreSQL that syncs every write
to a slow disk (Docker Desktop on macOS) spends most of a minute per test
there; a disposable server started with `-c fsync=off` takes seconds.

## CI test shards

GitHub Actions (`.github/workflows/ci.yml`) runs the backend suite as four
parallel shards plus two separate jobs for the Celery/IEX crash-recovery
integration tests, which start their own `redis-server` and take minutes each,
and the PostgreSQL job described above. The required "Backend tests" check is
a gate job that passes only when every shard, both crash-recovery jobs and the
PostgreSQL job passed.

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
