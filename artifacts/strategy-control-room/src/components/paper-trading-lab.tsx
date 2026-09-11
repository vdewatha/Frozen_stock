

import { useEffect, useMemo, useState } from "react";
import { Activity, CirclePause, Play, RefreshCw, X } from "lucide-react";

import {
  PaperTrade,
  PaperTradingRunResponse,
  StrategyMemory,
  closePaperTrade,
  getPaperTrades,
  getStrategyMemory,
  reconcilePaperTrades,
  runPaperSignal
} from "@/lib/api";

const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
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

function numberValue(value: string | number | null | undefined) {
  return Number(value ?? 0);
}

export function PaperTradingLab() {
  const [symbol, setSymbol] = useState("SPY");
  const [strategy, setStrategy] = useState("moving_average_crossover");
  const [status, setStatus] = useState("Ready");
  const [isBusy, setIsBusy] = useState(false);
  const [lastRun, setLastRun] = useState<PaperTradingRunResponse | null>(null);
  const [trades, setTrades] = useState<PaperTrade[]>([]);
  const [memory, setMemory] = useState<StrategyMemory[]>([]);

  const openTrades = useMemo(() => trades.filter((trade) => trade.status === "open"), [trades]);
  const closedTrades = useMemo(() => trades.filter((trade) => trade.status === "closed"), [trades]);

  async function refresh() {
    const [tradeRows, memoryRows] = await Promise.all([getPaperTrades(), getStrategyMemory()]);
    setTrades(tradeRows);
    setMemory(memoryRows);
  }

  useEffect(() => {
    let active = true;
    Promise.all([getPaperTrades(), getStrategyMemory()]).then(([tradeRows, memoryRows]) => {
      if (!active) return;
      setTrades(tradeRows);
      setMemory(memoryRows);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Refresh failed"); });
    return () => { active = false; };
  }, []);

  async function runAction(action: () => Promise<void>) {
    setIsBusy(true);
    try {
      await action();
      await refresh();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Paper trading action failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 xl:flex-row xl:items-end xl:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Activity size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Paper Trading Simulator</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="grid gap-2 sm:grid-cols-[120px_190px_auto_auto_auto]">
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Symbol</span>
            <input className="focus-ring h-10 rounded-md border border-line px-3 uppercase" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
          </label>
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Strategy</span>
            <select className="focus-ring h-10 rounded-md border border-line px-3" value={strategy} onChange={(event) => setStrategy(event.target.value)}>
              {strategies.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md bg-mint px-3 text-sm font-semibold text-white" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Generating signal and checking risk");
            const result = await runPaperSignal(symbol, strategy);
            setLastRun(result);
            setStatus(result.approved ? `Opened paper trade ${result.paper_trade_id}` : `Blocked: ${result.reason}`);
          })}>
            <Play size={16} />
            Run Signal
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Reconciling open trades");
            const result = await reconcilePaperTrades();
            setStatus(`Checked ${result.checked}, closed ${result.closed}`);
          })}>
            <RefreshCw size={16} />
            Reconcile
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Refreshing paper ledger");
          })}>
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[1.3fr_1fr]">
        <div className="rounded-md border border-line">
          <div className="flex items-center justify-between border-b border-line p-3">
            <div className="text-sm font-semibold">Paper Ledger</div>
            <div className="text-xs text-slate-500">{openTrades.length} open, {closedTrades.length} closed</div>
          </div>
          <div className="max-h-80 overflow-auto">
            {trades.length ? trades.slice(0, 12).map((trade) => (
              <div className="grid grid-cols-[70px_1fr_auto] gap-3 border-b border-line p-3 text-sm" key={trade.id}>
                <div>
                  <div className="font-semibold">{trade.symbol}</div>
                  <div className="text-xs text-slate-500">{trade.status}</div>
                </div>
                <div>
                  <div>{trade.reason_exited || trade.reason_entered || "Paper trade"}</div>
                  <div className="mt-1 text-xs text-slate-500">
                    Qty {numberValue(trade.quantity).toFixed(4)} at {currency.format(numberValue(trade.entry_price))}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className={numberValue(trade.profit_loss) >= 0 ? "font-semibold text-mint" : "font-semibold text-coral"}>
                    {currency.format(numberValue(trade.profit_loss))}
                  </span>
                  {trade.status === "open" ? (
                    <button className="focus-ring inline-flex h-8 w-8 items-center justify-center rounded-md border border-line" disabled={isBusy} title="Close paper trade" onClick={() => runAction(async () => {
                      setStatus(`Closing trade ${trade.id}`);
                      await closePaperTrade(trade.id);
                      setStatus(`Closed trade ${trade.id}`);
                    })}>
                      <X size={15} />
                    </button>
                  ) : null}
                </div>
              </div>
            )) : (
              <div className="p-4 text-sm text-slate-600">No paper trades yet</div>
            )}
          </div>
        </div>

        <div className="grid gap-3">
          <div className="rounded-md border border-line p-3">
            <div className="text-xs font-semibold uppercase text-slate-500">Last Signal</div>
            {lastRun ? (
              <div className="mt-2 grid grid-cols-2 gap-2 text-sm">
                <span>Action</span><strong className="text-right">{lastRun.action}</strong>
                <span>Approved</span><strong className="text-right">{lastRun.approved ? "Yes" : "No"}</strong>
                <span>Confidence</span><strong className="text-right">{percent.format(lastRun.confidence)}</strong>
                <span>Quantity</span><strong className="text-right">{lastRun.quantity.toFixed(4)}</strong>
              </div>
            ) : (
              <div className="mt-2 text-sm text-slate-600">No signal run yet</div>
            )}
          </div>
          <div className="rounded-md border border-line p-3">
            <div className="text-xs font-semibold uppercase text-slate-500">Strategy Memory</div>
            {memory.length ? (
              <div className="mt-2 grid gap-2">
                {memory.slice(0, 3).map((row) => (
                  <div className="rounded-md bg-panel p-2 text-sm" key={row.id}>
                    <div className="flex justify-between gap-2">
                      <strong>{row.symbol}</strong>
                      <span>{row.sample_size ?? 0} samples</span>
                    </div>
                    <div className="mt-1 flex justify-between gap-2 text-xs text-slate-600">
                      <span>Win {percent.format(numberValue(row.win_rate))}</span>
                      <span>PF {numberValue(row.profit_factor).toFixed(2)}</span>
                      <span>Conf {percent.format(numberValue(row.confidence_score))}</span>
                    </div>
                    {row.notes ? <div className="mt-1 text-xs text-slate-600">{row.notes}</div> : null}
                  </div>
                ))}
              </div>
            ) : (
              <div className="mt-2 text-sm text-slate-600">Memory updates after closed paper trades</div>
            )}
          </div>
          <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-mint">
            <div className="flex items-center gap-2 font-semibold"><CirclePause size={16} /> Paper-only lock active</div>
          </div>
        </div>
      </div>
    </section>
  );
}
