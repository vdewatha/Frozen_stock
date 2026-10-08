import assert from "node:assert/strict";
import { test } from "node:test";
import { intradayRepairSymbols } from "./readiness-checklist";
import type { ReadinessSnapshot } from "@/lib/api";

const snapshot = {
  generated_at: "2026-10-08T17:26:00Z",
  overall_status: "blocked",
  summary: { ready: 0, warning: 0, blocked: 1 },
  checks: [{
    name: "Intraday feed",
    status: "blocked",
    message: "Historical repair required",
    details: {
      symbols: {
        AAPL: { status: "ready", missing_intervals: [] },
        MSFT: { status: "incomplete", missing_intervals: ["2026-10-08T16:14:00Z"] },
        SPY: { status: "ready", missing_intervals: [] },
      },
    },
  }],
  paper_trading_allowed: false,
} satisfies ReadinessSnapshot;

test("intraday repair targets incomplete symbols even when daily data is current", () => {
  assert.deepEqual(intradayRepairSymbols(snapshot), ["MSFT"]);
  assert.deepEqual(intradayRepairSymbols(null), []);
});
