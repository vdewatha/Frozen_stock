# Paper Execution Evidence Qualification — 2026-09-17

## Decision

**NO-GO — no available paper venue is qualified under the unchanged
accounting contract.**

This is a documentation and existing-evidence assessment. It is not a
credentialed broker probe, account certification, provider switch, account
initialization, reset, reconciliation, order, cancellation, or launch
authorization. No missing fee, timestamp, or activity evidence is inferred.

The contract remains:

- every cash-affecting activity must be attributable and replayable;
- fills must have immutable broker identity, order linkage, quantity, price,
  precise broker time, and explicit cost evidence;
- history must be complete across pagination, delayed activities, and session
  boundaries;
- restart reconciliation must be demonstrable without treating a missing
  response as an empty history;
- unknown costs and timestamps remain unknown and block recovery and launch.

## Capability matrix

| Requirement | Tradier sandbox | Alpaca paper | Interactive Brokers paper candidate |
| --- | --- | --- | --- |
| Explicit commissions and cash-affecting fees | **Fail for qualification.** Tradier's account-history documentation describes commission and fees for history, but also states history is only available for live accounts. The sandbox therefore cannot provide the required complete cost stream. | **Not established.** The activity API documents `FEE` activity types, but its documented fill shape does not promise a per-fill `commission` field. Alpaca also states that paper trading does not account for regulatory fees, dividends, or borrow fees. Missing paper costs cannot be relabeled as explicit zero costs. | **Partial documentation only.** Trade history and commission-report documentation expose commission fields, but no paper-account evidence or application adapter has been certified. |
| Precise immutable activity time | **Fail for qualification.** Tradier documents that account history does not include the specific hours and minutes when a position or order was created or closed; sandbox history is unavailable. | **Partial schema compatibility, not qualification.** FILL activities document `transaction_time`, but non-trade activities document `date`, which is not a precise instant. The observed account had no fills, so no live paper evidence exists for the required fields. | **Partial documentation only.** Trade history documents `trade_time`, but this is not a complete cash-affecting activity timeline. |
| Complete order/activity pagination | **Fail for sandbox.** The documented history endpoint is unavailable for sandbox accounts; the sandbox order list is current-session evidence rather than a complete historical cursor. | **Documented mechanism, implementation/evidence gap.** Account activities use the last activity ID as `page_token`. The current adapter expects a returned continuation token and must be corrected and tested before qualification. Empty history did not exercise a full-page boundary. | **Fail for the required historical scope.** The documented trade-history endpoint returns at most seven days of trades, not a complete account activity history. |
| Delayed activity and session-boundary coverage | **Fail.** Nightly live history cannot repair the unavailable sandbox history or provide intraday sandbox activity completeness. | **Not established.** The API exposes historical activities, but the paper simulation omits important live-market effects and no delayed fee, non-fill, or late-activity evidence was observed. | **Not established.** A seven-day trade view is not evidence for delayed non-trade activity or older missed sessions. |
| Restart reconciliation | **Not established.** Current-session reads and null-shaped sandbox history cannot prove what happened during downtime or between sessions. | **Not established.** Two fresh-client reads of an empty account are insufficient; no historical execution, fee, outage, or late-activity replay was exercised. | **Not established.** No adapter, durable activity capture, paper-account replay, or restart test exists in this project. |

## Evidence status by venue

### Tradier sandbox

**Documented capability:** balances, positions, current orders, and order
details are available through the sandbox account endpoints. The account
history endpoint documents historical events and commission fields for live
accounts only.

**Observed qualification:** the existing read-only assessment recorded valid
account/position/current-order shapes but JSON-null history responses. The
application correctly keeps `evidence_complete=false` and blocks
reconciliation.

**Implementation readiness:** no adapter change can manufacture unavailable
sandbox history, missing order numbers, or missing precise times. Tradier
production market data must remain separate from any paper execution decision.

**Launch authorization:** none. A Tradier sandbox switch or initialization
would remain blocked.

### Alpaca paper

**Documented capability:** account activities provide stable IDs and
ID-based `page_token` pagination. FILL activities document `transaction_time`
and `order_id`; other activity types may document only `date`. The paper
environment is a simulation and explicitly omits regulatory fees, dividends,
and borrow fees.

