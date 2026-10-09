import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, BrainCircuit, Check, ChevronRight, Circle, Clock3, FlaskConical, Lock, RefreshCw } from "lucide-react";
import { RoleGate } from "@/components/access-control";
import { getAgentResearchRuns, startAgentResearch, type AgentResearchRun } from "@/lib/api";

const SYMBOLS = ["AAPL", "MSFT", "SPY", "QQQ"] as const;
type StageState = "complete" | "running" | "waiting" | "blocked";

function stageTone(state: StageState) {
  return state === "complete" ? "border-emerald-200 bg-emerald-50 text-emerald-800" : state === "running" ? "border-sky-200 bg-sky-50 text-sky-800" : state === "blocked" ? "border-red-200 bg-red-50 text-red-800" : "border-line bg-white text-slate-600";
}

function latestBySymbol(runs: AgentResearchRun[]) {
  return new Map(SYMBOLS.map(symbol => [symbol, runs.find(run => run.symbol === symbol)]));
}

function stageState(run: AgentResearchRun | undefined, stage: "context" | "claude" | "evaluation" | "paper" | "learning"): StageState {
  if (!run) return "waiting";
  if (stage === "context") return run.source_snapshot.length ? "complete" : "blocked";
  if (stage === "claude") return run.status === "completed" ? "complete" : run.status === "queued" || run.status === "running" ? "running" : "blocked";
  if (stage === "evaluation") return run.evaluation.status === "complete" ? "complete" : run.status === "completed" ? "waiting" : "blocked";
  if (stage === "paper") return "blocked";
  return run.evaluation.status === "complete" ? "complete" : "waiting";
}

function Stage({ title, number, state, detail }: { title: string; number: string; state: StageState; detail: string }) {
  const Icon = state === "complete" ? Check : state === "blocked" ? AlertTriangle : state === "running" ? RefreshCw : Clock3;
  return <div className={`min-w-[150px] flex-1 border p-3 ${stageTone(state)}`}><div className="flex items-center justify-between gap-2"><span className="text-[10px] font-semibold uppercase tracking-[0.08em]">{number}</span><Icon size={15} className={state === "running" ? "animate-spin" : ""} /></div><strong className="mt-3 block text-sm">{title}</strong><span className="mt-1 block text-[11px] leading-4 opacity-80">{detail}</span><span className="mt-3 block text-[10px] font-semibold uppercase">{state}</span></div>;
}

function runDetail(run: AgentResearchRun | undefined) {
  if (!run) return "No run recorded for this symbol.";
  if (run.status === "queued" || run.status === "running") return "This symbol is currently being processed.";
  if (run.error) return run.error;
  return run.result?.rationale ?? "No recommendation text was returned.";
}

