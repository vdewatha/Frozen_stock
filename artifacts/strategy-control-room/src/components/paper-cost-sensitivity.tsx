import type { PaperCostSensitivityReport } from "@/lib/api";

export function PaperCostSensitivity({ report }: { report?: PaperCostSensitivityReport }) {
  if (!report) return null;
  const money = (value: string) => value?.trim() && Number.isFinite(Number(value))
    ? new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 6 }).format(Number(value))
    : "Not measured";
  return <section aria-label="Modeled cost sensitivity" className="border-b border-line p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-semibold">Modeled Cost Sensitivity</h3>
      <span className="text-xs font-medium text-amber-800">Hypothetical, not realized profit</span>
    </div>
    {report.status === "research_only" && <>
      <p className="mt-2 text-xs text-slate-600">Unreported commissions: {report.fills_without_reported_commission ?? "Unknown"} fills</p>
      <table className="mt-3 w-full table-fixed text-left text-xs">
        <thead><tr><th className="py-2 pr-2 font-medium">Extra cost (bps/side)</th><th className="py-2 pr-2 font-medium">Modeled extra cost</th><th className="py-2 font-medium">Modeled cash change</th></tr></thead>
        <tbody>{report.scenarios.map(row => <tr key={row.additional_cost_bps_per_side} className="border-t border-line">
          <td className="py-2 pr-2">{row.additional_cost_bps_per_side}</td>
          <td className="break-words py-2 pr-2 tabular-nums">{money(row.additional_modeled_cost)}</td>
          <td className="break-words py-2 tabular-nums">{money(row.modeled_fill_cash_change)}</td>
        </tr>)}</tbody>
      </table>
    </>}
    <p className="mt-3 text-xs text-slate-600">{report.reason}</p>
    <p className="mt-1 break-words text-xs text-slate-500">Model: {report.assumptions.version}</p>
  </section>;
}
