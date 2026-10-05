export type ForwardComparisonReport = {
  paired_observations: number;
  strategies: { name: string; brier: number | null; log_loss: number | null }[];
};

const names: Record<string, string> = {
  online_sgd: "Online learner", momentum_5m: "Momentum", reversal_5m: "Mean reversion",
  historical: "Historical baseline", neutral: "50/50 baseline",
  always_long: "Always long",
};

export function ForwardComparison({ report }: { report?: ForwardComparisonReport }) {
  const metric = (value: number | null) => value != null && Number.isFinite(value) ? value.toFixed(4) : "Not measured";
  return <section aria-label="Forward strategy comparison" className="mt-4 border-t border-line pt-3">
    <h4 className="text-sm font-semibold">Strategy comparison</h4>
    <p className="mt-1 text-xs text-slate-600">Paired outcomes: {report?.paired_observations ?? 0}. Direction forecasts, not trade returns.</p>
    {report && report.paired_observations > 0 ? <table className="mt-2 w-full table-fixed text-xs">
      <thead><tr className="border-b border-line text-left"><th className="w-1/2 py-2">Approach</th><th>Brier</th><th>Log loss</th></tr></thead>
      <tbody>{report.strategies.map(row => <tr key={row.name} className="border-b border-line">
        <th className="py-2 pr-2 text-left font-medium break-words">{names[row.name] ?? row.name}</th>
        <td className="break-words">{metric(row.brier)}</td><td className="break-words">{metric(row.log_loss)}</td>
      </tr>)}</tbody>
    </table> : <p className="mt-2 text-xs text-slate-500">Awaiting new forward outcomes. Earlier forecasts are excluded.</p>}
  </section>;
}

export type ShadowReturnsReport = {
  paired_observations: number; unavailable_observations: number; invalid_observations: number;
  strategies: { name: string; long_observations: number; mean_gross_bps: number;
    mean_net_bps_by_cost: Record<string, number> }[];
};

export function ShadowReturns({ report }: { report?: ShadowReturnsReport }) {
  const [cost, setCost] = useState("5");
  const metric = (value: number | undefined) => value != null && Number.isFinite(value) ? value.toFixed(2) : "Not measured";
  return <section aria-label="Forward shadow returns" className="mt-4 border-t border-line pt-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h4 className="text-sm font-semibold">Shadow price returns</h4>
      <label className="flex items-center gap-2 text-xs">Cost per side
        <select aria-label="Shadow cost per side" className="h-8 rounded border border-line px-2" value={cost} onChange={event => setCost(event.target.value)}>
          {["0", "1", "5", "10"].map(value => <option key={value} value={value}>{value} bps</option>)}
        </select>
      </label>
    </div>
    <p className="mt-1 text-xs text-amber-800">Hypothetical IEX prices, not broker profit. Overlapping observations, not portfolio returns.</p>
    <p className="mt-1 text-xs text-slate-600">Paired: {report?.paired_observations ?? 0}. Missing: {report?.unavailable_observations ?? 0}. Invalid: {report?.invalid_observations ?? 0}.</p>
    {report && report.paired_observations > 0 ? <table className="mt-2 w-full table-fixed text-xs">
      <caption className="pb-2 text-left text-slate-600">Mean return per forecast (bps). Long or cash; next-minute open to target close.</caption>
      <thead><tr className="border-b border-line text-left"><th className="w-2/5 py-2">Approach</th><th>Longs</th><th>Gross</th><th>After cost</th></tr></thead>
      <tbody>{report.strategies.map(row => <tr key={row.name} className="border-b border-line">
        <th className="break-words py-2 pr-2 text-left font-medium">{names[row.name] ?? row.name}</th>
        <td className="break-words">{row.long_observations}</td>
        <td className="break-words">{metric(row.mean_gross_bps)}</td><td className="break-words">{metric(row.mean_net_bps_by_cost[cost])}</td>
      </tr>)}</tbody>
    </table> : <p className="mt-2 text-xs text-slate-500">Awaiting predeclared forward price observations.</p>}
  </section>;
}
import { useState } from "react";

