import assert from "node:assert/strict";
import { test } from "node:test";

import { latestEquitySnapshots, MAX_VISIBLE_EQUITY_SNAPSHOTS } from "./stock-paper-ledger-utils";
import type { StockPaperEquitySnapshot } from "@/lib/api";

function snapshot(index: number): StockPaperEquitySnapshot {
  return {
    cash: String(index),
    equity: String(index),
    last_equity: String(index),
    buying_power: String(index),
    observed_at: `2026-09-13T00:${String(index).padStart(2, "0")}:00.000Z`,
  };
}

test("keeps only the newest 20 snapshots from the API's newest-first order", () => {
  const snapshots = Array.from({ length: 365 }, (_, index) => snapshot(365 - index));

  const visible = latestEquitySnapshots(snapshots);

  assert.equal(MAX_VISIBLE_EQUITY_SNAPSHOTS, 20);
  assert.equal(visible.length, 20);
  assert.equal(visible[0]?.observed_at, snapshots[0]?.observed_at);
  assert.equal(visible.at(-1)?.observed_at, snapshots[19]?.observed_at);
  assert.equal(visible.some((item) => item.observed_at === snapshots[20]?.observed_at), false);
  assert.equal(snapshots.length, 365);
});

test("keeps all snapshots when the retained history has fewer than 20 rows", () => {
  const snapshots = Array.from({ length: 7 }, (_, index) => snapshot(index));

  assert.deepEqual(latestEquitySnapshots(snapshots), snapshots);
});

test("keeps an empty equity history empty", () => {
  assert.deepEqual(latestEquitySnapshots([]), []);
});