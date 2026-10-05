import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, RefreshCw } from "lucide-react";
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { API_BASE_URL, authenticatedFetch } from "@/lib/api";
import { RoleGate } from "@/components/access-control";
import { ForwardComparison, ShadowReturns, ReturnChallenger, type ReturnChallengerReport, type ForwardComparisonReport, type ShadowReturnsReport } from "./forward-comparison";

type IexStatus = {
  enabled: boolean;
  poll_fresh: boolean;
  last_collection: null | { status: string; checked_at: string; failure_class: string | null };
  symbols: { symbol: string; total_bars: number; latest_bar: string | null; points: { time: string; close: number }[] }[];
  learning?: { symbols: { symbol: string; scored: number; pending: number; expired: number; metric_window: number; brier: number | null; baseline_brier: number | null; neutral_brier: number | null; beats_neutral_baseline: boolean | null; latest_prediction_at: string | null; strategy_comparison?: ForwardComparisonReport; shadow_returns?: ShadowReturnsReport; return_challenger?: ReturnChallengerReport }[] };
};

async function request<T>(path: string, method = "GET"): Promise<T> {
  const response = await authenticatedFetch(`${API_BASE_URL}/market-data/iex/${path}`, { method, cache: "no-store" });
  if (!response.ok) throw new Error(`IEX request failed (${response.status})`);
  return response.json();
}

export function IexResearchPanel() {
  const [symbol, setSymbol] = useState("SPY");
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ["iex-research"], queryFn: () => request<IexStatus>("status"), refetchInterval: 30000 });
  const collect = useMutation({ mutationFn: () => request("collect", "POST"), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["iex-research"] }) });
  const selected = status.data?.symbols.find(item => item.symbol === symbol);
  const learning = status.data?.learning?.symbols.find(item => item.symbol === symbol);
  return <section className="border-y border-line bg-white py-4" aria-label="IEX research feed">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="text-base font-semibold">IEX Research Feed</h2>
        <p className="text-sm text-amber-800">Single-exchange observations. Not qualified for execution.</p></div>
      <div className="flex flex-wrap items-center gap-2">
        <select aria-label="IEX symbol" value={symbol} onChange={event => setSymbol(event.target.value)} className="h-9 rounded border border-line px-2">
          {["AAPL", "MSFT", "QQQ", "SPY"].map(value => <option key={value}>{value}</option>)}
        </select>
        <button title="Refresh IEX status" aria-label="Refresh IEX status" disabled={status.isFetching} onClick={() => void status.refetch()} className="flex h-9 w-9 items-center justify-center rounded border border-line"><RefreshCw size={16} /></button>
        <RoleGate requires="researcher"><button disabled={!status.data?.enabled || collect.isPending} onClick={() => collect.mutate()} className="flex h-9 items-center gap-2 rounded border border-line px-3 text-sm"><Download size={16} />Collect</button></RoleGate>
      </div>
    </div>
    <div className="my-3 flex flex-wrap gap-x-6 gap-y-1 text-sm">
      <span>Collector: {status.data ? status.data.enabled ? status.data.poll_fresh ? "Polling" : "No recent poll" : "Disabled" : "Loading"}</span>
      <span>Last result: {status.data?.last_collection?.status ?? "None"}</span>
      <span>{selected?.total_bars ?? 0} stored bars</span>
      <span>Latest bar: {selected?.latest_bar ? new Date(selected.latest_bar).toLocaleString() : "None"}</span>
    </div>
    {status.error || collect.error ? <p role="alert" className="text-sm text-red-700">{(status.error ?? collect.error)?.message}</p> : null}
    {status.data?.last_collection?.failure_class ? <p role="alert" className="text-sm text-red-700">Collection unavailable: {status.data.last_collection.failure_class}</p> : null}
    {collect.isSuccess ? <p role="status" className="text-sm text-slate-600">Collection queued. Awaiting worker result.</p> : null}
    <div className="my-3 border-y border-line py-3 text-sm" aria-label="Online research learning">
      <h3 className="font-semibold">{symbol} Forward Learning</h3>
      <p className="text-amber-800">Research only. Prediction accuracy is not trading profit.</p>
      <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1">
        <span>Scored: {learning?.scored ?? 0}</span><span>Pending: {learning?.pending ?? 0}</span><span>Expired: {learning?.expired ?? 0}</span>
        <span>Brier (last {learning?.metric_window ?? 0}): {learning?.brier?.toFixed(4) ?? "Awaiting outcomes"}</span>
        <span>Historical baseline: {learning?.baseline_brier?.toFixed(4) ?? "Awaiting outcomes"}</span>
        <span>50/50 baseline: {learning?.neutral_brier?.toFixed(4) ?? "Awaiting outcomes"}</span>
        <span>Beats 50/50: {learning?.beats_neutral_baseline == null ? "Awaiting outcomes" : learning.beats_neutral_baseline ? "Yes" : "No"}</span>
      </div>
      <p className="mt-1 text-slate-600">Last prediction: {learning?.latest_prediction_at ? new Date(learning.latest_prediction_at).toLocaleString() : "Waiting for fresh market-session observations"}</p>
      <ForwardComparison report={learning?.strategy_comparison} />
      <ShadowReturns report={learning?.shadow_returns} />
      <ReturnChallenger report={learning?.return_challenger} />
    </div>
    <div className="h-56 min-w-0">
      {selected?.points.length ? <ResponsiveContainer width="100%" height="100%">
        <LineChart data={selected.points} margin={{ left: 8, right: 20, top: 12, bottom: 8 }}>
          <XAxis dataKey="time" minTickGap={70} padding={{ left: 12, right: 30 }} tick={{ fontSize: 11 }} tickFormatter={time => new Date(time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} />
          <YAxis domain={["auto", "auto"]} width={70} tickFormatter={value => Number(value).toFixed(2)} />
          <Tooltip labelFormatter={time => new Date(String(time)).toLocaleString()} />
          <Line dataKey="close" stroke="#0f8b6f" dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer> : <p className="pt-16 text-center text-sm text-slate-500">No IEX observations available</p>}
    </div>
  </section>;
}
