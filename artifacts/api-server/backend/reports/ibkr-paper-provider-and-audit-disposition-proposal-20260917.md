# IBKR Paper Provider and Audit Disposition Proposal — 2026-09-17

## Decision status

**DISCOVERY COMPLETE — NO PROVIDER SWITCH, ACCOUNT SETUP, ORDER, OR AUDIT
CLEARANCE AUTHORIZED.**

This proposal is the result of the selected IBKR paper-integration discovery
and reviewer-disposition preparation scope. It does not establish an IBKR
connection, create or initialize an account, change the active paper broker,
submit or cancel an order, import a balance, resolve a notification, alter the
kill switch, or clear the historical audit incident.

The current launch decision remains **NO-GO**. The existing Tradier sandbox
route is not qualified under the unchanged accounting contract, Alpaca has not
been qualified from account-specific execution evidence, and IBKR is not yet an
implemented or tested provider boundary.

## 1. Discovery result

### Replit integration availability

The available integration directory was searched for Interactive Brokers,
IBKR, and brokerage paper-trading connections. No IBKR connector or existing
IBKR connection is available in this environment. The results did not provide
an integration that can be attached for this purpose.

The existing application boundary also confirms that IBKR cannot be selected
as a configuration-only change:

- `active_paper_broker` currently accepts only `tradier_sandbox` and
  `alpaca_paper`;
- account lookup and gateway selection are implemented only for those two
  venues;
- no IBKR credential pair, account binding, execution adapter, durable
  activity importer, or restart replay state exists;
- changing the setting without a new boundary would either fail validation or
  bypass the accounting contract.

No credentials were requested or read, and no provider configuration was
changed.

### Official IBKR capability that was verified

The official documentation supports these limited conclusions:

1. **Web API trade history is not a complete historical ledger.** The trade
   history endpoint exposes execution identifiers, UTC `trade_time`, order
   identifiers/order references, quantities, prices, and a commission field,
   but limits the requested lookback to a maximum of seven days.
2. **TWS API execution and commission callbacks provide useful execution
   evidence.** The execution API and commission report expose execution
   identity and commission information. The documentation also describes
   shorter execution visibility for IB Gateway, so a callback stream cannot be
   treated as durable history unless the application captures it continuously
   and proves restart recovery.
3. **Flex Web Service can retrieve configured reports.** Flex queries are
   created in Client Portal and retrieved through an API. IBKR reporting
   documentation describes activity statements as covering account activity
   including cash transactions, dividends, corporate actions, and trades.
   Flex report references also expose trade and cash-transaction sections,
   including trade IDs, transaction amounts, types, and date/time fields.
4. **Paper behavior is not identical to live behavior.** IBKR documents that
   paper trading uses additional simulated technologies and that order
   execution behavior may vary. Availability and field completeness of the
   required Flex reports for the specific paper account remain unverified.

Therefore, IBKR documentation makes a possible evidence architecture
plausible, but does **not** qualify an IBKR paper account for this project.
The seven-day Web API result and an in-memory TWS/IB Gateway stream alone are
insufficient.

## 2. Proposed IBKR evidence boundary

The recommended design is a new provider boundary with two independently
captured evidence paths:

### A. Execution-time capture

Use one approved IBKR execution channel, to be selected only after the
operator confirms where its gateway/session will run and how it will be
supervised:

- Client Portal Web API, or
- TWS/IB Gateway API with a documented, restartable process boundary.

Capture each returned execution and commission report durably before the
execution is used by reconciliation or learning. Each record must retain
provider identity, account binding, order ID or client order reference,
execution ID, symbol/contract identity, side, quantity, price, precise
provider time, currency, and explicit commission/cost state.

An unavailable callback, expired session, or missing field must be recorded as
**unknown** and must block reconciliation. It must not be converted into an
empty result or zero cost.

### B. Historical and cash-activity reconciliation

Configure a paper-account Flex Activity Query and, if needed, a Trade
Confirmation Query that can be generated and retrieved without relying on the
seven-day trade-history endpoint. The evidence plan must request and retain,
at minimum:

