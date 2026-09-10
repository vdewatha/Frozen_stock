# Serialized paper execution ledger

This is **paper-only experimental infrastructure**, not a live execution gateway
or a declaration that a model has qualified. Every trial requires a distinct,
recorded operator approval; model registration and shadow prediction alone grant
no execution permission. The account starts with its kill switch engaged.

## Transaction and network protocol

Use a fresh SQLAlchemy Session for each `execution_transaction(db)` context.
The context acquires a SQLite `BEGIN IMMEDIATE` write lock or a PostgreSQL
transaction-scoped advisory lock. It commits on successful exit and rolls back
on error. Other service functions flush as needed but never commit.

1. `create_account(db, starting_cash)` creates the singleton BTC/USD spot paper
   account once. Resetting existing balances is deliberately forbidden.
2. `approve_trial(db, binding_id=..., actor=..., max_notional=...,
   max_exposure=..., fee_rate=...)` binds an immutable research run and shadow
   specification to an explicitly nonqualifying trial and a hashed risk policy.
   Actor represents the authenticated operator/service principal; individual
   user identity and durable security audit infrastructure are separate work.
3. An operator explicitly disarms `set_kill_switch(db, False)`.
4. `reserve_intent` validates the latest persisted actionable decision, rejects
   stale (over 15 minutes), future, or backfilled observations, and reserves
   cash including fees or existing BTC inventory. Price must be within 1% of
   the decision reference. Quantity, notional, and total exposure are bounded.
   The same client id and exact payload is idempotent; conflicting reuse fails.
5. Commit the reservation. In a separate transaction `mark_submitting` persists
   the provider reconciliation context **before** the remote request; commit.
6. Make the dry-run network request outside the database transaction. Never
   retry a `submitting` or `unknown` order merely because a request timed out.
7. `record_submission` records the verified remote order identity or marks an
   ambiguous outcome `unknown`. One outstanding order globally prevents another
   entry or exit until that uncertainty is resolved.
8. `apply_fill` consumes uniquely identified provider fills. Duplicate identical
   events are harmless; conflicting duplicates, overfills, oversells, excessive
   fees and limit violations fail closed. Fees must be normalized to quote USD
   before calling; unsupported fee currencies need explicit reconciliation.
9. Only after all known fills are applied, `reconcile` records authoritative
   terminal status (`filled`, `cancelled`, `rejected`) and releases reservations.
   Cancellation can follow a partial fill; rejection cannot erase actual fills.

The kill switch blocks reservation and submission; it does not prevent receiving
fills or reconciling existing orders. It does not automatically cancel orders at
the provider. `submitting` after a process crash is an unresolved remote outcome,
not permission to resend. Context carries provider trade/order identifiers and
the pre-submission order snapshot where needed by the provider adapter.

`abandon_unsubmitted` releases an expired/revoked local reservation only while
its status is still `approved`, with no provider context or identity. It cannot
release `submitting`, `unknown`, or remotely accepted orders. Submission rechecks
the decision age, latest-decision status, binding integrity and approval activity.

## Accounting

Risk policy hash version 2 applies maximum notional to entries only. Risk-reducing
exits may exceed that entry limit after price appreciation, but are strictly
bounded by available owned inventory. Old policy hashes are rejected and require
a new explicit approval; no approval is automatically upgraded.

Available cash is `cash - reserved_cash`; available BTC is
`quantity - reserved_quantity`. Buy fills debit notional plus fee and credit BTC;
sell fills debit BTC and credit notional minus fee. Filled quantity accumulates
per order. This ledger allows no shorts, leverage, withdrawals, multiple pairs,
or live account credentials. All decisions remain nonqualifying research trials.

Amounts use exact eight-decimal fixed-point integer database storage, including
SQLite. Inputs with greater precision are rejected. Buy reservations round up
to that precision; fill notional rounds half-even. A sub-unit zero-notional fill
is rejected. Provider reconciliation must use the same documented precision.

The ledger itself has no network access. Its provider adapter is responsible for
proving dry-run mode, enforcing exact account/pair/venue isolation, validating
remote evidence, and detecting divergence before allowing another submission.
No remote balance or fill is guessed from a successful HTTP status.

`audit_account` recomputes cash and inventory from immutable fill records and
starting cash, and reservations from intents, under the same serialized context.
Any negative, overreserved or inconsistent balance fails the accounting check.
This is internal ledger consistency, not a claim of exchange-wallet equality.
Both reservation and submission run this check before authorizing an action.

## Validation

`PYTHONPATH=backend python -m unittest discover -s backend/tests -p test_paper_execution.py -v`

Tests use disposable SQLite databases and cover simultaneous duplicate intents,
partial fills, replay, fees/cash/inventory, rejection release, unknown outcomes,
kill switch, binding mismatch, stale/backfilled decisions, and amount limits.
PostgreSQL 16 migration/schema parity, concurrent singleton creation using the
advisory lock, exact amount storage and kill-switch persistence were also checked
in a disposable instance. Full provider/order concurrency remains a separate gate.
