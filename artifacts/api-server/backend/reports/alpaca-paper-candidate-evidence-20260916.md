# Alpaca Paper candidate validation — 2026-09-16

## Decision

**No-go for a provider switch or paper-loop launch. Complete evidence is not established.**

The user explicitly approved read-only validation of Alpaca Paper. This is a completed candidate assessment, not a venue certification. No orders were placed or cancelled; no account was initialized/reset, ledger reconciled, provider configuration changed, or trading gate relaxed.

## Fresh broker observations

Probe time: 2026-09-17 02:55:29 UTC (2026-09-16 America/New_York).
The existing adapter was instantiated directly with a GET-only subclass, without changing the active provider. Two separate clients read account, positions, all orders, and all activities. Only aggregate field-presence/count evidence was emitted; balances, account identifiers, credentials, and raw activity payloads are excluded.

| Contract | Observed evidence | Assessment |
| --- | --- | --- |
| Balances | Both reads returned an account object with numeric cash/equity and an identity; no `updated_at` | Readable, but no broker snapshot timestamp; not a reconciled accounting equation |
| Positions | Empty list on both reads | Structurally valid; nonempty position normalization unproven |
| Historical orders | Empty list on both reads | Accessible, but lifecycle history, paging boundaries, and old terminal orders unproven |
| Fills/order linkage | No fills | Execution IDs, timestamps, quantities, and order linkage unproven; zero missing-field counts over zero fills are not a pass |
| Activities | One identified non-fill activity on each read | Historical endpoint exists; complete multi-page history not exercised |
| Costs | No fill commissions available to evaluate | Unknown, not zero; no basis to grant `costs_known` |
| Stable replay | Orders and complete returned activity payloads equal across fresh clients | Small-sample repeatability only; not restart/missed-session recovery certification |
| Recovery | No historical executions or outages exercised | Insufficient evidence for interrupted fills, late fees, or missed-session recovery |

## Documentation and adapter review

Official documentation retrieved during this review:

- https://docs.alpaca.markets/us/docs/account-activities.md — historical account activities, stable activity IDs, fill `transaction_time`/`order_id`, non-trade `date`/`net_amount`, and ID-based `page_token` pagination.
- https://docs.alpaca.markets/us/v1.1/reference/getaccountactivities — `page_token` is the last item ID on the current page.
- https://docs.alpaca.markets/us/docs/paper-trading — paper simulation omits regulatory fees and dividends, among other live-market effects. These omissions cannot be reinterpreted as broker-reported zero costs under this project's unchanged contract.

`app/services/stock_paper_ledger.py` provides the relevant boundary:

- `AlpacaPaperClient` fixes the base URL to the paper endpoint.
- Orders use bounded timestamp pagination. A full ambiguous boundary fails closed. This empty account cannot validate the cursor against real historical orders.
- Activities are fetched in full on every reconciliation, but `_pages` expects a returned continuation token. Alpaca documents deriving the next token from the last activity ID. A full bare-list page therefore raises rather than proving a complete import. The one-row response did not exercise that limitation.
- The documented fill schema does not promise a `commission` field; non-trade activities can have a date rather than a precise transaction timestamp. Provider schema compatibility is not evidence that the ledger's stricter cost and immutable-timestamp rules are satisfied.
- `_snapshot` defaults a missing `evidence_complete` flag to true. That is an implementation default, **not candidate certification**. No initialization or reconciliation was invoked in this review.

## Reproduction

From `artifacts/api-server/backend`, with the existing securely configured paper credentials:

```sh
PYTHONPATH=. python3.11 scripts/validate_alpaca_paper_evidence.py
```

The script permits only the four evidence GET endpoints, emits redacted summaries, and always labels its output `not_established_by_read_only_probe`. It neither calls the ledger nor creates database state. Repeating it can observe different account evidence; this report records the observation above.

## Conditions before reconsideration

The focused [cost and timestamp qualification](alpaca-paper-cost-timestamp-qualification-20260917.md)
concludes **not established** under the unchanged contract and adds offline
provider-shaped fixtures plus redacted field assessments to the read-only probe.
It does not supersede the fresh observations above or authorize another probe.

1. Demonstrate complete historical paging, including full pages and boundary failures, without silently dropping records.
2. Supply existing broker-paper executions and cash-affecting activities sufficient to verify immutable IDs/timestamps, fill-order links, explicit cost evidence, and the accounting equation. No test orders are authorized by this assessment.
3. Demonstrate restart/missed-session and late-activity recovery using that evidence. Fresh-client replay of empty orders is insufficient.
4. Resolve any provider-schema gaps without changing unknown costs to zero or fabricating timestamps.
5. Obtain separate authorization for any provider switch, and rerun all launch gates. This assessment does not clear the Tradier report's independent feed, risk, audit, notification, or recovery blockers.