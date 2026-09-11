

import { useEffect, useState } from "react";
import { CheckCircle2, Cloud, Lock, RefreshCw, TriangleAlert } from "lucide-react";

import { DeploymentMonitorSnapshot, getDeploymentMonitor } from "@/lib/api";

function statusClasses(status: string): string {
  if (status === "blocked") {
    return "border-coral/30 bg-red-50 text-coral";
  }
  if (status === "warning") {
    return "border-amber-200 bg-amber-50 text-amber-700";
  }
  return "border-emerald-200 bg-emerald-50 text-mint";
}

function iconFor(status: string) {
  if (status === "ready") {
    return <CheckCircle2 size={17} />;
  }
  return <TriangleAlert size={17} />;
}

function listText(values: string[]): string {
  return values.length ? values.join(", ") : "None";
}

function compactDetails(details: Record<string, unknown>): string {
  const keys = ["redis_url", "missing_assets", "stale_assets", "untrusted_assets", "missing_required_jobs"];
  return keys
    .filter((key) => typeof details[key] !== "undefined")
    .map((key) => {
      const value = details[key];
      if (Array.isArray(value)) {
        return `${key.replaceAll("_", " ")}: ${value.length ? value.join(", ") : "none"}`;
      }
      return `${key.replaceAll("_", " ")}: ${String(value)}`;
    })
    .join(" | ");
}

export function DeploymentMonitorPanel() {
  const [snapshot, setSnapshot] = useState<DeploymentMonitorSnapshot | null>(null);
  const [status, setStatus] = useState("Loading deployment monitor");
  const [isBusy, setIsBusy] = useState(true);

  async function refresh() {
    setIsBusy(true);
    try {
      const data = await getDeploymentMonitor();
      setSnapshot(data);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Deployment monitor failed");
    } finally {
      setIsBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    getDeploymentMonitor().then((data) => {
      if (!active) return;
      setSnapshot(data);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Deployment monitor failed"); })
      .finally(() => { if (active) setIsBusy(false); });
    return () => { active = false; };
  }, []);

  const overall = snapshot?.status ?? "warning";

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Cloud size={19} className={overall === "blocked" ? "text-coral" : overall === "warning" ? "text-amber-600" : "text-mint"} />
            <h2 className="text-base font-semibold">Deployment Monitor</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={refresh} type="button">
          <RefreshCw size={16} />
          Refresh
        </button>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[260px_1fr]">
        <div className={`rounded-md border p-3 text-sm ${statusClasses(overall)}`}>
          <div className="font-semibold">{snapshot?.deployable ? "Deployable" : "Not deployable"}</div>
          <div className="mt-2 grid gap-1">
            <span>Environment {snapshot?.environment ?? "unknown"}</span>
            <span>Readiness {snapshot?.readiness_status ?? "loading"}</span>
            <span>{snapshot?.paper_trading_allowed ? "Paper runs allowed" : "Paper runs gated"}</span>
          </div>
          <div className="mt-3 flex items-center gap-2 font-semibold">
            <Lock size={16} />
            {snapshot?.live_trading_allowed ? "Live trading enabled" : "Live trading locked"}
          </div>
        </div>

        <div className="grid gap-3">
          <div className="grid gap-2 md:grid-cols-2">
            <div className="rounded-md border border-line bg-panel p-3 text-sm">
              <div className="font-semibold">Blockers</div>
              <div className="mt-2 flex flex-wrap gap-2">
                {snapshot?.blockers.length ? snapshot.blockers.map((blocker) => (
                  <span className="rounded-md border border-coral/30 bg-red-50 px-2 py-1 text-xs font-semibold text-coral" key={blocker}>{blocker}</span>
                )) : <span className="text-slate-600">{listText([])}</span>}
              </div>
            </div>
            <div className="rounded-md border border-line bg-panel p-3 text-sm">
              <div className="font-semibold">Readiness Blockers</div>
              <div className="mt-2 text-slate-600">{listText(snapshot?.readiness_blockers ?? [])}</div>
            </div>
          </div>

          <div className="grid gap-2 md:grid-cols-2">
            {snapshot?.checks.map((check) => (
              <div className="rounded-md border border-line p-3 text-sm" key={check.name}>
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="font-semibold">{check.name}</div>
                    <div className="mt-1 text-slate-600">{check.message}</div>
                  </div>
                  <span className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-semibold ${statusClasses(check.status)}`}>
                    {iconFor(check.status)}
                    {check.status}
                  </span>
                </div>
                <div className="mt-2 text-xs text-slate-500">{compactDetails(check.details)}</div>
              </div>
            )) ?? <div className="text-sm text-slate-600">Loading deployment checks</div>}
          </div>
        </div>
      </div>
    </section>
  );
}
