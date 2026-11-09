

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

type EquityChartProps = {
  data: { date: string; value?: number; equity?: number }[];
};

export function equityDateLabel(value: string, detail = false): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "Unknown date";
  const daily = /^\d{4}-\d{2}-\d{2}$/.test(value);
  return date.toLocaleString("en-US", {
    month: "short", day: "numeric", timeZone: daily ? "UTC" : "America/New_York",
    ...(detail && !daily ? { hour: "numeric", minute: "2-digit", timeZoneName: "short" } as const : {}),
  });
}

export function equityChartPoints(data: EquityChartProps["data"]) {
  return data.flatMap(point => {
    const value = point.value ?? point.equity;
    return typeof value === "number" && Number.isFinite(value) ? [{ date: point.date, value }] : [];
  });
}

// The series is daily points for the account's history plus 15-minute points
// for the last two days. On an evenly spaced category axis those two days
// filled most of the width, so the axis is a real time scale.
export function equityTimePoints(data: EquityChartProps["data"]) {
  return equityChartPoints(data).flatMap(point => {
    const daily = /^\d{4}-\d{2}-\d{2}$/.test(point.date);
    const t = Date.parse(daily ? `${point.date}T21:00:00Z` : point.date);
    return Number.isFinite(t) ? [{ ...point, t }] : [];
  });
}

export function EquityChart({ data }: EquityChartProps) {
  const chartData = equityTimePoints(data);
  const label = (t: unknown) => chartData.find(point => point.t === Number(t))?.date ?? new Date(Number(t)).toISOString();
  if (chartData.length === 0) return <p className="p-8 text-sm text-slate-500">Equity history is unavailable until reconciled account snapshots have been recorded.</p>;
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
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={value => equityDateLabel(new Date(Number(value)).toISOString())} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} minTickGap={36} />
          <YAxis tick={{ fontSize: 11 }} tickLine={false} axisLine={false} domain={["dataMin - 500", "dataMax + 500"]} width={68} />
          <Tooltip position={{ x: 0, y: 0 }} wrapperStyle={{ maxWidth: "100%", whiteSpace: "normal" }}
            labelFormatter={value => equityDateLabel(label(value), true)}
            formatter={(value) => [`$${Number(value).toLocaleString()}`, "Equity"]} />
          <Area type="monotone" dataKey="value" stroke="#0f8b6f" strokeWidth={2} fill="url(#equityFill)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
