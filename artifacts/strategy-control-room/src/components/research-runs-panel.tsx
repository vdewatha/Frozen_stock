import { useEffect, useState } from "react";
import { RoleGate } from "@/components/access-control";
import {
  getAgentResearchRuns,
  getResearchRuns,
  startAgentResearch,
  type AgentResearchRun,
  type ResearchRunSummary,
} from "@/lib/api";

function score(run: ResearchRunSummary, model: string) {
  const value = run.metrics[model]?.brier_score;
  return Number.isFinite(value) ? value.toFixed(5) : "Unavailable";
}

function agentStatus(run: AgentResearchRun) {
  if (run.status === "completed") return "Complete";
  if (run.status === "unavailable") return "Provider unavailable";
  if (run.status === "failed") return "Failed";
  return run.status.charAt(0).toUpperCase() + run.status.slice(1);
}

export function ResearchRunsPanel() {
  const [runs, setRuns] = useState<ResearchRunSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [agentRuns, setAgentRuns] = useState<AgentResearchRun[]>([]);
  const [agentTotal, setAgentTotal] = useState(0);
  const [symbol, setSymbol] = useState("AAPL");
  const [busy, setBusy] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

  async function refresh() {
    setBusy(true);
    setError("");
    try {
      const [page, agentPage] = await Promise.all([getResearchRuns(), getAgentResearchRuns()]);
      setRuns(page.items);
      setTotal(page.total);
      setAgentRuns(agentPage.items);
      setAgentTotal(agentPage.total);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Research registry unavailable");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    void refresh().finally(() => {
      if (!active) return;
    });
    const timer = window.setInterval(() => {
      if (active) void refresh();
    }, 15_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  async function startRun() {
    setStarting(true);
    setError("");
    try {
      await startAgentResearch(symbol);
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Research request could not be queued");
    } finally {
      setStarting(false);
    }
  }

  return <div className="grid gap-4">
    <section className="rounded-md border border-line bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">Offline Research Registry</h2>
          <p className="mt-1 text-sm text-slate-600">Experimental models only. Brier score measures probability error (lower is better), not profit. Registration never authorizes trading.</p>
        </div>
        <button className="focus-ring rounded-md border border-line px-3 py-2 text-sm" disabled={busy} onClick={() => void refresh()}>{busy ? "Loading…" : "Refresh"}</button>
      </div>
      {error && <p className="mt-2 text-sm text-coral" role="alert">{error}. Previously loaded results, if shown, may be stale.</p>}
      {!busy && !error && runs.length === 0 && <p className="mt-3 text-sm text-slate-500">No registered runs in this database. Train and register an offline artifact using the documented CLI.</p>}
      {runs.length > 0 && <><p className="mt-3 text-xs text-slate-500">Latest {runs.length} of {total} registered runs</p>
        <div className="overflow-x-auto"><table className="mt-2 w-full min-w-[700px] text-left text-sm">
          <thead><tr><th className="py-2">Run / instrument</th><th>Holdout</th><th>Logistic Brier</th><th>Forest Brier</th><th>Baseline Brier</th><th>Trading</th></tr></thead>
          <tbody>{runs.map(run => <tr className="border-t border-line" key={run.run_id}>
            <td className="py-3"><div>{run.symbol} · {run.horizon_bars}-bar horizon</div><code title={run.run_id} className="text-xs text-slate-500">{run.run_id.slice(0, 12)}</code></td>
            <td><div>{run.holdout_rows} observations</div><div className="text-xs text-slate-500">{run.holdout_start.slice(0, 10)} – {run.holdout_end.slice(0, 10)}</div></td>
            <td>{score(run, "logistic_regression")}</td><td>{score(run, "random_forest")}</td><td>{score(run, "training_prevalence_baseline")}</td>
            <td>Not eligible</td>
          </tr>)}</tbody>
        </table></div>
      </>}
    </section>

    <section className="rounded-md border border-amber-200 bg-amber-50 p-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-amber-950">TradingAgents Shadow Research</h2>
          <p className="mt-1 max-w-3xl text-sm leading-5 text-amber-900">Pinned upstream research lane: v0.4.0. This sends bounded, timestamped market/news context to the approved language-model provider and stores a separately evaluated recommendation. It has no broker, order, risk, approval, or model-promotion authority.</p>
        </div>
        <RoleGate requires="researcher">
          <div className="flex items-center gap-2">
            <label className="text-sm font-medium text-amber-950" htmlFor="agent-research-symbol">Symbol</label>
            <select id="agent-research-symbol" className="rounded-md border border-amber-300 bg-white px-2 py-2 text-sm" value={symbol} onChange={event => setSymbol(event.target.value)}>
              {["AAPL", "MSFT", "QQQ", "SPY"].map(value => <option key={value}>{value}</option>)}
            </select>
            <button className="focus-ring rounded-md bg-amber-900 px-3 py-2 text-sm font-semibold text-white" disabled={starting} onClick={() => void startRun()}>{starting ? "Queueing…" : "Run research"}</button>
          </div>
        </RoleGate>
      </div>
      {agentTotal === 0 && <p className="mt-4 text-sm text-amber-900">No shadow runs yet. Missing provider access is shown as unavailable; no placeholder recommendation is created.</p>}
      {agentRuns.length > 0 && <div className="mt-4 grid gap-3">
        {agentRuns.map(run => <article className="rounded-md border border-amber-200 bg-white p-3" key={run.run_id}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="font-semibold">{run.symbol} <span className="ml-2 rounded-full bg-slate-100 px-2 py-1 text-xs font-medium text-slate-700">{agentStatus(run)}</span></div>
            <span className="text-xs text-slate-500">{new Date(run.created_at).toLocaleString()}</span>
          </div>
          {run.result ? <><p className="mt-2 text-sm"><span className="font-semibold">Recommendation:</span> {run.result.recommendation} · confidence {Math.round(run.result.confidence * 100)}%</p><p className="mt-1 text-sm leading-5 text-slate-700">{run.result.rationale}</p></> : run.error ? <p className="mt-2 text-sm text-coral">{run.error}</p> : <p className="mt-2 text-sm text-slate-600">The bounded run is {run.status}; this panel refreshes automatically.</p>}
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500"><span>{run.framework_version}</span><span>{run.prompt_version}</span><span>Forward evaluation: {run.evaluation.status}</span><span>Baseline comparison: {run.evaluation.comparison?.status ?? "pending"}</span><span>Not eligible for trading</span></div>
          {run.evaluation.status === "complete" && <p className="mt-2 text-xs text-slate-600">Observed {run.evaluation.outcome_date}: {run.evaluation.outcome_up ? "up" : "down"} ({(Number(run.evaluation.outcome_return ?? 0) * 100).toFixed(2)}%). The agent recommendation is scored directionally only; it is not a calibrated probability.</p>}
          {run.result?.limitations?.length ? <p className="mt-2 text-xs text-slate-500">Limitations: {run.result.limitations.join(" · ")}</p> : null}
        </article>)}
      </div>}
    </section>
  </div>;
}