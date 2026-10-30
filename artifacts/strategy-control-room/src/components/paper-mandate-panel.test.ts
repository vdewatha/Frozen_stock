import assert from "node:assert/strict";
import test from "node:test";

import { mandateRequestFromDraft } from "./paper-mandate-panel";

test("mandate draft becomes an exact, paper-only scheduled request", () => {
  const request = mandateRequestFromDraft({
    symbols: " spy, qqq  iwm,",
    maxNotional: "10000",
    maxRiskPerTrade: "0.0025",
    maxLoss: "0.02",
    maxSessionsPerCycle: "20",
    maxSessions: "60",
    expiresOn: "2026-12-01",
    allowReduce: false,
    allowFlatten: true,
    reason: "  standing mandate  ",
  });
  assert.deepEqual(request.symbols, ["SPY", "QQQ", "IWM"]);
  assert.deepEqual(request.triggers, ["scheduled"]);
  assert.deepEqual(request.remaining_position_policies, ["hold", "flatten"]);
  assert.equal(request.loss_limits.unit, "fraction_of_baseline_equity");
  assert.equal(request.max_sessions, 60);
  assert.equal(request.expires_at, "2026-12-01T21:00:00.000Z");
  assert.equal(request.reason, "standing mandate");
});
