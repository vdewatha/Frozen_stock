"use client";

import { useEffect, useState } from "react";
import { BrainCircuit, RefreshCw } from "lucide-react";

import { MemoryReplayGroup, MemoryReplayResponse, getMemoryReplay } from "@/lib/api";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 2 });

function metric(value: number | null): string {
  return value === null || typeof value === "undefined" ? "pending" : percent.format(value);
}

function tone(value: number | null): string {
  if (value === null || typeof value === "undefined") {
    return "text-slate-500";
  }
  if (value > 0) {
    return "text-mint";
  }
  if (value < 0) {
    return "text-coral";
  }
  return "text-slate-600";
}

function GroupCard({ group }: { group: MemoryReplayGroup }) {
  return (
    <div className={`rounded-md border p-2 text-xs ${group.replay_gate.allows_memory_increase ? "border-emerald-200 bg-emerald-50" : "border-line bg-panel"}`}>
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-semibold text-ink">{group.label}</div>
          <div className="text-slate-500">{group.row_count} rows | {group.memory_adjusted.complete} complete</div>
        </div>
        <span className={group.replay_gate.allows_memory_increase ? "font-semibold text-mint" : "font-semibold text-amber-700"}>
          {group.replay_gate.status}
        </span>
      </div>
      <div className="mt-2 grid grid-cols-3 gap-1 text-slate-600">
        <span>Avg {metric(group.delta.avg_return)}</span>
        <span>Hit {metric(group.delta.hit_rate)}</span>
        <span>Sel {group.delta.selected >= 0 ? "+" : ""}{group.delta.selected}</span>
      </div>
    </div>
  );
}

