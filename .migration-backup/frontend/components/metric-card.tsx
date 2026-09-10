type MetricCardProps = {
  label: string;
  value: string;
  delta?: string;
  tone?: "neutral" | "good" | "bad" | "warn";
};

const toneClass = {
  neutral: "text-ink",
  good: "text-mint",
  bad: "text-coral",
  warn: "text-amber"
};

export function MetricCard({ label, value, delta, tone = "neutral" }: MetricCardProps) {
  return (
    <div className="rounded-md border border-line bg-white p-4">
      <div className="text-xs font-semibold uppercase tracking-normal text-slate-500">{label}</div>
      <div className={`mt-2 text-2xl font-semibold ${toneClass[tone]}`}>{value}</div>
      {delta ? <div className="mt-1 text-sm text-slate-500">{delta}</div> : null}
    </div>
  );
}
