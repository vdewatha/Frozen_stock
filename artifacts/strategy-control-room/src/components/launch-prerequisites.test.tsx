import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { LaunchPrerequisiteResults } from "./launch-prerequisites";
import type { LaunchPrerequisites } from "@/lib/api";

const result: LaunchPrerequisites = {
  cycle_id: null,
  checked_at: "2026-09-17T12:00:00Z",
  status: "blocked",
  eligible_for_approval: false,
  reason: "Paper ledger reconciliation is unavailable",
  gates: {
    paper_ledger: { status: "fail", reason: "Paper ledger reconciliation is unavailable" },
    verified_feed: { status: "unknown", evidence: { regular_session: false }, reason: "Regular-session verification required" },
  },
};

test("shows exact blocked reasons alongside outside-session unknowns and authority limits", () => {
  const html = renderToStaticMarkup(<LaunchPrerequisiteResults result={result} loading={false} />);
  for (const text of ["Paper ledger reconciliation is unavailable", "unknown outside session", "Regular-session verification required", "implicitly authorize a provider switch", "required preflight stage"]) {
    assert.ok(html.includes(text), text);
  }
});

test("missing and failed refresh evidence never claims current readiness", () => {
  const missing = renderToStaticMarkup(<LaunchPrerequisiteResults loading={false} />);
  assert.match(missing, /Approval is unavailable/);
  const failed = renderToStaticMarkup(<LaunchPrerequisiteResults result={{ ...result, status: "ready" }} loading={false} error="Connection lost" />);
  assert.match(failed, /Unknown — refresh failed/);
  assert.match(failed, /previous results, not current authorization/);
});