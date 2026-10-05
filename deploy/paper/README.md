# Persistent paper-learning stack

This is a private, paper-only deployment, not proof of trading profitability.
It runs the existing governed learning loop; it does not create approvals,
qualify a broker, clear a halt, or authorize live orders.

## Start

Run from the repository root on a Docker host with Compose v2.24 or newer:

```bash
python3 artifacts/api-server/backend/scripts/prepare_paper_deployment.py --output deploy/paper/paper.env
python3 deploy/paper/prepare_web_access.py
docker compose --env-file deploy/paper/paper.env -f deploy/paper/compose.yaml up -d --build
```

The initializer refuses to overwrite an existing configuration. The file is
private (0600), ignored by Git, and contains randomly generated role keys.
It intentionally contains no broker credentials. Do not publish it or put
these secrets in browser configuration. Do not run `compose config` without
`--quiet` in shared logs: its expanded output contains secrets.

An optional repository-root `.env` is also loaded, overriding `paper.env` values.
It is Git-ignored and must have mode 0600. Container networking and live safety
are fixed in Compose: its Redis URL overrides host `localhost`, and live trading
and live broker credentials remain disabled. Rotate any credentials shared in
chat before longer-term operation. A `localhost:1106` AI proxy from another
hosting environment is not a working AI endpoint inside these containers;
the dummy integration key does not provide an AI service.

Compose explicitly selects `alpaca_paper` for all runtime services. Initialization
is still an explicit admin action, not a startup side effect. The running local
account uses `alpaca-activities-v2`; reconciliation alone does not certify costs,
performance, broker qualification, or permission to place orders.

Portfolio > Ledger includes a provisional change since initialization: closing
observed equity minus opening observed equity minus net external cash funding.
Reported fees are already included in equity and are not deducted twice. This
is not daily P/L, an inception return, or a qualification metric. Unknown cash
journals, unmatched snapshot boundaries, cash/inventory residuals, and journal
digest mismatches withhold the calculation. `smoke-performance.mjs` checks the
deployed projection and an invalid-evidence fixture without broker writes.

For this Mac's isolated validation VM, prepend
`--context colima-frozen-stock` after `docker`. Its VM can be started with
`colima start --profile frozen-stock --activate=false`.

### macOS Login Startup

For the existing `frozen-stock` Colima profile only:

```bash
.local/paper-runtime/bin/python deploy/paper/macos_host_service.py install
```

This installs two user LaunchAgents: `com.frozenstock.paper.startup` starts the
existing VM at login, without switching Docker context or rewriting its config;
`com.frozenstock.paper.ac-awake` runs `caffeinate -s`, preventing system sleep
while connected to AC power. The assertion lasts while this agent is enabled,
even if the app's containers are manually stopped. Global power settings are
not changed. Docker's existing `unless-stopped` policies handle container
restart; intentionally stopped containers are not forcibly reactivated.

The startup job exits successfully when the VM is already running. It retries
startup failures with a 60-second launchd throttle, but is not a continuous VM
health supervisor. Logs are under `~/Library/Logs/FrozenStock/startup.log`.
Inspect with `launchctl print gui/$(id -u)/com.frozenstock.paper.startup` and
`launchctl print gui/$(id -u)/com.frozenstock.paper.ac-awake`.

To remove both agents and release this sleep assertion, without stopping the VM:

```bash
.local/paper-runtime/bin/python deploy/paper/macos_host_service.py uninstall
```

Keep the Mac plugged in with reliable networking. This is not independent 24/7
cloud hosting: a GUI login is required, battery-only sleep is not prevented,
and power loss, lid closure, forced sleep and network loss remain limitations.
Installation/removal and the running assertion were tested; a real reboot and
cold-start recovery have not been tested. No auto-login or disk-security settings
are changed, and no trading authorization is granted by starting services.

The browser console listens on http://127.0.0.1:8088 and opens directly into the
workspace without a sign-in page. The loopback Nginx proxy supplies viewer access
only for unauthenticated GET/HEAD requests. The viewer key is provisioned into
private Git-ignored `web.env`, not browser assets. The API port still requires
authentication; absent/invalid credentials cannot perform write operations.
Do not expose this automatic-viewer proxy beyond loopback.

