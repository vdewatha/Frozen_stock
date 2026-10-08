---
name: Automatic accounting recovery
description: The narrow evidence standard for clearing a stock-paper accounting residual automatically.
---

Automatic accounting recovery may clear a residual only when a later broker payload enriches an existing fill with a previously missing commission, the immutable fill fields still match, and that fee exactly offsets the persisted cash residual. Repeated account snapshots are never proof.

**Why:** A stable Alpaca balance does not explain how an earlier cash or inventory mismatch occurred, and approving it would allow an unexplained residual to resume paper execution.

**How to apply:** Keep recovery halted and notify an operator for unknown fees, unsupported account flows, stale timestamps, mismatches, open orders, or monitoring failures. After the accounting proof, still require the normal cooldown, fresh reconciliation, monitoring, and in-flight-order gates.

When evaluating a halted account's research-policy recovery evidence, residual safety comes from the active paper account, not from an execution-policy summary. A missing summary field must never be treated as proof that no residual exists.

**Why:** The execution-policy summary does not carry the account's unexplained-residual flag; interpreting an omitted field as false could admit unsafe recovery.

**How to apply:** Require an active account and consult its residual evidence alongside the independent policy, reconciliation, and venue-authorization gates.

A fresh broker reconciliation can legitimately coexist with a durable monitoring halt. A halted account may pass the reconciliation prerequisite only when its broker evidence is fresh and reconciliation is no longer required; this does not clear the halt or bypass the remaining recovery gates.

**Why:** Successful reconciliation updates evidence without erasing the independent monitoring incident. Requiring the account to be unhalted before explicit recovery creates a circular dependency. The halt itself can also cause transitional broker-health and reconciliation monitoring breaches; these must not be mistaken for independent failures once fresh, residual-free reconciliation is proven.

**How to apply:** Evaluate freshness and the reconciliation-required flag independently from the durable halt, rejecting stale, missing, or future-dated evidence and preserving operator revalidation, cooldown, monitoring, and residual checks. Any monitoring exception is limited to those two transitional broker checks with fresh, residual-free reconciliation and reconciliation-required=false; all other breaches remain blocking.

Active forward trials must re-check accounting safety at observation and pending-execution boundaries; a residual discovered after activation must pause the trial before any new paper submission.

**Why:** Resume-time validation cannot protect a trial from a later reconciliation failure, and an already-persisted qualifying decision must not bypass the newly unsafe ledger.

**How to apply:** Treat reconciliation-required, unexplained-residual, and lost accounting verification as fail-closed worker states, and persist only safe accounting-state flags in the trial audit evidence.