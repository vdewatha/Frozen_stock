# Freqtrade dry-run execution bridge

Update: exact-tag restart recovery, automatic observed external-exit accounting,
and real-provider round-trip/restart acceptance are now implemented. See
[running experiment](LIVE_PAPER_EXPERIMENT.md) and [recovery](PAPER_RECOVERY.md).
External exits still halt new trading for review; they never fabricate a model
decision or grant qualification. Terminal-only provider changes are recorded as
zero-quantity evidence rows, not duplicate executions.

The bridge is paper-only and deliberately narrower than a general exchange
adapter. It supports one BTC/USD spot position, one in-flight limit order, and
explicit operator-approved nonqualifying trials. It does not enable live money.

## Contract

`FreqtradeDryRunClient` verifies every provider action against Freqtrade 2026.8,
Kraken, spot, hourly timeframe, `dry_run=true`, `ObservationOnly` strategy,
`kraken-paper-execution` bot name, force-entry enabled, no position adjustment,
no shorts, and a BTC/USD-only whitelist. Submissions additionally require running
state; reconciliation works while stopped. Remote transport requires HTTPS;
HTTP is restricted to loopback. No redirects or environment proxies are used.

`dispatch_intent(session_factory, client, intent_id)`:

1. Checks provider inventory and outstanding orders against the local ledger.
2. Revalidates the approved decision and kill switch in a serialized transaction.
3. Commits `submitting`, an entry tag, and provider context **before** any POST.
4. Makes exactly one limit-entry or limit-exit request.
5. Reconciles the returned trade's exact order evidence.

Network failures leave an `unknown` intent. Process death can leave `submitting`.
Neither state may be resubmitted. For ambiguous entries, inspect the provider and
pass the exact trade ID to `reconcile_intent(..., trade_id=...)`; the recorded
entry tag must match. Exits are bound to the recorded trade and previous order IDs.
Missing/ambiguous/replaced orders remain blocked for manual investigation.

## Accounting and explicit limitations

- Cumulative provider fill quantity/cost becomes incremental immutable fills.
  Snapshot hashes and unique IDs prevent double counting. Revised cumulative
  evidence fails closed. The ledger uses exact eight-decimal fixed point.
- Cash is a **modeled paper ledger**, not the provider's wallet balance. Fees use
  the operator-approved quote fee rate. Reported provider rates must not exceed
  that rate; non-USD fee currencies and base-asset fees are unsupported. Responses
  label this `modeled_approved_rate`. This is not production fee reconciliation.
- Provider cost must agree with its average price, and each incremental cost must
  remain exactly representable at ledger precision. Unsupported rounding is
  blocked rather than silently changing cash.
- Provider inventory drift engages the durable local kill switch. Automatic
  protective stop-loss orders can still happen in Freqtrade; they are external
  activity, not approved model decisions. Replaced or extra orders block automated
  reconciliation and require operator investigation. The local kill switch does
  **not** cancel an already submitted order or disable a provider stop-loss.
- No pyramiding, shorts, leverage, autonomous promotion, automatic cancellation,
  live credentials, or real-money authorization is implemented here.
- Historical profitable results and these modeled fee calculations do not make
  any model eligible for qualification.

Tests use an isolated database and mocked provider transport; they cover the
dry-run guard, payload contract, partial fills, retries, mismatched identities,
fee/cost failures, durable drift kill, rejection and buy/sell round trips. A live
provider dry-run acceptance test is a separate operational check.
