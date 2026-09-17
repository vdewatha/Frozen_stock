import { useEffect, useRef, useState } from "react";
import { RoleGate } from "@/components/access-control";
import {
  createAgentComparisonReport,
  getAgentComparisonReports,
  type AgentComparisonReport,
  type ComparisonObservation,
} from "@/lib/agent-comparison-api";

const percent = (value: number | null | undefined) =>
  value == null || !Number.isFinite(value) ? "Unavailable" : `${(value * 100).toFixed(1)}%`;
const direction = (value: boolean | null | undefined) =>
  value == null ? "Not scored" : value ? "Correct" : "Incorrect";

function Observation({ item }: { item: ComparisonObservation }) {
  return <tr className="border-t border-line align-top" data-testid={`comparison-observation-${item.run_id}`}>
    <td className="py-3 pr-3"><div className="font-medium">{item.symbol}</div><div className="text-xs">{item.source_cutoff ?? "No cutoff"} → {item.outcome_date ?? "Label pending"}</div></td>
    <td className="py-3 pr-3"><div>{item.agent.recommendation ?? "Unavailable"}</div><div className="text-xs">{item.agent.recommendation === "HOLD" ? "HOLD · not directional" : direction(item.agent.directional_correct)}</div></td>
    <td className="py-3 pr-3"><div>{item.baseline.status === "complete" ? `P(up) ${percent(item.baseline.probability_up)}` : item.baseline.status}</div>
      {item.baseline.status === "complete" && <div className="text-xs">{direction(item.baseline.directional_correct)} · Brier {item.baseline.brier_score?.toFixed(5) ?? "Unavailable"}</div>}
    </td>
    <td className="py-3 pr-3"><span className="font-medium capitalize">{item.status === "complete" ? "Matched" : item.status}</span>
      {item.reason && <p className="mt-1 max-w-xs text-xs text-slate-600">{item.reason}</p>}
      <details className="mt-2 text-xs">
        <summary className="cursor-pointer text-slate-700" data-testid={`comparison-provenance-${item.run_id}`}>Sources & versions</summary>
        <dl className="mt-2 max-w-sm space-y-1 break-words text-slate-600">
          <div><dt className="inline font-medium">Run: </dt><dd className="inline">{item.run_id}</dd></div>
          <div><dt className="inline font-medium">Framework / prompt: </dt><dd className="inline">{item.framework_version} / {item.prompt_version}</dd></div>
          <div><dt className="inline font-medium">Agent model: </dt><dd className="inline">{item.model_name}</dd></div>
          <div><dt className="inline font-medium">Baseline: </dt><dd className="inline">{item.baseline.source ?? "Unavailable"} · prediction {item.baseline.prediction_id ?? "unavailable"}</dd></div>
          <div><dt className="inline font-medium">Baseline model version: </dt><dd className="inline">{item.baseline.model_version ?? "Not recorded by existing model"}</dd></div>
          <div><dt className="inline font-medium">Baseline created: </dt><dd className="inline">{item.baseline.created_at ?? "Unavailable"}</dd></div>
          <div><dt className="inline font-medium">Source digest: </dt><dd className="inline font-mono">{item.source_sha256}</dd></div>
        </dl>
        <pre className="mt-2 max-h-48 max-w-sm overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2">{JSON.stringify(item.source_snapshot, null, 2)}</pre>
      </details>
    </td>
  </tr>;
}

export function ComparisonReportDetail({ snapshot }: { snapshot: AgentComparisonReport }) {
  const { report } = snapshot;
  return <div className="mt-4" data-testid="comparison-report-detail">
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <h3 className="font-semibold">Saved comparison</h3>
      <span className="rounded bg-slate-100 px-2 py-1 capitalize">{report.status}</span>
      <span className="text-xs text-slate-500">{new Date(snapshot.created_at).toLocaleString()}</span>
    </div>
    <p className="mt-2 text-xs text-slate-600">{report.scope.description}</p>
    <p className="mt-1 text-xs text-slate-600">{report.scope.symbols.join(", ") || "No eligible symbols"} · {report.scope.horizon_days} daily-observation horizon · cutoffs {report.scope.source_cutoff_start ?? "unavailable"} – {report.scope.source_cutoff_end ?? "unavailable"}</p>
    <div className="mt-3 grid grid-cols-2 gap-2 text-sm md:grid-cols-4" data-testid="comparison-coverage">
      {[
        ["Matched", report.coverage.matched], ["Pending", report.coverage.pending],
        ["Unavailable", report.coverage.unavailable], ["Matched HOLD (not directional)", report.coverage.hold],
      ].map(([label, value]) => <div className="rounded border border-line p-3" key={label}><div className="text-xs text-slate-500">{label}</div><div className="mt-1 font-semibold">{value}</div></div>)}
    </div>
    <div className="mt-3 grid gap-2 text-sm md:grid-cols-3" data-testid="comparison-metrics">
      <div><span className="text-slate-500">Agent direction accuracy</span><div className="font-semibold">{percent(report.metrics.agent_directional_accuracy)}</div></div>
      <div><span className="text-slate-500">Existing-model direction accuracy</span><div className="font-semibold">{percent(report.metrics.baseline_directional_accuracy)}</div></div>
      <div><span className="text-slate-500">Existing-model Brier score</span><div className="font-semibold">{report.metrics.baseline_brier_score?.toFixed(5) ?? "Unavailable"}</div></div>
    </div>
    <p className="mt-2 text-xs text-slate-600">Both direction scores use the same {report.metrics.directional_pair_count} matched BUY/SELL pairs. HOLD is excluded. Brier uses all {report.coverage.matched} matched existing-model probabilities; no agent probability or Brier score is inferred.</p>
    {report.observations.length > 0 && <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[720px] text-left text-sm">
      <thead><tr><th className="py-2">Symbol / cutoff → outcome</th><th>Agent direction</th><th>Existing model</th><th>Match / provenance</th></tr></thead>
      <tbody>{report.observations.map(item => <Observation item={item} key={item.run_id} />)}</tbody>
    </table></div>}
    <ul className="mt-3 list-disc space-y-1 pl-4 text-xs text-slate-500">{report.limitations.map(text => <li key={text}>{text}</li>)}</ul>
    <details className="mt-3 text-xs text-slate-500">
      <summary className="cursor-pointer" data-testid="comparison-report-identity">Report identity & policy</summary>
      <p className="mt-1 break-all">{snapshot.report_id} · {report.schema_version} · {report.policy_version}</p>
      <p className="mt-1 break-all font-mono">SHA-256 {snapshot.content_sha256}</p>
    </details>
  </div>;
}

