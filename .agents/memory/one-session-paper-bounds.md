---
name: Exact one-session paper bounds
description: Durable rules for configuring and enforcing a governed paper session.
---

Cycle-owned one-session paper trials must carry an explicit regular-session date, America/New_York start and end timestamps, exact AAPL/MSFT/QQQ/SPY universe, numeric exposure and loss caps, and immutable stop/order/position policies. Reservation and dispatch must re-read that durable approval so a missed worker deadline cannot authorize late entries.

**Why:** An inferred multi-session default allowed approval, worker timing, and broker submission to disagree about the actual paper run.

**How to apply:** Treat the cycle’s configured bounds as a mutable pointer and each approval as an immutable snapshot; any changed bound must make the prior approval mismatch before a new approval can authorize work.