export function ResearchControlRoom() {
  const [runs, setRuns] = useState<AgentResearchRun[]>([]);
  const [total, setTotal] = useState(0);
  const [selectedSymbol, setSelectedSymbol] = useState<(typeof SYMBOLS)[number]>("AAPL");
  const [busy, setBusy] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");

  async function refresh() {
    setBusy(true);
    try {
      const page = await getAgentResearchRuns();
      setRuns(page.items);
      setTotal(page.total);
      setError("");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Research stream unavailable");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5_000);
    return () => window.clearInterval(timer);
  }, []);

  const bySymbol = useMemo(() => latestBySymbol(runs), [runs]);
  const selectedRun = bySymbol.get(selectedSymbol);
  const contextRows = selectedRun?.source_snapshot.filter(row => row.kind === "daily_price") ?? [];
  const latestObservedValue = contextRows.at(-1)?.observed_at;
  const latestObserved = typeof latestObservedValue === "string" ? latestObservedValue : undefined;

  async function startRun() {
    setStarting(true);
    try {
      await startAgentResearch(selectedSymbol);
      await refresh();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Research could not be queued");
    } finally {
      setStarting(false);
    }
  }

  const stageDetails = (stage: "context" | "claude" | "evaluation" | "paper" | "learning") => {
    const state = stageState(selectedRun, stage);
    if (stage === "context") return latestObserved ? `Latest observation ${new Date(latestObserved).toLocaleString()}` : "Waiting for bounded source context";
    if (stage === "claude") return selectedRun?.result ? `${selectedRun.result.recommendation} · ${Math.round(selectedRun.result.confidence * 100)}% self-reported confidence` : "Provider response pending";
    if (stage === "evaluation") return selectedRun?.evaluation.status === "complete" ? "Forward outcome scored" : "Waiting for future observations";
    if (stage === "paper") return "Research-only; order path disabled";
    return selectedRun?.evaluation.status === "complete" ? "Outcome available for memory" : "No outcome label yet";
  };

  return <div className="grid gap-7">
    <section className="research-control-room border-y border-line bg-white">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line px-1 pb-5">
        <div><div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.08em] text-mint"><FlaskConical size={15} />Live research control room</div><h2 className="mt-2 text-xl font-semibold text-ink">Pipeline and symbol investigation</h2><p className="mt-1 max-w-3xl text-sm leading-6 text-slate-600">Select a symbol to follow its bounded market context, Claude analysis, evaluation state, and paper-trading decision in one trace.</p></div>
        <div className="flex items-center gap-2"><span className="text-xs text-slate-500">{busy ? "Refreshing…" : `${total} recorded runs`}</span><button className="focus-ring inline-flex h-9 items-center gap-2 border border-line px-3 text-xs font-semibold" onClick={() => void refresh()} disabled={busy}><RefreshCw size={14} className={busy ? "animate-spin" : ""} />Refresh</button></div>
      </div>
      {error && <p className="mt-4 border-l-2 border-coral bg-red-50 px-3 py-2 text-sm text-coral" role="alert">{error}</p>}
      <div className="mt-5 flex items-center gap-2 overflow-x-auto pb-2">{SYMBOLS.map(symbol => { const run = bySymbol.get(symbol); const selected = symbol === selectedSymbol; return <button key={symbol} className={`focus-ring shrink-0 border px-4 py-2 text-left ${selected ? "border-mint bg-mint/10" : "border-line bg-white"}`} onClick={() => setSelectedSymbol(symbol)}><strong className="block text-sm">{symbol}</strong><span className="mt-1 block text-[10px] uppercase text-slate-500">{run?.status ?? "no run"}</span></button>; })}</div>
      <div className="mt-4 overflow-x-auto pb-2"><div className="flex min-w-[820px] items-stretch gap-2"><Stage title="Market context" number="01" state={stageState(selectedRun, "context")} detail={stageDetails("context")} /><ChevronRight className="mt-10 shrink-0 text-slate-300" size={18} /><Stage title="Claude research" number="02" state={stageState(selectedRun, "claude")} detail={stageDetails("claude")} /><ChevronRight className="mt-10 shrink-0 text-slate-300" size={18} /><Stage title="Forward evaluation" number="03" state={stageState(selectedRun, "evaluation")} detail={stageDetails("evaluation")} /><ChevronRight className="mt-10 shrink-0 text-slate-300" size={18} /><Stage title="Paper decision" number="04" state={stageState(selectedRun, "paper")} detail={stageDetails("paper")} /><ChevronRight className="mt-10 shrink-0 text-slate-300" size={18} /><Stage title="Learning memory" number="05" state={stageState(selectedRun, "learning")} detail={stageDetails("learning")} /></div></div>

      <div className="mt-7 grid gap-6 border-t border-line pt-6 xl:grid-cols-[minmax(0,1.3fr)_minmax(320px,0.7fr)]">
        <div><div className="flex flex-wrap items-center justify-between gap-3"><div><span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-slate-500">Selected symbol</span><h3 className="mt-1 text-2xl font-semibold text-ink">{selectedSymbol}</h3></div><RoleGate requires="researcher"><button className="focus-ring inline-flex h-9 items-center gap-2 bg-ink px-3 text-xs font-semibold text-white" disabled={starting} onClick={() => void startRun()}><BrainCircuit size={14} />{starting ? "Queueing…" : "Run research"}</button></RoleGate></div>
          <div className="mt-5 grid gap-3 sm:grid-cols-3"><div className="border border-line p-3"><span className="text-[10px] uppercase text-slate-500">Recommendation</span><strong className="mt-2 block text-lg">{selectedRun?.result?.recommendation ?? "Unavailable"}</strong></div><div className="border border-line p-3"><span className="text-[10px] uppercase text-slate-500">Evaluation</span><strong className="mt-2 block text-lg">{selectedRun?.evaluation.status ?? "Waiting"}</strong></div><div className="border border-red-200 bg-red-50 p-3 text-red-900"><span className="text-[10px] uppercase">Eligibility</span><strong className="mt-2 flex items-center gap-1 text-lg"><Lock size={15} />Not eligible</strong></div></div>
          <div className="mt-5 border-l-2 border-mint pl-4"><p className="text-sm leading-6 text-slate-700">{runDetail(selectedRun)}</p>{selectedRun?.result?.limitations?.length ? <p className="mt-3 text-xs leading-5 text-slate-500"><strong>Known limitations:</strong> {selectedRun.result.limitations.join(" · ")}</p> : null}</div>
        </div>
        <aside className="border border-line bg-slate-50 p-4"><div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.08em] text-slate-600"><Circle size={10} className="fill-mint text-mint" />Evidence trace</div><dl className="mt-4 grid gap-3 text-xs"><div className="flex justify-between gap-4 border-b border-line pb-2"><dt className="text-slate-500">Source rows</dt><dd className="font-semibold text-ink">{selectedRun?.source_snapshot.length ?? 0}</dd></div><div className="flex justify-between gap-4 border-b border-line pb-2"><dt className="text-slate-500">Latest context</dt><dd className="text-right font-semibold text-ink">{latestObserved ? new Date(latestObserved).toLocaleString() : "Unavailable"}</dd></div><div className="flex justify-between gap-4 border-b border-line pb-2"><dt className="text-slate-500">Model</dt><dd className="text-right font-semibold text-ink">{selectedRun?.model_name ?? "Not run"}</dd></div><div className="flex justify-between gap-4"><dt className="text-slate-500">Broker authority</dt><dd className="flex items-center gap-1 font-semibold text-red-700"><Lock size={12} />None</dd></div></dl></aside>
      </div>
    </section>
  </div>;
}
