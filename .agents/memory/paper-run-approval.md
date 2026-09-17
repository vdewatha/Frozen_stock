---
name: Paper-run approval
description: Durable approval rules for scheduled stock paper-learning handoffs.
---

Scheduled paper-learning handoffs require an immutable, cycle-scoped approval that exactly matches the current non-secret runtime bounds. Approval records are paper-only and never grant live authority. New approval records are revisions for the same cycle when a provider or runtime bound changes.

**Why:** Task acceptance requires an explicit, reviewable launch authorization rather than treating training acceptance or an unstructured operator payload as permission to start.

**How to apply:** Keep the approval check before trial preflight/start, project only safe configuration and digests, and require the authenticated operator actor to be recorded with the supplied approving actors.