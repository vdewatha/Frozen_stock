

import { useEffect, useMemo, useState } from "react";
import { Landmark, RefreshCw, UploadCloud } from "lucide-react";

import { EconomicIndicator, MacroContextSummary, getEconomicIndicators, getMacroContext, importEconomicData } from "@/lib/api";
import { StatusPill } from "@/components/status-pill";

function numberValue(value: string | number | null | undefined) {
  return Number(value ?? 0);
}

function label(value: string) {
  return value.replaceAll("_", " ");
}

export function EconomicContextLab() {
  const [status, setStatus] = useState("Ready");
  const [isBusy, setIsBusy] = useState(false);
  const [context, setContext] = useState<MacroContextSummary | null>(null);
  const [indicators, setIndicators] = useState<EconomicIndicator[]>([]);

  const latestRows = useMemo(() => indicators.slice(0, 10), [indicators]);

  async function refreshMacro() {
    const [contextResponse, indicatorRows] = await Promise.all([getMacroContext(), getEconomicIndicators()]);
    setContext(contextResponse);
    setIndicators(indicatorRows);
  }

  useEffect(() => {
    let active = true;
    Promise.all([getMacroContext(), getEconomicIndicators()]).then(([contextResponse, indicatorRows]) => {
      if (!active) return;
      setContext(contextResponse);
      setIndicators(indicatorRows);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Macro refresh failed"); });
    return () => { active = false; };
  }, []);

  async function runAction(action: () => Promise<void>) {
    setIsBusy(true);
    try {
      await action();
      await refreshMacro();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Macro action failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Landmark size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Economic Context</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Importing fallback macro series");
            const response = await importEconomicData();
            setStatus(`Stored ${response.indicator_ids.length} ${response.source} observations`);
          })}>
            <UploadCloud size={16} />
            Import
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Refreshing macro context");
          })}>
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[0.9fr_1.1fr]">
        <div className="rounded-md border border-line p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold">Macro Summary</div>
            <StatusPill status={context?.macro_label ?? "unknown"} />
          </div>
          <div className="mt-3 grid gap-3 text-sm">
            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-md bg-panel p-2">
                <div className="text-xs font-semibold uppercase text-slate-500">Rates</div>
                <div className="mt-1">{label(context?.rate_regime ?? "unknown")}</div>
              </div>
              <div className="rounded-md bg-panel p-2">
                <div className="text-xs font-semibold uppercase text-slate-500">Inflation</div>
                <div className="mt-1">{label(context?.inflation_regime ?? "unknown")}</div>
              </div>
              <div className="rounded-md bg-panel p-2">
                <div className="text-xs font-semibold uppercase text-slate-500">Labor</div>
                <div className="mt-1">{label(context?.labor_regime ?? "unknown")}</div>
              </div>
              <div className="rounded-md bg-panel p-2">
                <div className="text-xs font-semibold uppercase text-slate-500">Volatility</div>
                <div className="mt-1">{label(context?.volatility_regime ?? "unknown")}</div>
              </div>
            </div>
            <div className="rounded-md border border-line bg-panel p-3 text-slate-700">{context?.summary ?? "No macro context loaded yet"}</div>
            <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber">Macro context informs regime/model explanations only. It never approves trades.</div>
          </div>
        </div>

        <div className="rounded-md border border-line">
          <div className="border-b border-line p-3 text-sm font-semibold">Latest Indicator Observations</div>
          <div className="max-h-72 overflow-auto">
            {latestRows.length ? latestRows.map((row) => (
              <div className="grid grid-cols-[1fr_auto] gap-3 border-b border-line p-3 text-sm" key={row.id}>
                <div>
                  <div className="font-medium">{label(row.indicator_name)}</div>
                  <div className="mt-1 text-xs text-slate-500">{row.observation_date} | {row.source}</div>
                </div>
                <div className="text-right">
                  <div className="font-semibold">{numberValue(row.value).toFixed(row.unit === "index" ? 1 : 2)}</div>
                  <div className="text-xs text-slate-500">{row.unit}</div>
                </div>
              </div>
            )) : (
              <div className="p-4 text-sm text-slate-600">No macro observations stored yet</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
