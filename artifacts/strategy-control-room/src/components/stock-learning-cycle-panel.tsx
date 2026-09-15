import { useEffect, useState } from "react";
import { CheckCircle2, GitBranch, RefreshCw, ShieldAlert } from "lucide-react";

import { RoleGate } from "@/components/access-control";
import { StatusPill } from "@/components/status-pill";
import {
  actOnStockLearningCycle,
  getErrorMessage,
  getStockLearningCycles,
  getStockLearningScheduleControl,
  reviewStockLearningCycle,
  updateStockLearningScheduleControl,
  type StockLearningScheduleControl,
  type StockLearningCycle,
  type StockLearningCycleAction,
} from "@/lib/api";

function gateClass(status: string): string {
  if (status === "pass") return "text-mint";
  if (status === "fail" || status === "blocked") return "text-coral";
  return "text-amber-700";
}

function ComparisonSummary({ cycle }: { cycle: StockLearningCycle }) {
  const gate = cycle.gates.challenger_comparison;
  if (!gate) return null;
  const evidence = (gate.evidence ?? {}) as {
    challenger_model_run_id?: string;
    incumbent_model_run_id?: string | null;
    minimum_sample_count?: number;
  };
  return (
    <div className="mt-3 rounded border border-slate-200 bg-white p-2 text-xs" data-testid="learning-cycle-comparison">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold text-ink">Challenger comparison</span>
        <span className={gateClass(gate.status)}>{gate.status}</span>
      </div>
      <div className="mt-1 text-slate-600">
        {gate.reason ?? "Accuracy, calibration, cost, risk, and sample gates passed independently."}
      </div>
      <div className="mt-1 flex flex-wrap gap-3 text-slate-500">
        <span>Challenger: {evidence.challenger_model_run_id ?? cycle.model_run_id ?? "unavailable"}</span>
        <span>Incumbent: {evidence.incumbent_model_run_id ?? "baseline only"}</span>
        <span>Minimum samples: {evidence.minimum_sample_count ?? "unknown"}</span>
      </div>
    </div>
  );
}

