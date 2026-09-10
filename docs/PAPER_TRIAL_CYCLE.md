# Bounded nonqualifying paper-trial cycle

`run_paper_trial_cycle(factory, client, approval_id)` connects one latest shadow
decision to the existing paper gateway. An explicit active operator approval and
an already-disarmed account kill switch must exist. The cycle cannot approve a
model, initialize capital, clear a halt, promote a model, or enable live trading.

Each invocation first runs provider recovery. A blocked recovery or engaged
kill switch stops the cycle. The latest forward decision must match the exact
approved binding; the ledger revalidates its hash, age, approval and risk limits
both when reserving and immediately before submission.

- Hold signals wait without orders.
- Buy signals do not add to an existing position.
- Sell signals cannot create shorts and use at most available BTC inventory.
- Buy notional is bounded by the approved maximum notional, maximum exposure,
  and available cash after fee reservation. Quantity rounds down to eight
  decimal places. Policy version 2 applies notional limits to entries only;
  exits sell the full available owned position even when appreciation puts its
  proceeds above the original entry limit. Exits cannot exceed owned inventory.
- Limit prices are reference price plus 0.1% for buys or minus 0.1% for sells,
  rounded to USD 0.10. The ledger's independent price-deviation bound still applies.
- Client IDs are deterministic: `trial-{approval_id}-decision-{decision_id}`.
  A consumed decision is never traded a second time.
- A matching reservation that demonstrably never started submission may resume
  after a restart. Stale reservations or reservations belonging to another
  approval require explicit abandonment. `submitting`, `unknown` and `submitted`
  orders are only recovered, never resubmitted.

The method runs one bounded cycle and sends at most one dispatch request. A
supervisor decides when to call it again. Exceptions remain visible for recovery;
no failed POST is retried by this method. All results remain explicitly
nonqualifying dry-run evidence with live authorization false.

Tests exercise risk sizing, deterministic idempotency, holds, no shorts or
pyramiding, approval revocation, sticky recovery halts, and crash-before-dispatch
resumption. Transport/recovery boundaries are mocked here; their separate suites
and disposable real dry-run acceptance tests cover provider behavior.
