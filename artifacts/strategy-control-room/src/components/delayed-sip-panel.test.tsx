import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { DelayedSipSummary, type DelayedSipStatus } from "./delayed-sip-panel";

const snapshot: DelayedSipStatus = {
  enabled: true, poll_fresh: true, delay_minutes: 16,
  last_collection: { status: "observed", failure_class: null },
  comparison_window_start: "2026-09-28T13:30:00Z", comparison_window_end: "2026-09-28T14:30:00Z",
  symbols: [{ symbol: "SPY", total_bars: 123, window_bars: 60, iex_window_bars: 50,
    matched_minutes: 50, mean_abs_close_gap_bps: null, latest_bar: "2026-09-28T14:29:00Z" }],
};

test("delayed feed distinguishes unknown price gap from measured zero", () => {
  const missing = renderToStaticMarkup(<DelayedSipSummary data={snapshot} />);
  assert.match(missing, /Unknown/);
  assert.match(missing, /123/);
  assert.match(missing, /Matched minutes/);
  assert.match(missing, /Polling/);
  const measured = renderToStaticMarkup(<DelayedSipSummary data={{ ...snapshot,
    symbols: [{ ...snapshot.symbols[0], mean_abs_close_gap_bps: 0 }] }} />);
  assert.match(measured, /0\.00/);
  assert.doesNotMatch(measured, /Unknown/);
});

test("disabled and stale collectors do not display polling", () => {
  assert.match(renderToStaticMarkup(<DelayedSipSummary data={{ ...snapshot, enabled: false }} />), /Disabled/);
  const stale = renderToStaticMarkup(<DelayedSipSummary data={{ ...snapshot, poll_fresh: false, last_collection: null }} />);
  assert.match(stale, /No recent poll/);
  assert.match(stale, /None/);
  assert.doesNotMatch(stale, /Polling/);
});
