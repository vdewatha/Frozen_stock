---
name: Live broker isolation
description: Durable safety rules for introducing live execution beside the paper broker.
---

Live execution must use a separately configured provider boundary and its own
durable account, order, activity, fill, and snapshot records. Configuration
never authorizes execution: a live order must pass the shared safety contract,
actor authorization, current-data gate, account reconciliation gate, and
order-specific risk checks at both reservation and dispatch.

**Why:** Paper evidence and live provider truth have different operational and
financial consequences. Reusing paper records or retrying an ambiguous POST
could turn a research or recovery failure into an untracked live position.

**How to apply:** Keep live credentials, endpoints, routes, and migrations
separate. Commit order intent before one submission attempt; on timeout,
unknown status, incomplete import, changed immutable broker fields, or missing
external activity, halt and reconcile rather than retrying blindly.