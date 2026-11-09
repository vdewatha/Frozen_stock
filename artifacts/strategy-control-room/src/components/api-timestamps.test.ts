import assert from "node:assert/strict";
import test from "node:test";

import { normalizeUtcTimestamps } from "../lib/api";

test("offset-less API timestamps are read as UTC, not local time", () => {
  const payload = normalizeUtcTimestamps({
    generated_at: "2026-11-09T16:06:00.777729",
    checks: [{ observed_at: "2026-11-09T16:06:00" }],
  });
  assert.equal(payload.generated_at, "2026-11-09T16:06:00.777729Z");
  assert.equal(new Date(payload.generated_at).toISOString(), "2026-11-09T16:06:00.777Z");
  assert.equal(payload.checks[0].observed_at, "2026-11-09T16:06:00Z");
});

test("dates, zoned timestamps and other strings are left alone", () => {
  const payload = normalizeUtcTimestamps({
    date: "2026-11-09",
    zoned: "2026-11-09T16:06:00+00:00",
    utc: "2026-11-09T16:06:00Z",
    note: "2026-11-09T16:06 is when it ran",
    count: 3,
    missing: null,
  });
  assert.deepEqual(payload, {
    date: "2026-11-09",
    zoned: "2026-11-09T16:06:00+00:00",
    utc: "2026-11-09T16:06:00Z",
    note: "2026-11-09T16:06 is when it ran",
    count: 3,
    missing: null,
  });
});
