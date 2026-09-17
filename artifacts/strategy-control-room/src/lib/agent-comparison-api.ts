import { API_BASE_URL, authenticatedFetch } from "./api";

export type ComparisonObservation = {
  run_id: string;
  symbol: string;
  status: "complete" | "pending" | "unavailable";
  reason: string | null;
  source_cutoff: string | null;
  outcome_date: string | null;
  framework_version: string;
  prompt_version: string;
  model_name: string;
  source_snapshot: Array<Record<string, unknown>>;
  source_sha256: string;
  agent: { recommendation: string | null; directional_correct: boolean | null };
  baseline: {
    status: string;
    reason?: string | null;
    prediction_id?: number;
    source?: string;
    created_at?: string;
    model_version?: string | null;
    probability_up?: number;
    directional_correct?: boolean;
    brier_score?: number;
  };
};

export type AgentComparisonReport = {
  report_id: string;
  created_at: string;
  content_sha256: string;
  report: {
    schema_version: string;
    policy_version: string;
    status: string;
    eligible_for_trading: false;
    scope: {
      symbols: string[];
      horizon_days: number;
      run_count: number;
      source_cutoff_start: string | null;
      source_cutoff_end: string | null;
      description: string;
    };
    coverage: { matched: number; pending: number; unavailable: number; hold: number };
    metrics: {
      agent_directional_accuracy: number | null;
      baseline_directional_accuracy: number | null;
      baseline_brier_score: number | null;
      directional_pair_count: number;
    };
    observations: ComparisonObservation[];
    limitations: string[];
  };
};

async function readResponse<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(`Comparison reports unavailable (HTTP ${response.status})`);
  return response.json() as Promise<T>;
}

export async function getAgentComparisonReports(offset = 0) {
  return readResponse<{ items: AgentComparisonReport[]; total: number; limit: number; offset: number }>(
    await authenticatedFetch(`${API_BASE_URL}/research/agent-comparison-reports?limit=10&offset=${offset}`, { cache: "no-store" }),
  );
}

export async function createAgentComparisonReport() {
  return readResponse<AgentComparisonReport>(await authenticatedFetch(`${API_BASE_URL}/research/agent-comparison-reports`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  }));
}