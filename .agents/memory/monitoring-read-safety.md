---
name: Monitoring read safety
description: Safety boundary between read-only monitoring views and active evaluations.
---

Monitoring status reads must be side-effect free. If no persisted evaluation exists, return `unknown` with no checks or actions; only an explicit scheduled or operator evaluation may persist evidence or pause trading.

**Why:** A dashboard request can be made by a viewer and must not unexpectedly mutate broker or model state, especially when the monitor has no evidence yet.

**How to apply:** Keep snapshot retrieval separate from evaluation. Use the evaluation service only from the scheduled job or an explicitly authorized operator route.