"use client";

import { useEffect, useMemo, useState } from "react";
import { ClipboardList, RefreshCw, ShieldAlert } from "lucide-react";

import { AuditLog, AuditLogFilters, getAuditLogs } from "@/lib/api";

type AuditView = {
  label: string;
  filters: AuditLogFilters;
};

const views: AuditView[] = [
  { label: "All", filters: {} },
  { label: "Readiness", filters: { event_type: "readiness_gate" } },
  { label: "Risk Settings", filters: { event_type: "risk_rule" } },
  { label: "Safety", filters: { event_type: "safety_control" } },
  { label: "Lifecycle", filters: { event_type: "strategy_governance" } }
];

const riskLabels: Record<string, string> = {
  min_confidence: "Min confidence",
  max_daily_drawdown: "Daily drawdown",
  max_strategy_drawdown: "Strategy drawdown",
  max_open_positions: "Open positions",
  max_open_positions_per_strategy: "Per-strategy positions",
  max_symbol_exposure: "Symbol exposure",
  max_risk_per_trade: "Risk per trade",
  stop_after_consecutive_losses: "Loss pause count",
  kill_switch_enabled: "Kill switch"
};

function formatValue(value: unknown): string {
  if (typeof value === "number") {
    return Math.abs(value) < 1 ? value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "") : String(value);
  }
  if (typeof value === "boolean") {
    return value ? "on" : "off";
  }
  if (value === null || typeof value === "undefined") {
    return "none";
  }
  return String(value);
}

