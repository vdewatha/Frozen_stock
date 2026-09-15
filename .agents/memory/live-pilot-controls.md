---
name: Controlled live pilot
description: Durable rules for moving from validated paper operation to narrowly scoped live exposure.
---

The live pilot is a separate, persisted authorization boundary: a live broker account and generic safety approval are necessary but never sufficient. Activation must bind an immutable paper model, point-in-time evidence, a bounded allowlist/budget/window, and two attributable approvers; order reservation and dispatch must recheck those controls.

**Why:** Unrestricted live trading must remain impossible by default, and a favorable short sample must not silently change the model or capital budget.

**How to apply:** Keep pilot status, limits, approvals, checklist evidence, review results, and rollback target durable and auditable. Treat expiry, broker/data uncertainty, monitoring failure, model demotion, worker loss, and emergency-stop as fail-closed conditions.