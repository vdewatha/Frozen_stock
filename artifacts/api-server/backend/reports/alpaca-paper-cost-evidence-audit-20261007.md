# Alpaca paper cost evidence audit — 2026-10-07

## Disposition

**External evidence blocker remains; no cost verification or trading authority granted.**
Audited repository HEAD `021cf97d6cf3b2bfe0b7c697daebb86ce07630b1`.
This is a code/documentation audit and an ingestion proposal, not a fresh account
certification. No credentials, broker account APIs, orders, account reset,
provider changes, kill switches, or operational database were used or changed.
Only this report was added. The tests use synthetic evidence and isolated databases.

## Official source findings

Checked on 2026-10-07:

- [Trading Account Activities](https://docs.alpaca.markets/us/docs/account-activities)
  documents fill identity, order ID, quantity, price and execution time, but its
  TradeActivity properties do not promise a commission field. Non-trade cash
  events have a date and net amount; FEE and CFEE are distinct activity types.
- [Paper Trading](https://docs.alpaca.markets/us/docs/paper-trading)
  explicitly excludes regulatory fees and latency-related slippage from the
  simulation. Missing charges cannot establish observed zero costs or live-cost realism.
- [List Account Documents](https://docs.alpaca.markets/us/reference/getdocsforaccount)
  documents `GET /v1/accounts/{account_id}/documents` on the **Broker API**
  sandbox host, using Basic authentication. It lists both `trade_confirmation`
  and `trade_confirmation_json` document types, with inclusive start/end date filters.
- [Download Account Document](https://docs.alpaca.markets/us/v1.4.2/reference/downloaddocfromaccount)
  documents the Broker API download endpoint and a redirect to a signed PDF URL.
  Its JSON accept-header statement concerns monthly statements; do not infer a
  confirmation JSON schema from that statement.

Inference: the documented Broker API documents endpoint is not a supported
drop-in endpoint for the existing paper Trading API client. These sources do
not establish document availability or entitlement for this exact paper account.
They also do not establish that a confirmation provides individual execution
IDs, rather than order-level aggregates. Do not invent either capability.

## Current implementation

All paths below are relative to `artifacts/api-server/backend/`.

| File / boundary | Finding |
| --- | --- |
| `app/services/stock_paper_ledger.py`, `AlpacaPaperClient._activity_pages` | Retrieves complete bounded activity pages and retains raw dictionaries. There is no commission-field stripping to fix. |
| Same file, `_upsert_fills` | Missing commission persists as `fee=None`, `cost_known=False`; exact later commission enrichment is accepted only while other fill evidence remains unchanged. |
| Same file, reconciliation | V2 cannot become `costs_known` or `accounting_verified` merely through commission enrichment; both complete-account proofs remain separate. |
| `app/services/alpaca_activity_v2.py` | Keeps date-only cash entries separate from execution timestamps, rejects invalid amounts, and blocks ambiguous mixed fill/account fee attribution. |
| `app/models/stock_paper.py` | Fill fee and raw activity storage exist; there is no dedicated confirmation source, document digest or per-component cost evidence model. |
| `app/services/paper_research_accounting.py` | Requires persisted fees to equal raw activity commissions. A document importer that directly edits fill fees would violate this independent check. |
| `app/services/alpaca_paper_cost_contract.py` | Zero commission is explicitly a research assumption; costs and launch remain unverified. |
| `app/services/paper_venue_qualification.py` | Complete commissions, precise times, pagination, delayed events and replay evidence remain required independently. |

No broker trade-confirmation ingestion implementation was found in application
services. The existing activity enrichment path can consume explicit broker
commissions **if actually returned**; it must not be fed locally fabricated
activity payloads derived from fee schedules, cash residuals or assumptions.

## Concrete ingestion proposal

Implement only after obtaining an authentic source sample and the source's
account/environment entitlement. No speculative transport or schema is needed
to preserve the current truthful unknown state.

1. Add a separate append-only confirmation evidence store. Retain original bytes
   in restricted storage, content SHA-256, broker document ID, account/environment
   binding, document period, retrieval time, source endpoint or manual-upload
   provenance, parser version, reviewer, and correction/supersession links.
   A hash proves content stability, not broker authenticity. Manually supplied
   documents remain unverified until their origin and account binding are checked.
2. Use a separate read-only document client if Broker API access is established.
   Never redirect existing paper credentials to Broker API or a download host.
   Follow signed downloads without forwarding authentication, bound size/time,
   and record source completeness and unavailable/pending documents explicitly.
   Prefer the documented JSON document type only once a real payload establishes
   its schema; otherwise preserve PDFs and review extraction against the source.
3. Store cost components separately: commission, regulatory fee, other fee,
   currency, amount, evidence scope and explicit unknown/zero/value status.
   Match exact account, environment, execution ID, order ID and economic fields.
   An order-level amount stays order-level unless the broker supplies an
   unambiguous allocation. Never prorate across partial fills to manufacture
   observed per-fill evidence. Missing fields remain unknown; reject nonfinite
   amounts and route corrections/rebates to explicit review.
4. Resolve document evidence through a new provenance-aware read model rather
   than overwriting `StockPaperBrokerActivity.raw_payload` or adding a fake
   `commission` to it. Preserve the raw journal and its digest. Conflicts with
   activity-reported commission require review; identical reimports are idempotent.
5. Link any account-level FEE/CFEE entry to its documented component before
   considering attribution complete. A confirmation explains a charge; it does
   not book another cash debit. Reject ambiguous overlap and require exact cash
   and inventory reconciliation, complete date coverage and delayed/corrected
   document capture. Do not use fee schedules or residuals as reported costs.
6. Update independent accounting assessment to verify this evidence read model
   only after its versioned contract and tests exist. Verified commission alone
   must not imply all-in cost verification. Spread/slippage needs separately
   defined market-reference evidence and remains unknown otherwise. Preserve
   current recovery, qualification, activation, strategy/risk and kill-switch gates.

Required importer tests: explicit zero versus absent/null; wrong account or
environment; duplicate/conflicting document; changed execution; order aggregate
with multiple fills; unsupported currency; malformed/nonfinite amount; revised
document; incomplete document coverage; account-fee overlap; lost source bytes;
restart replay; unchanged activity digest; and no change to trading authority.

## Focused verification

From `artifacts/api-server/backend`, using the repository `.venv/bin/python`:

```sh
PYTHONPATH=. /Users/ainz_ool_gown/Documents/'Trading App'/.venv/bin/python -m pytest -q \
  tests/test_alpaca_paper_cost_timestamp_contract.py \
  tests/test_alpaca_paper_cost_contract.py \
  tests/test_alpaca_activity_v2.py \
  tests/test_alpaca_activity_ledger.py \
  tests/test_paper_research_accounting.py \
  tests/test_alpaca_activity_pagination.py \
  tests/test_stock_paper_ledger.py
```

The first audit run reported **166 passed, 1 failed, 6 subtests passed**. The
failure was the legacy cash-event temporal handling issue described below.
That defect was corrected so the watermark applies only to execution fills;
date-only cash activities use their effective/settlement date. The rerun
produced **167 passed, 6 subtests passed**, with two dependency warnings, in
17.09 seconds.

Previously observed failure:
`tests/test_alpaca_activity_ledger.py::test_legacy_review_halt_reclassifies_imported_cash_activity_after_exact_match`
expected `reconciled` but received `halted`. An isolated reproduction
confirmed the halt reason: `Late broker fill predates the reconciliation
watermark; full accounting reconstruction is required`. The legacy equation
was comparing a date-only 2026-10-07 FEE against the precise reconciliation
watermark. The fix is limited to excluding date-only cash activities from that
execution watermark comparison; V2 date-only cash reconciliation tests also
pass. This is distinct from missing per-fill cost evidence.

## External blocker and next evidence

Need an authentic, account-bound paper confirmation/export or an officially
supported paper API response containing explicit costs and adequate execution
linkage, plus a documented way to retrieve complete and corrected evidence.
Broker API documentation alone does not supply that evidence. A provider/support
answer must establish whether this particular Trading API paper account has
such documents, how they are retrieved, and which costs are actually represented.
No support message was sent. No live account, new order or provider switch is
proposed as a workaround. If Alpaca cannot supply this source, the unchanged
per-fill/all-in cost requirement remains unsatisfied; modeled paper research can
continue to describe assumptions only within its existing authorization contract.

A read-only request to the documented Broker API sandbox document endpoint was
also tested on October 7 using the configured paper Trading API key pair and
the reconciled account binding. It returned HTTP `401 unauthorized`. No order
endpoint was called. This confirms that the current credentials do not provide
Broker API document access; it does not prove that the paper account has no
documents. A Broker API credential/account entitlement or an authenticated
broker-provided confirmation export is still required.
