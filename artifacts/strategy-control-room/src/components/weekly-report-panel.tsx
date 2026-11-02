import { useQuery } from "@tanstack/react-query";
import { CalendarCheck } from "lucide-react";
import { API_BASE_URL, authenticatedFetch, getErrorMessage } from "@/lib/api";
import { formatPct, formatUsd } from "@/components/paper-scorecard-panel";

type Totals = { trades: number; win_rate: number | null; total_pnl: number | null; avg_pnl: number | null; paper_bps?: number | null };

export type WeeklyReport = {
  week: string;
  generated_at: string;
  this_week: Totals;
  all_time: Totals;
  goal: { target_win_rate: number; min_trades_for_evidence: number };
  strategies: {
    strategy_id: number | null; strategy_name: string; status: string | null;
    week_trades: number; week_pnl: number | null; trades: number; win_rate: number | null;
    total_pnl: number | null; paper_bps: number | null; backtest_bps: number | null;
    backtest_source: string | null; verdict: string;
  }[];
  blocked_buys: { submitted_buys: number; blocked_buys: number; top_reasons: { reason: string; count: number }[] };
  short_pilot: { enabled: boolean; gross_pl: string | null; filled_orders: number } | null;
  open_unrealized_pnl: number | null;
};

async function loadReport(): Promise<{ latest: WeeklyReport | null; preview: WeeklyReport }> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock-paper/weekly-report`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Weekly report unavailable (${response.status})`);
  return response.json();
}

function bps(value: number | null) { return value === null ? "n/a" : `${value > 0 ? "+" : ""}${value.toFixed(1)} bps`; }
function label(value: string | null) { return value ? value.replaceAll("_", " ") : "unknown"; }

export function WeeklyReportView({ report, stored }: { report: WeeklyReport; stored: boolean }) {
  const { this_week: week, all_time: total, goal } = report;
  return <div className="space-y-6">
    <section>
      <div className="section-heading"><h2><CalendarCheck size={17} className="mr-2 inline" />Week {report.week}</h2>
        <span className="section-meta">{stored ? "Stored weekly report" : "Live preview; the stored report is written Saturday morning"} · {new Date(report.generated_at).toLocaleString("en-US", { timeZone: "America/New_York" })} ET</span></div>
      <dl className="short-monitor-metrics">
        <div><dt>This week</dt><dd>{week.trades} trades<small>{formatUsd(week.total_pnl)} · win {formatPct(week.win_rate)}</small></dd></div>
        <div><dt>All time</dt><dd>{total.trades} trades<small>{formatUsd(total.total_pnl)} · win {formatPct(total.win_rate)}</small></dd></div>
        <div><dt>Avg per trade</dt><dd>{formatUsd(total.avg_pnl)}<small>{bps(total.paper_bps ?? null)}</small></dd></div>
        <div><dt>Open positions</dt><dd>{formatUsd(report.open_unrealized_pnl)}<small>unrealized, not in totals</small></dd></div>
      </dl>
      <p className="section-meta">Goal: win {formatPct(goal.target_win_rate)} while making money. Results are judged once a strategy has {goal.min_trades_for_evidence} closed trades.</p>
    </section>
    <section>
      <div className="section-heading"><h2>Strategies against their backtest</h2><span className="section-meta">Net return per trade, paper vs research</span></div>
      {report.strategies.length ? <div className="short-monitor-table"><table><thead><tr><th>Strategy</th><th>This week</th><th>All-time trades</th><th>Win rate</th><th>Net P&amp;L</th><th>Paper</th><th>Backtest</th><th>Verdict</th></tr></thead>
        <tbody>{report.strategies.map(row => <tr key={`${row.strategy_id}-${row.strategy_name}`}>
          <th scope="row">{row.strategy_name}<small>{label(row.status)}</small></th>
          <td>{row.week_trades}<small>{formatUsd(row.week_pnl)}</small></td>
          <td>{row.trades}</td><td>{formatPct(row.win_rate)}</td><td>{formatUsd(row.total_pnl)}</td>
          <td>{bps(row.paper_bps)}</td><td title={row.backtest_source ?? undefined}>{bps(row.backtest_bps)}</td><td>{row.verdict}</td>
        </tr>)}</tbody></table></div> : <p className="section-meta">No closed trades yet.</p>}
    </section>
    <section>
      <div className="section-heading"><h2>Why buy signals did not trade</h2><span className="section-meta">Past 7 days · {report.blocked_buys.submitted_buys} placed, {report.blocked_buys.blocked_buys} blocked</span></div>
      {report.blocked_buys.top_reasons.length ? <div className="short-monitor-table"><table><thead><tr><th>Reason</th><th>Times</th></tr></thead>
        <tbody>{report.blocked_buys.top_reasons.map(item => <tr key={item.reason}><th scope="row">{item.reason}</th><td>{item.count}</td></tr>)}</tbody></table></div>
        : <p className="section-meta">No blocked buy signals recorded this week.</p>}
    </section>
    {report.short_pilot?.enabled && <section>
      <div className="section-heading"><h2>Short automation</h2><span className="section-meta">Own paper account</span></div>
      <p className="section-meta">{report.short_pilot.filled_orders} filled orders · gross P/L {report.short_pilot.gross_pl === null ? "n/a" : formatUsd(Number(report.short_pilot.gross_pl))} (costs unverified)</p>
    </section>}
  </div>;
}

export function WeeklyReportPanel() {
  const query = useQuery({ queryKey: ["weekly-report"], queryFn: loadReport, refetchInterval: 300_000, retry: 1 });
  if (query.error) return <p role="alert" className="workspace-error">{getErrorMessage(query.error, "Weekly report unavailable")}</p>;
  if (!query.data) return <p role="status">Loading weekly report...</p>;
  const { latest, preview } = query.data;
  return <div className="space-y-8">
    <WeeklyReportView report={preview} stored={false} />
    {latest && latest.week !== preview.week && <><hr className="border-line" /><WeeklyReportView report={latest} stored /></>}
  </div>;
}