export function MemoryReplayPanel() {
  const [replay, setReplay] = useState<MemoryReplayResponse | null>(null);
  const [status, setStatus] = useState("Loading replay evaluator");
  const [isBusy, setIsBusy] = useState(true);

  async function refresh() {
    setIsBusy(true);
    try {
      const data = await getMemoryReplay(60, 3);
      setReplay(data);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Replay evaluator failed");
    } finally {
      setIsBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    getMemoryReplay(60, 3).then((data) => {
      if (!active) return;
      setReplay(data);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Replay evaluator failed"); })
      .finally(() => { if (active) setIsBusy(false); });
    return () => { active = false; };
  }, []);

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <BrainCircuit size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Memory Replay Evaluator</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={refresh} type="button">
          <RefreshCw size={16} />
          Refresh
        </button>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[260px_1fr]">
        <div className="grid content-start gap-2 text-sm">
          <div className="rounded-md border border-line bg-panel p-3">
            <div className="font-semibold">Replay Coverage</div>
            <div className="mt-2 grid gap-1 text-slate-600">
              <span>Rows {replay?.evaluated_rows ?? 0}</span>
              <span>Complete {replay?.complete_rows ?? 0}</span>
              <span>Pending {replay?.pending_rows ?? 0}</span>
              <span>Top {replay?.top_k ?? 3} per event</span>
            </div>
          </div>
          <div className="rounded-md border border-line bg-panel p-3">
            <div className="font-semibold">Memory Delta</div>
            <div className="mt-2 grid gap-1 text-slate-600">
              <span className={tone(replay?.delta.avg_return ?? null)}>Avg return {metric(replay?.delta.avg_return ?? null)}</span>
              <span className={tone(replay?.delta.hit_rate ?? null)}>Hit rate {metric(replay?.delta.hit_rate ?? null)}</span>
              <span className={tone(replay?.delta.cumulative_return ?? null)}>Cumulative {metric(replay?.delta.cumulative_return ?? null)}</span>
              <span className={tone(replay?.delta.max_drawdown ?? null)}>Drawdown {metric(replay?.delta.max_drawdown ?? null)}</span>
            </div>
          </div>
          <div className={`rounded-md border p-3 text-sm ${replay?.replay_gate.allows_memory_increase ? "border-emerald-200 bg-emerald-50 text-mint" : "border-amber-200 bg-amber-50 text-amber-700"}`}>
            <div className="font-semibold">Replay Gate {replay?.replay_gate.status ?? "loading"}</div>
            <div className="mt-2 grid gap-1 text-xs">
              <span>{replay?.replay_gate.complete_samples ?? 0} / {replay?.replay_gate.min_complete_samples ?? 0} complete samples</span>
              <span>Avg delta {metric(replay?.replay_gate.avg_return_delta ?? null)}</span>
              <span>Hit delta {metric(replay?.replay_gate.hit_rate_delta ?? null)}</span>
              {replay?.replay_gate.blockers.slice(0, 2).map((blocker) => <span key={blocker}>{blocker}</span>)}
            </div>
          </div>
          <div className="rounded-md border border-line bg-panel p-3">
            <div className="font-semibold">Gate Alerts</div>
            <div className="mt-2 grid gap-1 text-xs text-slate-600">
              <span>Open gates {replay?.approval_alerts.open_gates.length ?? 0}</span>
              <span>New alerts {replay?.approval_alerts.created ?? 0}</span>
              <span>Refreshed {replay?.approval_alerts.updated ?? 0}</span>
              <span>Resolved {replay?.approval_alerts.resolved ?? 0}</span>
              {replay?.approval_alerts.open_gates.slice(0, 3).map((gate) => (
                <span className="font-medium text-mint" key={`${gate.scope}-${gate.scope_key}`}>
                  {gate.scope_label}
                </span>
              ))}
              {replay && !replay.approval_alerts.open_gates.length ? <span>No setup is approved for memory sizing yet.</span> : null}
            </div>
          </div>
        </div>

        <div className="grid gap-3">
          <div className="grid gap-2 md:grid-cols-2">
            {[
              ["Baseline", replay?.baseline],
              ["Memory Adjusted", replay?.memory_adjusted]
            ].map(([label, data]) => (
              <div className="rounded-md border border-line bg-panel p-3 text-sm" key={String(label)}>
                <div className="font-semibold">{String(label)}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-slate-600">
                  <span>Selected</span><strong className="text-right text-ink">{typeof data === "object" && data ? data.selected : 0}</strong>
                  <span>Complete</span><strong className="text-right text-ink">{typeof data === "object" && data ? data.complete : 0}</strong>
                  <span>Hit rate</span><strong className="text-right text-ink">{typeof data === "object" && data ? metric(data.hit_rate) : "pending"}</strong>
                  <span>Avg return</span><strong className="text-right text-ink">{typeof data === "object" && data ? metric(data.avg_return) : "pending"}</strong>
                  <span>Cumulative</span><strong className="text-right text-ink">{typeof data === "object" && data ? metric(data.cumulative_return) : "pending"}</strong>
                  <span>Max drawdown</span><strong className="text-right text-ink">{typeof data === "object" && data ? metric(data.max_drawdown) : "pending"}</strong>
                </div>
              </div>
            ))}
          </div>

          <div className="grid gap-2 lg:grid-cols-3">
            <div>
              <div className="mb-1 text-xs font-semibold uppercase text-slate-500">By Setup</div>
              <div className="grid gap-2">
                {replay?.groups.by_symbol_strategy.slice(0, 3).map((group) => <GroupCard group={group} key={group.key} />)}
              </div>
            </div>
            <div>
              <div className="mb-1 text-xs font-semibold uppercase text-slate-500">By Strategy</div>
              <div className="grid gap-2">
                {replay?.groups.by_strategy.slice(0, 3).map((group) => <GroupCard group={group} key={group.key} />)}
              </div>
            </div>
            <div>
              <div className="mb-1 text-xs font-semibold uppercase text-slate-500">By Symbol</div>
              <div className="grid gap-2">
                {replay?.groups.by_symbol.slice(0, 3).map((group) => <GroupCard group={group} key={group.key} />)}
              </div>
            </div>
          </div>
          <div>
            <div className="mb-1 text-xs font-semibold uppercase text-slate-500">By Regime</div>
            <div className="grid gap-2 lg:grid-cols-3">
              {replay?.groups.by_regime.slice(0, 3).map((group) => <GroupCard group={group} key={group.key} />)}
            </div>
          </div>

          <div className="max-h-[360px] overflow-auto rounded-md border border-line">
            {replay?.rows.length ? replay.rows.slice(0, 12).map((row) => (
              <div className="grid gap-2 border-b border-line p-3 text-sm" key={`${row.source}-${row.source_id}-${row.symbol}-${row.strategy}`}>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="font-semibold">{row.symbol} | {row.strategy_name}</div>
                    <div className="text-xs text-slate-500">{row.source.replaceAll("_", " ")} | {row.outcome_status}</div>
                  </div>
                  <div className={`text-right text-xs font-semibold ${tone(row.outcome_return)}`}>
                    {metric(row.outcome_return)}
                  </div>
                </div>
                <div className="grid gap-2 rounded-md border border-line bg-panel p-2 text-xs text-slate-600 md:grid-cols-4">
                  <span>Base {row.base_score.toFixed(2)}</span>
                  <span>Memory {row.memory_score.toFixed(2)}</span>
                  <span>Base rank {row.baseline_rank}</span>
                  <span>Memory rank {row.memory_rank}</span>
                  <span>{row.baseline_selected ? "Baseline selected" : "Baseline skipped"}</span>
                  <span>{row.memory_selected ? "Memory selected" : "Memory skipped"}</span>
                  <span>Gate {row.base_threshold.toFixed(2)}</span>
                  <span>Adjusted {row.adjusted_threshold.toFixed(2)}</span>
                </div>
              </div>
            )) : (
              <div className="p-4 text-sm text-slate-600">No replay rows available yet.</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