export function AgentComparisonReports() {
  const [reports, setReports] = useState<AgentComparisonReport[]>([]);
  const [selected, setSelected] = useState("");
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const requestId = useRef(0);
  const snapshot = reports.find(item => item.report_id === selected) ?? reports[0];

  async function load(nextOffset: number) {
    const id = ++requestId.current;
    setLoading(true);
    setError("");
    try {
      const page = await getAgentComparisonReports(nextOffset);
      if (id !== requestId.current) return;
      setReports(page.items);
      setTotal(page.total);
      setOffset(page.offset);
      setSelected(current => page.items.some(item => item.report_id === current) ? current : page.items[0]?.report_id ?? "");
    } catch (failure) {
      if (id === requestId.current) setError(failure instanceof Error ? failure.message : "Comparison reports unavailable");
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }

  useEffect(() => {
    void load(0);
    return () => { requestId.current++; };
  }, []);

  async function save() {
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const saved = await createAgentComparisonReport();
      await load(0);
      // An unchanged snapshot may be older than the first history page.
      setReports(items => items.some(item => item.report_id === saved.report_id) ? items : [saved, ...items]);
      setSelected(saved.report_id);
      setNotice("Report saved. Unchanged evidence reuses its existing snapshot; earlier reports are preserved.");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Comparison report could not be saved");
    } finally {
      setSaving(false);
    }
  }

  return <section className="rounded-md border border-line bg-white p-4" data-testid="agent-comparison-reports">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-base font-semibold">Exact-match shadow comparison reports</h2>
        <p className="mt-1 max-w-3xl text-sm text-slate-600">Compare the same symbol, source cutoff date, and five-day horizon. Saved snapshots never change and do not authorize promotion, paper trading, or live trading.</p></div>
      <div className="flex gap-2">
        <button data-testid="refresh-comparison-reports" className="focus-ring rounded-md border border-line px-3 py-2 text-sm" disabled={loading || saving} onClick={() => void load(offset)}>{loading ? "Loading…" : "Refresh history"}</button>
        <RoleGate requires="researcher"><button data-testid="save-comparison-report" className="focus-ring rounded-md bg-amber-900 px-3 py-2 text-sm font-semibold text-white" disabled={saving || loading} onClick={() => void save()}>{saving ? "Saving…" : "Save current comparison"}</button></RoleGate>
      </div>
    </div>
    {error && <p className="mt-3 text-sm text-coral" role="alert" data-testid="comparison-error">{error}. Previously loaded snapshots remain historical evidence.</p>}
    {notice && <p className="mt-3 text-sm text-slate-600" role="status" data-testid="comparison-save-notice">{notice}</p>}
    {!loading && !error && reports.length === 0 && <p className="mt-3 text-sm text-slate-500">No saved comparison reports. A researcher can save current evidence, including explicit pending labels and unavailable baseline predictions.</p>}
    {reports.length > 0 && <div className="mt-4 flex flex-wrap items-center gap-2">
      <label className="text-xs font-medium" htmlFor="comparison-snapshot">Snapshot history</label>
      <select data-testid="select-comparison-report" id="comparison-snapshot" className="max-w-full rounded-md border border-line bg-white p-2 text-xs" value={snapshot?.report_id ?? ""} onChange={event => setSelected(event.target.value)}>
        {reports.map(item => <option key={item.report_id} value={item.report_id}>{new Date(item.created_at).toLocaleString()} · {item.report.status} · {item.report_id.slice(0, 8)}</option>)}
      </select>
      <span className="text-xs text-slate-500">{total} saved snapshots</span>
      <button data-testid="comparison-history-newer" className="focus-ring rounded border border-line px-2 py-1 text-xs" disabled={loading || saving || offset === 0} onClick={() => void load(Math.max(0, offset - 10))}>Newer</button>
      <button data-testid="comparison-history-older" className="focus-ring rounded border border-line px-2 py-1 text-xs" disabled={loading || saving || offset + 10 >= total} onClick={() => void load(offset + 10)}>Older</button>
    </div>}
    {snapshot && <ComparisonReportDetail snapshot={snapshot} />}
  </section>;
}