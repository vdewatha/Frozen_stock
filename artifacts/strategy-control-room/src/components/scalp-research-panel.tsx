import { useEffect, useState } from "react";
import { Activity, Lock, Play, RefreshCw } from "lucide-react";
import { RoleGate } from "@/components/access-control";
import { getScalpResearchRuns, startScalpResearch, type ScalpResearchRun } from "@/lib/api";

const symbols = ["AAPL", "MSFT", "SPY"];

export function ScalpResearchPanel() {
  const [runs, setRuns] = useState<ScalpResearchRun[]>([]);
  const [busy, setBusy] = useState(false);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const refresh = async () => { setBusy(true); try { setRuns((await getScalpResearchRuns()).items); setError(""); } catch (failure) { setError(failure instanceof Error ? failure.message : "Scalp research unavailable"); } finally { setBusy(false); } };
  useEffect(() => { void refresh(); const timer = window.setInterval(() => void refresh(), 15_000); return () => window.clearInterval(timer); }, []);
  const start = async () => { setStarting(true); try { await startScalpResearch(symbols); await refresh(); } catch (failure) { setError(failure instanceof Error ? failure.message : "Could not start scalp research"); } finally { setStarting(false); } };
  return <section className="border-y border-line bg-white p-1">
    <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line px-1 pb-5"><div><div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.08em] text-mint"><Activity size={15} />Separate scalp research</div><h2 className="mt-2 text-xl font-semibold text-ink">1-minute paper research lane</h2><p className="mt-1 max-w-3xl text-sm leading-6 text-slate-600">AAPL, MSFT and SPY only. Results are isolated from daily research and cannot submit orders.</p></div><div className="flex gap-2"><button className="focus-ring inline-flex h-9 items-center gap-2 border border-line px-3 text-xs font-semibold" disabled={busy} onClick={() => void refresh()}><RefreshCw size={14} className={busy ? "animate-spin" : ""} />Refresh</button><RoleGate requires="researcher"><button className="focus-ring inline-flex h-9 items-center gap-2 bg-ink px-3 text-xs font-semibold text-white" disabled={starting} onClick={() => void start()}><Play size={14} />{starting ? "Queueing…" : "Start study"}</button></RoleGate></div></div>
    {error && <p className="mt-4 border-l-2 border-coral bg-red-50 px-3 py-2 text-sm text-coral" role="alert">{error}</p>}
    <div className="mt-5 grid gap-3 sm:grid-cols-3"><div className="border border-line p-3"><span className="text-[10px] uppercase text-slate-500">Data</span><strong className="mt-2 block text-lg">Alpaca IEX · 1m</strong></div><div className="border border-line p-3"><span className="text-[10px] uppercase text-slate-500">Strategies</span><strong className="mt-2 block text-lg">EMA + mean reversion</strong></div><div className="border border-red-200 bg-red-50 p-3 text-red-900"><span className="text-[10px] uppercase">Execution</span><strong className="mt-2 flex items-center gap-1 text-lg"><Lock size={15} />Disabled</strong></div></div>
    {!runs.length && !busy ? <p className="mt-5 text-sm text-slate-500">No scalp studies recorded yet.</p> : <div className="mt-5 grid gap-3">{runs.map(run => <div key={run.run_id} className="border border-line p-4"><div className="flex flex-wrap justify-between gap-2"><strong>{run.symbols.join(", ")} · {run.timeframe}</strong><span className="text-xs uppercase text-slate-500">{run.status}</span></div><div className="mt-3 grid gap-3 sm:grid-cols-3 text-xs text-slate-600"><span>Bars: {Object.values(run.data_snapshot).reduce((sum, item) => sum + Number(item.bars ?? 0), 0)}</span><span>Provider: {String(run.assumptions.provider ?? "unknown")}</span><span className="flex items-center gap-1"><Lock size={12} />Research-only</span></div></div>)}</div>}
  </section>;
}
