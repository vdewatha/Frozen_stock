const statusClass: Record<string, string> = {
  paper_trading_active: "bg-emerald-50 text-mint border-emerald-200",
  paper_trading_candidate: "bg-amber-50 text-amber border-amber-200",
  promoted: "bg-emerald-50 text-mint border-emerald-200",
  applied: "bg-emerald-50 text-mint border-emerald-200",
  rejected: "bg-red-50 text-coral border-red-200",
  needs_more_data: "bg-amber-50 text-amber border-amber-200",
  queued: "bg-slate-100 text-slate-700 border-slate-200",
  bull_trend: "bg-emerald-50 text-mint border-emerald-200",
  bear_stress: "bg-red-50 text-coral border-red-200",
  volatile_range: "bg-amber-50 text-amber border-amber-200",
  sideways_range: "bg-slate-100 text-slate-700 border-slate-200",
  transition: "bg-blue-50 text-blue-700 border-blue-200",
  unclassified: "bg-slate-100 text-slate-700 border-slate-200",
  positive: "bg-emerald-50 text-mint border-emerald-200",
  negative: "bg-red-50 text-coral border-red-200",
  neutral: "bg-slate-100 text-slate-700 border-slate-200",
  tight_policy: "bg-amber-50 text-amber border-amber-200",
  growth_stress: "bg-red-50 text-coral border-red-200",
  goldilocks: "bg-emerald-50 text-mint border-emerald-200",
  mixed_macro: "bg-slate-100 text-slate-700 border-slate-200",
  policy_stress: "bg-red-50 text-coral border-red-200",
  unknown: "bg-slate-100 text-slate-700 border-slate-200",
  live_blocked: "bg-emerald-50 text-mint border-emerald-200",
  live_enabled: "bg-red-50 text-coral border-red-200",
  research: "bg-slate-100 text-slate-700 border-slate-200",
  paused: "bg-red-50 text-coral border-red-200",
  retired: "bg-zinc-100 text-zinc-700 border-zinc-300",
  hold: "bg-slate-100 text-slate-700 border-slate-200",
  pause: "bg-red-50 text-coral border-red-200",
  retire: "bg-zinc-100 text-zinc-700 border-zinc-300",
  promote: "bg-emerald-50 text-mint border-emerald-200"
};

export function StatusPill({ status }: { status: string }) {
  return (
    <span className={`inline-flex h-6 items-center rounded border px-2 text-xs font-medium ${statusClass[status] ?? statusClass.research}`}>
      {status.replaceAll("_", " ")}
    </span>
  );
}