Overview, Markets, Portfolio, Learning, Research, Risk & Safety, Activity, and
System have separate routes and focused tabs. Additional permissions can be
applied at System > Access with a role key from private configuration. These
keys stay in tab memory and disappear on reload. No automatic operator/admin
access or trading authority is granted. After rotating viewer credentials, rerun
`prepare_web_access.py` and recreate the web container. This helper needs
`python-dotenv`, available in the project's Python runtime.
The API also listens on http://127.0.0.1:8010; `/api/health` is public and the
operational API requires authentication. On a cloud VM use an SSH tunnel; do not expose
this local-role-key deployment directly to the Internet. A public rollout
requires the existing production identity gateway and HTTPS.

### Replit always-on worker mode

The published Replit Autoscale deployment intentionally runs only the web/API
gateway. It must not be treated as a worker host: readiness will correctly show
zero Celery workers and no learning collection will run there. For a single
always-on VM deployment, provide a private Redis URL and set
`PAPER_WORKERS_ENABLED=true`. The production gateway then supervises exactly one
intraday worker, market-data worker, learning worker, paper-execution worker,
risk worker, watchdog and leased beat scheduler. The flag defaults to false, so
Autoscale cannot accidentally create duplicate schedulers or workers. Keep
`ALLOW_LIVE_TRADING=false`; this mode is paper-only and still requires the
application's data, accounting, strategy and risk gates.

Do not enable this flag on a horizontally scaled deployment. Replit Reserved VM
or another single-instance host is required, along with durable PostgreSQL and
Redis configuration. Verify `/api/system/readiness` and worker heartbeats after
startup; a healthy web endpoint alone is insufficient.

## Research console

- The IEX Research Feed collects AAPL/MSFT/QQQ/SPY every five minutes through
  the official Alpaca SDK. It persists real completed one-minute observations
  with explicit `alpaca_iex` / `iex` provenance. It has no order authority and
  is not a substitute for the existing consolidated-feed execution contract.
- Collector freshness means a poll ran recently, not that the market is open.
  The chart displays exchange timestamps; off-session observations remain dated.
- Historical imports use yfinance 1.7.0, then the separately labeled Yahoo
  fallback. Training remains available through Stock Training, with immutable
  snapshots, baseline comparisons and holdout-reuse protection.
- A completed training job is not a passing strategy. The holdout report shows
  whether both Brier score and log loss beat the baseline. Even a lower error
  does not establish profit, significance, or permission to trade.
- UI rendering can be checked with `node deploy/paper/smoke-ui.mjs` after
  installing workspace dependencies and Playwright Chromium. This test reads
  a local researcher key to verify temporary access and reset-on-reload. It also
  checks anonymous write rejection without submitting broker orders.

See [the repository comparison](OPEN_SOURCE_REVIEW.md) for adoption decisions
and remaining qualification work. The local stack and cloud stack have separate
databases and access keys; local results are not silently copied into cloud evidence.

## Oracle micro deployment

`compose.oracle-micro.yaml` is an override for the Always Free x86 micro VM.
Use it together with `compose.yaml`, never on its own. It retains separate
queues, prefork task timeouts, the watchdog, and the scheduler lease, while
limiting learning concurrency and numerical-library threads to one. PostgreSQL
uses smaller buffers. A 1 GB VM requires swap (the current server has 2 GB);
large batch training may still exceed its capacity. Do not increase worker
counts on this host or interpret successful startup as a load-test result.

The full-stack micro trial was too slow: a data-status request timed out after
20 seconds while roughly 1 GB of swap was in use. For this host, additionally
use `compose.oracle-observer.yaml`. This schedules only real IEX collection
(including per-symbol online learning) and delayed SIP collection. Execution,
risk, batch-learning, intraday, and watchdog workers are excluded from its
default service set. It is research observation, not an operational trading
deployment. The observer configuration is tested locally but has not yet been
qualified on the cloud host; the user redirected deployment toward Replit.

