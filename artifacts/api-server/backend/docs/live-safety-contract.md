# Live safety contract

This document defines the governance boundary for a future live-trading
executor. It does not enable live credentials, submit live orders, change the
paper campaign, or make a profitability claim. The current application remains
paper-only.

## State machine

The durable `live_safety_state` singleton records one of these modes:

| Mode | Meaning | Allowed next modes |
| --- | --- | --- |
| `research` | Model and feature work; no execution authority | `paper`, `shadow` |
| `paper` | Broker-shaped actions may use the governed paper ledger | `research`, `shadow` |
| `shadow` | Live-shaped decisions are observed without live order authority | `paper`, `canary-live` |
| `canary-live` | A separately approved, bounded live trial; every order remains subject to the live contract | `shadow`, `paper`, `approved-live`, `emergency-stop` |
| `approved-live` | The only state that could authorize a future live executor | `canary-live`, `paper`, `emergency-stop` |
| `emergency-stop` | Live authority is revoked pending explicit revalidation | `research`, `paper`, `shadow` |

Transitions are explicit, role-protected, reasoned, and append-only audited.
The emergency stop is only reachable from a live state. Returning from it
requires a non-empty revalidation evidence digest. A denied transition also
creates a durable safety event and an audit-chain entry.

There is no direct `research -> canary-live`, `paper -> approved-live`, or
`shadow -> approved-live` transition. `approved-live` requires a prior
`canary-live` state and two distinct approval actors.

## Non-negotiable activation gates

The shared `evaluate_live_safety()` decision must report `pass` for every gate
before a live transition or a live order can be authorized:

1. **Environment separation** — the process is in the explicitly named
   approved-live environment, live execution is configured there, and a named
   live broker is present. A boolean setting by itself is never authorization.
2. **Operator approval** — an explicit approval actor and timestamp are
   persisted. Approved-live additionally requires a distinct secondary actor.
3. **Broker/account verification** — account identity, trading mode, and broker
   permissions are verified by the isolated broker integration without
   exposing credentials in API responses or audit logs.
4. **Current data** — the decision feed and its exchange/ingestion timestamps
   are current and complete for the decision universe.
5. **Monitoring health** — the monitor is current, clear, and independent of
   the executor.
6. **Recovery readiness** — watchdog and monitor heartbeats are current, the
   recovery state is armed or explicitly resumable, and no accounting review
   is outstanding.
7. **Immutable model lineage** — the model, dataset, binding, feature policy,
   and decision evidence resolve to immutable hashes and the approved lineage.

Missing, stale, conflicting, or malformed evidence is `unknown` or `fail`,
never `pass`. The decision is blocked unless all gates pass and the durable
mode is `approved-live`. The current broker-account gate intentionally remains
unknown because this task does not add a live broker.

## Trust boundaries and threats

### Browser to API

The browser is an untrusted presentation and input surface. It may display
state and submit a reasoned transition request, but it cannot supply gate
proof, role credentials, model lineage, broker credentials, or order
authorization. The API must authenticate every non-health request, apply the
role matrix server-side, validate the target state, and ignore client-provided
approval claims that are not represented by the server-side transition.

Threats: forged role headers, replayed transition payloads, CSRF through a
browser session, hidden live action in a UI refresh, and stale cached
readiness. Mitigations: bearer role keys, explicit admin transition route,
append-only events, no implicit transitions, server-side reevaluation, and
read-only status endpoints.

### API to broker

Broker secrets are server-side only. The API must use an isolated live broker
adapter that verifies account mode before any submission and binds the
returned client/order identity to the approved lineage. An uncertain response
must halt and reconcile; it must never be retried blindly.

Threats: paper/live endpoint mix-up, wrong account, partial submission,
credential leakage, and treating a configuration flag as broker proof.
Mitigations: environment-separated endpoints and secrets, broker-sourced
account verification, unique client IDs, timeout uncertainty as a halt, and
the shared live order gate immediately before submission.

### API to database

The database stores state and evidence, not authorization by itself. The
singleton mode row is mutable only through the transition service. Safety event
rows and audit rows are append-only and digest-linked. Model, dataset, binding,
and readiness rows remain immutable according to their existing constraints.

Threats: direct row edits, stale reads during concurrent transitions, lineage
swaps, and deletion of denial evidence. Mitigations: singleton/check
constraints, transaction locks in transition implementations, immutable event
fences, hash comparisons, and fail-closed unknown results.

### Worker to API/database/broker

Workers are untrusted execution principals and may crash, duplicate, or lose
coordination. Every worker that could ever place a live order must call the
same live-order decision immediately before submission. A worker must not infer
authority from a cached mode, model row, paper binding, or task arguments.

Threats: stale task execution after demotion, duplicate retries, missing
heartbeats, and a worker bypassing the API. Mitigations: current transaction
checks, unique client IDs, watchdog independence, emergency stop, and
fail-closed coordination loss.

### Operator/admin

Operator and admin credentials identify service principals, not a complete
human approval workflow. Live activation requires recorded, distinct approval
actors and reviewable evidence. Emergency stop remains available to the
operator path and must not depend on model performance.

Threats: single-person error, credential sharing, social engineering, and
approval of stale evidence. Mitigations: least privilege, dual approval for
approved-live, bounded evidence timestamps, explicit reasons, audit review,
and automatic demotion/blocking on any gate regression.

## Launch checklist

No live launch may proceed until every item has a durable, reviewable answer:

- [ ] A separately deployed approved-live environment exists; paper
      credentials and endpoints cannot resolve there.
- [ ] The broker adapter proves live account identity, account mode, buying
      power, and order permissions without returning secrets.
- [ ] Operator and distinct secondary approvals are recorded with timestamps,
      reasons, and evidence digests.
- [ ] Current feed, session completeness, and ingestion latency are passing for
      every decision symbol.
- [ ] Independent monitoring and watchdog heartbeats are current and clear.
- [ ] Recovery is armed, accounting is reconciled, and rollback evidence is
      tested.
- [ ] The model, dataset, features, policy, binding, and canary decision
      evidence form one immutable lineage.
- [ ] The canary budget, symbols, session window, order limits, and stop
      conditions are written down and independently reviewed.
- [ ] Browser/API, worker, broker, and database access logs are available;
      denied transitions are visible to audit reviewers.
- [ ] An uncertain broker response has a tested halt-and-reconcile procedure.
- [ ] A dry run proves that stale data, missing monitoring, lost Redis/worker,
      lost watchdog, account mismatch, lineage mismatch, and revoked approval
      all block the live order gate.

## Rollback and evidence requirements

The emergency-stop transition is the first response to an account, data,
monitoring, lineage, or execution anomaly. It must record the actor, reason,
time, prior mode, current gates, and event/audit digests. No order retry occurs
while submission status is uncertain.

To resume, keep the state in `emergency-stop`, reconcile broker activity and
the ledger, resolve the root cause, and produce a new revalidation digest.
Return first to `paper` or `shadow`; do not jump directly to a live state.
Re-run every gate and obtain fresh approvals before a new canary. Any missing
or conflicting evidence leaves the state blocked.

The minimum retained evidence bundle contains:

- state transition and denied-transition event IDs and hashes;
- audit-chain IDs and hashes;
- gate snapshot, timestamp, and evidence digests;
- broker account verification and reconciliation identifiers;
- model/dataset/binding lineage hashes;
- monitor/watchdog heartbeat and recovery identifiers;
- order client IDs, broker IDs, and uncertainty/reconciliation outcomes.
