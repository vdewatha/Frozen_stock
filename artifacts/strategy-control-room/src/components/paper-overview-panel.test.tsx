import React from "react";
import test from "node:test";
import assert from "node:assert/strict";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { Router } from "wouter";
import { AttributionEvidenceBlock, PaperOverviewPanel } from "./paper-overview-panel";
import type { AttributionEvidence, PaperOverview } from "@/lib/paper-overview-api";

const ownedEvidence: AttributionEvidence = {
  broker_quantity: "32", fill_count: 1, strategy_net_quantity: "32", fill_net_quantity: "32", unexplained_quantity: "0",
  issues: [], summary: "1 fill from strategy orders net exactly to the broker quantity of 32.", owner_assigned_automatically: false,
};
const externalEvidence: AttributionEvidence = {
  broker_quantity: "32", fill_count: 2, strategy_net_quantity: "0", fill_net_quantity: "30", unexplained_quantity: "2",
  issues: [
    { code: "external_orders", fills: 2, quantity: "30", detail: "2 fills netting 30 shares came from broker orders submitted outside this app and imported during reconciliation; no strategy placed them.", proposed_review: "Confirm who submitted these broker orders; ownership can only be added through an explicit, reviewed attribution record." },
    { code: "quantity_mismatch", fills: 0, quantity: null, detail: "Persisted fills net 30 shares but the broker reports 32; 2 shares are not covered by same-account fill history.", proposed_review: "Compare broker activity history with the imported fill window; missing fills enter only through reconciliation review, never by assumption." },
  ],
  summary: "2 fills netting 30 shares came from broker orders submitted outside this app and imported during reconciliation; no strategy placed them. Persisted fills net 30 shares but the broker reports 32; 2 shares are not covered by same-account fill history.",
  owner_assigned_automatically: false,
};

const overview: PaperOverview = {
  generated_at: "2026-10-30T14:30:00Z",
  mode: "paper",
  session: { state: "open", opens_at: null, closes_at: null, entry_start: null, entry_end: null },
  bot: { state: "unknown", reason: null, kill_switch: null, mandate_status: "active" },
  bounds: { starts_at: null, ends_at: null, max_positions: 4, max_gross: "40000", per_symbol: null, stop_fraction: null },
  account: { status: "available", last_reconciled_at: "2026-10-30T14:20:00Z", stale: false, equity: "50842.16", cash: "1234.50", exposure: "49607.66", exposure_pct: 97.57, pnl_verified: false, realized_pl: null },
  positions: [{
    symbol: "NVDA", quantity: "32", entry_price: "118.42", current_price: "124.86", market_value: "3995.52",
    unrealized_pl: "206.08", unrealized_pl_pct: 5.43, observed_at: "2026-10-30T14:20:00Z",
    attribution: "attributed", strategy: "Channel Breakout", strategy_ids: [8], attribution_evidence: ownedEvidence, entry_at: "2026-10-21T13:42:00Z",
    sessions_held: 7, horizon_sessions: 20, exit_intent: "Horizon exit", stop_price: "113.68",
  }],
  strategies: [{
    id: 8, name: "Channel Breakout", status: "active", open_positions: 1, position_limit: null,
    last_signal: { action: "BUY", symbol: "NVDA", at: "2026-10-30T13:42:00Z", withheld: true, reason: "Entry window closed" },
    next_evaluation_at: null,
  }],
  research_count: 1,
  equity_curve: [],
  activity: [{ id: "event-1", kind: "withheld", symbol: "AAPL", strategy: null, at: null, detail: "Entry window closed" }],
};

function render(data: PaperOverview): string {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(["paper-overview"], data);
  const html = renderToStaticMarkup(<Router ssrPath="/"><QueryClientProvider client={client}><PaperOverviewPanel /></QueryClientProvider></Router>);
  client.clear();
  return html;
}

