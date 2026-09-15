---
name: Accuracy vintage revalidation
description: Point-in-time accuracy reports must detect later feature restatements without rewriting outcome history.
---

Accuracy evidence must revalidate the exact feature data version whenever a report is built, even if the decision already has an immutable resolved outcome. A later restatement invalidates that sample for current accuracy credit and should be reported as blocked/restated evidence, while preserving the original outcome row.

**Why:** Provider rows can be corrected after a label is recorded. Trusting only the first resolution would let a later data correction silently preserve false accuracy.

**How to apply:** Keep outcome labels append-only, but recompute the feature-vintage contract for every bounded report window and exclude any decision whose stored version no longer matches the available provider rows.