"use client";

import { useEffect, useState } from "react";
import { LineChart, RefreshCw } from "lucide-react";

import { TradeScorecardResponse, getTradeScorecard } from "@/lib/api";

const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 2 });

function tone(value: number): string {
  if (value > 0) {
    return "text-mint";
  }
  if (value < 0) {
    return "text-coral";
  }
  return "text-slate-600";
}

export function TradeScorecard() {
  const [scorecard, setScorecard] = useState<TradeScorecardResponse | null>(null);
  const [status, setStatus] = useState("Loading trade scorecard");
  const [isBusy, setIsBusy] = useState(true);

  async function refresh() {
    setIsBusy(true);
    try {
      const data = await getTradeScorecard(40);
      setScorecard(data);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Trade scorecard failed");
    } finally {
      setIsBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    getTradeScorecard(40).then((data) => {
      if (!active) return;
      setScorecard(data);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Trade scorecard failed"); })
      .finally(() => { if (active) setIsBusy(false); });
    return () => { active = false; };
  }, []);

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <LineChart size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Predictive Trade Scorecard</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={refresh} type="button">
          <RefreshCw size={16} />
          Refresh
        </button>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[240px_1fr]">
        <div className="grid content-start gap-2 text-sm">
          <div className="rounded-md border border-line bg-panel p-3">
            <div className="font-semibold">Paper Trade Evidence</div>
            <div className="mt-2 grid gap-1 text-slate-600">
              <span>Open {scorecard?.open_trades ?? 0}</span>
              <span>Closed {scorecard?.closed_trades ?? 0}</span>
              <span>Positive open {scorecard?.positive_open_trades ?? 0}</span>
              <span>Realized win {scorecard?.realized_win_rate === null || typeof scorecard?.realized_win_rate === "undefined" ? "n/a" : percent.format(scorecard.realized_win_rate)}</span>
              <span>Open avg {scorecard?.avg_open_return === null || typeof scorecard?.avg_open_return === "undefined" ? "n/a" : percent.format(scorecard.avg_open_return)}</span>
            </div>
          </div>
        </div>

        <div className="max-h-[420px] overflow-auto rounded-md border border-line">
          {scorecard?.rows.length ? scorecard.rows.map((row) => (
            <div className="grid gap-2 border-b border-line p-3 text-sm" key={row.paper_trade_id}>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <div className="font-semibold">{row.symbol} | {row.strategy_name}</div>
                  <div className="text-slate-500">
                    {row.status} | {row.age_days}d old | horizon {row.horizon_days ? `${row.horizon_days}d` : "n/a"}
                  </div>
                </div>
                <span className={row.on_track ? "rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 text-xs font-semibold text-mint" : "rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-xs font-semibold text-amber-700"}>
                  {row.on_track ? "on track" : "needs review"}
                </span>
              </div>
              <div className="grid gap-2 rounded-md border border-line bg-panel p-2 text-xs text-slate-600 md:grid-cols-4">
                <span>Entry {currency.format(row.entry_price)}</span>
                <span>Current {currency.format(row.current_price)}</span>
                <span className={tone(row.profit_loss)}>P/L {currency.format(row.profit_loss)}</span>
                <span className={tone(row.profit_loss_pct)}>Return {percent.format(row.profit_loss_pct)}</span>
                <span>Entry prob {percent.format(row.probability_up_at_entry)}</span>
                <span>Entry edge {percent.format(row.expected_return_at_entry)}</span>
                <span>Qty {row.quantity.toFixed(4)}</span>
                <span>{row.predictive_model_supported ? "Model supported" : "No model edge"}</span>
              </div>
            </div>
          )) : (
            <div className="p-4 text-sm text-slate-600">No paper trades to score yet.</div>
          )}
        </div>
      </div>
    </section>
  );
}