**Observed qualification:** the existing read-only assessment found an empty
order history and one non-fill activity, with no fills from which commissions,
timestamps, or order linkage could be verified. The existing focused contract
assessment correctly reports costs and precise activity times as unknown.

**Implementation readiness:** the activity adapter now uses the documented
last-activity-ID cursor after a full page, de-duplicates repeated boundary
rows, rejects missing/repeated/non-advancing cursors, and stops at a bounded
page limit. Order pagination remains on its separate documented `until`
timestamp cursor. Offline contract tests pass; this is implementation
readiness only, not venue qualification. Separate fee activity mapping would
still not solve the paper simulation's documented omission of real-world
costs. A provider-specific contract change would be required to accept modeled
or omitted costs, and that is outside this task and the current accounting
contract.

**Launch authorization:** none. Even a corrected adapter and positive
field-level fixture would provide implementation readiness only, not venue
qualification or permission to switch providers.

### Interactive Brokers paper candidate

**Documented capability:** the Web API trade-history endpoint documents
precise UTC `trade_time` and commission fields, and the TWS commission report
documents execution commission data.

**Observed qualification:** none in this project. The Web API documentation
limits trade history to seven days, and the available evidence does not show a
complete paper-account activity stream for fees, transfers, positions, and
late events. It cannot currently satisfy the full contract.

**Implementation readiness:** this would be a new provider boundary, not a
configuration toggle. It would require an approved paper account, a separate
adapter and durable schemas for orders, executions, commission reports,
cash-affecting activities, and cursor/replay state, plus an evidence plan that
starts before any autonomous run. No connection, credentials, or test order is
authorized by this assessment.

**Launch authorization:** none.

## Required provider-specific decisions

1. **Keep the current no-go.** Continue using Tradier production only as a
   separately governed market-data source; do not treat it as a solution to
   the sandbox accounting-history gap.
2. **If Alpaca is reconsidered,** obtain explicit approval for a credentialed
read-only evidence operation using the corrected activity adapter, followed by
independent recovery tests. The operation must produce non-empty fills and
cash-affecting activities with explicit costs and precise broker times, and
must demonstrate delayed-activity and restart recovery. No test orders are
authorized by this report.
3. **If IBKR is selected as an alternative,** obtain separate approval for a
   new paper-provider integration and define how the seven-day trade-history
   limit is supplemented by durable activity capture. Do not claim that the
   documented commission field alone proves complete accounting.
4. **If the user wants modeled-cost research instead,** that requires a
   separately approved contract and explicit nonqualifying labeling. It cannot
   clear the current broker-accounting gate or authorize autonomous paper
   orders.

## Final boundary

The distinction is deliberate:

- **Documented capability** is what a provider says an endpoint can return.
- **Observed qualification** requires redacted, account-specific evidence
  satisfying every contract field and completeness condition.
- **Implementation readiness** means the adapter can preserve that evidence
  fail-closed; it is not proof that the provider supplies it.
- **Launch authorization** requires separate explicit approval and all other
  readiness, risk, feed, recovery, notification, audit, and model gates.

This assessment establishes none of the last two for a broker. The downstream
bounded paper-learning task must remain blocked until a venue is actually
qualified and the separate approval flow is complete.

## Official sources

- https://docs.tradier.com/reference/brokerage-api-accounts-get-account-history
- https://docs.tradier.com/docs/account-details
- https://docs.alpaca.markets/us/docs/account-activities.md
- https://docs.alpaca.markets/us/docs/paper-trading.md
- https://www.interactivebrokers.com/docs/web-api/api-reference/trading/trading-orders/get-trade-history.md
- https://www.interactivebrokers.com/docs/tws-api/doc/order-management/commission-and-fees-report

## Existing project evidence

- `reports/tradier-paper-launch-evidence-20260916.md`
- `reports/alpaca-paper-candidate-evidence-20260916.md`
- `reports/alpaca-paper-cost-timestamp-qualification-20260917.md`
- `app/services/stock_paper_ledger.py`
