

import { useEffect, useState } from "react";
import { Activity, CheckCircle2, Database, RefreshCw, ShieldAlert, TriangleAlert } from "lucide-react";

import { IntradayImportResponse, MarketImportResponse, ReadinessSnapshot, getReadiness, importIntradayMarketData, importMarketData } from "@/lib/api";
import { RoleGate } from "@/components/access-control";

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

function detailText(details: Record<string, unknown>): string {
  function readable(value: unknown): string {
    if (Array.isArray(value)) {
      return value.length ? value.map(readable).join(", ") : "none";
    }
    if (value && typeof value === "object") {
      const entries = Object.entries(value as Record<string, unknown>);
      return entries.length
        ? entries.map(([key, nested]) => `${key}: ${readable(nested)}`).join("; ")
        : "none";
    }
    return String(value);
  }

  const entries = Object.entries(details)
    .filter(([, value]) => value !== null && typeof value !== "undefined")
    .slice(0, 3)
    .map(([key, value]) => `${key.replaceAll("_", " ")}: ${readable(value)}`);
  return entries.join(" | ");
}

function marketDataRepairSymbols(snapshot: ReadinessSnapshot | null): string[] {
  const marketData = snapshot?.checks.find((check) => check.name === "Market data");
  const details = marketData?.details ?? {};
  const missing = Array.isArray(details.missing_assets) ? details.missing_assets : [];
  const stale = Array.isArray(details.stale_assets) ? details.stale_assets : [];
  return Array.from(new Set([...missing, ...stale].filter((symbol): symbol is string => typeof symbol === "string")));
}

export function ReadinessChecklist() {
  const [snapshot, setSnapshot] = useState<ReadinessSnapshot | null>(null);
  const [status, setStatus] = useState("Loading readiness");
  const [isBusy, setIsBusy] = useState(true);

  async function refresh() {
    setIsBusy(true);
    try {
      const data = await getReadiness();
      setSnapshot(data);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Readiness check failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function refreshMarketData() {
    const symbols = marketDataRepairSymbols(snapshot);
    if (!symbols.length) {
      setStatus("No stale or missing active assets to refresh");
      return;
    }
    setIsBusy(true);
    setStatus(`Refreshing market data for ${symbols.join(", ")}`);
    try {
      const results: MarketImportResponse[] = [];
      for (const symbol of symbols) {
        results.push(await importMarketData(symbol));
      }
      const imported = results.reduce((total, result) => total + result.rows_imported, 0);
      setStatus(`Imported ${imported} rows for ${symbols.length} assets`);
      await refresh();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Market data refresh failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function refreshIntradayData() {
    const symbols = marketDataRepairSymbols(snapshot);
    if (!symbols.length) {
      setStatus("No stale or missing active assets to refresh");
      return;
    }
    setIsBusy(true);
    setStatus(`Refreshing Alpaca SIP intraday data for ${symbols.join(", ")}`);
    try {
      const results: IntradayImportResponse[] = [];
      for (const symbol of symbols) {
        results.push(await importIntradayMarketData(symbol));
      }
      const imported = results.reduce(
        (total, result) =>
          total + result.results.reduce((subtotal, row) => subtotal + (row.rows_imported ?? 0), 0),
        0,
      );
      setStatus(`Imported ${imported} completed 1-minute bars; readiness refreshed`);
      await refresh();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Intraday refresh failed");
    } finally {
      setIsBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    getReadiness().then((data) => {
      if (!active) return;
      setSnapshot(data);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Readiness check failed"); })
      .finally(() => { if (active) setIsBusy(false); });
    return () => { active = false; };
  }, []);

  const overall = snapshot?.overall_status ?? "warning";
  const repairSymbols = marketDataRepairSymbols(snapshot);

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <ShieldAlert size={19} className={overall === "blocked" ? "text-coral" : overall === "warning" ? "text-amber-600" : "text-mint"} />
            <h2 className="text-base font-semibold">System Readiness</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <RoleGate requires="researcher"><button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy || !repairSymbols.length} onClick={refreshMarketData} type="button">
            <Database size={16} />
            Refresh Data
          </button></RoleGate>
           <RoleGate requires="researcher"><button data-testid="button-refresh-intraday" className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy || !repairSymbols.length} onClick={refreshIntradayData} type="button">
             <Activity size={16} />
             Refresh intraday
           </button></RoleGate>
          <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={refresh} type="button">
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[220px_1fr]">
        <div className={`rounded-md border p-3 text-sm ${statusClasses(overall)}`}>
          <div className="font-semibold">Overall {overall}</div>
          <div className="mt-2 grid gap-1">
            <span>Ready {snapshot?.summary.ready ?? 0}</span>
            <span>Warnings {snapshot?.summary.warning ?? 0}</span>
            <span>Blocked {snapshot?.summary.blocked ?? 0}</span>
          </div>
          <div className="mt-2 font-semibold">{snapshot?.paper_trading_allowed ? "Paper runs allowed" : "Paper runs gated"}</div>
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
               <div className="mt-2 text-xs text-slate-500">{detailText(check.details)}</div>
               {check.name.toLowerCase().includes("market") ? <MarketFeedDetails details={check.details} /> : null}
            </div>
          )) ?? <div className="text-sm text-slate-600">Loading checks</div>}
        </div>
      </div>
    </section>
  );
}

function MarketFeedDetails({ details }: { details: Record<string, unknown> }) {
  const value = (key: string): string => {
    const item = details[key];
    if (item === null || typeof item === "undefined") return "Unavailable";
    if (Array.isArray(item)) return item.length ? item.join(", ") : "None";
    return String(item);
  };
  return (
    <div className="mt-2 grid gap-1 rounded border border-line bg-panel p-2 text-xs" data-testid="market-feed-readiness-details">
      <div className="font-semibold text-slate-600">Data provenance</div>
      <div>Mode: {value("data_mode")} · Provider: {value("provider")} · Feed: {value("feed") === "Unavailable" ? value("feed_class") : value("feed")} · Cadence: {value("cadence")}</div>
      <div>Exchange timestamp: {value("exchange_timestamp")} · Ingestion: {value("ingestion_time")}</div>
      <div>Latency: {value("latency_seconds")} · Missing intervals: {value("missing_intervals")}</div>
      <div className="text-coral">Unavailable reason: {value("unavailable_reason")}</div>
    </div>
  );
}
