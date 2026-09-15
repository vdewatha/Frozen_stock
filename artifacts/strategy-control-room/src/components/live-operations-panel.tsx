import { useEffect, useState } from "react";
import { Activity, AlertTriangle, CircleCheck, CircleHelp, Download, RefreshCw, ShieldAlert } from "lucide-react";

import { RoleGate } from "@/components/access-control";
import { acknowledgeNotification, getErrorMessage, getLiveOperations, getLiveOperationsEvidence, type LiveOperationsSnapshot, type LiveOperationsStatus } from "@/lib/api";

function statusClasses(status: LiveOperationsStatus): string {
  if (status === "healthy") return "border-emerald-200 bg-emerald-50 text-mint";
  if (status === "blocked") return "border-coral/30 bg-red-50 text-coral";
  if (status === "degraded") return "border-amber-200 bg-amber-50 text-amber-700";
  return "border-slate-300 bg-slate-50 text-slate-600";
}

function StatusIcon({ status }: { status: LiveOperationsStatus }) {
  if (status === "healthy") return <CircleCheck size={16} />;
  if (status === "blocked" || status === "degraded") return <ShieldAlert size={16} />;
  return <CircleHelp size={16} />;
}

function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "Not observed";
}

export function LiveOperationsPanel() {
  const [snapshot, setSnapshot] = useState<LiveOperationsSnapshot | null>(null);
  const [status, setStatus] = useState("Loading live operations");
  const [busy, setBusy] = useState(true);
  const [exported, setExported] = useState(false);

  async function refresh() {
    setBusy(true);
    try {
      const next = await getLiveOperations();
      setSnapshot(next);
      setStatus(`Updated ${formatTime(next.generated_at)}`);
    } catch (error) {
      setStatus(getErrorMessage(error, "Live operations unavailable"));
    } finally {
      setBusy(false);
    }
  }

  async function exportEvidence() {
    try {
      const evidence = await getLiveOperationsEvidence(100);
      const blob = new Blob([JSON.stringify(evidence, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "live-operations-evidence.json";
      link.click();
      URL.revokeObjectURL(url);
      setExported(true);
      window.setTimeout(() => setExported(false), 2500);
    } catch (error) {
      setStatus(getErrorMessage(error, "Evidence export unavailable"));
    }
  }

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 60_000);
    return () => window.clearInterval(interval);
  }, []);

  const overall = snapshot?.status ?? "unknown";
  const components = snapshot ? Object.entries(snapshot.components) : [];

  async function acknowledge(id: number) {
    try {
      await acknowledgeNotification(id);
      await refresh();
    } catch (error) {
      setStatus(getErrorMessage(error, "Alert acknowledgement failed"));
    }
  }

  return (
    <section className="rounded-md border border-line bg-white" data-testid="live-operations-panel">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Activity size={19} className={overall === "healthy" ? "text-mint" : overall === "blocked" ? "text-coral" : "text-amber-600"} />
            <h2 className="text-base font-semibold">Live Operations</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="flex gap-2">
          <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={busy} onClick={() => void refresh()} type="button">
            <RefreshCw size={16} /> Refresh
          </button>
          <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" onClick={() => void exportEvidence()} type="button">
            <Download size={16} /> {exported ? "Exported" : "Export evidence"}
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[240px_1fr]">
        <div className={`rounded-md border p-3 text-sm ${statusClasses(overall)}`}>
          <div className="flex items-center gap-2 font-semibold"><StatusIcon status={overall} /> Overall {overall}</div>
          <div className="mt-2">Mode: <strong>{snapshot?.mode ?? "Unknown"}</strong></div>
          <div className="mt-1">{snapshot?.live_orders_allowed ? "Live orders allowed" : "Live orders blocked"}</div>
          <div className="mt-3 text-xs">Checked {formatTime(snapshot?.generated_at)}</div>
        </div>

        <div className="grid gap-3">
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {[
              ["Broker equity", snapshot?.account?.equity ?? "Unknown"],
              ["Open orders", snapshot ? String(snapshot.metrics.open_orders) : "Unknown"],
              ["Positions", snapshot ? String(snapshot.positions.length) : "Unknown"],
              ["Rejected / uncertain", snapshot ? `${snapshot.metrics.rejected_orders_last_24h} / ${snapshot.metrics.uncertain_orders}` : "Unknown"],
            ].map(([label, value]) => (
              <div className="rounded-md border border-line bg-panel p-3 text-sm" key={label}>
                <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
                <div className="mt-1 font-semibold">{value}</div>
              </div>
            ))}
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            {components.map(([name, component]) => (
              <div className="rounded-md border border-line p-3 text-sm" key={name}>
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="font-semibold">{name.replaceAll("_", " ")}</div>
                    <div className="mt-1 text-slate-600">{component.reason}</div>
                  </div>
                  <span className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-semibold ${statusClasses(component.status)}`}>
                    <StatusIcon status={component.status} /> {component.status}
                  </span>
                </div>
                <div className="mt-2 text-xs text-slate-500">Observed {formatTime(component.observed_at)}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="grid gap-4 border-t border-line p-4 lg:grid-cols-2">
        <div>
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold"><AlertTriangle size={16} /> Alerts</div>
          <div className="grid gap-2">
            {snapshot?.alerts.length ? snapshot.alerts.slice(0, 8).map((alert, index) => (
              <div className={`rounded-md border p-3 text-sm ${statusClasses(alert.state)}`} key={`${alert.id ?? alert.source}-${index}`}>
                <div className="flex items-start justify-between gap-3">
                  <strong>{alert.title}</strong>
                  <span className="text-xs">{formatTime(alert.observed_at)}</span>
                </div>
                <div className="mt-1">{alert.reason}</div>
                <div className="mt-1 flex items-center justify-between gap-2 text-xs">
                  <span>Source: {alert.source}{alert.acknowledged ? " · acknowledged" : ""}</span>
                  {alert.id && !alert.acknowledged ? (
                    <RoleGate requires="admin">
                      <button className="focus-ring rounded border border-current px-2 py-1 font-semibold" onClick={() => void acknowledge(alert.id!)} type="button">Acknowledge</button>
                    </RoleGate>
                  ) : null}
                </div>
              </div>
            )) : <div className="rounded-md border border-line p-3 text-sm text-slate-500">No alert observations yet.</div>}
          </div>
        </div>
        <div>
          <div className="mb-2 text-sm font-semibold">Open orders</div>
          {snapshot?.open_orders.length ? (
            <div className="overflow-x-auto rounded-md border border-line">
              <table className="w-full min-w-[520px] text-left text-xs">
                <thead className="bg-panel uppercase text-slate-500"><tr><th className="px-3 py-2">Order</th><th className="px-3 py-2">Symbol</th><th className="px-3 py-2">State</th><th className="px-3 py-2">Attribution</th></tr></thead>
                <tbody>{snapshot.open_orders.slice(0, 8).map((order) => (
                  <tr className="border-t border-line" key={order.id}>
                    <td className="px-3 py-2">{order.client_order_id}</td>
                    <td className="px-3 py-2">{order.side} {order.quantity} {order.symbol}</td>
                    <td className="px-3 py-2">{order.uncertain_submission ? "uncertain" : order.status}</td>
                    <td className="px-3 py-2">{order.actor} · {order.request_id ?? "no request id"}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          ) : <div className="rounded-md border border-line p-3 text-sm text-slate-500">No open live orders observed.</div>}
        </div>
      </div>
    </section>
  );
}