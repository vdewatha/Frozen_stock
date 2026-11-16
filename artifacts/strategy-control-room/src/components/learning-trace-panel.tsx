import { useQuery } from "@tanstack/react-query";
import { Route } from "lucide-react";
import { API_BASE_URL, authenticatedFetch, getErrorMessage, readJson } from "@/lib/api";
import { StatusPill } from "@/components/status-pill";

type Outcome = "no_change" | "watching" | "retained" | "reverted" | "closed";

export type LearningChain = {
  experiment_id: number;
  ran_at: string | null;
  strategy_type: string | null;
  template: string | null;
  diagnosis: { lesson_id: string | null; cause: string | null; lesson: string | null; losses: number | null; scope: string | null; source: string | null; attribution_ids: number[] };
  experiment: { status: string; adopted: string | null; description: string | null; setups: number | null; cost_seconds: number | null };
  change: { change_id: string; adopted_at: string | null; description: string | null } | null;
  forward: {
    state: string | null; verdict: string | null; judged_at: string | null; trades_after: number; trades_needed: number; sample_sufficient: boolean;
    expectancy_before_bps: number | null; expectancy_after_bps: number | null; before_after_bps: number | null;
    realised_gain_bps: number | null; realised_source: string | null; comparison: string;
  } | null;
  outcome: Outcome;
  complete: boolean;
  gaps: string[];
};

export type LearningTrace = {
  summary: {
    chains: number; with_lesson_id: number; complete: number; changes_adopted: number; outcomes: Record<Outcome, number>;
    retained_on_sufficient_sample: number; research_seconds: number | null; experiments_with_cost: number;
  };
  evidence: string;
  chains: LearningChain[];
  unlinked_changes: string[];
  note: string;
};

async function loadTrace(): Promise<LearningTrace> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock-paper/learning-trace`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Learning trace unavailable (${response.status})`);
  return readJson(response);
}

const OUTCOME_LABEL: Record<Outcome, string> = { no_change: "No change", watching: "Watching", retained: "Retained", reverted: "Reverted", closed: "Inconclusive" };
const OUTCOME_PILL: Record<Outcome, string> = { no_change: "neutral", watching: "collecting", retained: "passed", reverted: "failed", closed: "inconclusive" };

function label(value: string | null) { return value ? value.replaceAll("_", " ") : "n/a"; }
function bps(value: number | null) { return value === null ? "n/a" : `${value > 0 ? "+" : ""}${value.toFixed(1)} bps`; }
function seconds(value: number | null) { return value === null ? "not recorded" : value >= 60 ? `${(value / 60).toFixed(1)} min` : `${value.toFixed(1)} s`; }

export function LearningTraceView({ trace }: { trace: LearningTrace }) {
  const { summary } = trace;
  return <div className="short-monitor space-y-8">
    <section>
      <div className="section-heading"><h2><Route size={17} className="mr-2 inline" />Does the learning improve outcomes?</h2>
        <span className="section-meta">{summary.chains} experiments · {summary.complete} fully traced</span></div>
      <p className="section-meta">{trace.evidence}</p>
      <dl className="short-monitor-metrics">
        <div><dt>Changes adopted</dt><dd>{summary.changes_adopted}<small>of {summary.chains} experiments</small></dd></div>
        <div><dt>Retained</dt><dd>{summary.outcomes.retained}<small>{summary.retained_on_sufficient_sample} on a sufficient sample</small></dd></div>
        <div><dt>Reverted</dt><dd>{summary.outcomes.reverted}<small>{summary.outcomes.watching} still watching</small></dd></div>
        <div><dt>Research cost</dt><dd>{seconds(summary.research_seconds)}<small>{summary.experiments_with_cost} experiments timed</small></dd></div>
      </dl>
      {trace.unlinked_changes.length > 0 && <p role="alert" className="workspace-error">Changes with no experiment behind them: {trace.unlinked_changes.join(", ")}</p>}
    </section>

    <section>
      <div className="section-heading"><h2>From lesson to outcome</h2><span className="section-meta">{trace.chains.length} shown, newest first</span></div>
      <p className="section-meta">{trace.note}</p>
      {trace.chains.length ? <div className="short-monitor-table"><table>
        <thead><tr><th>Diagnosed problem</th><th>Experiment</th><th>Change</th><th>Forward evaluation</th><th>Outcome</th></tr></thead>
        <tbody>{trace.chains.map(chain => <tr key={chain.experiment_id}>
          <th scope="row">{label(chain.diagnosis.cause)}<small className="block font-normal">{chain.diagnosis.losses ?? "n/a"} losses · {label(chain.diagnosis.scope)}</small>
            <small className="block font-normal">{chain.diagnosis.lesson_id ? `lesson ${chain.diagnosis.lesson_id} · ${chain.diagnosis.attribution_ids.length} trades named` : "no lesson id"}</small></th>
          <td>{label(chain.strategy_type)} · {label(chain.template)}<small className="block">{chain.ran_at ? chain.ran_at.slice(0, 10) : "n/a"} · {chain.experiment.setups ?? "n/a"} setups · {seconds(chain.experiment.cost_seconds)}</small></td>
          <td>{chain.change ? <>{chain.change.description ?? chain.change.change_id}<small className="block">adopted {chain.change.adopted_at ? chain.change.adopted_at.slice(0, 10) : "n/a"}</small></> : "None adopted"}</td>
          <td>{chain.forward ? <>{chain.forward.trades_after} of {chain.forward.trades_needed} trades
            <small className="block">before {bps(chain.forward.expectancy_before_bps)} · after {bps(chain.forward.expectancy_after_bps)}</small>
            <small className="block">realised {bps(chain.forward.realised_gain_bps)} · {chain.forward.comparison}</small></> : chain.change ? "Not judged yet" : "n/a"}</td>
          <td><StatusPill status={OUTCOME_PILL[chain.outcome]} /><small className="block">{OUTCOME_LABEL[chain.outcome]}</small>
            {chain.gaps.map(gap => <small key={gap} className="block">Gap: {gap}</small>)}</td>
        </tr>)}</tbody></table></div>
        : <p className="section-meta">No experiment has run yet.</p>}
    </section>
  </div>;
}

export function LearningTracePanel() {
  const query = useQuery({ queryKey: ["learning-trace"], queryFn: loadTrace, refetchInterval: 300_000, retry: 1 });
  if (query.error) return <p role="alert" className="workspace-error">{getErrorMessage(query.error, "Learning trace unavailable")}</p>;
  if (!query.data) return <p role="status">Loading learning trace...</p>;
  return <LearningTraceView trace={query.data} />;
}
