import React from "react";
import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { ForwardComparison, ShadowReturns, ReturnChallenger } from "./forward-comparison";

test("return learner never retroactively claims old results", () => {
  const html = renderToStaticMarkup(<ReturnChallenger />);
  assert.match(html, /Earlier results are excluded/);
  assert.match(html, /not broker profit/);
  assert.doesNotMatch(html, /<table/);
});

test("nonoverlapping sample exposes pending and missing outcomes", () => {
  const html = renderToStaticMarkup(<ReturnChallenger report={{ paired_observations: 0,
    unavailable_observations: 0, invalid_observations: 0, mae_bps: null, zero_baseline_mae_bps: null,
    strategies: [], nonoverlapping: { selected_forecasts: 4, paired_observations: 0, pending: 2,
      expired: 1, unavailable_observations: 1, invalid_declarations: 0, invalid_observations: 0,
      strategies: [] } }} />);
  assert.match(html, /Ten-minute evaluation sample/);
  assert.match(html, /Pending: 2/);
  assert.match(html, /Expired: 1/);
  assert.match(html, /Missing: 1/);
  assert.match(html, /Independence and profitability unproven/);
});

test("return learner shows paired cost-adjusted evidence and prediction error", () => {
  const html = renderToStaticMarkup(<ReturnChallenger report={{ paired_observations: 10,
    unavailable_observations: 1, invalid_observations: 0, mae_bps: 7.5, zero_baseline_mae_bps: 8,
    latest_prediction: { training_examples: 70, minimum_training: 50, action: "cash", predicted_gross_bps: 2 },
    strategies: [{ name: "challenger", long_observations: 2, mean_net_bps: -1.2 }] }} />);
  assert.match(html, /Training observations: 70/);
  assert.match(html, /7.50 bps/);
  assert.match(html, /-1.20/);
  assert.match(html, /5 bps per side/);
});

test("empty observations never imply a score or profit", () => {
  const html = renderToStaticMarkup(<ForwardComparison />);
  assert.match(html, /Awaiting new forward outcomes/);
  assert.match(html, /not trade returns/);
  assert.doesNotMatch(html, /<table/);
});

test("shadow empty state does not imply realized profit", () => {
  const html = renderToStaticMarkup(<ShadowReturns />);
  assert.match(html, /Awaiting predeclared forward price observations/);
  assert.match(html, /not broker profit/);
  assert.match(html, /not portfolio returns/);
  assert.doesNotMatch(html, /<table/);
});

test("shadow table defaults to five bps costs and exposes missing evidence", () => {
  const html = renderToStaticMarkup(<ShadowReturns report={{ paired_observations: 3, unavailable_observations: 2,
    invalid_observations: 1, strategies: [{name: "momentum_5m", long_observations: 2,
      mean_gross_bps: 7, mean_net_bps_by_cost: {"5": -3.1}}] }} />);
  assert.match(html, /Missing: 2/);
  assert.match(html, /Invalid: 1/);
  assert.match(html, /-3.10/);
  assert.match(html, /7.00/);
  assert.match(html, /Mean return per forecast/);
});
test("paired results show named approaches and finite metrics", () => {
  const html = renderToStaticMarkup(<ForwardComparison report={{paired_observations: 7, strategies: [
    {name: "momentum_5m", brier: 0.16, log_loss: 0.51},
    {name: "reversal_5m", brier: NaN, log_loss: null},
  ]}} />);
  assert.match(html, /Paired outcomes: 7/);
  assert.match(html, /Momentum/);
  assert.match(html, /Mean reversion/);
  assert.match(html, /0.1600/);
  assert.match(html, /Not measured/);
  assert.doesNotMatch(html, /NaN/);
});
