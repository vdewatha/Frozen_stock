

import { useMemo, useState } from "react";
import { Activity, Database, Play, RefreshCw } from "lucide-react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { BacktestResponse, MarketDataHealth, MarketImportResponse, PriceHistoryResponse, getMarketDataHealth, getPriceHistory, importIntradayMarketData, importMarketData, runBacktest } from "@/lib/api";
import { RoleGate } from "@/components/access-control";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

const strategies = [
  ["moving_average_crossover", "Moving Average"],
  ["rsi_mean_reversion", "RSI Reversion"],
  ["macd_momentum", "MACD Momentum"],
  ["ensemble", "Ensemble"],
  ["model_predictive_long", "Model Predictive"],
  ["bollinger_mean_reversion", "Bollinger Reversion"],
  ["channel_breakout", "Channel Breakout"],
  ["trend_pullback", "Trend Pullback"]
];

export function MarketLab() {
  const [symbol, setSymbol] = useState("SPY");
  const [strategy, setStrategy] = useState("moving_average_crossover");
  const [importResult, setImportResult] = useState<MarketImportResponse | null>(null);
  const [prices, setPrices] = useState<PriceHistoryResponse | null>(null);
  const [backtest, setBacktest] = useState<BacktestResponse | null>(null);
  const [health, setHealth] = useState<MarketDataHealth | null>(null);
  const [status, setStatus] = useState("Ready");
  const [isBusy, setIsBusy] = useState(false);

  const priceChart = useMemo(
    () => prices?.rows.map((row) => ({ date: row.date, close: row.close })) ?? [],
    [prices]
  );

  async function handleImport() {
    setIsBusy(true);
    setStatus("Importing market data");
    try {
      const result = await importMarketData(symbol);
      setImportResult(result);
      const history = await getPriceHistory(symbol);
      setPrices(history);
      setStatus(`Imported ${result.rows_imported} rows from ${result.source}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Import failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function handleLoadHistory() {
    setIsBusy(true);
    setStatus("Loading price history");
    try {
      const history = await getPriceHistory(symbol);
      setPrices(history);
      setStatus(`Loaded ${history.rows.length} rows from ${history.source}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Price load failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function handleIntradayImport() {
    setIsBusy(true);
    setStatus("Importing Alpaca SIP 1-minute bars");
    try {
      const result = await importIntradayMarketData(symbol);
      const imported = result.results.reduce((total, row) => total + (row.rows_imported ?? 0), 0);
      setStatus(`Imported ${imported} completed regular-session bars`);
      setHealth(await getMarketDataHealth(symbol));
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Intraday import failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function handleLoadHealth() {
    setIsBusy(true);
    setStatus("Checking market feed health");
    try {
      setHealth(await getMarketDataHealth(symbol));
      setStatus("Market feed health updated");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Market health unavailable");
    } finally {
      setIsBusy(false);
    }
  }

  async function handleRunBacktest() {
    setIsBusy(true);
    setStatus("Running backtest");
    try {
      const result = await runBacktest(symbol, strategy);
      setBacktest(result);
      setStatus(`Backtest completed using ${result.source}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Backtest failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Database size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Market Data & Backtest Lab</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="grid gap-2 sm:grid-cols-[120px_190px_auto_auto_auto_auto]">
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Symbol</span>
            <input className="focus-ring h-10 rounded-md border border-line px-3 uppercase" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
          </label>
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Strategy</span>
            <select className="focus-ring h-10 rounded-md border border-line px-3" value={strategy} onChange={(event) => setStrategy(event.target.value)}>
              {strategies.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </label>
          <RoleGate requires="researcher" className="self-end"><button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={handleImport}>
            <RefreshCw size={16} />
            Import
          </button></RoleGate>
           <RoleGate requires="researcher" className="self-end"><button data-testid="button-import-intraday" className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={handleIntradayImport}>
             <Activity size={16} />
             Intraday
           </button></RoleGate>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={handleLoadHistory}>
            <Database size={16} />
            History
          </button>
           <button data-testid="button-refresh-feed-health" className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={handleLoadHealth}>
             <Activity size={16} />
             Feed health
           </button>
          <RoleGate requires="researcher" className="self-end"><button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white" disabled={isBusy} onClick={handleRunBacktest}>
            <Play size={16} />
            Backtest
          </button></RoleGate>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[1.4fr_1fr]">
        <div className="min-h-72 rounded-md border border-line bg-panel p-3">
          <div className="mb-2 flex items-center justify-between text-sm">
            <span className="font-semibold">{prices?.symbol ?? symbol} Close History</span>
            <span className="text-slate-500">{prices ? `${prices.rows.length} rows, ${prices.source}` : "No history loaded"}</span>
          </div>
          <div className="h-64">
            <ResponsiveContainer>
              <AreaChart data={priceChart} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
                <CartesianGrid stroke="#d9dee7" strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} tickLine={false} axisLine={false} minTickGap={36} />
                <YAxis tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={64} domain={["dataMin - 5", "dataMax + 5"]} />
                <Tooltip />
                <Area type="monotone" dataKey="close" stroke="#0f8b6f" fill="#0f8b6f" fillOpacity={0.12} strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="grid gap-3">
          <div className="rounded-md border border-line p-3">
            <div className="text-xs font-semibold uppercase text-slate-500">Import Result</div>
            <div className="mt-2 text-sm">
              {importResult ? `${importResult.rows_imported} rows, ${importResult.start_date} to ${importResult.end_date}` : "No import run yet"}
            </div>
          </div>
          <div className="rounded-md border border-line p-3" data-testid="market-data-health">
            <div className="flex items-center justify-between gap-2 text-xs font-semibold uppercase text-slate-500">
              <span>Feed health · completed 1-minute regular session</span>
              <span className="rounded border border-line px-2 py-1 normal-case">{health?.data_mode ?? "Unavailable"}</span>
            </div>
            {health ? (
              <div className="mt-2 grid gap-1 text-sm sm:grid-cols-2">
                <span>Provider / feed</span><strong className="text-right">{health.provider ?? "Unavailable"} / {health.feed ?? health.feed_class ?? "Unavailable"}</strong>
                <span>Entitlement / config</span><strong className="text-right">{health.entitlement_state ?? (health.provider ? "reported by provider" : "Unavailable")} · {health.entitlement_configured === undefined ? "Unavailable" : health.entitlement_configured ? "configured" : "not configured"}</strong>
                <span>Exchange timestamp</span><strong className="text-right">{health.exchange_timestamp ?? "Unavailable"}</strong>
                <span>Ingestion time</span><strong className="text-right">{health.ingestion_timestamp ?? health.ingestion_time ?? "Unavailable"}</strong>
                <span>Latency</span><strong className="text-right">{health.latency_seconds === null ? "Unavailable" : `${health.latency_seconds.toFixed(1)}s`}</strong>
                <span>Missing intervals</span><strong className="text-right">{health.missing_intervals.length ? health.missing_intervals.join(", ") : "None reported"}</strong>
                <span>State</span><strong className={`text-right ${health.is_stale || health.is_incomplete || health.status === "stale" || health.status === "unavailable" ? "text-coral" : "text-mint"}`}>{health.is_stale || health.status === "stale" ? "Stale" : health.is_incomplete ? "Incomplete" : health.status === "unavailable" ? "Unavailable" : "Current"}</strong>
                {health.unavailable_reason ? <span className="sm:col-span-2 text-coral">Unavailable reason: {health.unavailable_reason}</span> : null}
              </div>
            ) : <div className="mt-2 text-sm text-slate-500">No real-time health result yet. Historical data and delayed data are not substitutes for the Alpaca SIP feed.</div>}
          </div>
          <div className="rounded-md border border-line p-3">
            <div className="text-xs font-semibold uppercase text-slate-500">Backtest Result</div>
            {backtest ? (
              <div className="mt-2 grid grid-cols-2 gap-2 text-sm">
                <span>Total return</span><strong className="text-right">{percent.format(backtest.total_return)}</strong>
                <span>Max drawdown</span><strong className="text-right">{percent.format(backtest.max_drawdown)}</strong>
                <span>Win rate</span><strong className="text-right">{percent.format(backtest.win_rate)}</strong>
                <span>Profit factor</span><strong className="text-right">{backtest.profit_factor.toFixed(2)}</strong>
                <span>Trades</span><strong className="text-right">{backtest.number_of_trades}</strong>
                <span>Score</span><strong className="text-right">{backtest.score.toFixed(2)}</strong>
              </div>
            ) : (
              <div className="mt-2 text-sm">No backtest run yet</div>
            )}
          </div>
          {backtest?.rejected ? (
            <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-coral">
              {backtest.rejection_reasons.join(" ")}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
