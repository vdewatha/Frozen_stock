import React from "react";
import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { PaperScorecardPanel, formatPct, formatUsd } from "./paper-scorecard-panel";
import { resolveWorkspacePage } from "../lib/workspace-navigation";

test("scorecard formatters never show NaN", () => {
  assert.equal(formatPct(0.8), "80.0%");
  assert.equal(formatPct(null), "n/a");
  assert.equal(formatUsd(-2), "-$2.00");
  assert.equal(formatUsd(Number.NaN), "n/a");
});

test("scorecard panel renders a read-only loading state under Portfolio", () => {
  const html = renderToStaticMarkup(<PaperScorecardPanel />);
  assert.match(html, /Paper trade scorecard/);
  assert.match(html, /Read-only/);
  assert.doesNotMatch(html, /Submit|Place order|Close position/);
  assert.equal(resolveWorkspacePage("/portfolio/scorecard")?.section, "scorecard");
});
