import type { StockPaperEquitySnapshot } from "@/lib/api";

export const MAX_VISIBLE_EQUITY_SNAPSHOTS = 20;

export function latestEquitySnapshots(snapshots: StockPaperEquitySnapshot[]): StockPaperEquitySnapshot[] {
  return snapshots.slice(0, MAX_VISIBLE_EQUITY_SNAPSHOTS);
}