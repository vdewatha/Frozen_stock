

import { useEffect, useMemo, useState } from "react";
import { FlaskConical, Play, RefreshCw } from "lucide-react";

import { StrategyExperiment, getStrategyExperiments, runStrategyExperiments } from "@/lib/api";
import { StatusPill } from "@/components/status-pill";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

function numberValue(value: unknown) {
  return Number(value ?? 0);
}

function formatParams(parameters: Record<string, unknown> | null) {
  if (!parameters) {
    return "none";
  }
  return Object.entries(parameters).map(([key, value]) => `${key.replaceAll("_", " ")} ${value}`).join(", ");
}

export function ExperimentManagerLab() {
  const [symbol, setSymbol] = useState("SPY");
  const [strategy, setStrategy] = useState("moving_average_crossover");
  const [applyPromotions, setApplyPromotions] = useState(false);
  const [status, setStatus] = useState("Ready");
  const [isBusy, setIsBusy] = useState(false);
  const [experiments, setExperiments] = useState<StrategyExperiment[]>([]);

  const latest = useMemo(() => experiments.slice(0, 6), [experiments]);

  async function refreshExperiments() {
    const rows = await getStrategyExperiments(symbol, strategy);
    setExperiments(rows);
  }

  useEffect(() => {
    let active = true;
    getStrategyExperiments(symbol, strategy).then((rows) => {
      if (!active) return;
      setExperiments(rows);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Experiment refresh failed"); });
    return () => { active = false; };
  }, [symbol, strategy]);

  async function runAction(action: () => Promise<void>) {
    setIsBusy(true);
    try {
      await action();
      await refreshExperiments();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Experiment action failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 xl:flex-row xl:items-end xl:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <FlaskConical size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Experiment Manager</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="grid gap-2 sm:grid-cols-[120px_220px_auto_auto_auto]">
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Symbol</span>
            <input className="focus-ring h-10 rounded-md border border-line px-3 uppercase" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
          </label>
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Strategy</span>
            <select className="focus-ring h-10 rounded-md border border-line px-3" value={strategy} onChange={(event) => setStrategy(event.target.value)}>
              <option value="moving_average_crossover">Moving Average</option>
              <option value="rsi_mean_reversion">RSI Mean Reversion</option>
              <option value="macd_momentum">MACD Momentum</option>
              <option value="model_predictive_long">Model Predictive</option>
              <option value="bollinger_mean_reversion">Bollinger Reversion</option>
              <option value="channel_breakout">Channel Breakout</option>
              <option value="trend_pullback">Trend Pullback</option>
            </select>
          </label>
          <label className="flex h-10 items-center gap-2 self-end rounded-md border border-line px-3 text-sm">
            <input checked={applyPromotions} className="h-4 w-4 accent-mint" type="checkbox" onChange={(event) => setApplyPromotions(event.target.checked)} />
            Apply winner
          </label>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md bg-mint px-3 text-sm font-semibold text-white" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Running parameter comparisons");
            const response = await runStrategyExperiments(symbol, strategy, applyPromotions);
            const promoted = response.experiments.filter((experiment) => experiment.decision === "promoted").length;
            const applied = response.applied_parameters ? " and applied the top candidate" : "";
            setStatus(`Ran ${response.experiments.length} experiments; ${promoted} promoted${applied}`);
          })}>
            <Play size={16} />
            Run
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Refreshing experiments");
          })}>
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[1fr_1fr]">
        {latest.length ? latest.map((experiment) => {
          const summary = experiment.paper_result_summary;
          const newMetrics = summary?.new_metrics ?? {};
          return (
            <div className="rounded-md border border-line p-3" key={experiment.id}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <h3 className="text-sm font-semibold">{experiment.experiment_name}</h3>
                  <div className="mt-1 text-xs text-slate-500">{new Date(experiment.created_at).toLocaleString()}</div>
                </div>
                <StatusPill status={experiment.decision ?? "unknown"} />
              </div>
              <p className="mt-2 text-sm leading-5 text-slate-600">{experiment.hypothesis}</p>
              <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
                <div className="rounded-md bg-panel p-2">
                  <div className="text-xs font-semibold uppercase text-slate-500">Candidate Parameters</div>
                  <div className="mt-1">{formatParams(experiment.new_parameters)}</div>
                </div>
                <div className="rounded-md bg-panel p-2">
                  <div className="text-xs font-semibold uppercase text-slate-500">Score Delta</div>
                  <div className={numberValue(summary?.delta_score) >= 0 ? "mt-1 font-semibold text-mint" : "mt-1 font-semibold text-coral"}>
                    {numberValue(summary?.old_score).toFixed(3)} {"->"} {numberValue(summary?.new_score).toFixed(3)} ({numberValue(summary?.delta_score).toFixed(3)})
                  </div>
                </div>
              </div>
              <div className="mt-3 grid grid-cols-4 gap-2 text-xs text-slate-500">
                <span>Return {percent.format(numberValue(newMetrics.total_return))}</span>
                <span>DD {percent.format(numberValue(newMetrics.max_drawdown))}</span>
                <span>Win {percent.format(numberValue(newMetrics.win_rate))}</span>
                <span>PF {numberValue(newMetrics.profit_factor).toFixed(2)}</span>
              </div>
              <div className="mt-2 text-xs text-slate-500">{summary?.reason}</div>
            </div>
          );
        }) : (
          <div className="rounded-md border border-line p-4 text-sm text-slate-600 xl:col-span-2">No persisted experiments yet</div>
        )}
      </div>
    </section>
  );
}
