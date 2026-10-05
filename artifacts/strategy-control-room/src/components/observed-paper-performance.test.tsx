import React from "react";
import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { ObservedPaperPerformance } from "./observed-paper-performance";
import type { StockPaperObservedPerformance } from "@/lib/api";

const performance: StockPaperObservedPerformance = {
  status: "provisional", qualifying: false, scope: "observed_since_initialization",
  net_change: "-2", external_net_funding: "401", reported_fee_expense: "2",
  reason: "Cost completeness is unverified", currency: "USD",
};

test("Observed change is explicitly provisional with fees already included", () => {
  const html = renderToStaticMarkup(<ObservedPaperPerformance performance={performance} />);
  assert.match(html, /Provisional, not qualified performance/);
  assert.match(html, /-\$2\.00/);
  assert.match(html, /\$401\.00/);
  assert.match(html, /Reported fee subtotal, already included/);
  assert.match(html, /Total costs unverified/);
});
test("Zero reported fees do not imply known zero total costs", () => {
  const html = renderToStaticMarkup(<ObservedPaperPerformance performance={{...performance,
    reported_fee_expense: "0", new_fills_without_commission: 2}} />);
  assert.match(html, /2 fills without reported commission/);
  assert.match(html, /Total costs unverified/);
});
test("Missing evidence does not render zero performance", () => {
  const html = renderToStaticMarkup(<ObservedPaperPerformance performance={{...performance, status: "unavailable", net_change: null}} />);
  assert.doesNotMatch(html, /<dl|\$0\.00/);
  assert.match(html, /Cost completeness is unverified/);
  assert.equal(renderToStaticMarkup(<ObservedPaperPerformance />), "");
});
test("Nonfinite and blank values are not rendered as money", () => {
  const html = renderToStaticMarkup(<ObservedPaperPerformance performance={{...performance, net_change: "NaN", external_net_funding: ""}} />);
  assert.match(html, /Not measured/);
  assert.doesNotMatch(html, /\$NaN|\$0\.00/);
});
