# Alpaca Paper cost and activity timestamp qualification — 2026-09-17

## Explicit result

**NOT ESTABLISHED — no-go for qualification, provider switch, or launch.**

Existing evidence in `alpaca-paper-candidate-evidence-20260916.md` cannot
satisfy the unchanged cost and immutable activity timestamp contract.
This is a review of that recorded evidence and synthetic contract tests, not
a fresh broker observation. No network probe, account reset, initialization,
reconciliation, order placement/cancellation, or provider change was performed.
Launch gates are unchanged.

| Requirement | Existing evidence and documented shape | Qualification |
| --- | --- | --- |
| Explicit commission evidence | Recorded account had no fills; documented FILL shape does not promise `commission` | **Unknown**, not zero. Neither an empty sample nor paper simulation's omission of fees grants cost credit |
| Precise immutable activity time | FILL documents `transaction_time`; non-trade activities can expose only `date` | **Not established** across activities. A date provides a calendar day, not a precise instant |
| Replay stability | Two earlier reads had equal returned payloads | Payload repeatability does not establish missing costs or precise timestamps |

The earlier report did not record timestamp field presence for its one non-fill
activity. We do **not** claim that actual activity was date-only. The
documentation-compatible date-only fixture demonstrates the unresolved schema
gap; the earlier observation alone cannot establish that this gap is absent.

## Ledger compatibility

Reviewed `app/services/stock_paper_ledger.py`:

- `_upsert_fills` persists missing commission as `fee=None`, `cost_known=False`.
  It accepts an explicit broker zero as known, and permits only exact late
  commission enrichment without rewriting other immutable fill evidence.
- The accounting residual calculation can use zero provisionally in arithmetic,
  but separately tracks incomplete commissions. That provisional arithmetic is
  **not** reported cost evidence or permission to mark costs known.
- `_activity_timestamp` prefers `transaction_time`, then `created_at`, but
  falls back to reconciliation observation time. Its general timestamp parser
  also does not enforce timezone-qualified precision. Such a derived value is
  **not acceptable qualification evidence**. The new assessment never calls
  this fallback, never converts `date` to midnight, and never infers a time
  from an activity ID.
- Raw activity payload mutation remains fail-closed; fill timestamp mutation is
  rejected. Identical replay does not upgrade a fallback timestamp into broker
  evidence.

No runtime ledger behavior was loosened or replaced. In particular, this work
does not make date-only non-trade activity safe for the precise-time contract.
Accepting calendar-day precision would require a separately approved contract
and storage design, not an invented timestamp.

## Executable assessment and fixtures

`scripts/alpaca_paper_contract.py` adds a pure, redacted field assessment to each
read of `scripts/validate_alpaca_paper_evidence.py`:

- Missing, null, malformed, or non-finite commission stays unknown.
- Missing, date-only, malformed, or timezone-less activity time stays unknown.
- `transaction_time`, or otherwise `created_at`, must contain a precise
  timezone-qualified datetime. No observation-time fallback is used.
- No fills or no returned activities cannot pass vacuously; unavailable
  activity evidence remains unknown.
- Even explicit commissions plus precise times yield only
  `field_contract_satisfied_only`, with `launch_authorized=false`. The overall
  probe still reports `not_established_by_read_only_probe`.

`tests/fixtures/alpaca_paper_cost_timestamp.json` contains synthetic documented
FILL-without-commission and date-only JNLC shapes. Explicit cost and precise
`created_at` extensions are hypothetical positive controls, **not claims that
Alpaca supplies those fields**. Tests cover the assessment, redacted probe
output, ledger unknown-cost persistence, exact zero-cost enrichment, stable
replay, and rejection of changed immutable timestamps.

Verification (offline, from the backend directory):

```sh
PYTHONPATH=. python3.11 -m pytest -q \
  tests/test_alpaca_paper_cost_timestamp_contract.py \
  tests/test_alpaca_candidate_probe.py \
  tests/test_stock_paper_ledger.py
```

## Reconsideration boundary

Qualifying existing broker evidence must provide explicit commission evidence
and precise broker activity times without inference. If Alpaca cannot supply
them, it remains incompatible with this unchanged contract. No test orders
are authorized to obtain that evidence.

Historical pagination and incomplete-history recovery are independent
requirements and outside this result. Order linkage, accounting, recovery,
and all other launch gates still require their own evidence even if these
two field checks eventually pass. The queued launch-blocker work must retain
this no-go result until qualifying evidence exists.