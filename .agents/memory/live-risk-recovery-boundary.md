---
name: Live risk and recovery boundary
description: Separation between ordinary live-entry risk gates and emergency containment behavior.
---

Normal live entries require explicit current evidence for liquidity participation, turnover, daily loss, strategy drawdown, leverage, concentration, buying power, and long-only inventory. Missing or malformed metrics block the order.

**Why:** A live order must not infer safety from an account snapshot that lacks decision-time risk evidence, but emergency containment still has to work when normal entry risk is unavailable.

**How to apply:** Keep recovery flatten/cancel actions bounded by reconciliation, broker-state, and idempotency checks; do not let ordinary entry kill-switch or participation limits prevent containment. Never label a nonterminal flatten as successful.