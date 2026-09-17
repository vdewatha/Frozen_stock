import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ComparisonReportDetail } from "./agent-comparison-reports";
import type { AgentComparisonReport } from "../lib/agent-comparison-api";

const snapshot: AgentComparisonReport = {
  report_id: "fixture-report", created_at: "2020-01-20T12:00:00Z", content_sha256: "a".repeat(64),
  report: {
    schema_version: "v1", policy_version: "v1", status: "partial", eligible_for_trading: false,
    scope: { symbols: ["AAPL"], horizon_days: 5, run_count: 2, source_cutoff_start: "2020-01-06", source_cutoff_end: "2020-01-06", description: "Bounded research evidence" },
    coverage: { matched: 1, pending: 0, unavailable: 1, hold: 1 },
    metrics: { agent_directional_accuracy: null, baseline_directional_accuracy: null, baseline_brier_score: 0.04, directional_pair_count: 0 },
    observations: [
      {
        run_id: "hold", symbol: "AAPL", status: "complete", reason: null,
        source_cutoff: "2020-01-06", outcome_date: "2020-01-13",
        framework_version: "framework-v1", prompt_version: "prompt-v1", model_name: "agent-v1",
        source_snapshot: [{ source: "fixture" }], source_sha256: "b".repeat(64),
        agent: { recommendation: "HOLD", directional_correct: null },
        baseline: { status: "complete", probability_up: 0.8, brier_score: 0.04, directional_correct: true, source: "existing-model", model_version: "model-v2" },
      },
      {
        run_id: "missing", symbol: "AAPL", status: "unavailable", reason: "No exact five-day baseline",
        source_cutoff: "2020-01-06", outcome_date: "2020-01-13",
        framework_version: "framework-v1", prompt_version: "prompt-v1", model_name: "agent-v1",
        source_snapshot: [], source_sha256: "c".repeat(64),
        agent: { recommendation: "BUY", directional_correct: true },
        baseline: { status: "unavailable", model_version: null },
      },
    ],
    limitations: ["Research only; no trading authority"],
  },
};

test("saved comparisons expose metrics, missing matches, HOLD exclusions, and provenance", () => {
  const html = renderToStaticMarkup(<ComparisonReportDetail snapshot={snapshot} />);
  assert.match(html, /HOLD · not directional/);
  assert.match(html, /No exact five-day baseline/);
  assert.match(html, /0 matched BUY\/SELL pairs/);
  assert.match(html, /0\.04000/);
  assert.match(html, /no agent probability or Brier score is inferred/);
  assert.match(html, /Not recorded by existing model/);
  for (const text of ["framework-v1", "prompt-v1", "agent-v1", "model-v2", "fixture-report", "2020-01-13"]) {
    assert.ok(html.includes(text), text);
  }
});