import React from "react";
import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { PaperCostSensitivity } from "./paper-cost-sensitivity";
import type { PaperCostSensitivityReport } from "@/lib/api";

const report: PaperCostSensitivityReport = {
  status: "research_only", qualifying: false, costs_verified: false, launch_authorized: false,
  reason: "Missing broker fees remain unknown", assumptions: { version: "paper-turnover-stress-v1" },
  fills_without_reported_commission: 2,
  scenarios: [{ additional_cost_bps_per_side: 5, additional_modeled_cost: "0.01", modeled_fill_cash_change: "-0.000078" }],
};
test("cost assumptions are visible and sub-cent losses are not rendered as zero", () => {
  const html = renderToStaticMarkup(<PaperCostSensitivity report={report} />);
  assert.match(html, /Hypothetical, not realized profit/);
  assert.match(html, /-\$0\.000078/);
  assert.match(html, /Unreported commissions: 2 fills/);
  assert.match(html, /Missing broker fees remain unknown/);
});
test("unavailable evidence does not render a scenario table", () => {
  assert.equal(renderToStaticMarkup(<PaperCostSensitivity />), "");
  const html = renderToStaticMarkup(<PaperCostSensitivity report={{ ...report, status: "unavailable", scenarios: [] }} />);
  assert.doesNotMatch(html, /<table/);
});
test("invalid money is not coerced to a zero or NaN", () => {
  const html = renderToStaticMarkup(<PaperCostSensitivity report={{ ...report, scenarios: [
    { additional_cost_bps_per_side: 5, additional_modeled_cost: "", modeled_fill_cash_change: "NaN" },
  ] }} />);
  assert.match(html, /Not measured/);
  assert.doesNotMatch(html, /\$NaN|\$0\.00/);
});
