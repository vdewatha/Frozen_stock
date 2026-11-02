import { useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export type ShortPerformance = {
  currency: string;
  costs_verified: boolean;
  net_pl: string | null;
  scope: string;
  points: { observed_at: string; gross_pl: string; filled_orders: number | null; audit_event_id: number }[];
};

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const sessionDay = (stamp: number) => new Date(stamp).toLocaleDateString("en-CA", { timeZone: "America/New_York" });
const tickTime = (stamp: number) => new Date(stamp).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });

export function shortProfitPoints(performance?: ShortPerformance, period = "all", now = Date.now()) {
  return (performance?.points ?? []).flatMap(point => {
    const stamp = Date.parse(point.observed_at);
    // Number(null) and Number("") are zero; missing evidence must remain missing.
    const gross = point.gross_pl == null || String(point.gross_pl).trim() === "" ? NaN : Number(point.gross_pl);
    if (!Number.isFinite(stamp) || !Number.isFinite(gross) || stamp > now + 5000
        || (period === "today" && sessionDay(stamp) !== sessionDay(now))) return [];
    return [{ stamp, gross, filledOrders: point.filled_orders, auditId: point.audit_event_id }];
  }).sort((a, b) => a.stamp - b.stamp);
}

export function shortProfitPlot(points: ReturnType<typeof shortProfitPoints>) {
  const plot: { stamp: number; gross: number | null }[] = [];
  points.forEach((point, index) => {
    if (index && point.stamp - points[index - 1].stamp > 150_000) {
      plot.push({ stamp: (point.stamp + points[index - 1].stamp) / 2, gross: null });
    }
    plot.push({ stamp: point.stamp, gross: point.gross });
  });
  return plot;
}

export function ShortProfitChart({ performance, now = Date.now() }: { performance?: ShortPerformance; now?: number }) {
  const [period, setPeriod] = useState("today");
  const points = shortProfitPoints(performance, period, now);
  const latest = points.at(-1);
  const minTime = points[0]?.stamp;
  const maxTime = latest?.stamp;
  const range = points.length === 1 ? [minTime! - 30_000, maxTime! + 30_000] : [minTime ?? 0, maxTime ?? 1];
  const ticks = points.length > 1 ? [minTime!, maxTime!] : minTime == null ? [] : [minTime];
  const crossDay = minTime != null && maxTime != null && sessionDay(minTime) !== sessionDay(maxTime);
  const formatTick = (stamp: number) => crossDay
    ? new Date(stamp).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    : tickTime(stamp);
  return <section aria-label="Short-position profit and loss">
      <div className="section-heading"><div><h2>Short-position P/L</h2><p className="section-meta">Cumulative gross USD · times ET · fees and borrow costs excluded</p></div>
      <select aria-label="P/L period" value={period} onChange={event => setPeriod(event.target.value)}><option value="today">Today</option><option value="all">Recorded history</option></select>
    </div>
    <dl className="short-monitor-metrics short-profit-metrics">
      <div><dt>Gross P/L</dt><dd>{latest ? money.format(latest.gross) : "Not observed"}</dd></div>
      <div><dt>Filled orders</dt><dd>{latest?.filledOrders ?? "Not observed"}</dd></div>
      <div><dt>Net P/L</dt><dd>Not verified</dd></div>
    </dl>
    {points.length ? <div className="short-profit-chart" role="img" aria-label={`Observed gross short-position P/L. Latest ${money.format(latest!.gross)}. ${points.length} recorded snapshots.`}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={shortProfitPlot(points)} margin={{ top: 20, right: 38, bottom: 8, left: 0 }} accessibilityLayer>
          <CartesianGrid stroke="#dfe5e8" vertical={false} />
          <XAxis dataKey="stamp" type="number" scale="time" domain={range} ticks={ticks} interval="preserveStartEnd" tickFormatter={formatTick} minTickGap={45} tick={{ fontSize: 12 }} />
          <YAxis width={76} domain={([min, max]: [number, number]) => [Math.min(0, min) - Math.max(1, (max - min) * .1), Math.max(0, max) + Math.max(1, (max - min) * .1)]} tickFormatter={value => money.format(Number(value))} tick={{ fontSize: 12 }} />
          <ReferenceLine y={0} stroke="#69757e" strokeDasharray="4 4" />
          <Tooltip position={{ x: 0, y: 0 }} wrapperStyle={{ maxWidth: "100%" }}
            labelFormatter={value => new Date(Number(value)).toLocaleString("en-US", { timeZone: "America/New_York", timeZoneName: "short" })}
            formatter={value => [money.format(Number(value)), "Gross P/L"]} />
          <Line dataKey="gross" type="linear" stroke="#176b93" strokeWidth={2} dot={points.length < 3 ? { r: 4 } : false} connectNulls={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div> : <p className="section-meta">No P/L observations recorded for this period. Missing history is not treated as zero.</p>}
    {latest?.filledOrders === 0 && <p className="section-meta">No filled short orders yet. The recorded $0 gross P/L is not a profit gain.</p>}
    <p className="section-meta">Broker-observed owned fills and open-position marks · {points.length} observation{points.length === 1 ? "" : "s"} · latest 1,000 reconciliations. Missing observation intervals remain gaps.</p>
    {!!points.length && <details className="short-profit-observations"><summary>Latest observations</summary><div className="short-monitor-table"><table><thead><tr><th>Observed (ET)</th><th>Gross P/L (USD)</th><th>Filled orders</th><th>Audit event</th></tr></thead><tbody>{points.slice(-20).reverse().map(point => <tr key={point.auditId}><td>{new Date(point.stamp).toLocaleString("en-US", { timeZone: "America/New_York" })}</td><td>{money.format(point.gross)}</td><td>{point.filledOrders ?? "Not observed"}</td><td>{point.auditId}</td></tr>)}</tbody></table></div></details>}
  </section>;
}
