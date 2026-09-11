

import { useEffect, useState } from "react";
import { ClipboardList, Play, RefreshCw } from "lucide-react";

import { StatusPill } from "@/components/status-pill";
import { RoleGate } from "@/components/access-control";
import { AuditLog, StrategyGovernanceResponse, evaluateStrategies, getAuditLogs, getLatestStrategyGovernanceScorecard } from "@/lib/api";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

function formatCheckValue(label: string, value: number): string {
  if (label.toLowerCase().includes("rate") || label.toLowerCase().includes("drawdown")) {
    return percent.format(value);
  }
  if (label.toLowerCase().includes("factor")) {
    return value.toFixed(2);
  }
  if (label.toLowerCase().includes("confidence")) {
    return value.toFixed(2);
  }
  return String(value);
}

function CheckGroup({ title, checks }: { title: string; checks: StrategyGovernanceResponse["decisions"][number]["checks"][string] }) {
  return (
    <div className="rounded-md border border-line bg-white p-2">
      <div className="mb-2 text-xs font-semibold uppercase text-slate-500">{title}</div>
      <div className="grid gap-1">
        {checks.map((check) => (
          <div className="flex items-center justify-between gap-2 text-xs" key={`${title}-${check.label}`}>
            <span className={check.passed ? "text-mint" : "text-slate-500"}>{check.label}</span>
            <span className={check.passed ? "font-semibold text-mint" : "text-slate-500"}>
              {formatCheckValue(check.label, check.actual)} {check.comparator} {formatCheckValue(check.label, check.threshold)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

type GovernanceComparison = Awaited<ReturnType<typeof getLatestStrategyGovernanceScorecard>>["comparison"];

function GovernanceComparisonPanel({ comparison, previousCreatedAt }: { comparison: GovernanceComparison | null; previousCreatedAt: string | null }) {
  if (!comparison) {
    return null;
  }
  return (
    <div className="rounded-md border border-line bg-panel p-3 text-xs text-slate-700">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold uppercase text-slate-500">Previous Run Comparison</span>
        {previousCreatedAt ? <span className="text-slate-500">Previous {new Date(previousCreatedAt).toLocaleString()}</span> : null}
      </div>
      <div className="text-sm text-slate-700">{comparison.message}</div>
      <div className="mt-3 grid gap-2 lg:grid-cols-3">
        <div className="rounded-md border border-line bg-white p-2">
          <div className="mb-1 font-semibold uppercase text-slate-500">Lifecycle</div>
          {comparison.decision_changes.length ? comparison.decision_changes.slice(0, 3).map((change) => (
            <div className="border-t border-line py-1" key={`${change.strategy_id}-${change.change_type}`}>
              <div className="font-medium text-ink">{change.strategy_name}</div>
              <div>{change.previous_decision ?? "none"} / {change.previous_status ?? "none"} {"->"} {change.current_decision ?? "none"} / {change.current_status ?? "none"}</div>
            </div>
          )) : <div className="text-slate-500">No lifecycle changes.</div>}
        </div>
        <div className="rounded-md border border-line bg-white p-2">
          <div className="mb-1 font-semibold uppercase text-slate-500">Memory</div>
          {comparison.memory_changes.length ? comparison.memory_changes.slice(0, 3).map((change) => {
            const sampleDelta = change.metrics.sample_size?.delta;
            const confidenceDelta = change.metrics.confidence_score?.delta;
            return (
              <div className="border-t border-line py-1" key={change.strategy_id}>
                <div className="font-medium text-ink">{change.strategy_name}</div>
                <div>Samples {sampleDelta === null || typeof sampleDelta === "undefined" ? "n/a" : `${sampleDelta >= 0 ? "+" : ""}${sampleDelta}`}</div>
                <div>Confidence {confidenceDelta === null || typeof confidenceDelta === "undefined" ? "n/a" : `${confidenceDelta >= 0 ? "+" : ""}${confidenceDelta.toFixed(3)}`}</div>
              </div>
            );
          }) : <div className="text-slate-500">No memory movement.</div>}
        </div>
        <div className="rounded-md border border-line bg-white p-2">
          <div className="mb-1 font-semibold uppercase text-slate-500">Thresholds</div>
          {comparison.threshold_changes.length ? comparison.threshold_changes.slice(0, 3).map((change) => (
            <div className="border-t border-line py-1" key={`${change.group}-${change.metric}`}>
              <div className="font-medium text-ink">{change.group} {change.metric.replaceAll("_", " ")}</div>
              <div>{String(change.previous ?? "none")} {"->"} {String(change.current ?? "none")}</div>
            </div>
          )) : <div className="text-slate-500">No threshold changes.</div>}
        </div>
      </div>
    </div>
  );
}

export function GovernanceLab() {
  const [status, setStatus] = useState("Ready");
  const [isBusy, setIsBusy] = useState(false);
  const [result, setResult] = useState<StrategyGovernanceResponse | null>(null);
  const [snapshotCreatedAt, setSnapshotCreatedAt] = useState<string | null>(null);
  const [previousSnapshotCreatedAt, setPreviousSnapshotCreatedAt] = useState<string | null>(null);
  const [comparison, setComparison] = useState<GovernanceComparison | null>(null);
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [targetStrategyId, setTargetStrategyId] = useState<number | null>(null);

  async function refreshLogs() {
    const rows = await getAuditLogs(20);
    setLogs(rows);
  }

  useEffect(() => {
    async function loadInitialState() {
      const [snapshot] = await Promise.all([getLatestStrategyGovernanceScorecard(), refreshLogs()]);
      if (snapshot.scorecard) {
        setResult(snapshot.scorecard);
        setSnapshotCreatedAt(snapshot.created_at);
        setPreviousSnapshotCreatedAt(snapshot.previous_created_at);
        setComparison(snapshot.comparison);
        setStatus(`Loaded governance scorecard from ${snapshot.created_at ? new Date(snapshot.created_at).toLocaleString() : "latest run"}`);
      }
    }
    loadInitialState().catch((error) => setStatus(error instanceof Error ? error.message : "Governance refresh failed"));
  }, []);

  useEffect(() => {
    function syncTargetFromHash() {
      const match = window.location.hash.match(/^#strategy-governance-row-(\d+)$/);
      setTargetStrategyId(match ? Number(match[1]) : null);
    }
    syncTargetFromHash();
    window.addEventListener("hashchange", syncTargetFromHash);
    return () => window.removeEventListener("hashchange", syncTargetFromHash);
  }, []);

  async function runAction(action: () => Promise<void>) {
    setIsBusy(true);
    try {
      await action();
      await refreshLogs();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Governance action failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white scroll-mt-4" id="strategy-governance">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <ClipboardList size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Strategy Governance</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <RoleGate requires="admin"><button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Evaluating lifecycle rules");
            const response = await evaluateStrategies();
            setResult(response);
            const snapshot = await getLatestStrategyGovernanceScorecard();
            setSnapshotCreatedAt(snapshot.created_at ?? new Date().toISOString());
            setPreviousSnapshotCreatedAt(snapshot.previous_created_at);
            setComparison(snapshot.comparison);
            const notificationCount = response.notifications?.created ?? 0;
            setStatus(`Evaluated ${response.evaluated}; changed ${response.changed}; opened ${notificationCount} governance notification${notificationCount === 1 ? "" : "s"}`);
          })}>
            <Play size={16} />
            Evaluate
          </button></RoleGate>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Refreshing audit log");
          })}>
            <RefreshCw size={16} />
            Audit Log
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[1.2fr_1fr]">
        <div className="rounded-md border border-line">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line p-3">
            <div className="text-sm font-semibold">Lifecycle Decisions</div>
            {snapshotCreatedAt ? <div className="text-xs text-slate-500">Snapshot {new Date(snapshotCreatedAt).toLocaleString()}</div> : null}
          </div>
          <div className="max-h-72 overflow-auto">
            <div className="border-b border-line p-3">
              <GovernanceComparisonPanel comparison={comparison} previousCreatedAt={previousSnapshotCreatedAt} />
            </div>
            {result?.decisions.length ? result.decisions.map((decision) => {
              const isTargeted = targetStrategyId === decision.strategy_id;
              return (
              <div
                className={`grid scroll-mt-6 gap-2 border-b border-line p-3 text-sm ${isTargeted ? "bg-amber-50 ring-2 ring-amber-300" : ""}`}
                id={`strategy-governance-row-${decision.strategy_id}`}
                key={decision.strategy_id}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <strong>{decision.strategy_name}</strong>
                    <div className="mt-1 text-xs text-slate-500">{decision.strategy_type.replaceAll("_", " ")}</div>
                  </div>
                  <div className="flex flex-wrap items-center justify-end gap-2">
                    <StatusPill status={decision.decision} />
                    <span className={decision.old_status === decision.new_status ? "text-slate-500" : "font-semibold text-coral"}>
                      {decision.old_status} {"->"} {decision.new_status}
                    </span>
                  </div>
                </div>
                <div className="text-slate-600">{decision.reason}</div>
                <div className="grid grid-cols-4 gap-2 text-xs text-slate-500">
                  <span>Samples {decision.memory.sample_size ?? 0}</span>
                  <span>Win {Number(decision.memory.win_rate ?? 0).toFixed(2)}</span>
                  <span>PF {Number(decision.memory.profit_factor ?? 0).toFixed(2)}</span>
                  <span>Conf {Number(decision.memory.confidence_score ?? 0).toFixed(2)}</span>
                </div>
                {decision.memory.notes ? (
                  <div className="rounded-md border border-line bg-panel p-2 text-xs text-slate-600">{decision.memory.notes}</div>
                ) : null}
                <div className="grid gap-2 lg:grid-cols-3">
                  <CheckGroup title="Retire" checks={decision.checks.retirement ?? []} />
                  <CheckGroup title="Pause" checks={decision.checks.pause ?? []} />
                  <CheckGroup title="Promote" checks={decision.checks.promotion ?? []} />
                </div>
              </div>
            );
            }) : (
              <div className="p-4 text-sm text-slate-600">Run evaluation to review strategy lifecycle status.</div>
            )}
          </div>
        </div>

        <div className="rounded-md border border-line">
          <div className="border-b border-line p-3 text-sm font-semibold">Recent Audit Events</div>
          <div className="max-h-72 overflow-auto">
            {logs.length ? logs.map((log) => (
              <div className="border-b border-line p-3 text-sm" key={log.id}>
                <div className="flex items-center justify-between gap-2">
                  <strong>{log.event_type.replaceAll("_", " ")}</strong>
                  <span className="text-xs text-slate-500">{log.status}</span>
                </div>
                <div className="mt-1 text-slate-600">{log.message}</div>
                <div className="mt-1 text-xs text-slate-500">{log.action} | {new Date(log.created_at).toLocaleString()}</div>
              </div>
            )) : (
              <div className="p-4 text-sm text-slate-600">No audit events yet</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
