# Priority implementation and operating sequence

**Subsequent operational increment:** the real-data training and paper runtime
are now running. See [current experiment](LIVE_PAPER_EXPERIMENT.md) for authoritative
state, controls and limitations. The earlier verification below remains historical.

Implemented scope: durable Kraken data → version-bound shadow inference →
serialized paper accounting → Freqtrade dry-run submission/reconciliation.
This is an experimental paper workflow, not completion of the full 24-package
production plan. No model is qualified and no live-money path was enabled.

## Delivered

| Priority | Implementation | Review / acceptance |
| --- | --- | --- |
| Durable data | Immutable hourly BTC/USD candles, hash/revision/gap/freshness checks, batch audits, idempotent collection | Independent review, real public Kraken collection, fixture concurrency/rollback tests |
| Shadow model | Portable numeric JSON export, verified model/data/feature identity, causal inference, horizon outcomes, backfill marking | Independent review; real training/registry/collector/shadow integration using fixture market data |
| Paper ledger | Exact fixed-point balances, reservations, approved version-bound trials, kill switch, accounting audit, fills and terminal reconciliation | Independent review; duplicate/concurrent intent, partial fill, corruption, timeout and risk tests |
| Freqtrade bridge | Pinned dry-run transport, at-most-once submission, buy/sell limits, cumulative-fill reconciliation, drift halt | Independent review; mocked order round trips; actual running dry-run config/API verification with zero orders |
| Operations | Collection/shadow runner, optional Compose profile, persistent local Freqtrade launcher, authenticated controls | Schema preflight, role/default-disabled tests, SQLite and PostgreSQL migrations |

## Run in increments

1. **Choose an isolated database.** Do not point migration commands at an old
   unversioned database without the backup/adoption procedure in
   [database migrations](DATABASE_MIGRATIONS.md). Upgrade the selected database
   to the current migration head (`0008` or later). The running experiment already
   has its own migrated database; do not reinitialize it or migrate an unrelated
   existing database.
2. **Collect real data.** Set `DATABASE_URL` explicitly and run
   `python backend/scripts/run_crypto_pipeline.py`. Inspect `/crypto/status` and
   collection audits. Then use `--continuous --interval-seconds 60`, or the
   [optional Compose profile](CRYPTO_PIPELINE.md). Neither path places orders.
3. **Train and register.** Use completed hourly data and the training CLI's
   `--instrument-id crypto_spot:KRAKEN:BTC:USD --timeframe-minutes 60`, with symbol
   `BTC/USD` or `BTC-USD`. Register the hashed bundle using the existing registry
   CLI. Only the logistic numeric JSON export is supported for shadow inference;
   daily models and executable joblib uploads cannot be substituted.
4. **Bind and observe.** An admin sends `/crypto/bindings` the registered `run_id`
   and exact `logistic_regression.json` object as `spec`. The runner processes all
   bindings, or a researcher calls `/crypto/bindings/{id}/observe`. Read
   `/crypto/decisions` and `/crypto/shadow-audits`. Historical initialization and
   late observations remain explicitly nonqualifying.
5. **Start an isolated paper venue when ready.** Create a private mode0700
   directory named `.paper-venue`, writable by container UID1000 on Linux.
   Supply distinct `FREQTRADE_USERNAME`, `FREQTRADE_PASSWORD` and
   `FREQTRADE_JWT_SECRET` local API credentials; passwords/secrets need 32+
   characters. Pull the exact image pinned in `backend/scripts/smoke_freqtrade.py`.
   Run `python backend/scripts/run_paper_venue.py --state-dir /absolute/path/.paper-venue`.
   It runs in the foreground, publishes only localhost:8080, keeps its simulated
   database across restarts, forces `--dry-run`, and passes no exchange keys.
   Docker administrators can inspect container configuration; protect host access.
   Ctrl-C stops a foreground process without deleting the paper state. The later
   operational increment installed two persistent containers; see
   [current experiment](LIVE_PAPER_EXPERIMENT.md). Do not launch duplicates.
