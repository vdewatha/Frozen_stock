import { useEffect, useState } from "react";
import { RefreshCw, TrendingDown } from "lucide-react";

import {
  getErrorMessage,
  getLossFeedbackState,
  getTradeAttributions,
  type LossBreakdownRow,
  type LossFeedbackState,
  type TradeAttributionProjection,
} from "@/lib/api";

const WINDOWS = [7, 30, 90] as const;

export function pct(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined || !Number.isFinite(value) ? "n/a" : `${(value * 100).toFixed(digits)}%`;
}

function label(value: string | null | undefined): string {
  return (value ?? "unknown").replace(/_/g, " ");
}

function tone(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) return "text-slate-600";
  return value > 0 ? "text-mint" : "text-coral";
}

function Breakdown({ title, name, rows }: { title: string; name: string; rows: LossBreakdownRow[] }) {
  return <div className="overflow-x-auto">
    <h3 className="mb-2 text-sm font-semibold">{title}</h3>
    {rows.length ? <table className="w-full min-w-[420px] text-left text-sm">
      <thead className="text-xs text-slate-500"><tr><th className="py-1 pr-3 font-medium">{name}</th><th className="py-1 pr-3 text-right font-medium">Trades</th><th className="py-1 pr-3 text-right font-medium">Losses</th><th className="py-1 pr-3 text-right font-medium">Loss return</th><th className="py-1 text-right font-medium">Net return</th></tr></thead>
      <tbody>{rows.slice(0, 8).map(row => <tr key={row.key} className="border-t border-line">
        <td className="py-1.5 pr-3">{label(row.key)}{row.costs_unknown ? <span className="ml-2 text-xs text-amber-700">{row.costs_unknown} cost unknown</span> : null}</td>
        <td className="py-1.5 pr-3 text-right">{row.trades}</td>
        <td className="py-1.5 pr-3 text-right">{row.losses}</td>
        <td className={`py-1.5 pr-3 text-right ${tone(row.loss_return)}`}>{pct(row.loss_return)}</td>
        <td className={`py-1.5 text-right font-medium ${tone(row.sum_return)}`}>{pct(row.sum_return)}</td>
      </tr>)}</tbody>
    </table> : <p className="text-sm text-slate-500">No attributed trades in this window.</p>}
  </div>;
}

/**
 * Paper loss feedback: why closed trades lost, which strategies the nightly
 * research is focusing on, governed parameter sets, meta-label filter modes
 * and per-strategy sizing weights. Read-only.
 */
