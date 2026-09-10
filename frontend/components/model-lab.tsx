"use client";

import { useMemo, useState } from "react";
import { BrainCircuit, Play } from "lucide-react";

import { ModelPredictionResponse, runModelPrediction } from "@/lib/api";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

function average(values: number[]) {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
}

export function ModelLab() {
  const [symbol, setSymbol] = useState("SPY");
  const [result, setResult] = useState<ModelPredictionResponse | null>(null);
  const [status, setStatus] = useState("Ready");
  const [isBusy, setIsBusy] = useState(false);

  const validationSummary = useMemo(() => {
    if (!result?.walk_forward.length) {
      return null;
    }
    return {
      folds: result.walk_forward.length,
      accuracy: average(result.walk_forward.map((fold) => fold.accuracy)),
      brier: average(result.walk_forward.map((fold) => fold.brier_score))
    };
  }, [result]);

  async function handleRunModel() {
    setIsBusy(true);
    setStatus("Training walk-forward models");
    try {
      const prediction = await runModelPrediction(symbol);
      setResult(prediction);
      setStatus(`Generated ${prediction.predictions.length} horizon forecasts from ${prediction.source}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Model run failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <BrainCircuit size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Probabilistic Model Lab</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="grid gap-2 sm:grid-cols-[120px_auto]">
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Symbol</span>
            <input className="focus-ring h-10 rounded-md border border-line px-3 uppercase" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
          </label>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md bg-mint px-3 text-sm font-semibold text-white" disabled={isBusy} onClick={handleRunModel}>
            <Play size={16} />
            Run Model
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[1.3fr_1fr]">
        <div className="grid gap-3 md:grid-cols-3">
          {result?.predictions.length ? (
            result.predictions.map((prediction) => (
              <div className="rounded-md border border-line bg-panel p-3" key={prediction.horizon_days}>
                <div className="text-xs font-semibold uppercase text-slate-500">{prediction.horizon_days}D Horizon</div>
                <div className="mt-2 text-2xl font-semibold text-mint">{percent.format(prediction.probability_up)}</div>
                <div className="mt-1 text-sm text-slate-600">Expected return {percent.format(prediction.expected_return)}</div>
                <div className="mt-3 grid gap-1 text-xs text-slate-500">
                  {Object.entries(prediction.probabilities_by_model).map(([model, probability]) => (
                    <div className="flex justify-between gap-2" key={model}>
                      <span>{model.replaceAll("_", " ")}</span>
                      <strong>{percent.format(probability)}</strong>
                    </div>
                  ))}
                </div>
              </div>
            ))
          ) : (
            <div className="rounded-md border border-line bg-panel p-3 text-sm text-slate-600 md:col-span-3">
              Run the model to generate 1D, 5D, and 20D probability forecasts.
            </div>
          )}
        </div>

        <div className="grid gap-3">
          <div className="rounded-md border border-line p-3">
            <div className="text-xs font-semibold uppercase text-slate-500">Walk-Forward Validation</div>
            {validationSummary ? (
              <div className="mt-2 grid grid-cols-2 gap-2 text-sm">
                <span>Folds</span><strong className="text-right">{validationSummary.folds}</strong>
                <span>Avg accuracy</span><strong className="text-right">{percent.format(validationSummary.accuracy)}</strong>
                <span>Avg Brier</span><strong className="text-right">{validationSummary.brier.toFixed(3)}</strong>
              </div>
            ) : (
              <div className="mt-2 text-sm text-slate-600">No validation folds yet</div>
            )}
          </div>
          <div className="rounded-md border border-line p-3">
            <div className="text-xs font-semibold uppercase text-slate-500">Latest Feature Snapshot</div>
            <div className="mt-2 max-h-40 overflow-auto text-xs text-slate-600">
              {result?.latest_features && Object.keys(result.latest_features).length ? (
                Object.entries(result.latest_features).slice(0, 8).map(([key, value]) => (
                  <div className="flex justify-between gap-3 py-1" key={key}>
                    <span>{key.replaceAll("_", " ")}</span>
                    <strong>{Number(value).toFixed(4)}</strong>
                  </div>
                ))
              ) : (
                "No feature snapshot yet"
              )}
            </div>
          </div>
          {result?.warnings.length ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber">
              {result.warnings.join(" ")}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
