---
name: Scheduled cycle admission serialization
description: Why concurrent scheduled cycles defer around the singleton active paper-canary binding.
---

Only one scheduled cycle may own the active paper-canary binding at a time. When another scheduled cycle is admitted concurrently, serialize on lifecycle admission and leave the contender explicitly deferred until the active cycle resolves.

**Why:** Replacing the active binding while its originating cycle is still running lets two paper trials proceed while one points at an inactive canary, which breaks lifecycle and audit lineage.

**How to apply:** Preserve cycle-owned binding and trial source keys; a contention result must contain no cross-cycle binding or trial pointers and must be recorded as a retryable admission deferral.