import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, RefreshCw } from "lucide-react";
import { API_BASE_URL, authenticatedFetch } from "@/lib/api";
import { RoleGate } from "@/components/access-control";

export type DelayedSipStatus = {
  enabled: boolean;
  poll_fresh: boolean;
  delay_minutes: number;
  last_collection: null | { status: string; failure_class: string | null };
  comparison_window_start: string;
  comparison_window_end: string;
  symbols: { symbol: string; total_bars: number; window_bars: number; iex_window_bars: number; matched_minutes: number; mean_abs_close_gap_bps: number | null; latest_bar: string | null }[];
};

async function request<T>(path: string, method = "GET"): Promise<T> {
  const response = await authenticatedFetch(`${API_BASE_URL}/market-data/delayed-sip/${path}`, { method, cache: "no-store" });
  if (!response.ok) throw new Error(`Delayed SIP request failed (${response.status})`);
  return response.json();
}

export function DelayedSipSummary({ data }: { data: DelayedSipStatus }) {
  return <>
    <div className="my-3 flex flex-wrap gap-x-6 gap-y-1 text-sm">
      <span>Collector: {!data.enabled ? "Disabled" : data.poll_fresh ? "Polling" : "No recent poll"}</span>
      <span>Last result: {data.last_collection?.status ?? "None"}</span>
      <span>Cutoff: {new Date(data.comparison_window_end).toLocaleString()}</span>
    </div>
    <div className="overflow-x-auto">
      <table className="w-full min-w-[620px] text-left text-sm">
        <thead className="border-b border-line text-slate-600"><tr>
          {["Symbol", "Stored SIP bars", "Window SIP", "Window IEX", "Matched minutes", "Mean close gap (bps)"].map(label => <th className="whitespace-nowrap px-2 py-2 font-medium" key={label}>{label}</th>)}
        </tr></thead>
        <tbody>{data.symbols.map(row => <tr key={row.symbol} className="border-b border-line">
          <th scope="row" className="px-2 py-2 font-medium">{row.symbol}</th>
          <td className="px-2 py-2">{row.total_bars}</td><td className="px-2 py-2">{row.window_bars}</td>
          <td className="px-2 py-2">{row.iex_window_bars}</td><td className="px-2 py-2">{row.matched_minutes}</td>
          <td className="px-2 py-2">{row.mean_abs_close_gap_bps?.toFixed(2) ?? "Unknown"}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </>;
}

export function DelayedSipPanel() {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ["delayed-sip-research"], queryFn: () => request<DelayedSipStatus>("status"), refetchInterval: 30000 });
  const collect = useMutation({ mutationFn: () => request("collect", "POST"), onSuccess: () => queryClient.invalidateQueries({ queryKey: ["delayed-sip-research"] }) });
  return <section className="min-w-0 border-y border-line bg-white py-4" aria-label="Delayed SIP research feed">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="text-base font-semibold">Delayed SIP Research</h2><p className="text-sm text-amber-800">{status.data?.delay_minutes ?? 16}+ minute delay. Not qualified for execution.</p></div>
      <div className="flex gap-2">
        <button title="Refresh delayed SIP status" aria-label="Refresh delayed SIP status" disabled={status.isFetching} onClick={() => void status.refetch()} className="flex h-9 w-9 items-center justify-center rounded border border-line"><RefreshCw size={16} /></button>
        <RoleGate requires="researcher"><button title="Collect delayed SIP bars" aria-label="Collect delayed SIP bars" disabled={!status.data?.enabled || collect.isPending} onClick={() => collect.mutate()} className="flex h-9 w-9 items-center justify-center rounded border border-line"><Download size={16} /></button></RoleGate>
      </div>
    </div>
    {status.error || collect.error ? <p role="alert" className="text-sm text-red-700">{(status.error ?? collect.error)?.message}</p> : null}
    {status.data?.last_collection?.failure_class ? <p role="alert" className="text-sm text-red-700">Collection unavailable: {status.data.last_collection.failure_class}</p> : null}
    {collect.isSuccess ? <p role="status" className="text-sm text-slate-600">Collection queued. Awaiting worker result.</p> : null}
    {status.data ? <DelayedSipSummary data={status.data} /> : <p role="status" className="my-3 text-sm text-slate-600">{status.isError ? "Status unavailable" : "Loading delayed observations"}</p>}
  </section>;
}
