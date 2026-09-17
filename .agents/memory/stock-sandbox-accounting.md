---
name: Stock sandbox accounting
description: User-approved stock paper venue and balance/cost evidence boundaries.
---

Use Alpaca Paper Trading for equities, import its reported sandbox balance without resetting it, and record reported fees while explicitly flagging unknown costs.

Following the later Tradier selection, approval to assess Alpaca again is read-only candidate validation, not authorization to switch back.

**Why:** The user approved validating the alternative without orders; an accessible empty account cannot demonstrate execution costs or missed-session recovery. Earlier venue approval must not override the current provider boundary.

**How to apply:** Require separate switch authorization and qualifying evidence; do not treat historical approval or a successful connectivity probe as launch permission.

**Why:** The user chose broker sandbox fills instead of a local simulator and explicitly approved importing existing balances rather than inventing or resetting starting capital. Data-feed selection alone was not execution-venue approval.

**How to apply:** Keep stock evidence separate from legacy simulated trades and the BTC/USD experiment. Never treat sandbox balances as live funds or absent fee evidence as zero costs. A working connection is not permission to submit test orders or purchase data subscriptions.