Prefer compiling the web app on the development machine rather than the micro
VM. With workspace dependencies installed, run `bash deploy/paper/build-cloud-web.sh`,
transfer `artifacts/strategy-control-room/dist/public`, and build the server's
web image with `docker build -t frozen-stock-web:local -f deploy/paper/Dockerfile.web-prebuilt .`.
This uses the same paper-auth flags and nginx configuration as the full build.
When packaging from macOS, set `COPYFILE_DISABLE=1` and exclude `._*` files;
AppleDouble metadata ending in `.py` otherwise breaks Alembic revision loading.
Both Docker build contexts also exclude that metadata and private credentials.

Generate fresh server role/database keys with `prepare_paper_deployment.py`.
`prepare_cloud_research_access.py --source .env --output <private-file>` exports
only explicit paper Alpaca credentials and the Tradier market-data key, with
owner-only permissions. It refuses to overwrite files and excludes live,
Clerk, AI, local role, and Tradier trading credentials. Transfer this private
file as the server's `.env`; it must never enter an image or Git. Generate
`web.env` on the server using `prepare_web_access.py`.

```bash
docker compose --env-file deploy/paper/paper.env -f deploy/paper/compose.yaml -f deploy/paper/compose.oracle-micro.yaml -f deploy/paper/compose.oracle-observer.yaml up -d --no-build
bash deploy/paper/connect-oracle.sh
node deploy/paper/smoke-cloud-ui.mjs
```

The dashboard is at `http://127.0.0.1:18088/` through the SSH tunnel. Its server
port remains loopback-only; do not open it in Oracle's firewall. The app runs
independently of the tunnel or laptop. Reconnect the tunnel to view it after
network changes. The script supports `ORACLE_PAPER_HOST` and
`ORACLE_LOCAL_WEB_PORT`; the UI smoke test supports `CLOUD_PAPER_ORIGIN`.
Private server role keys are kept in `.local/oracle/paper.env` on this machine.
No account initialization, trial approval, or halt reset is implied by deployment.

Oracle's [Always Free documentation](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
lists the micro shape and 200 GB combined home-region block/boot storage.
Confirm total account usage, not just this VM's size. Oracle can reclaim idle
free instances; this is not guaranteed uninterrupted hosting. Back up research
data independently and never use this experimental deployment for live money.

## Runtime layout

- PostgreSQL persists research, immutable lineage, ledger, and audit state.
- Redis uses AOF persistence with `noeviction`; it has no published host port.
- Intraday ingestion, general market data, learning, execution, and risk each
  have their own worker process. Heavy research cannot occupy risk workers.
- Beat has one renewable leadership lease. Its child stops before lease release.
- The independent watchdog does not depend on Celery worker availability.
- Models, operational reports, and the beat schedule use persistent volumes.
- Migration runs once before application services. Application containers are
  non-root and restart unless explicitly stopped. Host/VM restart and sleep
  remain host-level responsibilities; this does not make a laptop 24/7 hosting.

Increase `LEARNING_CONCURRENCY` cautiously or use `--scale learning=2` with
Compose. Do not scale the scheduler. The deployment monitor requires positive
worker pings, a dedicated intraday consumer, and coverage of every required
queue. Queue capacity does not prove that data or a trading session is valid.

## Read-only evidence

```bash
docker compose --env-file deploy/paper/paper.env -f deploy/paper/compose.yaml exec -T backend python scripts/check_paper_progress.py --output /data/reports/progress.json
docker compose --env-file deploy/paper/paper.env -f deploy/paper/compose.yaml exec -T backend python scripts/compare_paper_brokers.py --output /data/reports/broker-comparison.json
```

Add `--cycle-id <64-character-id>` to the progress command for an exact cycle.
The progress exit codes are 0 for eligibility to request a new approval, 1 for
not eligible, and 2 for unavailable evidence. An already running/completed
trial may correctly be ineligible for a *new* approval. This report is not a
trading authorization and never certifies profit. It records up to 100 recent
cycles, not lifetime totals. Broker comparison only reads account and position
data and emits redacted status; it does not import balances or switch providers.

To access a report outside the container:

```bash
docker compose --env-file deploy/paper/paper.env -f deploy/paper/compose.yaml cp backend:/data/reports/progress.json deploy/paper/progress.json
```

