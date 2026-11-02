import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { ShortProfitChart, shortProfitPoints, shortProfitPlot, type ShortPerformance } from "./short-profit-chart";

const now = Date.parse("2026-11-02T17:00:00Z");
const performance: ShortPerformance = { currency: "USD", net_pl: null, costs_verified: false,
  scope: "latest_1000_reconciliations", points: [
    { observed_at: "2026-11-02T16:58:00Z", gross_pl: "0", filled_orders: 0, audit_event_id: 1 },
    { observed_at: "2026-11-02T16:59:00Z", gross_pl: "10", filled_orders: 1, audit_event_id: 2 },
    { observed_at: "2026-11-02T17:00:00Z", gross_pl: "-15", filled_orders: 2, audit_event_id: 3 },
  ] };

test("profit series preserves zero, profits and losses without forced growth", () => {
  assert.deepEqual(shortProfitPoints(performance, "today", now).map(p => p.gross), [0, 10, -15]);
  assert.deepEqual(shortProfitPoints(undefined, "today", now), []);
});

test("invalid, absent and future observations are not converted to zero", () => {
  const bad = { ...performance, points: [
    { ...performance.points[0], gross_pl: "" },
    { ...performance.points[0], gross_pl: "NaN" },
    { ...performance.points[0], observed_at: "not a timestamp" },
    { ...performance.points[0], observed_at: "2030-01-01T00:00:00Z" },
  ] };
  assert.deepEqual(shortProfitPoints(bad, "all", now), []);
});

test("today means New York trading date and history is ordered by observation", () => {
  const midnight = Date.parse("2026-11-03T01:00:00Z");
  assert.equal(shortProfitPoints(performance, "today", midnight).length, 3);
  assert.equal(shortProfitPoints(performance, "today", Date.parse("2026-11-03T06:00:00Z")).length, 0);
  assert.deepEqual(shortProfitPoints({ ...performance, points: [...performance.points].reverse() }, "all", now).map(p => p.gross), [0, 10, -15]);
});

test("outages remain gaps rather than fabricated profit observations", () => {
  const points = shortProfitPoints({ ...performance, points: [performance.points[0], { ...performance.points[2], observed_at: "2026-11-02T17:10:00Z" }] }, "all", now + 600000);
  assert.deepEqual(shortProfitPlot(points).map(p => p.gross), [0, null, -15]);
});

test("no observations are shown as unavailable, not a zero profit", () => {
  const html = renderToStaticMarkup(<ShortProfitChart now={now} />);
  assert.match(html, /Not observed/);
  assert.match(html, /No P\/L observations/);
  assert.doesNotMatch(html, /recorded \$0 gross/);
});

test("zero-fill observations show flat gross results and unknown net costs", () => {
  const html = renderToStaticMarkup(<ShortProfitChart performance={{ ...performance, points: [performance.points[0]] }} now={now} />);
  assert.match(html, /\$0\.00/);
  assert.match(html, /not a profit gain/);
  assert.match(html, /fees and borrow costs excluded/);
  assert.match(html, /Not verified/);
  assert.match(html, /Audit event/);
});
