---
name: Automatic accounting recovery
description: The narrow evidence standard for clearing a stock-paper accounting residual automatically.
---

Automatic accounting recovery may clear a residual only when a later broker payload enriches an existing fill with a previously missing commission, the immutable fill fields still match, and that fee exactly offsets the persisted cash residual. Repeated account snapshots are never proof.

**Why:** A stable Alpaca balance does not explain how an earlier cash or inventory mismatch occurred, and approving it would allow an unexplained residual to resume paper execution.

**How to apply:** Keep recovery halted and notify an operator for unknown fees, unsupported account flows, stale timestamps, mismatches, open orders, or monitoring failures. After the accounting proof, still require the normal cooldown, fresh reconciliation, monitoring, and in-flight-order gates.

Active forward trials must re-check accounting safety at observation and pending-execution boundaries; a residual discovered after activation must pause the trial before any new paper submission.

**Why:** Resume-time validation cannot protect a trial from a later reconciliation failure, and an already-persisted qualifying decision must not bypass the newly unsafe ledger.

**How to apply:** Treat reconciliation-required, unexplained-residual, and lost accounting verification as fail-closed worker states, and persist only safe accounting-state flags in the trial audit evidence.