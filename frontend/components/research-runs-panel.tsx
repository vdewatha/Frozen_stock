"use client";

import { useEffect, useState } from "react";
import { getResearchRuns, type ResearchRunSummary } from "@/lib/api";

function score(run: ResearchRunSummary, model: string) {
  const value = run.metrics[model]?.brier_score;
  return Number.isFinite(value) ? value.toFixed(5) : "Unavailable";
}

export function ResearchRunsPanel() {
  const [runs, setRuns] = useState<ResearchRunSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    getResearchRuns().then(page => {
      if (active) { setRuns(page.items); setTotal(page.total); }
    }).catch(failure => {
      if (active) setError(failure instanceof Error ? failure.message : "Registry unavailable");
    }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, []);

  async function refresh() {
    setBusy(true); setError("");
    try {
      const page = await getResearchRuns();
      setRuns(page.items); setTotal(page.total);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Registry unavailable");
    } finally { setBusy(false); }
  }

  return <section className="rounded-md border border-line bg-white p-4">
    <div className="flex items-center justify-between gap-3">
      <h2 className="text-base font-semibold">Offline Research Registry</h2>
      <button className="focus-ring rounded-md border border-line px-3 py-2 text-sm" disabled={busy} onClick={refresh}>{busy ? "Loading…" : "Refresh"}</button>
    </div>
    <p className="mt-2 text-sm text-slate-600">Experimental models only. Brier score measures probability error (lower is better), not profit. Registration never authorizes trading.</p>
    {error && <p className="mt-2 text-sm text-coral" role="alert">{error}. Previously loaded results, if shown, may be stale.</p>}
    {!busy && !error && runs.length === 0 && <p className="mt-3 text-sm text-slate-500">No registered runs in this database. Train and register an offline artifact using the documented CLI.</p>}
    {runs.length > 0 && <>
      <p className="mt-3 text-xs text-slate-500">Latest {runs.length} of {total} registered runs</p>
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
  </section>;
}
