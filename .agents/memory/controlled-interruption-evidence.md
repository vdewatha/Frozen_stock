---
name: Controlled interruption evidence
description: Operational constraints for proving fail-closed behavior during an isolated paper soak.
---

The interruption matrix is only valid when feed, ledger, worker, beat-lease, and Redis actions have each been observed, their durable recovery/audit projections are present, and duplicate lineage checks pass. A Redis or beat interruption can tear down the monolithic local stack; restore the disposable stack before taking the final health snapshot.

**Why:** A normal deferred smoke can look safe while never exercising coordination loss, and the local stack intentionally shuts down when its Redis or beat child exits.

**How to apply:** Keep matrix entries fixed and fail readiness closed when any entry or evidence projection is missing. Treat forward-evidence completion separately from runtime interruption safety.