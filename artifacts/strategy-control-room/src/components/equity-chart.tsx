

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

type EquityChartProps = {
  data: { date: string; value?: number; equity?: number }[];
};

export function EquityChart({ data }: EquityChartProps) {
  if (data.length === 0) return <p className="p-8 text-sm text-slate-500">Equity history is unavailable until reconciled account snapshots have been recorded.</p>;
  const chartData = data.map((point) => ({ date: point.date, value: point.value ?? point.equity ?? 0 }));
  return (
    <div className="h-72 w-full">
      <ResponsiveContainer>
        <AreaChart data={chartData} margin={{ left: 0, right: 12, top: 12, bottom: 0 }}>
          <defs>
            <linearGradient id="equityFill" x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor="#0f8b6f" stopOpacity={0.28} />
              <stop offset="100%" stopColor="#0f8b6f" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#d9dee7" strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="date" tick={{ fontSize: 11 }} tickLine={false} axisLine={false} minTickGap={36} />
          <YAxis tick={{ fontSize: 11 }} tickLine={false} axisLine={false} domain={["dataMin - 500", "dataMax + 500"]} width={68} />
          <Tooltip formatter={(value) => [`$${Number(value).toLocaleString()}`, "Equity"]} />
          <Area type="monotone" dataKey="value" stroke="#0f8b6f" strokeWidth={2} fill="url(#equityFill)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
