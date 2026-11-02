import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Activity, ArrowRight, ShieldCheck } from "lucide-react";
import { Link } from "wouter";
import { API_BASE_URL, authenticatedFetch, getErrorMessage } from "@/lib/api";
import { ShortProfitChart, type ShortPerformance } from "@/components/short-profit-chart";

export type ShortPilotStatus = {
  autonomous: boolean;
  performance?: ShortPerformance;
  configured_enabled: boolean;
  entry_policy?: string;
  pilot: { status: string; symbols: string[]; max_order_notional: string; max_gross_notional: string; expires_at: string } | null;
  reconciliation?: { positions: Record<string, string>; observed_at: string } | null;
  automation_last_cycle: { checked_at: string; status: string; action: string; reason?: string; error_type?: string; refresh_failure?: string | null;
    decisions?: { symbol: string; action: string; reason?: string; execution_feed?: string;
      evidence?: { rsi2: number; sma200: number; enter: boolean; cover: boolean; daily_session: string } }[] } | null;
  orders: { client_order_id: string; symbol: string; intent: string; status: string; quantity: string; filled_quantity: string; unresolved: boolean; created_at?: string }[];
};

async function loadStatus(): Promise<ShortPilotStatus> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock-paper/short-pilot`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Short pilot status unavailable (${response.status})`);
  return response.json();
}

function useShortPilot() {
  return useQuery({ queryKey: ["short-pilot-status"], queryFn: loadStatus, refetchInterval: 5000, retry: 1 });
}

function useMonitorClock() {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

function label(value?: string) { return value ? value.replaceAll("_", " ") : "Not recorded"; }
function time(value?: string) { return value ? new Date(value).toLocaleString("en-US", { timeZone: "America/New_York" }) + " ET" : "Not observed"; }

export function shortWorkerState(data: ShortPilotStatus, now = Date.now()) {
  if (!data.autonomous) return "Not enabled";
  const stamp = Date.parse(data.automation_last_cycle?.checked_at ?? "");
  if (!Number.isFinite(stamp)) return "Awaiting first cycle";
  if (now - stamp > 150_000 || stamp > now + 5000) return "Heartbeat stale";
  return data.automation_last_cycle?.status === "blocked" ? "Cycle blocked" : "Running";
}

export function ShortPilotNotice() {
  const query = useShortPilot();
  const now = useMonitorClock();
  if (!query.data?.pilot) return null;
  return <div className="overview-notice"><Activity size={18} /><span><strong>Autonomous short pilot</strong><span className="notice-detail">{shortWorkerState(query.data, now)} · separate paper account lane</span></span><Link href="/portfolio/shorts">Live monitor <ArrowRight size={15} /></Link></div>;
}

export function ShortPilotSnapshot({ data, now = Date.now() }: { data: ShortPilotStatus; now?: number }) {
  const cycle = data.automation_last_cycle;
  const positions = Object.entries(data.reconciliation?.positions ?? {}).filter(([, quantity]) => Number(quantity) !== 0);
  return <div className="short-monitor">
    <section>
      <div className="section-heading"><h2>Autonomous short pilot</h2><span className="paper-label"><ShieldCheck size={15} />Development paper only</span></div>
      <dl className="short-monitor-metrics">
        <div><dt>Worker</dt><dd>{shortWorkerState(data, now)}</dd></div>
        <div><dt>Latest action</dt><dd>{label(cycle?.reason ?? cycle?.action)}</dd></div>
        <div><dt>Order / gross cap</dt><dd>{data.pilot ? `$${Number(data.pilot.max_order_notional).toLocaleString()} / $${Number(data.pilot.max_gross_notional).toLocaleString()}` : "No policy"}</dd></div>
        <div><dt>Broker orders</dt><dd>{data.orders.length}<small>{data.orders.filter(order => order.unresolved).length} unresolved</small></dd></div>
      </dl>
      <p className="section-meta">Cycle {time(cycle?.checked_at)} · Broker reconciliation {time(data.reconciliation?.observed_at)}</p>
      <p className="section-meta">Entry policy: {data.entry_policy === "regular_session" ? "Full regular market session, including afternoons. Exchange early closes apply." : "Not recorded"}</p>
      {cycle?.status === "blocked" && <p role="alert" className="workspace-error">{cycle.reason ?? cycle.error_type ?? "Cycle blocked"}</p>}
      {cycle?.refresh_failure && <p role="alert" className="workspace-error">Data refresh: {cycle.refresh_failure}</p>}
    </section>
    <ShortProfitChart performance={data.performance} now={now} />
    <section>
      <div className="section-heading"><h2>Strategy decisions</h2><span className="section-meta">RSI(2) short bounce · 200-session trend</span></div>
      <div className="short-monitor-table"><table><thead><tr><th>Symbol</th><th>Daily session</th><th>RSI(2)</th><th>200-day SMA</th><th>Execution feed</th><th>Decision</th></tr></thead>
        <tbody>{cycle?.decisions?.map((decision, index) => <tr key={`${decision.symbol}-${index}`}><th scope="row">{decision.symbol}</th><td>{decision.evidence?.daily_session ?? "Not available"}</td><td>{decision.evidence?.rsi2?.toFixed(2) ?? "Not available"}</td><td>{decision.evidence?.sma200?.toFixed(2) ?? "Not available"}</td><td>{label(decision.execution_feed)}</td><td>{label(decision.action)}{decision.reason && <small>{decision.reason}</small>}</td></tr>)}</tbody></table></div>
      {!cycle?.decisions?.length && <p className="section-meta">No symbol decisions recorded in the latest cycle.</p>}
    </section>
    <section>
      <div className="section-heading"><h2>Broker-observed short positions</h2><span className="section-meta">{positions.length} open</span></div>
      {data.reconciliation ? positions.length ? <div className="short-monitor-table"><table><thead><tr><th>Symbol</th><th>Signed quantity</th></tr></thead><tbody>{positions.map(([symbol, quantity]) => <tr key={symbol}><th scope="row">{symbol}</th><td>{quantity}</td></tr>)}</tbody></table></div> : <p className="section-meta">Account is flat at the last reconciliation.</p> : <p className="section-meta">No broker reconciliation recorded.</p>}
    </section>
    <section>
      <div className="section-heading"><h2>Recent broker intents</h2><span className="section-meta">Latest 20</span></div>
      {data.orders.length ? <div className="short-monitor-table"><table><thead><tr><th>Symbol</th><th>Intent</th><th>Shares / filled</th><th>Status</th><th>Created</th></tr></thead><tbody>{data.orders.slice(-20).reverse().map(order => <tr key={order.client_order_id}><th scope="row">{order.symbol}</th><td>{label(order.intent)}</td><td>{Number(order.quantity)} / {Number(order.filled_quantity)}</td><td>{label(order.status)}</td><td>{time(order.created_at)}</td></tr>)}</tbody></table></div> : <p className="section-meta">No broker order intents recorded. No fills or profits are implied.</p>}
      <p className="section-meta">All-in costs and P/L remain unverified. Policy expires {time(data.pilot?.expires_at)}.</p>
    </section>
  </div>;
}

export function ShortPilotMonitor() {
  const query = useShortPilot();
  const now = useMonitorClock();
  return <>{query.error && <p role="alert" className="workspace-error">{getErrorMessage(query.error, "Short pilot unavailable")}</p>}{query.data ? <ShortPilotSnapshot data={query.data} now={now} /> : !query.error && <p role="status">Loading autonomous paper status...</p>}</>;
}
