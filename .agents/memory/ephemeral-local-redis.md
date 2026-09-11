---
name: Ephemeral local Redis
description: Project constraint for Redis topology and persistence.
---

Run Redis locally at `redis://localhost:6379/0` with snapshotting and append-only
persistence disabled. Do not replace it with managed or persisted Redis unless
the user explicitly changes this decision.

**Why:** The user confirmed that queues and scheduler coordination are expected
to be ephemeral for this application.

**How to apply:** Start Redis in the same service environment as the API and
background processes. Health checks must still verify that Redis, workers, and
exactly one scheduler are actually running.