import { API_BASE_URL, authenticatedFetch, ApiError } from "./api";

export type OverviewPosition = {
  symbol: string;
  quantity: string;
  entry_price: string | null;
  current_price: string | null;
  market_value: string | null;
  unrealized_pl: string | null;
  unrealized_pl_pct: number | null;
  observed_at: string | null;
  attribution: "attributed" | "multiple" | "unknown";
  strategy: string | null;
  strategy_ids: number[];
  entry_at: string | null;
  sessions_held: number | null;
  horizon_sessions: number | null;
  exit_intent: string | null;
  stop_price: string | null;
};
export type OverviewStrategy = {
  id: number; name: string; status: string;
  open_positions: number; position_limit: number | null;
  last_signal: { action: string; symbol: string; at: string | null; withheld: boolean | null; reason: string | null } | null;
  next_evaluation_at: string | null;
};
export type PaperOverview = {
  generated_at: string;
  mode: "paper";
  session: { state: "open" | "closed"; opens_at: string | null; closes_at: string | null; entry_start: string | null; entry_end: string | null };
  bot: { state: string; reason: string | null; kill_switch: boolean | null; mandate_status: string | null };
  bounds: { starts_at: string | null; ends_at: string | null; max_positions: number | null; max_gross: string | null; per_symbol: string | null; stop_fraction: string | null };
  account: { status: string; last_reconciled_at: string | null; stale: boolean; equity: string | null; cash: string | null; exposure: string | null; exposure_pct: number | null; pnl_verified: boolean; realized_pl: string | null };
  positions: OverviewPosition[];
  strategies: OverviewStrategy[];
  research_count: number;
  activity: { id: string; kind: "buy" | "sell" | "withheld"; symbol: string; strategy: string | null; at: string | null; detail: string }[];
};

export async function getPaperOverview(): Promise<PaperOverview> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock-paper/overview`, { cache: "no-store" });
  if (!response.ok) throw new ApiError(response.status, "Paper overview unavailable");
  return response.json() as Promise<PaperOverview>;
}
