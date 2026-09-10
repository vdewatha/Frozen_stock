"use client";

import { useEffect, useState } from "react";
import { FlaskConical, Play, RefreshCw } from "lucide-react";

import { StatusPill } from "@/components/status-pill";
import { StrategyImprovementQueue, getStrategyImprovementQueue, runStrategyExperiments } from "@/lib/api";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

function metric(value: unknown): string {
  return typeof value === "number" ? value.toFixed(2) : "0.00";
}

function pct(value: unknown): string {
  return typeof value === "number" ? percent.format(value) : "0.0%";
}

export function StrategyImprovementQueuePanel() {
  const [queue, setQueue] = useState<StrategyImprovementQueue | null>(null);
  const [status, setStatus] = useState("Loading improvement queue");
  const [isBusy, setIsBusy] = useState(true);

  async function refresh() {
    setIsBusy(true);
    try {
      const data = await getStrategyImprovementQueue();
      setQueue(data);
      setStatus(`${data.queued} weak strateg${data.queued === 1 ? "y" : "ies"} queued | ${data.paused} paused | ${data.retired} retired`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Improvement queue failed");
    } finally {
      setIsBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    getStrategyImprovementQueue().then((data) => {
      if (!active) return;
      setQueue(data);
      setStatus(`${data.queued} weak strateg${data.queued === 1 ? "y" : "ies"} queued | ${data.paused} paused | ${data.retired} retired`);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Improvement queue failed"); })
      .finally(() => { if (active) setIsBusy(false); });
    return () => { active = false; };
  }, []);

  async function runPaperOnlyExperiment(row: StrategyImprovementQueue["rows"][number]) {
    setIsBusy(true);
    try {
      const response = await runStrategyExperiments(row.symbol, row.strategy_type, false);
      const promoted = response.experiments.filter((experiment) => experiment.decision === "promoted").length;
      setStatus(`Ran ${response.experiments.length} paper-only experiment(s) for ${row.strategy_name}; ${promoted} candidate${promoted === 1 ? "" : "s"} met promotion criteria`);
      await refresh();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Paper-only experiment run failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <FlaskConical size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Strategy Improvement Queue</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={refresh} type="button">
          <RefreshCw size={16} />
          Refresh
        </button>
      </div>

      <div className="grid gap-3 p-4">
        {queue?.rows.length ? queue.rows.map((row) => (
          <div className="rounded-md border border-line" key={row.strategy_id}>
            <div className="flex flex-col gap-3 border-b border-line p-3 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-semibold">{row.strategy_name}</h3>
                  <StatusPill status={row.current_status} />
                  <StatusPill status={row.severity} />
                </div>
                <div className="mt-1 text-xs text-slate-500">{row.strategy_type} | {row.symbol} | {row.market_regime}</div>
              </div>
              <a className="focus-ring inline-flex h-9 items-center justify-center rounded-md border border-line px-3 text-sm font-medium" href={`#strategy-governance-row-${row.strategy_id}`}>
                Governance Evidence
              </a>
              <button
                className="focus-ring inline-flex h-9 items-center justify-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white"
                disabled={isBusy}
                onClick={() => runPaperOnlyExperiment(row)}
                type="button"
              >
                <Play size={15} />
                Run Paper Experiments
              </button>
            </div>

            <div className="grid gap-3 p-3 xl:grid-cols-[0.9fr_1.1fr_1fr]">
              <div className="grid content-start gap-2">
                <div className="grid grid-cols-5 gap-2 text-xs text-slate-600">
                  <span>Samples {row.memory.sample_size ?? 0}</span>
                  <span>Win {pct(row.memory.win_rate)}</span>
                  <span>PF {metric(row.memory.profit_factor)}</span>
                  <span>Draw {pct(row.memory.avg_drawdown)}</span>
                  <span>Conf {metric(row.memory.confidence_score)}</span>
                </div>
                <div className="grid gap-1 text-xs text-slate-600">
                  {row.improvement_reasons.slice(0, 4).map((reason) => <span key={reason}>{reason}</span>)}
                </div>
              </div>

              <div className="grid gap-2">
                <div className="text-xs font-semibold uppercase text-slate-500">Proposed Experiments</div>
                {row.proposed_experiments.map((proposal) => (
                  <div className="rounded-md border border-line bg-panel p-2 text-xs text-slate-600" key={proposal.experiment_name}>
                    <div className="font-semibold text-ink">{proposal.experiment_name}</div>
                    <div>{proposal.hypothesis}</div>
                  </div>
                ))}
              </div>

              <div className="grid gap-2">
                <div className="text-xs font-semibold uppercase text-slate-500">Paper Gates</div>
                {row.paper_gates.map((gate) => (
                  <div className="flex items-start justify-between gap-2 rounded-md border border-line bg-panel p-2 text-xs" key={gate.name}>
                    <div>
                      <div className="font-semibold text-ink">{gate.name}</div>
                      <div className="text-slate-600">{gate.requirement}</div>
                    </div>
                    <StatusPill status={gate.status} />
                  </div>
                ))}
              </div>
            </div>

            {row.latest_experiment ? (
              <div className="border-t border-line bg-panel px-3 py-2 text-xs text-slate-600">
                Latest experiment: {row.latest_experiment.experiment_name ?? "unnamed"} | {row.latest_experiment.decision ?? "pending"}
              </div>
            ) : null}
          </div>
        )) : (
          <div className="rounded-md border border-line bg-panel p-4 text-sm text-slate-600">No paused or retired strategies need improvement experiments right now.</div>
        )}
      </div>
    </section>
  );
}
