import { useEffect, useState } from "react";
import { Activity, AlertTriangle, CheckCircle2, RefreshCw, ShieldAlert } from "lucide-react";

import { getStockMonitoring, runStockMonitoring, type StockMonitoringSnapshot } from "@/lib/api";
import { RoleGate } from "@/components/access-control";

function statusClasses(status: string): string {
  if (status === "breach") return "border-coral/30 bg-red-50 text-coral";
  if (status === "warning" || status === "unknown") return "border-amber-200 bg-amber-50 text-amber-700";
  return "border-emerald-200 bg-emerald-50 text-mint";
}

function iconFor(status: string) {
  return status === "clear" ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />;
}

export function StockMonitoringPanel() {
  const [snapshot, setSnapshot] = useState<StockMonitoringSnapshot | null>(null);
  const [status, setStatus] = useState("Loading continuous monitoring");
  const [busy, setBusy] = useState(true);

  async function refresh(run = false) {
    setBusy(true);
    try {
      const data = run ? await runStockMonitoring() : await getStockMonitoring();
      setSnapshot(data);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Monitoring unavailable");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 60_000);
    return () => window.clearInterval(interval);
  }, []);

  const checks = snapshot?.checks ?? [];
  const persistent = snapshot?.breaches.filter((item) => item.status === "persistent") ?? [];

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <ShieldAlert size={19} className={statusClasses(snapshot?.status ?? "unknown").split(" ").at(-1)} />
            <h2 className="text-base font-semibold">Continuous Stock Monitoring</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="flex gap-2">
          <RoleGate requires="operator">
            <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={busy} onClick={() => void refresh(true)} type="button">
              <Activity size={16} /> Evaluate now
            </button>
          </RoleGate>
          <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={busy} onClick={() => void refresh()} type="button">
            <RefreshCw size={16} /> Refresh
          </button>
        </div>
      </div>
      <div className="grid gap-4 p-4 xl:grid-cols-[220px_1fr]">
        <div className={`rounded-md border p-3 text-sm ${statusClasses(snapshot?.status ?? "unknown")}`}>
          <div className="font-semibold">Overall {snapshot?.status ?? "unknown"}</div>
          <div className="mt-2">{checks.filter((check) => check.status === "clear").length} clear · {checks.filter((check) => check.status === "warning").length} warnings · {checks.filter((check) => check.status === "breach").length} breaches</div>
          <div className="mt-2 font-semibold">{persistent.length ? `${persistent.length} persistent breach${persistent.length === 1 ? "" : "s"}` : "No persistent breaches"}</div>
        </div>
        <div className="grid gap-2 md:grid-cols-2">
          {checks.map((check) => (
            <div className="rounded-md border border-line p-3 text-sm" key={check.key}>
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="font-semibold">{check.metric.replaceAll("_", " ")}</div>
                  <div className="mt-1 text-slate-600">{check.message}</div>
                </div>
                <span className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-semibold ${statusClasses(check.status)}`}>
                  {iconFor(check.status)} {check.status}
                </span>
              </div>
              <div className="mt-2 text-xs text-slate-500">Category: {check.category} · Action: {check.action_scope}</div>
            </div>
          ))}
        </div>
      </div>
      {snapshot?.actions.length ? (
        <div className="border-t border-line bg-amber-50 px-4 py-3 text-sm text-amber-950">
          <strong>Automatic controls applied:</strong> {snapshot.actions.map((action) => action.action.replaceAll("_", " ")).join(", ")}.
        </div>
      ) : null}
    </section>
  );
}