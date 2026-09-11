import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Database,
  FileCheck2,
  LockKeyhole,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
} from "lucide-react";

import { RoleGate, canAccess, useAccessRole } from "@/components/access-control";
import { StatusPill } from "@/components/status-pill";
import {
  bindStockModel,
  cancelStockTraining,
  getActiveStockModelBinding,
  getStockTrainingJob,
  getStockTrainingJobs,
  getStockTrainingReport,
  getErrorMessage,
  startStockTraining,
  type StockDatasetSnapshot,
  type StockModelArtifact,
  type StockModelBinding,
  type StockTrainingComparison,
  type StockTrainingJob,
  type StockTrainingJobDetail,
  type StockTrainingMetric,
  type StockTrainingReport,
  type StockValidationSummary,
} from "@/lib/api";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 2 });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const decimal = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });
const TERMINAL_STATUSES = new Set(["complete", "completed", "succeeded", "failed", "cancelled", "canceled"]);
const ACTIVE_STATUSES = new Set(["queued", "running", "cancel_requested", "pending", "deferred"]);

function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.has(status.toLowerCase());
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "n/a";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

type ComparisonMetric = "calibration" | "brier_score" | "log_loss" | "cost_adjusted_return" | "net_return" | "max_drawdown" | "drawdown" | "sample_count" | "count" | "losing_count" | "losses";

