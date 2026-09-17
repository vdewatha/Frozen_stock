---
name: Shadow research evaluation
description: Durable boundary for external agent recommendations and forward comparison.
---

External agent recommendations are research evidence only. Their forward score must use observations strictly after the immutable source cutoff, and any baseline comparison must match the same symbol, eligible date, and horizon; otherwise show pending or unavailable rather than infer a score.

**Why:** A BUY/SELL/HOLD narrative is not a calibrated probability, and extraction-time context or a nearby baseline can leak future information into the comparison.

**How to apply:** Keep agent output in a separate immutable research record, preserve source timestamps and prompt/framework versions, and require exact future evidence before displaying comparison results or any readiness implication.