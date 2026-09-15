---
name: Paper graduation boundary
description: Governance rule separating immutable paper-graduation review from trading-state or live-authorization changes.
---

Paper graduation must be recorded as an append-only reviewer disposition over a hashed, redacted evidence package. Missing or failing evidence blocks approval, while a reviewer may still archive an explicit rejection. Every package remains paper-only and live-disabled.

**Why:** Graduation review aggregates frozen campaign, model, accounting, interruption, recovery, and monitoring evidence. Reusing mutable readiness or promotion state would weaken auditability and could accidentally couple an evidence decision to execution authority.

**How to apply:** New graduation evidence sources must contribute stable identifiers or bounded redacted summaries, never raw broker payloads, account identities, credentials, or unnecessary financial fields. Never make package creation call promotion, rebinding, resume, order, credential, or live-safety transition paths.