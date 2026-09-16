---
name: Stale artifact notifications
description: How to handle scheduled-job alerts after an immutable artifact is restored.
---

A scheduled-job notification can remain open after its immutable artifact has been restored. Verify the manifest and data-file hash, rerun the governed job, and resolve only that stale notification; keep unrelated broker, feed, monitoring, and audit blockers open.

**Why:** The paper observer can succeed after an artifact is restored without automatically retiring the earlier missing-artifact notification, and resolving it before verification would hide a real data problem.

**How to apply:** Treat notification resolution as evidence-based cleanup, never as a readiness override; recheck the full paper-readiness gate afterward.