test("paper overview renders API holdings and nullable schedule and limit values", () => {
  const html = render(overview);
  assert.match(html, /Open positions <span>1<\/span>/);
  assert.match(html, /NVDA/);
  assert.match(html, /\$118\.42/);
  assert.match(html, /\$124\.86/);
  assert.match(html, /7 \/ 20 sessions/);
  assert.match(html, /Unknown max/);
  assert.match(html, /Next eval · Unknown/);
  assert.match(html, /BUY withheld · entry window/);
});

test("an empty API position list has an explicit empty state", () => {
  const html = render({ ...overview, positions: [] });
  assert.match(html, /No open positions/);
  assert.doesNotMatch(html, /class="ov-symbol">NVDA/);
});

test("unknown attribution and stale values are explicitly warned, with null P\/L unverified", () => {
  const position = { ...overview.positions[0], attribution: "unknown" as const, strategy: null, strategy_ids: [], attribution_evidence: externalEvidence };
  const html = render({
    ...overview,
    positions: [position],
    account: { ...overview.account, stale: true, equity: null, cash: null, exposure: null, exposure_pct: null, realized_pl: null, pnl_verified: false },
  });
  assert.match(html, /Unattributed/);
  assert.match(html, /submitted outside this app/);
  assert.match(html, /last known holdings/i);
  assert.match(html, /Stale account values shown as last known/);
  assert.match(html, /Unverified/);
  assert.match(html, /Unknown/);
  assert.doesNotMatch(html, /\$0\.00/);
});

test("invalid nullable decimal strings never become fabricated zero amounts", () => {
  const position = { ...overview.positions[0], current_price: null, unrealized_pl: null, stop_price: null };
  const html = render({ ...overview, positions: [position] });
  assert.match(html, /<small>MARK<\/small><strong>Unknown<\/strong>/);
  assert.match(html, /<td class="numeric ov-number">Unknown<\/td>/);
  assert.match(html, /class="ov-unknown">Unverified<\/strong>/);
  assert.doesNotMatch(html, /\$0\.00|\$NaN/);
});

test("equity history is empty-stated without snapshots and labelled last-known when not reconciled", () => {
  assert.match(render(overview), /No equity history yet/);
  const html = render({
    ...overview,
    account: { ...overview.account, status: "halted" },
    equity_curve: [{ observed_at: "2026-10-30T14:00:00Z", equity: "99999.93" }, { observed_at: null, equity: "1" }, { observed_at: "2026-10-30T15:00:00Z", equity: null }],
  });
  assert.match(html, /Account status: halted\. Showing last known broker snapshots/);
  assert.doesNotMatch(html, /No equity history yet/);
});

test("expanded attribution evidence names each diagnostic and its proposed review without assigning an owner", () => {
  const position = { ...overview.positions[0], attribution: "unknown" as const, strategy: null, strategy_ids: [], attribution_evidence: externalEvidence };
  const html = renderToStaticMarkup(<AttributionEvidenceBlock position={position} overview={overview} />);
  assert.match(html, /Unattributed — imported external orders, quantity mismatch/);
  assert.match(html, /2 same-account fills · strategy fills net 0 of 32 broker shares · 2 not covered by fill history/);
  assert.match(html, /Imported external orders<\/b> \(2 fills\)/);
  assert.match(html, /Quantity mismatch<\/b>: Persisted fills net 30/);
  assert.match(html, /Proposed review \(not performed\): Confirm who submitted/);
  assert.match(html, /No owner is inferred from signals or assigned automatically/);
  assert.doesNotMatch(html, /Channel Breakout/);
  const owned = renderToStaticMarkup(<AttributionEvidenceBlock position={overview.positions[0]} overview={overview} />);
  assert.match(owned, /Attribution: <b>Channel Breakout<\/b>/);
  assert.match(owned, /net exactly to the broker quantity/);
  assert.doesNotMatch(owned, /Proposed review/);
});
