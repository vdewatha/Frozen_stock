import { useEffect, useState } from "react";
import { RefreshCw, Target } from "lucide-react";

import { getStockPaperScorecard, type PaperScorecard, type PaperScorecardGroup } from "@/lib/api";

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const WINDOWS = [7, 30, 90] as const;

export function formatPct(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? "n/a" : `${(value * 100).toFixed(1)}%`;
}

export function formatUsd(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? "n/a" : usd.format(value);
}

function tone(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) return "text-slate-600";
  return value > 0 ? "text-mint" : "text-coral";
}

function label(value: string): string {
  return value.replace(/_/g, " ");
}

function GroupTable({ title, rows, name }: { title: string; rows: PaperScorecardGroup[]; name: string }) {
  return <div className="overflow-x-auto">
    <h3 className="mb-2 text-sm font-semibold">{title}</h3>
    {rows.length ? <table className="w-full min-w-[520px] text-left text-sm">
      <thead className="text-xs text-slate-500"><tr><th className="py-1 pr-3 font-medium">{name}</th><th className="py-1 pr-3 text-right font-medium">Trades</th><th className="py-1 pr-3 text-right font-medium">Win rate</th><th className="py-1 pr-3 text-right font-medium">Expectancy</th><th className="py-1 pr-3 text-right font-medium">Profit factor</th><th className="py-1 text-right font-medium">Net P&amp;L</th></tr></thead>
      <tbody>{rows.map(row => <tr key={String(row.key)} className="border-t border-line">
        <td className="py-1.5 pr-3">{row.strategy_name ?? label(String(row.key))}{row.sources?.includes("exploration") ? <span className="ml-2 rounded bg-amber-50 px-1.5 py-0.5 text-xs text-amber-800">exploration</span> : null}</td>
        <td className="py-1.5 pr-3 text-right">{row.trades}</td>
        <td className="py-1.5 pr-3 text-right">{formatPct(row.win_rate)}</td>
        <td className={`py-1.5 pr-3 text-right ${tone(row.expectancy)}`}>{formatUsd(row.expectancy)}</td>
        <td className="py-1.5 pr-3 text-right">{row.profit_factor === null ? "n/a" : row.profit_factor.toFixed(2)}</td>
        <td className={`py-1.5 text-right font-medium ${tone(row.total_pnl)}`}>{formatUsd(row.total_pnl)}</td>
      </tr>)}</tbody>
    </table> : <p className="text-sm text-slate-500">No closed trades in this window.</p>}
  </div>;
}

