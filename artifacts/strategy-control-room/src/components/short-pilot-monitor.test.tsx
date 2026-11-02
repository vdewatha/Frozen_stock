import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { ShortPilotSnapshot, shortWorkerState, type ShortPilotStatus } from "./short-pilot-monitor";

const status: ShortPilotStatus = {
  autonomous: true, configured_enabled: true, entry_policy: "regular_session",
  pilot: { status: "active", symbols: ["AAPL"], max_order_notional: "1000", max_gross_notional: "3000", expires_at: "2026-11-09T15:00:00Z" },
  reconciliation: { positions: {}, observed_at: "2026-11-02T15:30:00Z" },
  automation_last_cycle: { checked_at: "2026-11-02T15:30:00Z", status: "observed", action: "no_new_entries", reason: "entry_window_closed",
    decisions: [{ symbol: "AAPL", action: "entry_window_closed", execution_feed: "ready", evidence: { rsi2: 55, sma200: 200, daily_session: "2026-10-30", enter: false, cover: false } }] },
  orders: [],
};

test("worker state distinguishes stale, missing, disabled and blocked evidence", () => {
  const now = Date.parse("2026-11-02T15:31:00Z");
  assert.equal(shortWorkerState(status, now), "Running");
  assert.equal(shortWorkerState(status, now + 180000), "Heartbeat stale");
  assert.equal(shortWorkerState({ ...status, automation_last_cycle: null }, now), "Awaiting first cycle");
  assert.equal(shortWorkerState({ ...status, autonomous: false }, now), "Not enabled");
  assert.equal(shortWorkerState({ ...status, automation_last_cycle: { ...status.automation_last_cycle!, status: "blocked" } }, now), "Cycle blocked");
});

test("monitor preserves observed decisions and never invents fills or profit", () => {
  const html = renderToStaticMarkup(<ShortPilotSnapshot data={status} />);
  assert.match(html, /entry window closed/);
  assert.match(html, /55\.00/);
  assert.match(html, /2026-10-30/);
  assert.match(html, /Full regular market session, including afternoons/);
  assert.match(html, /Account is flat at the last reconciliation/);
  assert.match(html, /No broker order intents recorded/);
  assert.match(html, /P\/L remain unverified/);
});

test("missing reconciliation is not shown as a flat account", () => {
  const html = renderToStaticMarkup(<ShortPilotSnapshot data={{ ...status, reconciliation: null }} />);
  assert.match(html, /No broker reconciliation recorded/);
  assert.doesNotMatch(html, /Account is flat/);
});