export type ReturnChallengerReport = {
  paired_observations: number; unavailable_observations: number; invalid_observations: number;
  mae_bps: number | null; zero_baseline_mae_bps: number | null;
  latest_prediction?: { training_examples: number; minimum_training: number; action: string; predicted_gross_bps: number } | null;
  strategies: { name: string; long_observations: number; mean_net_bps: number }[];
  nonoverlapping?: { selected_forecasts: number; pending: number; expired: number;
    paired_observations: number; unavailable_observations: number; invalid_observations: number;
    invalid_declarations: number; strategies: { name: string; long_observations: number; mean_net_bps: number }[] };
};

export function ReturnChallenger({ report }: { report?: ReturnChallengerReport }) {
  const metric = (value: number | null | undefined) => value != null && Number.isFinite(value) ? value.toFixed(2) : "Not measured";
  const latest = report?.latest_prediction;
  const cohort = report?.nonoverlapping;
  return <section aria-label="Cost-aware return learner" className="mt-4 border-t border-line pt-3">
    <h4 className="text-sm font-semibold">Cost-aware return learner</h4>
    <p className="mt-1 text-xs text-amber-800">Research only. Hypothetical 5 bps per side; overlapping price observations, not broker profit.</p>
    {latest && <p className="mt-1 text-xs text-slate-600">Training observations: {latest.training_examples}. Warmup: {latest.minimum_training}. Latest: {latest.action}, {metric(latest.predicted_gross_bps)} bps gross forecast.</p>}
    <p className="mt-1 text-xs text-slate-600">Paired: {report?.paired_observations ?? 0}. Missing: {report?.unavailable_observations ?? 0}. Invalid: {report?.invalid_observations ?? 0}.</p>
    {report && report.paired_observations > 0 ? <>
      <p className="mt-1 text-xs text-slate-600">Mean absolute error: {metric(report.mae_bps)} bps. Zero-return baseline: {metric(report.zero_baseline_mae_bps)} bps.</p>
      <table className="mt-2 w-full table-fixed text-xs">
        <caption className="pb-2 text-left text-slate-600">Mean return per forecast after modeled costs (bps).</caption>
        <thead><tr className="border-b border-line text-left"><th className="w-1/2 py-2">Approach</th><th>Longs</th><th>After cost</th></tr></thead>
        <tbody>{report.strategies.map(row => <tr key={row.name} className="border-b border-line">
          <th className="break-words py-2 pr-2 text-left font-medium">{row.name === "challenger" ? "Return learner" : "Always long"}</th>
          <td>{row.long_observations}</td><td className="break-words">{metric(row.mean_net_bps)}</td>
        </tr>)}</tbody>
      </table>
    </> : <p className="mt-2 text-xs text-slate-500">Awaiting new return-learner forecasts and outcomes. Earlier results are excluded.</p>}
    {cohort && <div className="mt-3 border-t border-line pt-3" aria-label="Non-overlapping return evaluation">
      <h5 className="text-xs font-semibold">Ten-minute evaluation sample</h5>
      <p className="mt-1 text-xs text-slate-600">Fixed slots, no overlapping holding windows. Independence and profitability unproven.</p>
      <p className="mt-1 text-xs text-slate-600">Selected: {cohort.selected_forecasts}. Paired: {cohort.paired_observations}. Pending: {cohort.pending}. Expired: {cohort.expired}. Missing: {cohort.unavailable_observations}. Invalid: {cohort.invalid_declarations + cohort.invalid_observations}.</p>
      {cohort.strategies.map(row => <p key={row.name} className="mt-1 text-xs text-slate-600">
        {row.name === "challenger" ? "Return learner" : "Always long"}: {metric(row.mean_net_bps)} bps mean after cost; {row.long_observations} longs.
      </p>)}
    </div>}
  </section>;
}