export function LossFeedbackPanel() {
  const [days, setDays] = useState<number>(30);
  const [attribution, setAttribution] = useState<TradeAttributionProjection | null>(null);
  const [state, setState] = useState<LossFeedbackState | null>(null);
  const [status, setStatus] = useState("Loading loss feedback");
  const [busy, setBusy] = useState(true);

  async function refresh(window = days) {
    setBusy(true);
    try {
      const [rows, loop] = await Promise.all([getTradeAttributions(window), getLossFeedbackState()]);
      setAttribution(rows);
      setState(loop);
      setStatus(`Updated ${new Date(rows.as_of).toLocaleTimeString()}`);
    } catch (error) {
      setStatus(getErrorMessage(error, "Loss feedback unavailable"));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { void refresh(days); }, [days]); // eslint-disable-line react-hooks/exhaustive-deps

  const summary = attribution?.summary;
  const meta = state?.meta_label;
  const allocation = state?.allocation;
  const applications = state?.parameter_applications;

  return <section aria-label="Loss feedback" className="rounded-md border border-line bg-white">
    <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
      <div>
        <div className="flex items-center gap-2"><TrendingDown size={19} className="text-coral" /><h2 className="text-base font-semibold">Learning from losses</h2></div>
        <p className="mt-1 text-sm text-slate-500">{status}. Paper only; changes reach paper trading only through a governed learning cycle.</p>
      </div>
      <div className="flex items-center gap-2">
        <div role="group" aria-label="Window" className="inline-flex rounded-md border border-line">
          {WINDOWS.map(value => <button key={value} type="button" aria-pressed={days === value} onClick={() => setDays(value)} className={`focus-ring h-10 px-3 text-sm ${days === value ? "bg-slate-100 font-semibold" : ""}`}>{value}d</button>)}
        </div>
        <button className="focus-ring inline-flex h-10 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={busy} onClick={() => void refresh()} type="button"><RefreshCw size={16} />Refresh</button>
      </div>
    </div>

    {summary ? <dl className="grid gap-4 border-b border-line p-4 sm:grid-cols-2 lg:grid-cols-4">
      <div><dt className="text-xs text-slate-500">Attributed closed trades</dt><dd className="mt-1 text-2xl font-semibold">{summary.trades}</dd></div>
      <div><dt className="text-xs text-slate-500">Losing trades</dt><dd className="mt-1 text-2xl font-semibold">{summary.losses}</dd><dd className="mt-1 text-xs text-slate-500">Loss rate {pct(summary.loss_rate, 1)}</dd></div>
      <div><dt className="text-xs text-slate-500">Summed loss return</dt><dd className={`mt-1 text-2xl font-semibold ${tone(summary.loss_return)}`}>{pct(summary.loss_return)}</dd></div>
      <div><dt className="text-xs text-slate-500">Costs not yet known</dt><dd className="mt-1 text-2xl font-semibold">{summary.costs_unknown}</dd><dd className="mt-1 text-xs text-slate-500">Never treated as zero</dd></div>
    </dl> : null}

    {summary ? <div className="grid gap-6 border-b border-line p-4 lg:grid-cols-3">
      <Breakdown title="By strategy" name="Strategy" rows={summary.by_strategy} />
      <Breakdown title="By symbol" name="Symbol" rows={summary.by_symbol} />
      <Breakdown title="By exit cause" name="Exit cause" rows={summary.by_exit_cause} />
    </div> : null}

    {state ? <div className="grid gap-6 border-b border-line p-4 lg:grid-cols-2">
      <div>
        <h3 className="mb-2 text-sm font-semibold">Nightly research agenda</h3>
        {state.agenda.items.length ? <ol className="space-y-2 text-sm">{state.agenda.items.map(item => <li key={item.strategy_type} className="rounded border border-line p-2">
          <div className="font-medium">{item.rank}. {label(item.strategy_type)} <span className={`ml-2 ${tone(item.loss_return)}`}>{pct(item.loss_return)}</span></div>
          <div className="text-xs text-slate-500">{item.losses} of {item.trades} lost · symbols {item.symbols.join(", ") || "n/a"} · focus {item.focus.map(label).join(", ") || "n/a"}</div>
        </li>)}</ol> : <p className="text-sm text-slate-500">No recent indicator-strategy losses; research uses the default sweep.</p>}
      </div>
      <div>
        <h3 className="mb-2 text-sm font-semibold">Governed parameter sets {applications?.enabled ? "" : "(application disabled)"}</h3>
        {applications?.applications.length ? <ul className="space-y-2 text-sm">{applications.applications.slice(0, 6).map(row => <li key={row.id} className="rounded border border-line p-2">
          <div className="font-medium">{label(row.strategy_type)} v{row.parameter_set?.version ?? "?"} <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs">{label(row.status)}</span></div>
          <div className="text-xs text-slate-500">{row.last_reason}{row.parameter_set?.experiment_id ? ` · experiment ${row.parameter_set.experiment_id}` : ""}</div>
        </li>)}</ul> : <p className="text-sm text-slate-500">No parameter set has been proposed yet.</p>}
      </div>
    </div> : null}

    {state ? <div className="grid gap-6 border-b border-line p-4 lg:grid-cols-2">
      <div className="overflow-x-auto">
        <h3 className="mb-2 text-sm font-semibold">Entry filter mode (configured {meta?.configured_mode}{meta?.auto_enforce_enabled ? ", auto-enforce on" : ", auto-enforce off"})</h3>
        {meta?.strategies.length ? <table className="w-full min-w-[420px] text-left text-sm">
          <thead className="text-xs text-slate-500"><tr><th className="py-1 pr-3 font-medium">Strategy</th><th className="py-1 pr-3 font-medium">Mode</th><th className="py-1 pr-3 text-right font-medium">Samples</th><th className="py-1 pr-3 text-right font-medium">Precision</th><th className="py-1 text-right font-medium">Recall</th></tr></thead>
          <tbody>{meta.strategies.map(row => <tr key={row.strategy_type} className="border-t border-line">
            <td className="py-1.5 pr-3">{label(row.strategy_type)}</td>
            <td className="py-1.5 pr-3 font-medium">{row.mode}</td>
            <td className="py-1.5 pr-3 text-right">{row.evidence.shadow?.samples ?? 0}</td>
            <td className="py-1.5 pr-3 text-right">{pct(row.evidence.shadow?.precision, 1)}</td>
            <td className="py-1.5 text-right">{pct(row.evidence.shadow?.recall, 1)}</td>
          </tr>)}</tbody>
        </table> : <p className="text-sm text-slate-500">No mode review has run yet.</p>}
        {meta ? <p className="mt-2 text-xs text-slate-500">Enforce after {meta.bar.min_samples} samples with precision ≥ {pct(meta.bar.min_precision, 0)} and recall ≥ {pct(meta.bar.min_recall, 0)}.</p> : null}
      </div>
      <div className="overflow-x-auto">
        <h3 className="mb-2 text-sm font-semibold">Sizing weights {allocation?.current ? `for ${allocation.current.session_date}` : ""} {allocation?.enabled ? "" : "(disabled)"}</h3>
        {allocation?.current?.weights.length ? <table className="w-full min-w-[320px] text-left text-sm">
          <thead className="text-xs text-slate-500"><tr><th className="py-1 pr-3 font-medium">Strategy</th><th className="py-1 pr-3 text-right font-medium">Weight</th><th className="py-1 pr-3 text-right font-medium">Trades</th><th className="py-1 font-medium">Status</th></tr></thead>
          <tbody>{allocation.current.weights.map(row => <tr key={row.strategy_type} className="border-t border-line">
            <td className="py-1.5 pr-3">{label(row.strategy_type)}</td>
            <td className={`py-1.5 pr-3 text-right font-medium ${row.multiplier < 1 ? "text-coral" : row.multiplier > 1 ? "text-mint" : ""}`}>{row.multiplier.toFixed(2)}×</td>
            <td className="py-1.5 pr-3 text-right">{row.trades ?? 0}</td>
            <td className="py-1.5">{label(row.status)}</td>
          </tr>)}</tbody>
        </table> : <p className="text-sm text-slate-500">No session weights recorded yet; strategies size at 1.00×.</p>}
        {allocation ? <p className="mt-2 text-xs text-slate-500">Bounded {allocation.policy.floor.toFixed(2)}× to {allocation.policy.ceiling.toFixed(2)}×; live sizing is not affected.</p> : null}
      </div>
    </div> : null}

    <div className="overflow-x-auto p-4">
      <h3 className="mb-2 text-sm font-semibold">Recent losing trades</h3>
      {attribution?.recent_losses.length ? <table className="w-full min-w-[760px] text-left text-sm">
        <thead className="text-xs text-slate-500"><tr><th className="py-1 pr-3 font-medium">Closed</th><th className="py-1 pr-3 font-medium">Strategy</th><th className="py-1 pr-3 font-medium">Symbol</th><th className="py-1 pr-3 font-medium">Exit cause</th><th className="py-1 pr-3 font-medium">Signal</th><th className="py-1 pr-3 font-medium">Regime</th><th className="py-1 pr-3 text-right font-medium">Entry slip</th><th className="py-1 pr-3 font-medium">Holding</th><th className="py-1 text-right font-medium">Net return</th></tr></thead>
        <tbody>{attribution.recent_losses.slice(0, 20).map(row => <tr key={row.id} className="border-t border-line">
          <td className="py-1.5 pr-3">{new Date(row.closed_at).toLocaleString()}</td>
          <td className="py-1.5 pr-3">{label(row.strategy_type)}</td>
          <td className="py-1.5 pr-3 font-medium">{row.symbol}</td>
          <td className="py-1.5 pr-3">{label(row.exit_cause)}</td>
          <td className="py-1.5 pr-3">{label(row.signal_quality?.bucket)}</td>
          <td className="py-1.5 pr-3">{label(row.regime?.trend)} / {label(row.regime?.volatility)}</td>
          <td className="py-1.5 pr-3 text-right">{row.slippage?.entry_bps === null || row.slippage?.entry_bps === undefined ? "n/a" : `${row.slippage.entry_bps.toFixed(1)} bps`}</td>
          <td className="py-1.5 pr-3">{row.holding?.sessions ?? "?"}{row.holding?.rule_sessions ? ` of ${row.holding.rule_sessions}` : ""} sessions</td>
          <td className={`py-1.5 text-right font-medium ${tone(row.net_return)}`}>{row.costs_known ? pct(row.net_return) : "cost unknown"}</td>
        </tr>)}</tbody>
      </table> : <p className="text-sm text-slate-500">No losing trades have been attributed in this window.</p>}
    </div>
  </section>;
}
