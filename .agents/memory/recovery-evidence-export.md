---
name: Recovery evidence export
description: Safety boundary for exposing broker proof evidence to audit reviewers.
---

Recovery evidence exports must expose the stored proof digest, digest status, and stable local/broker identifiers without returning raw broker payloads or financial fields.

**Why:** Broker payloads can contain provider-specific sensitive fields, while balances, fees, prices, and P/L can be mistaken for an independently verified accounting result.

**How to apply:** Keep reviewer exports read-only and identifier-only. If historical proof bundles are added, preserve the same redaction boundary for every immutable decision.