- trades and trade IDs;
- commissions and other explicit execution costs;
- cash transactions and their type, amount, currency, and provider time;
- dividends, corporate actions, and other cash-affecting events applicable to
  the account;
- account identity and statement/report period;
- report generation and retrieval references;
- a content hash and capture timestamp for every imported report.

The application must not assume that a report is complete merely because it
was returned. It must validate period boundaries, account identity, required
sections, duplicate IDs, ordering, and whether the report's period overlaps
the last durable capture.

### C. Replay and restart contract

The adapter should persist provider cursor/report state and the last
successful evidence boundary. A restart test must demonstrate that:

1. an execution received before restart is not duplicated;
2. an execution or cash activity created during downtime is recovered;
3. delayed commissions or other late activities are attached to the correct
   immutable execution/activity;
4. an incomplete report or session response fails closed rather than erasing
   prior evidence;
5. the next reconciliation can identify the exact unresolved period.

This requires durable schemas for provider accounts, orders, executions,
commission records, cash activities, report captures, and replay cursors.
Those schemas do not exist yet and must not be approximated by the current
Tradier/Alpaca tables without an explicit design review.

## 3. Feasible arrangement and dependencies

The route is feasible only if all of the following are confirmed before
implementation:

1. An IBKR paper account exists and its account identity can be bound
   explicitly. IBKR's own paper-account prerequisites and the account's
   reporting/API availability must be confirmed by the account owner.
2. The operator chooses an execution connectivity arrangement and confirms
   its hosting, session renewal, supervision, and restart behavior. This
   proposal assumes no unapproved always-on gateway host.
3. The account can create the required Flex queries and retrieve reports with
   sufficient paper-account coverage. If Flex reports omit required paper
   activity or cost fields, the route remains unqualified.
4. Secure server-side credential/session delivery is approved. There is no
   IBKR Replit integration to provide this boundary automatically; credentials
   must not be pasted into chat or embedded in source.
5. A separate authorization is issued for bounded qualification activity.
   Discovery approval did not authorize a test order. Non-empty fill evidence
   cannot be produced without an explicitly approved qualification action.
6. The provider contract is reviewed and versioned before any active-provider
   setting can accept `ibkr_paper`.

### Proposed qualification sequence

This is a future qualification plan, not an authorization to execute it:

1. Add the provider boundary in an isolated environment with live trading
   permanently disabled.
2. Perform read-only account identity, session, report-template, and empty
   baseline checks.
3. With separate written approval, run a bounded paper qualification that
   creates the minimum non-empty execution and cash evidence needed by the
   contract. Record exact bounds and stop conditions before dispatch.
4. Exercise delayed-report, pagination/period-boundary, session-expiry, and
   restart recovery paths.
5. Reconcile the execution stream against Flex reports and account balances;
   leave any missing fee, timestamp, cash event, or coverage boundary unknown.
6. Produce an account-specific redacted qualification package for independent
   review. Only an affirmative package can be considered for a later provider
   switch approval.

Until step 6 passes, the application must keep the current venue unchanged and
the downstream autonomous paper-learning task blocked.

## 4. Proposed reviewer disposition for the historical audit fork

### Proposed posture

**Acknowledge and contain the historical breach; do not mark the audit chain
clear.**

The first broken retained row and its incident fingerprint must remain
immutable. The existing quarantine decision is evidence that the incident was
detected and contained; it is not evidence that the historical chain verifies.
The notification must not be resolved merely because a reviewer has read it.

### Required disposition record

Before any implementation or application of this policy, an authorized
reviewer should approve an append-only disposition containing:

- the audit incident fingerprint;
- first broken row ID and preceding row ID;
- expected and observed predecessor/event digests;
- the exact retained-history boundary;
- the reviewer identity and reviewer principal type;
- the trust policy selected for post-boundary events;
- the evidence reviewed and its capture time;
- the decision reason and expiration/review date;
- an explicit statement that original audit rows were not rewritten;
- an explicit statement that live trading remains disabled and this
  disposition does not authorize paper activation.

