import type { StockPaperObservedPerformance } from "@/lib/api";

export function ObservedPaperPerformance({ performance }: { performance?: StockPaperObservedPerformance }) {
  if (!performance) return null;
  const money = (value?: string | null) => {
    if (!value?.trim() || !Number.isFinite(Number(value))) return "Not measured";
    return new Intl.NumberFormat("en-US", { style: "currency", currency: performance.currency || "USD" }).format(Number(value));
  };
  return <section aria-label="Observed paper performance" className="border-b border-line p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-semibold">Change since initialization</h3>
      <span className="text-xs font-medium text-amber-800">Provisional, not qualified performance</span>
    </div>
    {performance.status === "provisional" ? <dl className="mt-3 grid gap-4 sm:grid-cols-3">
      <div><dt className="text-xs text-slate-500">Equity change excluding funding</dt><dd className="mt-1 text-xl font-semibold">{money(performance.net_change)}</dd></div>
      <div><dt className="text-xs text-slate-500">Net deposits / withdrawals</dt><dd className="mt-1 font-semibold">{money(performance.external_net_funding)}</dd></div>
      <div><dt className="text-xs text-slate-500">Reported fee subtotal, already included</dt><dd className="mt-1 font-semibold">{money(performance.reported_fee_expense)}</dd>
        <dd className="mt-1 text-xs text-amber-800">Total costs unverified{performance.new_fills_without_commission ? `; ${performance.new_fills_without_commission} fills without reported commission` : ""}</dd>
      </div>
    </dl> : null}
    <p className="mt-3 text-xs text-slate-600">{performance.reason}</p>
    {performance.as_of && <p className="mt-1 text-xs text-slate-500">As of {new Date(performance.as_of).toLocaleString()}</p>}
  </section>;
}
