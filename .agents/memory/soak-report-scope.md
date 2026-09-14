---
name: Bounded soak report scope
description: Isolated soak reports must preserve cycle lineage while scoping ancillary evidence to the soak window.
---

Use the isolated schema's cycle rows as the authoritative lineage scope, even when a schema is reused for a retry. Scope ancillary monitoring and recovery rows to the soak window plus any identifiers directly linked to those cycles; timestamp-only filtering can hide deduplicated cycle lineage when database and application clocks differ.

**Why:** A bounded run can reuse a disposable schema after an interrupted attempt, and database timestamps may precede the launcher timestamp. Filtering cycles only by `since` produced an apparently empty soak report despite durable cycle rows.

**How to apply:** Keep cycle identifiers and cycle-owned events complete, use explicit window/link filters for high-volume monitoring and recovery evidence, and report incomplete forward evidence rather than inferring readiness from missing rows.