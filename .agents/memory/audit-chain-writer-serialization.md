---
name: Audit-chain writer serialization
description: Rules for preserving the PostgreSQL audit digest chain under concurrent and multi-event writes.
---

Audit writers must serialize on PostgreSQL and flush earlier audit rows in the
same transaction before selecting the previous digest. A hardening check that
finds a historical fork must report a breach and fail closed; it must not
rewrite or silently re-anchor persisted audit evidence. Record an idempotent
quarantine decision as a new append-only audit event, with the original broken
rows and their observed digests preserved.

**Why:** Multiple events created in one transaction can otherwise all link to
the last committed row, creating a fork even when each digest is individually
valid. Historical repair would destroy the evidence the audit chain is meant
to protect.

**How to apply:** Keep the advisory lock around audit writes, flush before
reading the previous digest, classify the first broken row and expected versus
observed links, then quarantine once and require explicit operator review plus
fresh evidence before recovery.