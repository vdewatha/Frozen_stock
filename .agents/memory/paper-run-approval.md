---
name: Paper-run approval
description: Durable approval rules for scheduled stock paper-learning handoffs.
---

Scheduled paper-learning handoffs require an immutable, cycle-scoped approval that exactly matches the current non-secret runtime bounds. Approval records are paper-only and never grant live authority. New approval records are revisions for the same cycle when a provider or runtime bound changes.

**Why:** Task acceptance requires an explicit, reviewable launch authorization rather than treating training acceptance or an unstructured operator payload as permission to start.

**How to apply:** Keep the approval check before trial preflight/start, project only safe configuration and digests, and require the authenticated operator actor to be recorded with the supplied approving actors.

Approval eligibility must distinguish missing approval from failed launch prerequisites and use fresh read-only prerequisite evidence rather than treating all historical blocked states as permanent.

**Why:** Scheduled handoff records missing approval as a blocked preflight state; requiring an unblocked cycle before offering approval creates a circular dependency. Viewing prerequisites must not run authenticated feed probes or broker reconciliation.

**How to apply:** Exclude only the approval requirement itself when evaluating eligibility, retain admission/lineage requirements, and allow current prerequisite checks to supersede their historical results.

Do not infer that a blocked preflight is approval-only from passing saved gates.

**Why:** Handoff failures can record only a reason while leaving prior passing gate evidence intact. A fresh global prerequisite check does not establish that this cycle still owns the active canary or has valid trial lineage.

**How to apply:** Check current cycle-specific binding and trial evidence, and require an identified approval-only blocker or an exact prerequisite repair before accepting a blocked/deferred cycle. Unexplained blockers require a governed handoff retry, not an approval override.