function metric(row: StockTrainingComparison, name: ComparisonMetric): number | null {
  const value = row[name] ?? row.metrics?.[name as keyof NonNullable<StockTrainingComparison["metrics"]>];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function metricText(row: StockTrainingComparison, name: ComparisonMetric, asPercent = false): string {
  const value = metric(row, name);
  if (value === null) return "n/a";
  return asPercent ? percent.format(value) : number.format(value);
}

function decimalMetricText(row: StockTrainingComparison, name: ComparisonMetric): string {
  const value = metric(row, name);
  return value === null ? "n/a" : decimal.format(value);
}

function phaseLabel(phase: StockTrainingComparison["phase"]): string {
  switch (phase) {
    case "purged_walkforward":
      return "Purged validation";
    case "calibration":
      return "Calibration";
    case "final_untouched_holdout":
      return "Untouched holdout";
    default:
      return phase || "Unspecified phase";
  }
}

function kindLabel(kind: StockTrainingComparison["kind"]): string {
  return kind === "baseline" ? "Baseline" : "Model";
}

function stockSymbols(raw: string): { symbols: string[]; error: string | null } {
  const symbols = [...new Set(raw.split(/[,\s]+/).map(symbol => symbol.trim().toUpperCase()).filter(Boolean))];
  if (!symbols.length) return { symbols: [], error: "Add at least one stock ticker." };
  if (symbols.length > 1) return { symbols, error: "The deployed verified trainer currently accepts one stock ticker per run." };
  const invalid = symbols.filter(symbol => !/^[A-Z][A-Z0-9.-]{0,15}$/.test(symbol));
  if (invalid.length) return { symbols, error: `Invalid stock ticker: ${invalid.join(", ")}.` };
  // The stock training surface must never dispatch a crypto/experimental run.
  const crypto = symbols.filter(symbol => ["BTC", "ETH", "XBT", "USD"].includes(symbol));
  if (crypto.length) return { symbols, error: `${crypto.join(", ")} is not eligible for stock training.` };
  return { symbols, error: null };
}

function snapshotFor(detail: StockTrainingJobDetail | null, report: StockTrainingReport | null): StockDatasetSnapshot | null {
  const nested = report?.dataset_snapshot ?? report?.snapshot ?? detail?.snapshot;
  if (nested) return nested;
  if (report?.dataset_snapshot_id) {
    return {
      snapshot_id: report.dataset_snapshot_id,
      verified: true,
      provider: report.provider ?? "verified provider",
      cutoff_at: report.cutoff_date ?? "",
      universe: report.universe ?? [],
      features: report.feature_config_id ? [report.feature_config_id] : [],
      hash: report.dataset_sha256 ?? report.dataset_artifact_sha256 ?? null,
    };
  }
  return null;
}

function modelFor(detail: StockTrainingJobDetail | null, report: StockTrainingReport | null): StockModelArtifact | null {
  const nested = report?.model ?? detail?.model;
  if (nested) return nested;
  if (report?.selected_model) {
    return {
      model_id: report.run_id,
      model_version: report.selected_model,
      status: report.status,
      hash: report.report_hash ?? report.hash ?? null,
      eligible_for_binding: report.eligible_for_binding,
      binding_blockers: report.binding_blockers,
    };
  }
  return null;
}

function comparisonRows(report: StockTrainingReport | null): StockTrainingComparison[] {
  if (!report) return [];
  const rows: StockTrainingComparison[] = [];
  const seen = new Set<string>();
  const appendRow = (row: StockTrainingComparison | null | undefined, phase?: StockTrainingComparison["phase"], kind?: StockTrainingComparison["kind"]) => {
    if (!row) return;
    const normalized = { ...row, phase: row.phase ?? phase, kind: row.kind ?? kind };
    const key = `${normalized.phase ?? "unknown"}:${normalized.kind ?? "unknown"}:${normalized.name}`;
    if (seen.has(key)) return;
    seen.add(key);
    rows.push(normalized);
  };

  const allComparisons = report.comparisons ?? [];
  const phaseRows = (phase: StockTrainingComparison["phase"]) => allComparisons.filter(row => row.phase === phase);

  // Prefer the report's phase-specific collections. They make it impossible to
  // mistake a holdout baseline for a validation model when the same model name
  // appears in more than one phase.
  const walkforward = report.validation?.walkforward_comparisons?.length
    ? report.validation.walkforward_comparisons
    : phaseRows("purged_walkforward");
  walkforward.forEach(row => appendRow(row, "purged_walkforward", "model"));

  const calibration = report.validation?.calibration_comparisons?.length
    ? report.validation.calibration_comparisons
    : phaseRows("calibration");
  calibration.forEach(row => appendRow(row, "calibration"));

  const holdoutModel = report.holdout?.chosen_model_metrics;
  const holdoutBaseline = report.holdout?.baseline_metrics;
  if (holdoutModel || holdoutBaseline) {
    appendRow(holdoutModel, "final_untouched_holdout", "model");
    appendRow(holdoutBaseline, "final_untouched_holdout", "baseline");
  } else {
    phaseRows("final_untouched_holdout").forEach(row => appendRow(row, "final_untouched_holdout"));
  }

  // Preserve rows from newer report producers that add a phase we do not yet
  // know about, while keeping their explicit phase/kind labels intact.
  allComparisons.forEach(row => appendRow(row));

  // Legacy reports may expose only metric maps. Keep their phase boundaries
  // explicit rather than presenting a holdout baseline as a validation model.
  if (!rows.length) {
    const append = (name: string, values: StockTrainingMetric | undefined, phase: StockTrainingComparison["phase"], kind: StockTrainingComparison["kind"]) => {
      if (!values) return;
      const returns = values.cost_aware_nonoverlapping_returns;
      appendRow({
        name,
        phase,
        kind,
        calibration: values.calibration ?? values.brier_score ?? null,
        brier_score: values.brier_score ?? null,
        log_loss: values.log_loss ?? null,
        gross_return: typeof returns?.total_return === "number" ? returns.total_return : null,
        net_return: typeof returns?.total_return === "number" ? returns.total_return : null,
        cost_adjusted_return: typeof returns?.total_return === "number" ? returns.total_return : null,
        max_drawdown: typeof returns?.max_drawdown === "number" ? returns.max_drawdown : null,
        sample_count: typeof returns?.sample_count === "number" ? returns.sample_count : values.sample_count ?? null,
        losing_count: typeof returns?.losses === "number" ? returns.losses : values.losses ?? null,
        metrics: values,
      });
    };
    Object.entries(report.walkforward_metrics ?? {}).forEach(([name, values]) => append(name, values, "purged_walkforward", "model"));
    Object.entries(report.calibration_metrics ?? {}).forEach(([name, values]) => append(name, values, "calibration", name === "training_prevalence_baseline" ? "baseline" : "model"));
    append("Selected model", report.final_holdout_metrics ?? undefined, "final_untouched_holdout", "model");
    append("Training prevalence baseline", report.final_holdout_baseline ?? undefined, "final_untouched_holdout", "baseline");
  }
  return rows;
}

function reportFailures(report: StockTrainingReport | null): string[] {
  if (!report) return [];
  const validation = report.validation ?? {};
  const holdout = report.holdout ?? {};
  return [...new Set([
    ...(report.failures ?? []),
    ...(report.failure_reasons ?? []),
    ...(validation.failures ?? []),
    ...(validation.failure_reasons ?? []),
    ...(holdout.failures ?? []),
    ...(holdout.failure_reasons ?? []),
  ])];
}

function statusMessage(job: StockTrainingJob | null): string {
  if (!job) return "Select a durable stock training job to inspect its immutable artifacts.";
  if (job.error || job.failure_reason) return job.error ?? job.failure_reason ?? "Training failed.";
  return job.message ?? `Job ${job.status}.`;
}

function SnapshotCard({ snapshot }: { snapshot: StockDatasetSnapshot | null }) {
  if (!snapshot) {
    return <div className="rounded-md border border-line bg-panel p-3 text-sm text-slate-500">No verified dataset snapshot attached to this job.</div>;
  }
  return (
    <div className="rounded-md border border-line bg-panel p-3 text-sm">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 font-semibold"><Database size={16} className="text-mint" /> Immutable dataset snapshot</div>
        <StatusPill status={snapshot.verified ? "verified" : snapshot.status ?? "invalid"} />
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <div><span className="text-slate-500">Snapshot</span><div className="break-all font-mono text-xs">{snapshot.snapshot_id}</div></div>
        <div><span className="text-slate-500">Provider</span><div>{snapshot.provider}{snapshot.provider_version ? ` · ${snapshot.provider_version}` : ""}</div></div>
        <div><span className="text-slate-500">Cutoff</span><div>{formatDate(snapshot.cutoff_at)}</div></div>
        <div><span className="text-slate-500">Rows / universe</span><div>{snapshot.rows == null ? "n/a" : number.format(snapshot.rows)} · {snapshot.universe.join(", ") || "n/a"}</div></div>
        <div className="sm:col-span-2"><span className="text-slate-500">Features</span><div>{snapshot.features.join(", ") || "n/a"}</div></div>
        <div className="sm:col-span-2"><span className="text-slate-500">Content hash</span><div className="break-all font-mono text-xs">{snapshot.sha256 ?? snapshot.hash ?? "n/a"}</div></div>
      </div>
       <div className="mt-3 flex items-start gap-2 rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900"><AlertTriangle size={14} className="mt-0.5 shrink-0" /><span><strong>Temporal caveat:</strong> this immutable artifact is a captured provider extraction, not a point-in-time historical dataset. Older-cutoff snapshots are research-only and cannot be bound.</span></div>
      {!snapshot.verified ? <div className="mt-3 flex items-center gap-2 rounded border border-red-200 bg-red-50 p-2 text-xs text-coral"><AlertTriangle size={14} /> This snapshot is not verified and cannot qualify for paper binding.</div> : null}
    </div>
  );
}

function ComparisonTable({ rows }: { rows: StockTrainingComparison[] }) {
  return (
    <div className="overflow-x-auto rounded-md border border-line">
      <table className="w-full min-w-[980px] text-left text-xs">
        <thead className="bg-panel uppercase text-slate-500">
          <tr>
            <th className="px-3 py-2">Phase</th>
            <th className="px-3 py-2">Model / baseline</th>
            <th className="px-3 py-2">Calibration / Brier</th>
            <th className="px-3 py-2">Log loss</th>
            <th className="px-3 py-2">Cost-adjusted return</th>
            <th className="px-3 py-2">Max drawdown</th>
            <th className="px-3 py-2">Samples</th>
            <th className="px-3 py-2">Losing results</th>
          </tr>
        </thead>
        <tbody>
          {rows.length ? rows.map((row, index) => (
            <tr className="border-t border-line" key={`${row.name}-${index}`}>
              <td className="px-3 py-3 whitespace-nowrap"><span className="font-semibold">{phaseLabel(row.phase)}</span></td>
              <td className="px-3 py-3"><div className="font-semibold">{row.name}</div><div className={`mt-1 text-[10px] font-semibold uppercase tracking-wide ${row.kind === "baseline" ? "text-amber-700" : "text-mint"}`}>{kindLabel(row.kind)}</div></td>
              <td className="px-3 py-3">{decimalMetricText(row, "calibration")}{metric(row, "brier_score") !== null ? ` · Brier ${decimalMetricText(row, "brier_score")}` : ""}</td>
              <td className="px-3 py-3">{decimalMetricText(row, "log_loss")}</td>
              <td className={`px-3 py-3 font-medium ${(metric(row, "cost_adjusted_return") ?? metric(row, "net_return") ?? 0) < 0 ? "text-coral" : "text-mint"}`}>{metricText(row, "cost_adjusted_return", true) !== "n/a" ? metricText(row, "cost_adjusted_return", true) : metricText(row, "net_return", true)}</td>
              <td className="px-3 py-3">{metricText(row, "max_drawdown", true) !== "n/a" ? metricText(row, "max_drawdown", true) : metricText(row, "drawdown", true)}</td>
              <td className="px-3 py-3">{metricText(row, "sample_count") !== "n/a" ? metricText(row, "sample_count") : metricText(row, "count")}</td>
              <td className={`px-3 py-3 ${(metric(row, "losing_count") ?? metric(row, "losses") ?? 0) > 0 ? "text-coral" : "text-slate-600"}`}>{metricText(row, "losing_count") !== "n/a" ? metricText(row, "losing_count") : metricText(row, "losses")}</td>
            </tr>
          )) : <tr><td className="px-3 py-4 text-slate-500" colSpan={8}>No comparison results were returned. A missing baseline is not treated as a pass.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function ValidationCard({ title, summary }: { title: string; summary: StockValidationSummary | null | undefined }) {
  if (!summary) return <div className="rounded-md border border-line p-3 text-sm text-slate-500">{title}: unavailable</div>;
  const failures = [...(summary.failures ?? []), ...(summary.failure_reasons ?? [])];
  return (
    <div className="rounded-md border border-line p-3 text-sm">
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold">{title}</span>
        <StatusPill status={summary.status ?? "unknown"} />
      </div>
      <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-slate-600">
        <span>Folds <strong>{summary.folds ?? "n/a"}</strong></span>
        <span>Purged <strong>{summary.purged_rows ?? "n/a"}</strong></span>
        <span>Embargo <strong>{summary.embargo_rows ?? "n/a"}</strong></span>
        <span>Samples <strong>{summary.holdout_rows ?? summary.validation_rows ?? "n/a"}</strong></span>
        <span className="col-span-2">Window <strong>{summary.holdout_start ?? "n/a"} – {summary.holdout_end ?? "n/a"}</strong></span>
      </div>
      {failures.length ? <div className="mt-3 grid gap-1 rounded border border-red-200 bg-red-50 p-2 text-xs text-coral">{[...new Set(failures)].map(failure => <span key={failure}>• {failure}</span>)}</div> : null}
    </div>
  );
}

export function StockTrainingLab() {
  const role = useAccessRole();
  const [universe, setUniverse] = useState("SPY");
  const [cutoff, setCutoff] = useState(new Date().toISOString().slice(0, 10));
  const [horizon, setHorizon] = useState("5");
  const [trigger, setTrigger] = useState<"manual" | "scheduled">("manual");
  const [jobs, setJobs] = useState<StockTrainingJob[]>([]);
  const [selectedJob, setSelectedJob] = useState<StockTrainingJobDetail | null>(null);
  const [report, setReport] = useState<StockTrainingReport | null>(null);
  const [binding, setBinding] = useState<StockModelBinding | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("Loading verified stock training jobs");
  const [error, setError] = useState("");
  const [bindingConfirmed, setBindingConfirmed] = useState(false);
  const [bindingReason, setBindingReason] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);

  const snapshot = useMemo(() => snapshotFor(selectedJob, report), [selectedJob, report]);
  const model = useMemo(() => modelFor(selectedJob, report), [selectedJob, report]);
  const comparisons = useMemo(() => comparisonRows(report), [report]);
  const failures = reportFailures(report);
  const canStart = canAccess(role, "researcher");
  const canBind = canAccess(role, "operator");
  const selectedJobId = selectedJob?.id ?? null;
  const canCancel = Boolean(selectedJob && ACTIVE_STATUSES.has(selectedJob.status.toLowerCase()) && canStart);

  async function loadJob(job: StockTrainingJob): Promise<void> {
    setBusy(true);
    setError("");
    try {
      const detail = await getStockTrainingJob(job.id);
      setSelectedJob(detail);
      const reportId = detail.result_run_id ?? detail.run_id ?? detail.report?.run_id;
      if (detail.report) setReport(detail.report);
      else if (reportId && (detail.report_available || isTerminal(detail.status))) setReport(await getStockTrainingReport(reportId));
      else setReport(null);
      setStatus(statusMessage(detail));
    } catch (failure) {
      setError(getErrorMessage(failure, "Stock training job details unavailable."));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    async function refresh() {
      try {
        const page = await getStockTrainingJobs();
        const currentBinding = await getActiveStockModelBinding();
        if (disposed) return;
        setJobs(page.items);
        setBinding(currentBinding);
        const active = page.items.find(job => ACTIVE_STATUSES.has(job.status.toLowerCase()));
        if (active && (!selectedJobId || String(selectedJobId) === String(active.id))) {
          const detail = await getStockTrainingJob(active.id);
          if (!disposed) {
            setSelectedJob(detail);
            if (detail.report) setReport(detail.report);
            setStatus(statusMessage(detail));
          }
        }
         if (!disposed) {
           timer = window.setTimeout(refresh, active ? 4000 : 8000);
           if (!active) setStatus(page.items.length ? "Stock training jobs loaded; watching for new work." : "No stock training jobs yet; watching for new work.");
         }
      } catch (failure) {
        if (!disposed) {
          setError(getErrorMessage(failure, "Stock training jobs unavailable."));
          timer = window.setTimeout(refresh, 8000);
        }
      }
    }
    void refresh();
    return () => {
      disposed = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [refreshTick, selectedJobId]);

  async function startTraining(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = stockSymbols(universe);
    if (parsed.error) {
      setError(parsed.error);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const job = await startStockTraining({
        symbols: parsed.symbols,
        cutoff_at: cutoff,
        horizon_bars: Number(horizon),
        provider: "yfinance",
        trigger,
        seed: 42,
      });
      setStatus(`Stock training ${job.status}. Snapshot verification is server-owned.`);
      setSelectedJob(job as StockTrainingJobDetail);
      setReport(null);
      setRefreshTick(value => value + 1);
    } catch (failure) {
      setError(getErrorMessage(failure, "Stock training could not be queued."));
    } finally {
      setBusy(false);
    }
  }

  async function cancelSelected() {
    if (!selectedJob) return;
    setBusy(true);
    setError("");
    try {
      const cancelled = await cancelStockTraining(selectedJob.id);
      setSelectedJob({ ...selectedJob, ...cancelled });
      setStatus(cancelled.message ?? "Cancellation requested.");
      setRefreshTick(value => value + 1);
    } catch (failure) {
      setError(getErrorMessage(failure, "Cancellation failed."));
    } finally {
      setBusy(false);
    }
  }

  async function bindSelected() {
    if (!selectedJob || !model || !snapshot || !bindingConfirmed || !bindingReason.trim()) return;
    setBusy(true);
    setError("");
    try {
      const nextBinding = await bindStockModel({
        model_id: model.model_id ?? model.model_version,
        snapshot_id: snapshot.snapshot_id,
        confirmation: "PAPER_ONLY_FROZEN_BINDING",
        purpose: "forward_paper_evaluation",
        reason: bindingReason.trim(),
      });
      setBinding(nextBinding);
      setBindingConfirmed(false);
      setBindingReason("");
      setStatus("Paper-only frozen model binding is active.");
    } catch (failure) {
      setError(getErrorMessage(failure, "Model binding was rejected."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="scroll-mt-4 rounded-md border border-line bg-white" id="stock-training">
      <div className="flex flex-col gap-3 border-b border-line p-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="flex items-center gap-2 text-mint"><ShieldCheck size={19} /><span className="text-xs font-semibold uppercase tracking-wide">Verified stock research</span></div>
          <h2 className="mt-1 text-lg font-semibold">Reproducible Stock Model Training</h2>
          <p className="mt-1 max-w-3xl text-sm text-slate-600">Every run binds an immutable provider snapshot, provenance and content hash. Validation is purged and embargoed, with an untouched holdout. This surface is stock-only and paper-only; it does not promote scheduled challengers.</p>
        </div>
        <div className="flex shrink-0 items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-semibold text-mint"><LockKeyhole size={14} /> Live trading disabled</div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[340px_1fr]">
        <div className="grid content-start gap-3">
          <form className="rounded-md border border-line bg-panel p-3" onSubmit={startTraining}>
            <div className="mb-3 flex items-center gap-2 font-semibold"><Play size={16} className="text-mint" /> Queue verified training</div>
            <div className="grid gap-3 text-sm">
              <label className="grid gap-1"><span className="font-medium">Stock universe</span><input className="focus-ring h-9 rounded border border-line bg-white px-2 uppercase" value={universe} onChange={event => setUniverse(event.target.value)} placeholder="SPY, QQQ, IWM" /><span className="text-xs text-slate-500">Tickers only. Crypto and experimental symbols are rejected.</span></label>
              <label className="grid gap-1"><span className="font-medium">Dataset cutoff</span><input className="focus-ring h-9 rounded border border-line bg-white px-2" type="date" value={cutoff} onChange={event => setCutoff(event.target.value)} /></label>
              <label className="grid gap-1"><span className="font-medium">Prediction horizon (bars)</span><select className="focus-ring h-9 rounded border border-line bg-white px-2" value={horizon} onChange={event => setHorizon(event.target.value)}><option value="1">1 bar</option><option value="5">5 bars</option><option value="20">20 bars</option></select><span className="text-xs text-slate-500">The server owns purge, embargo and cost assumptions.</span></label>
              <label className="grid gap-1"><span className="font-medium">Run trigger</span><select className="focus-ring h-9 rounded border border-line bg-white px-2" value={trigger} onChange={event => setTrigger(event.target.value as "manual" | "scheduled")}><option value="manual">Manual verified run</option><option value="scheduled">Scheduled challenger</option></select></label>
              {trigger === "scheduled" ? <div className="rounded border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">Scheduled runs are challengers only. They never replace or promote the frozen paper model automatically.</div> : null}
              <RoleGate requires="researcher"><button className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded bg-mint px-3 text-sm font-semibold text-white" disabled={busy || !canStart} type="submit"><Play size={15} />{busy ? "Queueing…" : "Verify snapshot & train"}</button></RoleGate>
            </div>
          </form>
          <div className="rounded-md border border-line p-3 text-xs text-slate-600">
            <div className="flex items-center gap-2 font-semibold text-ink"><FileCheck2 size={15} className="text-mint" /> Qualification gates</div>
            <ul className="mt-2 grid gap-1">
              <li>• Verified provider data and cutoff are required.</li>
              <li>• Features and labels are versioned with the snapshot.</li>
              <li>• Purge / embargo protects folds from horizon overlap.</li>
              <li>• Holdout results remain visible, including losses.</li>
            </ul>
          </div>
          <div className="rounded-md border border-line p-3">
            <div className="flex items-center justify-between gap-2"><div className="flex items-center gap-2 text-sm font-semibold"><Clock3 size={15} className="text-mint" /> Jobs</div><button className="focus-ring rounded border border-line p-1.5" aria-label="Refresh stock training jobs" disabled={busy} onClick={() => setRefreshTick(value => value + 1)} type="button"><RefreshCw size={14} /></button></div>
            <div className="mt-2 grid gap-1">
              {jobs.length ? jobs.map(job => <button className={`focus-ring rounded border p-2 text-left text-xs ${selectedJobId != null && String(selectedJobId) === String(job.id) ? "border-mint bg-emerald-50" : "border-line bg-panel"}`} key={job.id} onClick={() => void loadJob(job)} type="button"><div className="flex items-center justify-between gap-2"><span className="font-semibold">{job.trigger === "scheduled" ? "Scheduled challenger" : "Manual run"}</span><StatusPill status={job.status} /></div><div className="mt-1 text-slate-500">{formatDate(job.created_at)} · {job.dataset_snapshot_id ?? "snapshot pending"}</div></button>) : <div className="text-xs text-slate-500">No durable jobs returned.</div>}
            </div>
          </div>
        </div>

        <div className="grid content-start gap-4">
          {error ? <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-coral" role="alert"><AlertTriangle size={16} className="mt-0.5 shrink-0" />{error}</div> : null}
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line p-3">
            <div><div className="text-xs font-semibold uppercase text-slate-500">Selected job</div><div className="mt-1 text-sm font-medium">{selectedJob ? `${selectedJob.id} · ${selectedJob.status}` : "None selected"}</div><div className="mt-1 text-xs text-slate-500">{status}</div></div>
            <div className="flex flex-wrap gap-2">
              {selectedJob && !isTerminal(selectedJob.status) ? <RoleGate requires="researcher"><button className="focus-ring inline-flex h-9 items-center gap-2 rounded border border-red-200 px-3 text-xs font-semibold text-coral" disabled={busy || !canCancel} onClick={() => void cancelSelected()} type="button"><Square size={14} />Cancel job</button></RoleGate> : null}
              <button className="focus-ring inline-flex h-9 items-center gap-2 rounded border border-line px-3 text-xs font-semibold" disabled={busy || !selectedJob} onClick={() => selectedJob && void loadJob(selectedJob)} type="button"><RefreshCw size={14} />Refresh detail</button>
            </div>
          </div>

          {selectedJob || report ? <div className="grid gap-4">
            {selectedJob && (selectedJob.failure_detail || selectedJob.failure_code || selectedJob.queue_error) ? <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-coral"><div className="flex items-center gap-2 font-semibold"><AlertTriangle size={16} /> Durable job failure</div><div className="mt-2 grid gap-1 text-xs"><div>{selectedJob.failure_code ?? "queue_failure"}: {selectedJob.failure_detail ?? selectedJob.queue_error ?? "The worker did not complete this job."}</div><div>This failure is retained for recovery/audit and does not qualify a model for binding.</div></div></div> : null}
            <SnapshotCard snapshot={snapshot} />
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="rounded-md border border-line p-3 text-sm">
                <div className="flex items-center gap-2 font-semibold"><FileCheck2 size={16} className="text-mint" /> Model artifact</div>
                 {model ? <div className="mt-3 grid gap-2 text-xs text-slate-600"><div>Version <strong className="text-ink">{model.model_version}</strong></div><div>Status <StatusPill status={model.status ?? "challenger"} /></div><div>Artifact hash <strong className="break-all font-mono text-ink">{model.artifact_hash ?? model.sha256 ?? model.hash ?? "n/a"}</strong></div><div>Created {formatDate(model.created_at)}</div><div className="flex items-center gap-2">Binding eligibility <StatusPill status={model.eligible_for_binding === false ? "binding_blocked" : "binding_eligible"} /></div>{model.temporal_caveat ? <div className="rounded border border-amber-200 bg-amber-50 p-2 text-amber-900">{model.temporal_caveat}</div> : null}{model.eligible_for_binding === false ? <div className="rounded border border-red-200 bg-red-50 p-2 text-coral"><strong>Backend binding blockers</strong><ul className="mt-1 list-disc pl-4">{(model.binding_blockers?.length ? model.binding_blockers : ["This model is marked research-only by the backend."]).map(blocker => <li key={blocker}>{blocker}</li>)}</ul></div> : null}</div> : <div className="mt-2 text-slate-500">Model artifact is not available until training completes.</div>}
              </div>
              <div className="rounded-md border border-line p-3 text-sm">
                <div className="flex items-center gap-2 font-semibold"><Database size={16} className="text-mint" /> Report provenance</div>
                {report ? <div className="mt-3 grid gap-2 text-xs text-slate-600"><div>Report hash <strong className="break-all font-mono text-ink">{report.report_hash ?? report.hash ?? "n/a"}</strong></div><div>Completed {formatDate(report.completed_at)}</div><div>Code / runtime <strong className="break-all font-mono text-ink">{report.versions ? JSON.stringify(report.versions) : "n/a"}</strong></div><div className="break-all">Provenance {report.provenance ? JSON.stringify(report.provenance) : `${report.provider ?? "n/a"} · ${report.cutoff_date ?? "n/a"}`}</div></div> : <div className="mt-2 text-slate-500">Immutable report is available after a completed or failed run.</div>}
              </div>
            </div>
            <div className="grid gap-4 lg:grid-cols-2"><ValidationCard title="Purged walk-forward validation" summary={report?.validation} /><ValidationCard title="Untouched holdout" summary={report?.holdout} /></div>
            {failures.length ? <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-coral"><div className="flex items-center gap-2 font-semibold"><AlertTriangle size={16} /> Validation / holdout failures</div><div className="mt-2 grid gap-1 text-xs">{failures.map(failure => <div key={failure}>• {failure}</div>)}</div></div> : null}
            <div className="grid gap-2"><div className="flex items-center gap-2 text-sm font-semibold"><CheckCircle2 size={16} className="text-mint" /> Model, baseline and cost comparison</div><p className="text-xs text-slate-500">Losing results are retained. Calibration and cost-aware returns come from the immutable training report.</p><ComparisonTable rows={comparisons} /></div>
          </div> : <div className="rounded-md border border-dashed border-line p-8 text-center text-sm text-slate-500">Select a stock training job to inspect its immutable snapshot, model, report and validation evidence.</div>}

          <div className="rounded-md border border-line p-3">
            <div className="flex items-center justify-between gap-2"><div className="flex items-center gap-2 text-sm font-semibold"><LockKeyhole size={16} className="text-mint" /> Active paper model binding</div><StatusPill status={binding?.active ? "active" : "none"} /></div>
            {binding?.integrity_valid === false ? <div role="alert" className="mt-3 rounded border border-red-200 bg-red-50 p-3 text-xs text-coral"><strong>Binding unavailable: artifact integrity failed.</strong><div className="mt-1">{binding.integrity_error ?? "The registered model or snapshot is missing or has changed. This binding is not usable."}</div></div> : null}
            {binding ? <div className="mt-3 grid gap-1 text-xs text-slate-600"><div>Model <strong className="text-ink">{binding.model_version ?? binding.model_id ?? binding.model_run_id ?? "n/a"}</strong></div><div>Snapshot <strong className="font-mono text-ink">{binding.snapshot_id ?? binding.dataset_snapshot_id ?? "n/a"}</strong></div><div>Mode <strong className="text-mint">{binding.paper_only && binding.live_authorized !== true ? "paper-only · frozen" : "not eligible"}</strong></div><div>Bound {formatDate(binding.bound_at ?? binding.created_at)}{binding.bound_by ? ` by ${binding.bound_by}` : ""}</div><div>Binding hash <strong className="break-all font-mono text-ink">{binding.binding_hash ?? binding.binding_sha256 ?? "n/a"}</strong></div></div> : <div className="mt-2 text-xs text-slate-500">No active paper binding.</div>}
             {selectedJob && model?.eligible_for_binding === false ? <div className="mt-3 rounded border border-red-200 bg-red-50 p-3 text-xs text-coral"><div className="font-semibold">Binding disabled by backend eligibility</div><div className="mt-1">This model is research-only and cannot be frozen into the active paper binding.</div>{model.binding_blockers?.length ? <ul className="mt-1 list-disc pl-4">{model.binding_blockers.map(blocker => <li key={blocker}>{blocker}</li>)}</ul> : null}</div> : null}
            {selectedJob && model && snapshot && snapshot.verified && model.eligible_for_binding !== false && isTerminal(selectedJob.status) ? (
              <RoleGate requires="operator" className="mt-3">
                <div className="rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
                  <div className="font-semibold">Explicit operator/admin confirmation required</div>
                  <label className="mt-2 flex items-start gap-2"><input checked={bindingConfirmed} onChange={event => setBindingConfirmed(event.target.checked)} type="checkbox" /> <span>I confirm this immutable verified model is bound <strong>paper-only</strong> and frozen. This action cannot authorize live trading.</span></label>
                  <label className="mt-2 grid gap-1"><span>Reason for binding</span><textarea className="focus-ring min-h-16 rounded border border-amber-300 bg-white p-2" value={bindingReason} onChange={event => setBindingReason(event.target.value)} placeholder="Record the operator decision and evidence." /></label>
                  <button className="focus-ring mt-2 inline-flex h-9 items-center gap-2 rounded bg-mint px-3 text-xs font-semibold text-white" disabled={busy || !bindingConfirmed || !bindingReason.trim()} onClick={() => void bindSelected()} type="button"><LockKeyhole size={14} />Confirm frozen paper binding</button>
                </div>
              </RoleGate>
            ) : null}
            {selectedJob?.trigger === "scheduled" ? <div className="mt-3 rounded border border-line bg-panel p-2 text-xs text-slate-600">Scheduled challenger safety: this run can be inspected and compared, but it will never auto-promote or replace the active frozen paper binding.</div> : null}
             {!canBind && model?.eligible_for_binding !== false ? <div className="mt-3 text-xs text-slate-500">Requires Operator or Admin access to confirm a paper-only frozen binding.</div> : null}
          </div>
        </div>
      </div>
    </section>
  );
}