---
name: Stock holdout governance
description: Why scheduled stock challengers defer and historical-cutoff research cannot qualify for paper binding.
---

Keep holdout protection scoped to intersecting stock symbols and time periods, not just an exact universe identifier. Scheduled research must defer when fresh untouched holdout data is insufficient; it must not reuse a holdout to keep producing daily models.

**Why:** Changing a universe from one stock to two stocks does not make the first stock's already-inspected holdout independent. Frequent model production is less important than preserving honest selection evidence.

**How to apply:** Preserve this constraint when extending scheduling or forward evaluation. Never treat a deferred training run as authorization to replace the frozen paper model.

Treat a crash after durable holdout consumption but before evidence publication as an ambiguous, non-retryable evaluation unless completed evidence can be verified and adopted.

**Why:** Retrying model fitting is safe before holdout consumption, but retrying final scoring can silently turn a claimed one-use holdout into repeated evaluation.

**How to apply:** Keep the pre-scoring durable fence when changing job recovery; prefer a visible failed run to unrecorded holdout reuse.

Current provider-adjusted daily history is an extraction-time dataset, not a point-in-time reconstruction of what was known at a historical cutoff.

**Why:** Provider prices can be restated and current universe membership can contain later information. Hashes prove reproducibility, not historical availability.

**How to apply:** Keep historical-cutoff runs research-only unless true price/universe vintages are added. Disclose extraction-time limitations even for current-cutoff paper research.