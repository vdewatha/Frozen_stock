---
name: Exact one-session paper bounds
description: Durable rules for configuring and enforcing a governed paper session.
---

Cycle-owned one-session paper trials must carry an explicit regular-session date, America/New_York start and end timestamps, exact approved symbol universe, numeric exposure and loss caps, and immutable stop/order/position policies. Research-only symbols must not be restored to execution merely because they appeared in an older approval. Reservation and dispatch must re-read that durable approval so a missed worker deadline cannot authorize late entries. At final dispatch, pending trial notional is scoped by symbol for the symbol cap and across the full trial for the aggregate cap; cap denials persist the approved bound and observed exposure as ledger evidence before any broker request.

**Why:** An inferred multi-session default allowed approval, worker timing, and broker submission to disagree about the actual paper run. Execution scope can subsequently be narrowed when incomplete feeds must remain research-only.

**How to apply:** Treat the cycle’s configured bounds as a mutable pointer and each approval as an immutable snapshot; any changed bound must make the prior approval mismatch before a new approval can authorize work.