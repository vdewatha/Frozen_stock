---
name: Loss feedback loop
description: Rules for how paper losses may change behaviour (attribution, agenda, parameter sets, meta-label modes, allocation weights).
---
- Automated parameter changes never edit `Strategy.parameters`; they become immutable versioned parameter sets that must pass a governed learning cycle and are only then overlaid at paper signal time. The manual experiment-apply API path is a separate, pre-existing in-place path.
  **Why:** promotion must stay governed and traceable to the experiment and the losses behind it.
- Attribution rows are append-only revisions per trade; unknown costs keep net return NULL and are excluded from allocation weights. Exploration round trips never drive weights.
- Meta-label auto-enforce applies only when the configured mode is shadow; an auto-enforced strategy fails closed if the model is unavailable.
- Allocation weights are computed once per NYSE session and reused; they are shrink-first and bounded; live sizing is untouched.
**How to apply:** extend these rules rather than adding a second path that mutates strategies or treats unknown costs as zero.
