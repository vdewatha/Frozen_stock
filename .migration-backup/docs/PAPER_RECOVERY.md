# Unattended paper recovery

`backend/scripts/run_paper_recovery.py` is a read-only-provider reconciliation
worker. It never places, retries, cancels, or replaces orders; never clears the
local kill switch; and never authorizes live trading. Run it against an explicitly
selected, migrated research database and the pinned Kraken dry-run bot.

Required environment: `DATABASE_URL`, `FREQTRADE_PAPER_EXECUTION_ENABLED=true`,
`ALLOW_LIVE_TRADING=false`, `FREQTRADE_URL`, `FREQTRADE_USERNAME`, and
`FREQTRADE_PASSWORD`. Supply credentials through your process supervisor's secret
mechanism, not command-line arguments. Remote origins require HTTPS; HTTP is
restricted to loopback. Do not reuse an unreviewed legacy database.

From `backend`, run `python scripts/run_paper_recovery.py --continuous`.
The default interval is 30 seconds; `--interval-seconds` accepts 10–3600.
Without `--continuous`, one cycle runs and returns nonzero on blocked/error.
SIGINT/SIGTERM interrupts the interval wait. A database failure exits nonzero so
a supervisor can restart and alert; do not suppress repeated failures. The worker
does not migrate schemas or initialize accounts. No account means inactive.

Each cycle recovers `submitting`, `unknown`, and `submitted` intents from durable
provider context. An entry missing its remote trade ID is matched by its exact
persisted `paper:<client_order_id>` tag across open trades and **all** closed
history, bounded to 1,000 closed trades. No match, duplicate match, changed page
totals, history beyond the bound, or incompatible mode fails closed. Missing
evidence never releases reserves or triggers resubmission. Exits require the
original persisted trade ID and pre-submission order identities.

After reconciliation, the worker observes owned protective exits, recomputes
ledger accounting, and checks provider inventory/order ownership. Unresolved
state, drift, or provider failure persistently engages the kill switch and writes
a sanitized `paper_recovery` AuditLog event. It continues observing on later
cycles. Successful recovery does **not** clear a previous halt. Approved but
never-submitted reservations stay unsent; protective-exit handling may abandon
them once their assumptions no longer hold.

Audit events contain fixed reason codes, not arbitrary upstream error bodies.
Forward worker failures/blocked results to operational alerts using your
supervisor. Audit retention and history growth need capacity management; reaching
the history bound requires operator investigation rather than silent truncation.
Fees remain explicitly modeled at the approved rate, and all results remain
nonqualifying. These controls do not establish strategy profitability.