function payloadObject(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function riskDelta(log: AuditLog): string[] {
  const payload = payloadObject(log.payload);
  const oldValue = payloadObject(payload.old_value);
  const newValue = payloadObject(payload.new_value);
  return Object.keys(newValue)
    .filter((key) => oldValue[key] !== newValue[key] && key !== "paper_only")
    .slice(0, 4)
    .map((key) => `${riskLabels[key] ?? key}: ${formatValue(oldValue[key])} -> ${formatValue(newValue[key])}`);
}

function lifecycleDelta(log: AuditLog): string[] {
  const payload = payloadObject(log.payload);
  const oldStatus = payload.old_status;
  const newStatus = payload.new_status;
  const memory = payloadObject(payload.memory);
  const rows = oldStatus || newStatus ? [`Status: ${formatValue(oldStatus)} -> ${formatValue(newStatus)}`] : [];
  if (Object.keys(memory).length) {
    rows.push(`Evidence: ${formatValue(memory.sample_size)} samples, win ${formatValue(memory.win_rate)}, PF ${formatValue(memory.profit_factor)}`);
  }
  return rows;
}

function safetyDelta(log: AuditLog): string[] {
  const payload = payloadObject(log.payload);
  if (Array.isArray(payload.affected_strategy_ids)) {
    return [`Affected strategies: ${payload.affected_strategy_ids.length}`];
  }
  return riskDelta(log);
}

function readinessDelta(log: AuditLog): string[] {
  const payload = payloadObject(log.payload);
  const readiness = payloadObject(payload.readiness);
  const checks = Array.isArray(readiness.checks) ? readiness.checks.map(payloadObject) : [];
  const blocked = checks.filter((check) => check.status === "blocked");
  const warning = checks.filter((check) => check.status === "warning");
  const rows = [
    `Symbol: ${formatValue(payload.symbol)}`,
    `Strategy: ${formatValue(payload.strategy)}`,
    `Overall: ${formatValue(readiness.overall_status)}`,
    `Paper runs: ${readiness.paper_trading_allowed ? "allowed" : "gated"}`
  ];
  if (blocked.length) {
    rows.push(`Blocked checks: ${blocked.map((check) => formatValue(check.name)).join(", ")}`);
  }
  if (warning.length) {
    rows.push(`Warnings: ${warning.map((check) => formatValue(check.name)).join(", ")}`);
  }
  return rows;
}

function detailsFor(log: AuditLog): string[] {
  if (log.event_type === "readiness_gate") {
    return readinessDelta(log);
  }
  if (log.event_type === "risk_rule") {
    return riskDelta(log);
  }
  if (log.event_type === "strategy_governance") {
    return lifecycleDelta(log);
  }
  if (log.event_type === "safety_control") {
    return safetyDelta(log);
  }
  return [];
}

function toneFor(log: AuditLog): string {
  if (log.status === "blocked" || log.status === "rejected" || log.action === "pause") {
    return "border-coral/30 bg-red-50 text-coral";
  }
  if (log.status === "enabled" || log.action.includes("kill")) {
    return "border-amber-200 bg-amber-50 text-amber-700";
  }
  return "border-emerald-200 bg-emerald-50 text-mint";
}

export function AuditHistoryPanel() {
  const [selected, setSelected] = useState(views[0]);
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [status, setStatus] = useState("Loading audit history");
  const [isBusy, setIsBusy] = useState(true);

  const counts = useMemo(() => {
    return logs.reduce<Record<string, number>>((memo, log) => {
      memo[log.event_type] = (memo[log.event_type] ?? 0) + 1;
      return memo;
    }, {});
  }, [logs]);

  async function refresh(view = selected) {
    setIsBusy(true);
    try {
      const rows = await getAuditLogs(80, view.filters);
      setLogs(rows);
      setStatus(`${rows.length} events loaded`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Audit history failed");
    } finally {
      setIsBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    getAuditLogs(80, selected.filters).then((rows) => {
      if (!active) return;
      setLogs(rows);
      setStatus(`${rows.length} events loaded`);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Audit history failed"); })
      .finally(() => { if (active) setIsBusy(false); });
    return () => { active = false; };
  }, [selected]);

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <ShieldAlert size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Audit History</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          {views.map((view) => (
            <button
              className={view.label === selected.label ? "focus-ring h-10 rounded-md bg-mint px-3 text-sm font-semibold text-white" : "focus-ring h-10 rounded-md border border-line px-3 text-sm font-medium text-ink"}
              key={view.label}
              onClick={() => { if (view !== selected) setIsBusy(true); setSelected(view); }}
              type="button"
            >
              {view.label}
            </button>
          ))}
          <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => refresh()} type="button">
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[220px_1fr]">
        <div className="grid content-start gap-2 text-sm">
          <div className="rounded-md border border-line bg-panel p-3">
            <div className="flex items-center gap-2 font-semibold">
              <ClipboardList size={16} className="text-mint" />
              Current View
            </div>
            <div className="mt-2 text-slate-600">{selected.label}</div>
          </div>
          <div className="rounded-md border border-line p-3">
            <div className="font-semibold">Loaded Mix</div>
            <div className="mt-2 grid gap-1 text-slate-600">
              <span>Risk {counts.risk_rule ?? 0}</span>
              <span>Readiness {counts.readiness_gate ?? 0}</span>
              <span>Safety {counts.safety_control ?? 0}</span>
              <span>Lifecycle {counts.strategy_governance ?? 0}</span>
            </div>
          </div>
        </div>

        <div className="max-h-[420px] overflow-auto rounded-md border border-line">
          {logs.length ? logs.map((log) => {
            const details = detailsFor(log);
            return (
              <div className="grid gap-2 border-b border-line p-3 text-sm" key={log.id}>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="font-semibold">{log.action.replaceAll("_", " ")}</div>
                    <div className="text-slate-500">{log.event_type.replaceAll("_", " ")} | {new Date(log.created_at).toLocaleString()}</div>
                  </div>
                  <span className={`rounded-md border px-2 py-1 text-xs font-semibold ${toneFor(log)}`}>{log.status}</span>
                </div>
                {log.message ? <div className="text-slate-700">{log.message}</div> : null}
                {details.length ? (
                  <div className="grid gap-1 rounded-md border border-line bg-panel p-2 text-xs text-slate-600 md:grid-cols-2">
                    {details.map((detail) => <span key={detail}>{detail}</span>)}
                  </div>
                ) : null}
              </div>
            );
          }) : (
            <div className="p-4 text-sm text-slate-600">No audit events match this filter.</div>
          )}
        </div>
      </div>
    </section>
  );
}
