

import { useEffect, useMemo, useState } from "react";
import { CloudSun, Play, RefreshCw } from "lucide-react";

import { MarketRegime, detectMarketRegime, getMarketRegimes } from "@/lib/api";
import { StatusPill } from "@/components/status-pill";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

function numberValue(value: unknown) {
  return Number(value ?? 0);
}

function regimeLabel(value: string | null | undefined) {
  return (value ?? "unclassified").replaceAll("_", " ");
}

export function RegimeMonitorLab() {
  const [symbol, setSymbol] = useState("SPY");
  const [status, setStatus] = useState("Ready");
  const [isBusy, setIsBusy] = useState(false);
  const [regimes, setRegimes] = useState<MarketRegime[]>([]);

  const latest = useMemo(() => regimes[0] ?? null, [regimes]);

  async function refreshRegimes() {
    const rows = await getMarketRegimes();
    setRegimes(rows);
  }

  useEffect(() => {
    let active = true;
    getMarketRegimes().then((rows) => {
      if (!active) return;
      setRegimes(rows);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Regime refresh failed"); });
    return () => { active = false; };
  }, []);

  async function runAction(action: () => Promise<void>) {
    setIsBusy(true);
    try {
      await action();
      await refreshRegimes();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Regime action failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <CloudSun size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Market Regime Monitor</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="grid gap-2 sm:grid-cols-[120px_auto_auto]">
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Proxy</span>
            <input className="focus-ring h-10 rounded-md border border-line px-3 uppercase" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
          </label>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md bg-mint px-3 text-sm font-semibold text-white" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Detecting market regime");
            const response = await detectMarketRegime(symbol);
            setStatus(`Detected ${regimeLabel(response.market_regime)} for ${response.regime_date}`);
          })}>
            <Play size={16} />
            Detect
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Refreshing regimes");
          })}>
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[0.9fr_1.1fr]">
        <div className="rounded-md border border-line p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold">Latest Regime</div>
            <StatusPill status={latest?.market_regime ?? "unclassified"} />
          </div>
          {latest ? (
            <div className="mt-3 grid gap-3 text-sm">
              <div className="grid grid-cols-3 gap-2">
                <div className="rounded-md bg-panel p-2">
                  <div className="text-xs font-semibold uppercase text-slate-500">Trend</div>
                  <div className="mt-1">{regimeLabel(latest.spy_trend)}</div>
                </div>
                <div className="rounded-md bg-panel p-2">
                  <div className="text-xs font-semibold uppercase text-slate-500">Volatility</div>
                  <div className="mt-1">{regimeLabel(latest.volatility_regime)}</div>
                </div>
                <div className="rounded-md bg-panel p-2">
                  <div className="text-xs font-semibold uppercase text-slate-500">Risk Tone</div>
                  <div className="mt-1">{regimeLabel(latest.rate_regime)}</div>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-2 text-xs text-slate-500">
                <span>MA gap {percent.format(numberValue(latest.features.ma_50_vs_200))}</span>
                <span>20D vol {percent.format(numberValue(latest.features.realized_volatility_20d))}</span>
                <span>60D return {percent.format(numberValue(latest.features.return_60d))}</span>
              </div>
            </div>
          ) : (
            <div className="mt-3 text-sm text-slate-600">Run detection to classify the current market regime.</div>
          )}
        </div>

        <div className="rounded-md border border-line">
          <div className="border-b border-line p-3 text-sm font-semibold">Regime History</div>
          <div className="max-h-64 overflow-auto">
            {regimes.length ? regimes.map((regime) => (
              <div className="grid grid-cols-[92px_1fr_auto] gap-3 border-b border-line p-3 text-sm" key={regime.id}>
                <div className="font-medium">{regime.regime_date}</div>
                <div className="text-slate-600">
                  {regimeLabel(regime.spy_trend)} | {regimeLabel(regime.volatility_regime)} | {regimeLabel(regime.rate_regime)}
                </div>
                <StatusPill status={regime.market_regime ?? "unclassified"} />
              </div>
            )) : (
              <div className="p-4 text-sm text-slate-600">No regime rows stored yet</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
