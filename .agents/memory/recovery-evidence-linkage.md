---
name: Recovery evidence linkage
description: How persistent monitoring pauses and model demotions are represented in cycle observability.
---

Persistent monitor actions can create a low-level recovery event named `pause`; cycle observability must treat that event as a recovery decision and attach both the monitor snapshot and recovery event to the affected cycle.

**Why:** The active paper-binding pointer is intentionally removed when a champion is demoted so paper execution cannot continue. The immutable binding row and recovery state's last-known-good identifiers remain available for an explicit rollback.

**How to apply:** When adding monitor actions, return the durable recovery-event id to both scheduled and manual orchestration paths. Keep dashboard reads read-only, and require an explicit actor and non-empty reason for revalidation and rollback.