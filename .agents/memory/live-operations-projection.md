---
name: Live operations projection
description: Rules for joining live safety, broker, feed, runtime, monitoring, and evidence state for operator views.
---

The live operations projection must remain read-only, bounded, and redacted. It may expose attributable identifiers, timestamps, status reasons, proof digests, and computed risk metrics, but never raw broker payloads or credential-bearing fields. Missing or inconclusive evidence must be classified as unknown, uncertain, degraded, or blocked; it must never be rendered healthy.

**Why:** Operational visibility needs to support incident review without turning a dashboard or export into a second secret-bearing broker interface. Fail-closed classification prevents stale or absent observations from being mistaken for permission to operate.

**How to apply:** Add new live operational signals to the bounded projection and evidence export, preserve actor/correlation attribution, and keep mutations (including alert acknowledgement) on explicitly authorized routes.