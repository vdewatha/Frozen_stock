"use client";

import { useEffect, useMemo, useState } from "react";
import { BarChart3, Play, RefreshCw } from "lucide-react";

import {
  ModelPerformanceResponse,
  PersistedModelRunResponse,
  getModelPerformance,
  runAndPersistModel,
  scoreRealizedModelPredictions
} from "@/lib/api";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

function numberValue(value: string | number | null | undefined) {
  return Number(value ?? 0);
}

export function ModelPerformanceLab() {
  const [symbol, setSymbol] = useState("SPY");
  const [status, setStatus] = useState("Ready");
  const [isBusy, setIsBusy] = useState(false);
  const [lastRun, setLastRun] = useState<PersistedModelRunResponse | null>(null);
  const [performance, setPerformance] = useState<ModelPerformanceResponse | null>(null);

  const latestPredictions = useMemo(() => performance?.predictions.slice(0, 9) ?? [], [performance]);

  async function refreshPerformance() {
    const response = await getModelPerformance(symbol);
    setPerformance(response);
  }

  useEffect(() => {
    let active = true;
    getModelPerformance(symbol).then((response) => {
      if (!active) return;
      setPerformance(response);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Performance refresh failed"); });
    return () => { active = false; };
  }, [symbol]);

  async function runAction(action: () => Promise<void>) {
    setIsBusy(true);
    try {
      await action();
      await refreshPerformance();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Model performance action failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <BarChart3 size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Model Performance Tracker</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="grid gap-2 sm:grid-cols-[120px_auto_auto_auto]">
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Symbol</span>
            <input className="focus-ring h-10 rounded-md border border-line px-3 uppercase" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
          </label>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md bg-mint px-3 text-sm font-semibold text-white" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Persisting model run");
            const response = await runAndPersistModel(symbol);
            setLastRun(response);
            setStatus(`Saved ${response.saved_prediction_ids.length} predictions`);
          })}>
            <Play size={16} />
            Save Run
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Scoring realized predictions");
            const response = await scoreRealizedModelPredictions(symbol);
            setStatus(`Checked ${response.checked}, scored ${response.scored}`);
          })}>
            <RefreshCw size={16} />
            Score
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Refreshing model performance");
          })}>
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[1.15fr_1fr]">
        <div className="grid gap-3 sm:grid-cols-4">
          <div className="rounded-md border border-line bg-panel p-3">
            <div className="text-xs font-semibold uppercase text-slate-500">Stored</div>
            <div className="mt-2 text-2xl font-semibold">{performance?.total_predictions ?? 0}</div>
          </div>
          <div className="rounded-md border border-line bg-panel p-3">
            <div className="text-xs font-semibold uppercase text-slate-500">Realized</div>
            <div className="mt-2 text-2xl font-semibold">{performance?.realized_predictions ?? 0}</div>
          </div>
          <div className="rounded-md border border-line bg-panel p-3">
            <div className="text-xs font-semibold uppercase text-slate-500">Hit Rate</div>
            <div className="mt-2 text-2xl font-semibold">{performance?.hit_rate == null ? "n/a" : percent.format(performance.hit_rate)}</div>
          </div>
          <div className="rounded-md border border-line bg-panel p-3">
            <div className="text-xs font-semibold uppercase text-slate-500">Avg Brier</div>
            <div className="mt-2 text-2xl font-semibold">{performance?.avg_brier_score == null ? "n/a" : performance.avg_brier_score.toFixed(3)}</div>
          </div>
        </div>

        <div className="rounded-md border border-line p-3">
          <div className="text-xs font-semibold uppercase text-slate-500">Last Saved Run</div>
          <div className="mt-2 text-sm text-slate-600">
            {lastRun ? `${lastRun.saved_prediction_ids.length} predictions and ${lastRun.saved_validation_fold_ids.length} validation folds saved from ${lastRun.source}` : "No run saved from this panel yet"}
          </div>
        </div>

        <div className="rounded-md border border-line xl:col-span-2">
          <div className="border-b border-line p-3 text-sm font-semibold">Stored Prediction Rows</div>
          <div className="max-h-72 overflow-auto">
            {latestPredictions.length ? latestPredictions.map((prediction) => (
              <div className="grid grid-cols-[80px_80px_1fr_auto] gap-3 border-b border-line p-3 text-sm" key={prediction.id}>
                <div>
                  <div className="font-semibold">{prediction.symbol}</div>
                  <div className="text-xs text-slate-500">{prediction.horizon_days}D</div>
                </div>
                <div className={prediction.is_realized ? "text-mint" : "text-amber"}>
                  {prediction.is_realized ? "realized" : "pending"}
                </div>
                <div>
                  <div>Probability up {percent.format(numberValue(prediction.probability_up))}</div>
                  <div className="text-xs text-slate-500">Prediction date {prediction.prediction_date}</div>
                </div>
                <div className="text-right">
                  <div>{prediction.realized_return == null ? "n/a" : percent.format(numberValue(prediction.realized_return))}</div>
                  <div className="text-xs text-slate-500">Brier {prediction.brier_score == null ? "n/a" : numberValue(prediction.brier_score).toFixed(3)}</div>
                </div>
              </div>
            )) : (
              <div className="p-4 text-sm text-slate-600">No stored predictions yet</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