## Broker and data prerequisites

Configure paper keys privately in `paper.env`, then recreate the services.
Alpaca uses `PAPER_ALPACA_API_KEY`, `PAPER_ALPACA_API_SECRET`, and
`PAPER_BROKER_ACCOUNT_ID`. Tradier uses `TRADIER_API_KEY` and
`TRADIER_ACCOUNT_ID`. Do not add live credentials. Configuring both for the
comparison does not change `ACTIVE_PAPER_BROKER` or authorize activation.

Alpaca offers free paper accounts, but its free stock data covers IEX only.
Tradier sandbox data is delayed 15 minutes and its current adapter cannot
establish complete historical accounting. These limitations must remain visible;
neither free account is automatically qualified for this system's feed/cost gates.

Sources: [Alpaca data plans](https://docs.alpaca.markets/us/v1.1/docs/about-market-data-api),
[Alpaca paper accounts](https://alpaca.markets/support/types-accounts-alpaca-offers),
[Tradier sandbox](https://docs.tradier.com/docs/endpoints),
[Tradier delay](https://docs.tradier.com/docs/faq).

Historical imports already fail with `unavailable` when both real Yahoo paths
fail; they do not replace market observations with synthetic prices. Keep the
requested dataset provider consistent with stored provenance. Do not relabel
`yahoo_chart` data as `yfinance` to pass a gate.

## First session and learning evidence

1. Obtain account-specific provider evidence and independently qualify/activate
   a paper venue using the existing application workflows.
2. Import and reconcile the authorized paper account; preserve unknown costs.
3. Resolve accounting, audit, and recovery findings with recorded evidence,
   not by deleting history, dismissing alerts blindly, or editing the kill switch.
4. Create a cycle for AAPL/MSFT/QQQ/SPY and the exact future session. The scheduler
   now trains one configured-universe challenger; company/strategy experiment
   jobs remain research-only. Scheduling does not supply a session approval.
5. Once fresh gates pass, record the bounded session approval through the app.
   Observe orders, fills, terminal reconciliation, and delayed eligible outcomes.
6. Compare frozen challengers against the baseline using existing out-of-sample
   and forward-evidence gates. Keep a rejected challenger rejected. Passing code
   tests or a profitable simulation is not evidence of future live profit.
7. Repeat across sessions and validate recovery. Any live-money pilot remains a
   separate authorization process and is disabled by this deployment.

## Backups and recovery

### Network fault drill

```bash
python3 deploy/paper/verify_network_faults.py
```

Requires the local `frozen-stock-tests:local` image (build with
`docker compose -f deploy/paper/compose.test.yaml build tests`). The runner uses
a unique disposable Compose project with PostgreSQL, Redis and digest-pinned
[Shopify Toxiproxy 2.12.0](https://github.com/Shopify/toxiproxy/releases/tag/v2.12.0).
No host ports, broker credentials, production volumes, or external network are
available. The runner cleans up its own containers/network/volumes even on test
failure and has a three-minute overall bound.

The tests execute the application's actual `_run_job` wrapper and verify Redis
lease exclusion, Redis disconnection/delayed responses, PostgreSQL disconnection/
connection reset, and successful callbacks after recovery. Failed work must
roll back before a failure notification is committed, and a database outage
while recording that notification must not replace the original job error.

This is not proof of broker exactly-once execution, an ambiguous order-response
recovery, or host-reboot recovery. It does not undo already committed work or
external side effects; those still require durable idempotency and reconciliation.
PostgreSQL silent packet blackholes and mid-order network loss are not covered.

### Encrypted local backups

The self-hosting stack works directly with Docker Compose; Coolify is optional,
not a source of free server capacity. Use an always-on Linux host for unattended
uptime. This Mac's Colima VM stops providing service when the Mac sleeps or turns
off. No remote host or host-reboot recovery has been validated yet.

The backup and proxy-drill scripts select Colima on macOS and Docker's default
context on Linux. Override with `PAPER_DOCKER_CONTEXT`; an empty value explicitly
uses Docker's default context.

```bash
python3 deploy/paper/encrypted_backup.py init
python3 deploy/paper/encrypted_backup.py backup
```

Run `init` once only. It refuses to overwrite an existing vault. The private,
Git-ignored `.paper-vault/password` is required for recovery: put a copy in a
separate password manager before relying on backups. Losing it loses access.
The official Restic 0.19.1 image is pinned by digest. Containers are networkless,
unprivileged, read-only except explicit mounts, and never receive broker keys.

Each backup first restores a consistent PostgreSQL dump into a disposable
database and checks every table's row count and hash. It also checks model
archive contents. Restic encrypts these verified files, reads all repository
data for integrity, restores the exact new snapshot, and compares file hashes.
Fresh plaintext staging is removed afterward; this is not guaranteed secure
erasure. Existing `.paper-backups` archives are not removed. A failed run exits
nonzero; check that the report snapshot belongs to the latest successful run.

The vault contains `repository/`, `password`, and `last-verification.json`.
For recovery, use Restic with that repository and password file, select the
recorded snapshot ID, and run `restore SNAPSHOT --target EMPTY_DIRECTORY
--verify`. The restored `source/` directory contains `database.dump`,
`models.tar`, and the original verification manifest. Restore the database
into an isolated PostgreSQL instance before any controlled production recovery.
This does not clear recovery gates or authorize queue replay or broker orders.

This is **local encryption, not off-host disaster recovery**. Repository and key
currently share a machine. Copy the encrypted repository to independent storage
with the key stored separately, then repeat recovery on the destination host.
Secrets, roles/ACLs, Redis, and report volumes are not included. Database and
models are individually verified, not an atomic cross-resource checkpoint.
Backups are manual; no scheduling or remote storage is claimed as configured.

Run the actual Docker integration tests on this Mac using a Colima-shared path:

```bash
RUN_RESTIC_INTEGRATION=1 .local/paper-runtime/bin/python -m pytest -q --basetemp=.local/restic-test-tmp deploy/paper/test_encrypted_backup.py deploy/paper/test_verify_restore.py
```

Sources: [Restic](https://github.com/restic/restic),
[official Docker installation](https://restic.readthedocs.io/en/stable/020_installation.html),
[restore documentation](https://restic.readthedocs.io/en/stable/050_restore.html),
[Coolify](https://github.com/coollabsio/coolify),
[Colima](https://github.com/abiosoft/colima).

Back up PostgreSQL with `pg_dump` and back up the models/reports volumes together
with that database snapshot. Restore-test them on a separate stack before
claiming disaster recovery. Redis AOF is coordination durability, not an order
ledger backup. Never run `docker compose down -v` against retained research.

After restarting a worker, Redis, scheduler, or host, rerun the progress check
and verify account reconciliation and watchdog evidence before further paper
execution. Use the existing bounded-soak and crash-recovery tests for controlled
interruptions; do not conduct disruption experiments against a funded account.
## Restore Verification

Run from the repository root with Python 3.11+:

```sh
.local/paper-runtime/bin/python deploy/paper/verify_restore.py
```

This holds a read-only PostgreSQL snapshot, hashes every public table, creates
a custom-format dump, restores it into a disposable networkless database, and
compares every restored table to that snapshot. It also archives, extracts and
hash-checks model files. Temporary containers are removed afterward. Private
archives and the verification report remain in `.paper-backups/` (Git-ignored,
owner-only permissions). They contain sensitive data and are not encrypted or
off-host. The command does not certify broker recovery, restore access-control
roles, replay order queues, or change any trading safety gate.

## Isolated Backend Tests

The production web proxy uses Docker DNS re-resolution and checks API health,
not just static HTML. Test backend address changes, an actual outage and recovery
against the built web image without touching the running app:

```sh
.local/paper-runtime/bin/python deploy/paper/verify_proxy_recovery.py
```

The test-only image adds Redis for real worker-termination tests; the runtime
image remains unchanged. Build the current backend image first, then:

```sh
docker --context colima-frozen-stock compose -f deploy/paper/compose.test.yaml build tests
docker --context colima-frozen-stock compose -f deploy/paper/compose.test.yaml run --rm tests
docker --context colima-frozen-stock compose -f deploy/paper/compose.test.yaml down -v
```

The validation stack has its own PostgreSQL and Redis, no published ports, no
broker credentials injected, and an internal network. These commands never use
the production database as the test target. Stop and remove only this validation
project after the run. Local archive-security unit tests:

```sh
.local/paper-runtime/bin/python -m pytest -q deploy/paper/test_verify_restore.py
```

## Alpaca Activity V2

Migration `0049_alpaca_activity_ledger` adds an explicitly selected reconciliation
contract for new Alpaca paper ledgers. Existing accounts remain `legacy-v1` and
are never reset or silently reinterpreted. The admin initialization endpoint
`POST /api/stock-paper/initialize` accepts the optional body
`{"activity_contract":"alpaca-activities-v2"}` only for an already-selected Alpaca
paper provider. This does not select/qualify that provider or authorize orders.
No body preserves the original behavior.

V2 captures two consistent observations of cash, inventory, orders and activities
before freezing a baseline. These sequential API reads are not an atomic broker
snapshot. The baseline is an observed account, not proof of its inception history.
Later reconciliation replays the full activity history against that baseline,
rejects missing prior IDs and changed payloads, and handles date-only cash entries
without fabricating execution timestamps. Separate account fees are not allocated
to fills. Ambiguous fee attribution and unsupported corporate actions fail closed.
Residuals remain sticky even if later evidence matches. All-in costs stay unknown,
and legacy automatic cost qualification cannot grant V2 trading authority.
The status endpoint exposes the last dated reconciliation report separately from
the current account status; a prior matching report does not clear a later halt.

Exercise the actual credentials through allowlisted GET calls, with a disposable
SQLite ledger and fresh sessions, without touching the application database:

```sh
docker --context colima-frozen-stock compose --env-file deploy/paper/paper.env -f deploy/paper/compose.yaml run --rm --no-deps backend python -m scripts.validate_alpaca_activity_ledger
```

This probe cannot create orders or change the selected running provider. It
returns nonzero on blocked reconciliation. Empty fill history does not establish
execution quality, profitability, broker qualification or live-money readiness.

## Delayed Consolidated Research Data

The local stack enables `DELAYED_SIP_RESEARCH_ENABLED=true`. Its five-minute
Celery collector requests Alpaca `feed=sip`, raw one-minute bars, with an explicit
end at least 16 minutes old. Exchange sessions and early closes bound each
request. It uses the existing read-only, timeout-limited SDK transport; malformed,
duplicate, incomplete-page and entitlement failures never become fallback bars.
Set the flag to false to disable collection. No market-data plan is upgraded.

Rows use provider `alpaca_delayed_sip` and feed `sip_delayed`, separate from IEX
and the execution feed. The storage boundary also enforces the delay. The
control-room panel compares exact overlapping timestamps with IEX within the
reported session window. Close-price gaps are data comparisons, not returns or
strategy scores. Missing overlap is unknown, not zero. Neither this feed nor its
coverage can satisfy real-time execution qualification or train the existing IEX
forward learner. Raw bars do not establish corporate-action adjustment lineage.

Viewer status: `GET /api/market-data/delayed-sip/status`.
Researcher collection request: `POST /api/market-data/delayed-sip/collect`.
Both scheduled and manual requests use the existing shared job leases.

Actual-credential probe using disposable SQLite storage, two imports, and an
execution-isolation check:

```sh
docker --context colima-frozen-stock compose --env-file deploy/paper/paper.env -f deploy/paper/compose.yaml run --rm --no-deps backend python -m scripts.validate_delayed_sip
```

## Forward Research Learning

The IEX panel includes per-symbol forward-learning counters. While the stack is
running, the five-minute collector also advances `iex-sgd-v1`: it scores matured
predictions before updating a separate incremental model for each symbol. No
orders are sent. At first startup outside market hours, zero scored forecasts
is expected; the learner does not backdate predictions using downloaded history.
Fresh contiguous session bars are required, and missing target minutes expire.
Lower Brier score means less probability error, not trading profit. The latest
1,000 scored forecasts form the bounded model replay and displayed metric window.
