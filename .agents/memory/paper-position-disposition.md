---
name: Paper position disposition
description: How expired or operator-stopped paper trial positions should be represented.
---

An approved remaining-position policy describes the intended action, not proof that the action completed. Position status must keep the stop cause, durable lot exit intent, actual remaining broker positions, and reconciliation state distinct.

**Why:** A session can expire while a reduction or flattening order is still pending, and a dashboard must not imply that an intent was filled. Operators also need to see monitoring and reconciliation evidence while new entries are blocked.

**How to apply:** Derive the control-room disposition from immutable trial/lot stop evidence plus the latest read-only paper position snapshot. Use pending states until exits are observed, and keep missing reconciliation evidence unknown or requiring review.