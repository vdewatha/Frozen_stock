---
name: Immutable model registry lifecycle
description: The stock model registry is append-only, so lifecycle state and active binding ownership must be mutable companion records.
---

Stock model registry rows are protected by database immutability triggers. Champion/challenger lifecycle state must therefore live in a separate current-state table, while lifecycle events and binding activations remain append-only audit evidence.

**Why:** A migration that backfilled an existing registry row by updating it was rejected by the production trigger and prevented the API from starting.

**How to apply:** When adding mutable governance to immutable stock artifacts, use companion current-state/pointer tables with database uniqueness and record every transition as a separate event. Never update or delete the registry row.