export function StockLearningCyclePanel() {
  const [cycles, setCycles] = useState<StockLearningCycle[]>([]);
  const [scheduleControl, setScheduleControl] = useState<StockLearningScheduleControl | null>(null);
  const [reason, setReason] = useState("");
  const [status, setStatus] = useState("Loading governed learning cycles");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      const [nextCycles, nextControl] = await Promise.all([
        getStockLearningCycles(12),
        getStockLearningScheduleControl(),
      ]);
      setCycles(nextCycles);
      setScheduleControl(nextControl);
      setStatus("Cycle evidence refreshed");
    } catch (error) {
      setStatus(getErrorMessage(error));
    }
  }

  async function updateSchedule(action: "pause" | "resume") {
    if (!reason.trim()) return;
    setBusy(true);
    try {
      setScheduleControl(await updateStockLearningScheduleControl(action, reason));
      setReason("");
      await refresh();
    } catch (error) {
      setStatus(getErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function review(cycle: StockLearningCycle) {
    if (!reason.trim()) return;
    setBusy(true);
    try {
      await reviewStockLearningCycle(cycle.cycle_id, reason);
      setReason("");
      await refresh();
    } catch (error) {
      setStatus(getErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function act(cycle: StockLearningCycle, action: StockLearningCycleAction) {
    if (!reason.trim()) return;
    setBusy(true);
    try {
      await actOnStockLearningCycle(cycle.cycle_id, action, reason);
      setReason("");
      await refresh();
    } catch (error) {
      setStatus(getErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white p-4" data-testid="stock-learning-cycle-panel">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <GitBranch size={18} className="text-mint" />
          <div>
            <h2 className="text-base font-semibold">Governed stock learning cycle</h2>
            <p className="text-xs text-slate-500">Research artifacts remain paper-only until forward evidence and explicit operator action pass.</p>
          </div>
        </div>
        <button className="focus-ring inline-flex h-8 items-center gap-1 rounded border border-line px-2.5 text-xs font-semibold" onClick={() => void refresh()} type="button">
          <RefreshCw size={13} /> Refresh
        </button>
      </div>

      <RoleGate requires="operator" className="mt-4">
        <div className="rounded border border-amber-200 bg-amber-50 p-3" data-testid="stock-learning-schedule-control">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-sm font-semibold text-ink">Scheduled learning decisions</div>
              <div className="text-xs text-slate-600">
                {scheduleControl?.paused
                  ? `Paused: ${scheduleControl.pause_reason ?? "operator maintenance"}`
                  : "Running on its normal schedule"}
              </div>
            </div>
            <span className={`text-xs font-semibold ${scheduleControl?.paused ? "text-amber-800" : "text-mint"}`}>
              {scheduleControl?.paused ? "Paused" : "Active"}
            </span>
          </div>
          <div className="mt-2 flex flex-wrap gap-2">
            {!scheduleControl?.paused ? (
              <button className="focus-ring rounded bg-amber-700 px-2 py-1.5 text-xs font-semibold text-white disabled:opacity-50" disabled={busy || !reason.trim()} onClick={() => void updateSchedule("pause")} type="button">
                Pause scheduled learning
              </button>
            ) : (
              <button className="focus-ring rounded bg-mint px-2 py-1.5 text-xs font-semibold text-white disabled:opacity-50" disabled={busy || !reason.trim()} onClick={() => void updateSchedule("resume")} type="button">
                Resume scheduled learning
              </button>
            )}
            <span className="self-center text-xs text-slate-600">Paper execution safeguards and recovery controls remain independent.</span>
          </div>
        </div>
      </RoleGate>

      {cycles.length === 0 ? (
        <div className="mt-4 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          No durable cycle run exists yet. Scheduled work will show blocked or deferred prerequisite evidence here rather than claiming coverage.
        </div>
      ) : (
        <div className="mt-4 grid gap-3">
          {cycles.map((cycle) => (
            <article className="rounded border border-line bg-panel p-3" data-testid={`stock-learning-cycle-${cycle.cycle_id}`} key={cycle.cycle_id}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <StatusPill status={cycle.status} />
                  <span className="text-sm font-semibold">{cycle.stage.replaceAll("_", " ")}</span>
                  <span className="text-xs text-slate-500">{cycle.trigger} · {cycle.symbols.join(", ")}</span>
                </div>
                <span className="text-xs text-slate-500">{new Date(cycle.updated_at).toLocaleString()}</span>
              </div>
              <div className="mt-2 grid gap-2 text-xs text-slate-600 sm:grid-cols-2">
                <div><span className="font-semibold text-ink">Dataset:</span> {cycle.snapshot_id ?? "not admitted"}</div>
                <div><span className="font-semibold text-ink">Model:</span> {cycle.model_run_id ?? "not registered"}</div>
                <div><span className="font-semibold text-ink">Paper binding:</span> {cycle.binding_id ?? "awaiting admission"}</div>
                <div><span className="font-semibold text-ink">Forward trial:</span> {cycle.trial_id ?? "awaiting admission"}</div>
                <div><span className="font-semibold text-ink">Monitor:</span> {cycle.monitoring.status}</div>
                <div><span className="font-semibold text-ink">Recovery:</span> {cycle.recovery.status}</div>
              </div>
              <div className="mt-3 rounded border border-line bg-white p-2 text-xs" data-testid="learning-cycle-handoff">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-semibold text-ink">Automatic paper handoff</span>
                  <span className={gateClass(cycle.handoff.status === "running_forward_trial" ? "pass" : cycle.handoff.status === "blocked" ? "blocked" : "unknown")}>
                    {cycle.handoff.status.replaceAll("_", " ")}
                  </span>
                </div>
                <div className="mt-1 text-slate-600">
                  {cycle.handoff.reason ?? "Waiting for scheduled training evidence"}
                </div>
                <div className="mt-1 text-slate-500">
                  Preflight: {cycle.handoff.preflight?.status ?? "not checked"}
                  {cycle.handoff.report_id ? ` · report ${cycle.handoff.report_id}` : ""}
                  {cycle.handoff.report_decision ? ` · ${cycle.handoff.report_decision}` : ""}
                </div>
              </div>
              <div className="mt-3 rounded border border-slate-200 bg-white p-2 text-xs">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-semibold text-ink">Automatic paper promotion</span>
                  <span className={gateClass(cycle.automatic_promotion?.decision === "promoted" ? "pass" : cycle.automatic_promotion ? "blocked" : "unknown")}>
                    {cycle.automatic_promotion?.decision ?? "pending"}
                  </span>
                </div>
                {cycle.automatic_promotion ? (
                  <>
                    <div className="mt-1 text-slate-600">{cycle.automatic_promotion.reason}</div>
                    <div className="mt-1 text-slate-500">
                      {cycle.automatic_promotion.actor} · {cycle.automatic_promotion.source_job}
                      {cycle.automatic_promotion.after_binding_id ? ` · binding ${cycle.automatic_promotion.after_binding_id}` : ""}
                    </div>
                  </>
                ) : (
                  <div className="mt-1 text-slate-500">No scheduled decision has been recorded; missing evidence is not a pass.</div>
                )}
              </div>
              <ComparisonSummary cycle={cycle} />
              <div className="mt-2 grid gap-1 sm:grid-cols-2">
                {Object.entries(cycle.gates).map(([name, gate]) => (
                  <div className={`flex items-center gap-1 text-xs ${gateClass(gate.status)}`} key={name}>
                    {gate.status === "pass" ? <CheckCircle2 size={13} /> : <ShieldAlert size={13} />}
                    <span className="font-semibold">{name.replaceAll("_", " ")}</span>
                    <span>· {gate.status}</span>
                  </div>
                ))}
              </div>
              {cycle.last_reason ? <div className="mt-2 text-xs text-slate-600">{cycle.last_reason}</div> : null}
              <RoleGate requires="operator" className="mt-3">
                <div className="grid gap-2 rounded border border-amber-200 bg-amber-50 p-2">
                  <input className="focus-ring rounded border border-amber-300 bg-white px-2 py-1.5 text-xs" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Reason for review or lifecycle action" />
                  <div className="flex flex-wrap gap-2">
                    <button className="focus-ring rounded bg-mint px-2 py-1.5 text-xs font-semibold text-white disabled:opacity-50" disabled={busy || !reason.trim()} onClick={() => void review(cycle)} type="button">Review evidence</button>
                    {cycle.status === "operator_review" ? <button className="focus-ring rounded border border-mint px-2 py-1.5 text-xs font-semibold text-mint disabled:opacity-50" disabled={busy || !reason.trim()} onClick={() => void act(cycle, "mark_eligible")} type="button">Mark eligible</button> : null}
                    {cycle.status === "operator_review" ? <span className="self-center text-xs text-amber-800">Automatic promotion runs only for a completed, aligned paper canary; live trading remains disabled.</span> : null}
                  </div>
                </div>
              </RoleGate>
            </article>
          ))}
        </div>
      )}
      <div className="mt-3 text-xs text-slate-500">{status} · paper-only · live trading disabled</div>
    </section>
  );
}