export function PaperScorecardPanel() {
  const [days, setDays] = useState<number>(30);
  const [card, setCard] = useState<PaperScorecard | null>(null);
  const [status, setStatus] = useState("Loading scorecard");
  const [busy, setBusy] = useState(true);

  async function refresh(window = days) {
    setBusy(true);
    try {
      const data = await getStockPaperScorecard(window);
      setCard(data);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Scorecard unavailable");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { void refresh(days); }, [days]); // eslint-disable-line react-hooks/exhaustive-deps

  const head = card?.headline;
  const goal = card?.goal;
  const target = goal?.target_win_rate ?? 0.8;
  const winRate = head?.win_rate ?? null;
  const progress = winRate === null ? 0 : Math.min(1, winRate / target);
  const bench = card?.benchmark;
  const mtm = card?.open_mark_to_market;

  return <section aria-label="Paper trade scorecard" className="rounded-md border border-line bg-white">
    <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
      <div>
        <div className="flex items-center gap-2"><Target size={19} className="text-mint" /><h2 className="text-base font-semibold">Paper trade scorecard</h2></div>
        <p className="mt-1 text-sm text-slate-500">{status}. Closed bot trades, net of recorded fees. Read-only.</p>
      </div>
      <div className="flex items-center gap-2">
        <div role="group" aria-label="Window" className="inline-flex rounded-md border border-line">
          {WINDOWS.map(value => <button key={value} type="button" aria-pressed={days === value} onClick={() => setDays(value)} className={`focus-ring h-10 px-3 text-sm ${days === value ? "bg-slate-100 font-semibold" : ""}`}>{value}d</button>)}
        </div>
        <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={busy} onClick={() => void refresh()} type="button"><RefreshCw size={16} />Refresh</button>
      </div>
    </div>
    {card?.status === "unavailable" ? <p className="p-4 text-sm text-slate-600">{card.reason}</p> : null}
    {card && head && goal ? <>
      <dl className="grid gap-4 border-b border-line p-4 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <dt className="text-xs text-slate-500">Win rate vs {formatPct(target)} goal</dt>
          <dd className="mt-1 text-2xl font-semibold">{formatPct(winRate)}</dd>
          <dd className="mt-2 h-2 rounded bg-slate-100" aria-hidden="true"><div className={`h-2 rounded ${winRate !== null && winRate >= target ? "bg-emerald-500" : "bg-amber-500"}`} style={{ width: `${progress * 100}%` }} /></dd>
          {head.bootstrap_ci ? <dd className="mt-1 text-xs text-slate-500">95% CI {formatPct(head.bootstrap_ci.win_rate[0])} to {formatPct(head.bootstrap_ci.win_rate[1])}</dd> : <dd className="mt-1 text-xs text-slate-500">CI after 20 trades</dd>}
        </div>
        <div>
          <dt className="text-xs text-slate-500">Expectancy per trade</dt>
          <dd className={`mt-1 text-2xl font-semibold ${tone(head.expectancy)}`}>{formatUsd(head.expectancy)}</dd>
          <dd className="mt-1 text-xs text-slate-500">Payoff {head.payoff_ratio === null ? "n/a" : head.payoff_ratio.toFixed(2)} · PF {head.profit_factor === null ? "n/a" : head.profit_factor.toFixed(2)}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Closed trades</dt>
          <dd className="mt-1 text-2xl font-semibold">{head.trades}</dd>
          <dd className="mt-1 text-xs text-slate-500">{head.wins} wins · {head.losses} losses · net {formatUsd(head.total_pnl)}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Last {goal.window} trades</dt>
          <dd className="mt-1 text-2xl font-semibold">{goal.last_window_wins}/{goal.last_window_trades}</dd>
          <dd className={`mt-1 text-xs ${goal.met_last_window ? "text-mint" : "text-slate-500"}`}>{goal.met_last_window ? "Goal met: 8 of 10 and profitable" : `Net ${formatUsd(goal.last_window_pnl)}`}</dd>
        </div>
      </dl>
      <div className="grid gap-4 border-b border-line p-4 text-sm sm:grid-cols-3">
        {Object.entries(goal.rolling).map(([key, row]) => <div key={key}><span className="text-xs text-slate-500">{label(key)}{row.complete ? "" : ` (${row.trades} so far)`}</span><div className="font-medium">{formatPct(row.win_rate)} · <span className={tone(row.expectancy)}>{formatUsd(row.expectancy)}</span>/trade</div></div>)}
      </div>
      <div className="grid gap-4 border-b border-line p-4 text-sm sm:grid-cols-2">
        <div><span className="text-xs text-slate-500">SPY buy-and-hold, same window</span><div className="font-medium">{bench?.status === "available" ? <>{formatPct(bench.benchmark_return)} SPY vs {formatPct(bench.bot_return_on_equity)} bot (realized)</> : bench?.reason ?? "n/a"}</div></div>
        <div><span className="text-xs text-slate-500">Open positions, mark-to-market (not in stats)</span><div className="font-medium">{mtm ? <>{mtm.positions} open · <span className={tone(mtm.unrealized_pnl)}>{formatUsd(mtm.unrealized_pnl)}</span></> : "n/a"}</div></div>
      </div>
      <div className="grid gap-6 p-4 lg:grid-cols-2">
        <GroupTable title="By strategy" rows={card.by_strategy ?? []} name="Strategy" />
        <GroupTable title="By exit reason" rows={card.by_exit_reason ?? []} name="Exit" />
      </div>
      {card.mistakes && Object.keys(card.mistakes.primary_counts).length ? <div className="border-t border-line p-4 text-sm">
        <h3 className="mb-2 text-sm font-semibold">Why losing trades lost</h3>
        <ul className="flex flex-wrap gap-2">{Object.entries(card.mistakes.primary_counts).map(([name, count]) => <li key={name} className="rounded border border-line px-2 py-1">{label(name)} · {count} · {formatUsd(card.mistakes?.primary_net_pnl[name])}</li>)}</ul>
      </div> : null}
      <div className="overflow-x-auto border-t border-line p-4">
        <h3 className="mb-2 text-sm font-semibold">Recent trades</h3>
        {card.trades.length ? <table className="w-full min-w-[640px] text-left text-sm">
          <thead className="text-xs text-slate-500"><tr><th className="py-1 pr-3 font-medium">Exited</th><th className="py-1 pr-3 font-medium">Strategy</th><th className="py-1 pr-3 font-medium">Symbol</th><th className="py-1 pr-3 font-medium">Source</th><th className="py-1 pr-3 font-medium">Exit</th><th className="py-1 pr-3 text-right font-medium">Net P&amp;L</th><th className="py-1 font-medium">Mistake</th></tr></thead>
          <tbody>{card.trades.slice(0, 15).map(trade => <tr key={`${trade.entry_order_id}-${trade.exited_at}`} className="border-t border-line">
            <td className="py-1.5 pr-3">{new Date(trade.exited_at).toLocaleString()}</td>
            <td className="py-1.5 pr-3">{trade.strategy_name ?? trade.strategy_id ?? "unattributed"}</td>
            <td className="py-1.5 pr-3 font-medium">{trade.symbol}</td>
            <td className="py-1.5 pr-3">{trade.source}</td>
            <td className="py-1.5 pr-3">{label(trade.exit_reason)}</td>
            <td className={`py-1.5 pr-3 text-right font-medium ${tone(trade.net_pnl)}`}>{formatUsd(trade.net_pnl)}</td>
            <td className="py-1.5 text-slate-600">{trade.mistakes?.primary_mistake ? label(trade.mistakes.primary_mistake) : ""}</td>
          </tr>)}</tbody>
        </table> : <p className="text-sm text-slate-500">No closed trades in this window yet.</p>}
        {card.exploration_note ? <p className="mt-3 text-xs text-slate-500">{card.exploration_note}</p> : null}
      </div>
    </> : null}
  </section>;
}
