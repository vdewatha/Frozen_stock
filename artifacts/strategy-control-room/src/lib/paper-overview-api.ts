import { API_BASE_URL, authenticatedFetch, ApiError, readJson } from "./api";

export type AttributionIssueCode =
  | "external_orders" | "missing_order_link" | "unowned_fills" | "quantity_mismatch" | "no_fill_history";
export type AttributionIssue = {
  code: AttributionIssueCode;
  fills: number;
  quantity: string | null;
  detail: string;
  proposed_review: string;
};
export type AttributionEvidence = {
  broker_quantity: string;
  fill_count: number;
  /** Fills before the symbol was last flat; they do not bear on the current holding. */
  earlier_fill_count?: number;
  /** Shares held by the cost probe (owned by the probe, not a strategy). */
  probe_quantity?: string;
  strategy_net_quantity: string;
  fill_net_quantity: string;
  unexplained_quantity: string;
  issues: AttributionIssue[];
  summary: string;
  owner_assigned_automatically: false;
};
export type OverviewPosition = {
  symbol: string;
  quantity: string;
  entry_price: string | null;
  current_price: string | null;
  market_value: string | null;
  unrealized_pl: string | null;
  unrealized_pl_pct: number | null;
  /** The broker's own mark-to-market before fees; shown when the fee-verified value is masked. */
  broker_unrealized_pl?: string | null;
  broker_unrealized_pl_pct?: number | null;
  observed_at: string | null;
  attribution: "attributed" | "multiple" | "cost_probe" | "unknown";
  strategy: string | null;
  strategy_ids: number[];
  attribution_evidence: AttributionEvidence;
  entry_at: string | null;
  sessions_held: number | null;
  horizon_sessions: number | null;
  /**
   * "lot" (this holding's lot), "lots" (shared by its lots), "strategy_time_stop" (owner strategy policy),
   * "target_weight" (slow book: held to target, no time stop) or "cost_probe" (sold by the probe).
   */
  horizon_source?: "lot" | "lots" | "strategy_time_stop" | "target_weight" | "cost_probe" | null;
  exit_intent: string | null;
  stop_price: string | null;
};
export type OverviewStrategy = {
  id: number; name: string; status: string;
  open_positions: number; position_limit: number | null;
  last_signal: { action: string; symbol: string; at: string | null; withheld: boolean | null; reason: string | null } | null;
};
export type PaperOverview = {
  generated_at: string;
  mode: "paper";
  session: { state: "open" | "closed"; opens_at: string | null; closes_at: string | null; entry_start: string | null; entry_end: string | null };
  bot: { state: string; reason: string | null; kill_switch: boolean | null; mandate_status: string | null };
  bounds: { starts_at: string | null; ends_at: string | null; max_positions: number | null; max_gross: string | null; per_symbol: string | null };
  account: { status: string; last_reconciled_at: string | null; stale: boolean; equity: string | null; cash: string | null; exposure: string | null; exposure_pct: number | null; pnl_verified: boolean; broker_unrealized_pl?: string | null };
  equity_curve: { observed_at: string | null; equity: string | null }[];
  positions: OverviewPosition[];
  strategies: OverviewStrategy[];
  research_count: number;
  activity: { id: string; kind: "buy" | "sell" | "withheld"; symbol: string; strategy: string | null; at: string | null; detail: string }[];
};

export async function getPaperOverview(): Promise<PaperOverview> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock-paper/overview`, { cache: "no-store" });
  if (!response.ok) throw new ApiError(response.status, "Paper overview unavailable");
  return readJson<PaperOverview>(response);
}
