---
name: Recovery timestamp precision
description: Timestamp boundaries used to qualify monitoring evidence after a recovery pause.
---

Recovery evidence boundaries should use the precise application-side accounting halt timestamp when available; database-generated event timestamps may be coarser in local SQLite test databases.

**Why:** A clear snapshot must be strictly newer than the active pause or halt, and coarse server timestamps can make a genuinely later event share a timestamp with an earlier one.

**How to apply:** Compare monitoring `generated_at` with the later of the current pause and account halt boundaries, and use the account halt timestamp for precise test fixtures.