The reviewer must be independent of the actor who created the quarantine
decision. The supplied scope did not identify a reviewer, so no disposition
is ready to apply.

### Trust policy options for reviewer selection

The reviewer should choose one of these explicit policies:

| Policy | Effect | Launch consequence |
| --- | --- | --- |
| **Retain full block** | Treat the complete audit history as untrusted until a separately governed repair/rebuild is completed. | No audit-based launch eligibility. |
| **Split trust domains** | Preserve the breached prefix as historically breached; separately validate events after an explicit reviewed boundary with a new trust anchor that does not rewrite old rows. | Only a future policy may decide whether this is sufficient; it does not clear the full chain automatically. |
| **Reject disposition** | Do not accept a trust exception; continue quarantine and require a new evidence/review package. | No audit-based launch eligibility. |

The recommended selection is **Split trust domains with full launch block
until the policy is implemented, tested, and accepted by the launch
governance owner**. This preserves the truthful historical result while
allowing a future implementation to distinguish current post-incident
evidence from the breached prefix. It must not be implemented as a silent
re-anchor or by changing the existing digest fields.

### Required implementation guardrails if approved later

An approved implementation must:

1. preserve every original audit row and original digest;
2. retain the original `historical_fork` classification and fingerprint;
3. add an append-only disposition/anchor record rather than mutating the
   incident;
4. verify the post-boundary segment separately and report its boundary and
   trust policy;
5. require the authorized reviewer identity at write time;
6. make repeated application idempotent for the same incident fingerprint and
   policy version;
7. keep the full audit check non-clear unless the configured trust policy
   explicitly permits a split result;
8. keep paper activation, live trading, account recovery, and notification
   closure as separate gates.

The current operational-hardening code has quarantine behavior and separates
runtime health from the audit result, but it has no approved audit-disposition
workflow for these additional trust policies. The existing paper-graduation
review record is not a substitute: it governs a paper evidence package, not
the integrity status of the platform audit chain.

## 5. Current blockers and ownership

| Blocker | Current state | Owner/action needed |
| --- | --- | --- |
| IBKR account and reporting arrangement | Not established | Account owner confirms paper-account and report availability. |
| IBKR secure connectivity | No connector or adapter | Operator approves hosting/session boundary; engineering designs adapter. |
| IBKR evidence qualification | No account-specific evidence | Separate qualification authorization, then bounded evidence run. |
| Active provider switch | Not authorized | Requires affirmative qualified evidence and separate switch approval. |
| Historical audit disposition | Proposal only; no reviewer supplied | Authorized reviewer selects and signs a trust policy. |
| Main-environment accounting/recovery | Still blocked | Separate operator/accounting review; no reset or automatic clearance. |
| Four-symbol current feed and notifications | Existing launch blockers remain | Resolve only through their own evidence-backed workflows. |
| Downstream paper-learning session | Blocked | Remains pending until all gates independently pass. |

## Sources

Official IBKR documentation reviewed:

- [Trade History](https://www.interactivebrokers.com/docs/web-api/api-reference/trading/trading-orders/get-trade-history)
- [TWS API: Commission and Fees Report](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/commission-and-fees-report)
- [TWS API: Execution Details](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/execution-details/introduction)
- [TWS API: Paper Trading limitations](https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/limitations/paper-trading)
- [Flex Web Service](https://www.interactivebrokers.com/docs/web-api/flex-web-service)
- [IBKR reporting overview](https://www.interactivebrokers.com/docs/web-api/account-management/reporting/introduction)
- [Flex statement: trades](https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm)
- [Flex statement: cash transactions](https://www.ibkrguides.com/reportingreference/reportguide/cash%20transactionsfq.htm)

Existing project evidence:

- `reports/paper-execution-evidence-qualification-20260917.md`
- `reports/paper-launch-readiness-20260917.json`
- `app/core/config.py`
- `app/services/stock_paper_ledger.py`
- `app/services/operational_hardening.py`
- `app/services/stock_paper_graduation.py`
