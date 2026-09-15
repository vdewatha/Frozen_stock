---
name: Production identity boundary
description: The authorization boundary and credential separation required before any live-capable path is introduced.
---

Production authentication must use an attributable signed identity assertion whose subject is mapped to a role on the server. Caller-supplied role claims and shared deployment role keys are not production authorization.

**Why:** A browser-held bearer role key can identify a capability but cannot reliably attribute a live-capable action to an individual, and frontend role gates are not a security boundary.

**How to apply:** Keep local role-key authentication explicitly paper-only. In production, fail closed when identity mapping, signing configuration, or explicit paper credential configuration is incomplete; keep paper and live broker credentials separate and server-only.