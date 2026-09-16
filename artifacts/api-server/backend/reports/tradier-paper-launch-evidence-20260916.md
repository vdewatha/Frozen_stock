# Tradier Paper Launch Evidence — 2026-09-16

## Recommendation

**No-go. Keep the autonomous paper-learning loop disabled.**

The active paper venue is the Tradier sandbox. Its balance, position, and current-session order endpoints are readable, but the sandbox does not provide the complete historical activity evidence required by the ledger. The application therefore keeps `evidence_complete` false and rejects broker snapshots before initialization or reconciliation can establish a qualifying ledger.

This review did not place or cancel an order, initialize the account, reset an account, switch providers, delete history, waive costs, or enable live trading.

## Capability result

| Evidence required | Result | Finding |
| --- | --- | --- |
| Balances | Partial | Read-only balances returned a structurally valid USD response. Cash and broker update timestamp were not present in the normalized response. |
| Positions | Partial | Read-only positions returned a valid empty collection. |
| Orders | Incomplete | The current-session order list returned an empty collection; it is not a historical cursor. |
| Fills | Incomplete | Explicit execution rows can be inspected only from the incomplete current-session order view. |
| Activities/history | Fail | Official documentation says account history is unavailable for sandbox accounts. The fresh sandbox history response was JSON `null`. |
| Costs | Fail | Complete commissions, fees, transfers, and other cash-affecting activity cannot be proven; costs remain unknown. |
| Timestamps/identity | Incomplete | Current order identifiers and timestamps do not establish a complete immutable activity timeline. |
| Restart/missed-session recovery | Fail | Local capture plus current-session reads cannot prove what happened while the process or market session was unavailable. |

### Official sources verified

- [Account history endpoint](https://docs.tradier.com/reference/brokerage-api-accounts-get-account-history): history is nightly, lacks precise creation/closure times and order numbers, and is only available for live accounts.
- [Account details](https://docs.tradier.com/docs/account-details): confirms the sandbox base URL, account endpoints, and that account history is not presently available for sandbox/paper accounts. Gain/loss is nightly batch data and cannot replace a complete activity ledger.

The direct read-only probes also returned JSON `null` for transaction history, balance history, and gain/loss. An HTTP-success response with no collection is not treated as complete evidence.

## Fresh launch checklist

| Check | Status |
| --- | --- |
| Tradier provider boundary and separate credential configuration | Ready |
| Complete Tradier sandbox broker evidence | **Blocked** |
| Paper account initialization | **Blocked — uninitialized** |
| Accounting verification and known costs | **Blocked** |
| Tradier production intraday entitlement/completeness | **Blocked** — entitlement unverified and two completed intervals unresolved for each of four active symbols at observation time |
| Risk state | **Blocked** — global kill switch enabled |
| Critical notifications | **Blocked** — four unresolved critical notifications |
| Audit integrity | **Blocked** — retained historical audit-chain fork; rows preserved |
| Recovery | **Blocked** — cooldown and stale-watchdog pause reason; fresh reconciliation required |
| Model freshness | Warning — latest trusted stored prediction about 139.56 hours old |
| Strategy availability | Ready — one active strategy and two candidates |
| Workers/scheduler | Ready — two workers and exactly one beat lease/heartbeat |
| Live safety | Ready — live order placement disabled |

The generic readiness snapshot was `blocked` with three blocked checks, four ready checks, and one warning. Its generic broker-safety check describes the legacy paper-only routing contract; the stock-paper ledger's active Tradier evidence gate remains the accounting authority and is independently blocked.

## Existing autonomous sequence

Before any authorized start, the system requires a verified decision-session feed, healthy worker/scheduler evidence, a complete reconciled broker ledger, trusted dataset provenance, readiness history, and clear risk/notification/audit/recovery gates.

After an authorized start, the existing jobs create governed training evidence, hand off an approved model to a forward paper trial, collect and reconcile outcomes, compare challengers with the holdout protections, and permit only paper-only promotion. That sequence does not grant live authority. The twenty-session graduation campaign is outside this review.

## Alternatives requiring approval

1. Switch to a paper venue that can provide complete paginated orders, fills, activities, costs, stable timestamps, and restart recovery evidence, then rerun the same gates and tests.
2. Continue the existing local research simulator only with explicit nonqualifying labeling; it cannot prove broker-paper accounting or enable live trading.
3. Continue Tradier production market-data verification separately; better feed evidence cannot solve the sandbox accounting-history gap.