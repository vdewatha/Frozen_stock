import assert from "node:assert/strict";
import test from "node:test";
import { equityChartPoints, equityDateLabel, equityTimePoints } from "./equity-chart";

test("daily equity labels preserve the session date while snapshots use Eastern time", () => {
  assert.equal(equityDateLabel("2026-10-30"), "Oct 30");
  assert.match(equityDateLabel("2026-10-30T01:00:00Z", true), /Oct 29.*9:00 PM.*EDT/);
  assert.equal(equityDateLabel("invalid"), "Unknown date");
});

test("unknown equity is omitted, not fabricated as zero", () => {
  assert.deepEqual(equityChartPoints([
    {date:"2026-10-26"}, {date:"2026-10-27",equity:Number.NaN},
    {date:"2026-10-28",value:Number.POSITIVE_INFINITY},
    {date:"2026-10-29",equity:0}, {date:"2026-10-30",value:1000},
  ]), [{date:"2026-10-29",value:0}, {date:"2026-10-30",value:1000}]);
});

test("points sit on a time axis so recent intraday points do not crowd out the history", () => {
  const points = equityTimePoints([
    {date:"2026-09-11", equity:100000}, {date:"2026-11-09T15:00:00Z", equity:99700}, {date:"bad", equity:1},
  ]);
  assert.equal(points.length, 2);
  assert.equal(points[0].t, Date.parse("2026-09-11T21:00:00Z"));
  assert.equal(points[1].t, Date.parse("2026-11-09T15:00:00Z"));
});
