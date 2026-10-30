---
name: Paper attribution diagnostics
description: Why Overview holdings show Unattributed and how ownership diagnostics must stay descriptive
---

Unattributed holdings in the paper Overview are explained by persisted evidence classes
(imported external broker orders, missing order links, unowned app fills, quantity
mismatches, no fill history) that are descriptive only. No code path may choose an owner
from a diagnostic, from the latest strategy signal, or from a same-ticker lot.

**Why:** The October 2026 book held eight symbols whose every fill traced to broker orders
submitted outside the app and imported during reconciliation (bracket parents with held/new
child legs, no strategy link). Quantities reconciled exactly; only ownership was missing.
Assigning an owner would have fabricated strategy performance evidence.

**How to apply:** Any new attribution evidence must arrive via an explicit, reviewed record
(proposed in the diagnostic text, never performed). Keep the execution position-count gate's
fail-closed rule identical to the Overview rule, and keep accounting P/L masking independent
of attribution diagnostics.
