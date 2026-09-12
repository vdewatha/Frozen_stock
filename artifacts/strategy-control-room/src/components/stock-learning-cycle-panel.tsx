import { useEffect, useState } from "react";
import { CheckCircle2, GitBranch, RefreshCw, ShieldAlert } from "lucide-react";

import { RoleGate } from "@/components/access-control";
import { StatusPill } from "@/components/status-pill";
import {
  actOnStockLearningCycle,
  getErrorMessage,
  getStockLearningCycles,
  reviewStockLearningCycle,
  type StockLearningCycle,
  type StockLearningCycleAction,
} from "@/lib/api";

function gateClass(status: string): string {
  if (status === "pass") return "text-mint";
  if (status === "fail" || status === "blocked") return "text-coral";
  return "text-amber-700";
}

export function StockLearningCyclePanel() {
  const [cycles, setCycles] = useState<StockLearningCycle[]>([]);
  const [reason, setReason] = useState("");
  const [status, setStatus] = useState("Loading governed learning cycles");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      setCycles(await getStockLearningCycles(12));
      setStatus("Cycle evidence refreshed");
    } catch (error) {
      setStatus(getErrorMessage(error));
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

      {cycles.length === 0 ? (
        <div className="mt-4 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          No durable cycle run exists yet. Scheduled work will show blocked or deferred prerequisite evidence here rather than claiming coverage.
        </div>
      ) : (
        <div className="mt-4 grid gap-3">
          {cycles.map((cycle) => (
            <article className="rounded border border-line bg-panel p-3" key={cycle.cycle_id}>
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
                <div><span className="font-semibold text-ink">Monitor:</span> {cycle.monitoring.status}</div>
                <div><span className="font-semibold text-ink">Recovery:</span> {cycle.recovery.status}</div>
              </div>
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
                    {cycle.status === "operator_review" ? <span className="self-center text-xs text-amber-800">Bind and start canary through the existing lifecycle controls before promotion.</span> : null}
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