6. **Explicitly approve a paper trial.** Set backend
   `FREQTRADE_PAPER_EXECUTION_ENABLED=true` and its URL/username/password only for
   the selected paper instance. A backend running on the host can use the default
   loopback URL. A backend in a separate container needs a properly secured HTTPS
   connection; do not weaken the remote HTTPS guard. Admin initializes
   `/crypto/paper/account` with `starting_cash` as a decimal string, approves
   `/crypto/paper/trials` with binding ID, decimal-string risk limits/fee rate and
   `acknowledge_nonqualifying_trial: true`, then explicitly disables the local
   kill switch. This approval is not promotion or capital authorization.
7. **Reserve, dispatch, reconcile.** Operator sends `/crypto/paper/intents` a
   current actionable decision ID, approval ID, side, decimal-string quantity
   and limit price, and unique client order ID. Call `/{id}/dispatch` once.
   Poll `/{id}/reconcile` with `{}`; an ambiguous entry requires the independently
   inspected provider `trade_id`. Never resend `unknown` or `submitting` intents.
   `/{id}/abandon` only releases a never-submitted local reservation.
   Use `/crypto/paper/kill-switch/enable` to block further submissions.
8. **Review observed outcomes before expanding.** Do not automate promotion.
   Continue the full plan's evaluation, forward-evidence and operational gates.

Paths in steps 6–7 are under `/crypto/paper`; intent actions use
`/crypto/paper/intents/{id}/...`. All API requests require the appropriate bearer
role. Read endpoints are viewer-accessible; bindings, account creation, trial
approval and kill-switch disable are admin-only. There is no raw fill-write API.

## Deliberate limits—not live-readiness claims

- Paper cash includes **modeled approved-rate fees**, not reconciled remote wallet
  balances. Unsupported fees/precision, replaced orders and unexplained inventory
  fail closed. See [execution bridge](FREQTRADE_EXECUTION.md).
- The provider's protective stop-loss remains enabled. Observed autonomous exits
  are now reconciled into the ledger and engage the kill switch for review;
  execution is never automatically rearmed. The local kill switch is not a
  remote cancel-all control.
- Persistent paper services are installed. Actual disposable dry-run order/fill
  acceptance and lost-acknowledgement restart recovery passed. These synthetic
  acceptance fixtures are not model profitability evidence. Remote real-money
  wallet reconciliation remains outside the implemented paper workflow.
- The prior experimental model did not beat its baseline. Forward observation
  time, stronger evaluation, authenticated qualification evidence, deployment
  monitoring, individual identities and explicit capital approval remain gates.
  Software or a few positive trades cannot guarantee a profitable model.

Independent reviewers requested fixes for canonical instrument mismatch,
autoflush/kill-switch persistence, dispatch-time stale approval validation,
premature/rejected terminal transitions and unrepresentable provider cost deltas.
The builders implemented the fixes and reviewers accepted the scoped components.

## Historical verification before the operational increment

The later increment passed197 backend tests and migrations through0008; see
[current experiment](LIVE_PAPER_EXPERIMENT.md). The results below describe the
earlier implementation snapshot, not current service or acceptance status.

- 155 backend tests passed in the isolated Python 3.12 environment.
- Fresh SQLite upgrades through revision0007 and ORM schema parity passed.
- Disposable PostgreSQL16 passed all migrations, repeat upgrades, registry
  rollback/concurrent registration, concurrent account initialization, exact
  fixed-point persistence and kill-switch transaction checks.
- Actual public Kraken collection inserted720 completed hourly candles in a
  disposable database; a repeated cycle inserted0 duplicates.
- The pinned actual Freqtrade2026.8 running dry-run profile passed both read-only
  and execution-adapter config/whitelist checks with0 open trades. No order was
  submitted. The probe and PostgreSQL container were removed afterward.
- The frontend was unchanged in this increment; its prior checks are recorded
  in BUILD_PROGRESS.md. No claim of a new browser-to-provider end-to-end test.
