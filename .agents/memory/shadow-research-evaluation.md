---
name: Shadow research evaluation
description: Durable boundary for external agent recommendations and forward comparison.
---

External agent recommendations are research evidence only. Their forward score must use observations strictly after the immutable source cutoff, and any baseline comparison must match the same symbol, eligible date, and horizon; otherwise show pending or unavailable rather than infer a score.

**Why:** A BUY/SELL/HOLD narrative is not a calibrated probability, and extraction-time context or a nearby baseline can leak future information into the comparison.

**How to apply:** Keep agent output in a separate immutable research record, preserve source timestamps and prompt/framework versions, and require exact future evidence before displaying comparison results or any readiness implication.

Keep the five-observation target anchored to the source cutoff before checking whether it is later than the recommendation. Do not remove pre-decision observations and then take five more.

**Why:** A recommendation can complete after its source session. Filtering first silently changes the comparison horizon, even when both lanes still carry a five-day label.

**How to apply:** Select the exact cutoff-relative target first, then reject preknown outcomes. Missing decision times and ambiguous exact baselines are unavailable; baseline version metadata that was never recorded must remain explicitly unknown.