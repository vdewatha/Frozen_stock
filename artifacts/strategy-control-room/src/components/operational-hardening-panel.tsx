import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Fingerprint, RefreshCw, ShieldAlert } from "lucide-react";

import { RoleGate } from "@/components/access-control";
import { getOperationalHardening, runOperationalHardening, type OperationalHardeningReport } from "@/lib/api";

function statusClasses(status: string): string {
  if (status === "breach") return "border-red-200 bg-red-50 text-coral";
  if (status === "unknown") return "border-amber-200 bg-amber-50 text-amber-800";
  return "border-emerald-200 bg-emerald-50 text-mint";
}

export function OperationalHardeningPanel() {
  const [report, setReport] = useState<OperationalHardeningReport | null>(null);
  const [message, setMessage] = useState("Loading deployment hardening checks");
  const [busy, setBusy] = useState(false);

  async function refresh(run = false) {
    setBusy(true);
    try {
      const next = run ? await runOperationalHardening() : await getOperationalHardening();
      setReport(next);
      setMessage(`Updated ${new Date(next.generated_at).toLocaleTimeString()}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Operational hardening is unavailable");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 60_000);
    return () => window.clearInterval(interval);
  }, []);

  const checks = report?.checks ?? [];
  return (
    <section className="rounded-md border border-line bg-white" data-testid="panel-operational-hardening">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            {report?.status === "clear" ? <CheckCircle2 size={19} className="text-mint" /> : <ShieldAlert size={19} className="text-amber-700" />}
            <h2 className="text-base font-semibold">Operational Hardening</h2>
            <span className={`rounded border px-2 py-1 text-xs font-semibold ${statusClasses(report?.status ?? "unknown")}`}>{report?.status ?? "unknown"}</span>
          </div>
          <p className="mt-1 text-sm text-slate-500">Concurrency, worker leases, scheduler ownership, audit integrity, backup tooling, and live-trading guard.</p>
          <p className="mt-1 text-xs text-slate-500">{message}</p>
        </div>
        <div className="flex gap-2">
          <RoleGate requires="operator">
            <button className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={busy} onClick={() => void refresh(true)} type="button">
              <ShieldAlert size={15} /> Evaluate
            </button>
          </RoleGate>
          <button className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={busy} onClick={() => void refresh()} type="button">
            <RefreshCw size={15} /> Refresh
          </button>
        </div>
      </div>
      <div className="grid gap-2 p-4 md:grid-cols-2 xl:grid-cols-3">
        {checks.map((check) => (
          <div className="rounded-md border border-line p-3 text-sm" key={check.key}>
            <div className="flex items-start justify-between gap-2">
              <span className="font-semibold">{check.key.replaceAll("_", " ")}</span>
              <span className={`rounded border px-2 py-1 text-xs font-semibold ${statusClasses(check.status)}`}>
                {check.status === "clear" ? <CheckCircle2 size={13} className="inline" /> : <AlertTriangle size={13} className="inline" />} {check.status}
              </span>
            </div>
            <p className="mt-2 text-xs text-slate-600">{check.message}</p>
          </div>
        ))}
      </div>
      {report ? (
        <div className="grid gap-3 border-t border-line bg-slate-50 p-4 text-xs">
          <div className="flex items-start gap-2">
            <Fingerprint size={15} className="mt-0.5 text-slate-500" />
            <div><strong>Configuration digest:</strong> <code className="break-all">{report.configuration_digest.sha256}</code></div>
          </div>
          <div><strong>Incident procedure:</strong> {report.incident_procedure}</div>
        </div>
      ) : null}
    </section>
  );
}