import { useQuery } from "@tanstack/react-query";
import { GraduationCap } from "lucide-react";
import { API_BASE_URL, authenticatedFetch, getErrorMessage } from "@/lib/api";
import { formatPct, formatUsd } from "@/components/paper-scorecard-panel";
import { StatusPill } from "@/components/status-pill";

export type Totals = { trades: number; wins: number; win_rate: number | null; avg_return: number | null; net_pnl: number | null };
type Check = { key: string; label: string; status: "ok" | "warning" | "failing" | "waiting"; detail: string };

export type LearningScoreboard = {
  generated_at: string;
  health: { session: { date: string; in_progress: boolean } | null; checks: Check[] };
  progress: {
    overall: Totals;
    weeks: (Totals & { week: string })[];
    strategies: (Totals & { strategy_type: string; recent: Totals; earlier: Totals; evidence: string })[];
    goal: { target_win_rate: number; min_trades_for_evidence: number };
  };
  causes: { diagnosed: number; by_cause: Record<string, number>; by_family: Record<string, number>; contributing: Record<string, number> };
  repeating_failures: { strategy_type: string; cause: string; count: number; share_of_losses: number; symbols: string[]; lesson?: string | null; proposal?: string | null }[];
  news: {
    observed_at: string | null; status: string | null; error: string | null; articles_fetched: number | null;
    admitted: { symbol: string; name: string | null; articles: number | null; avg_sentiment: number | null; expires_at: string | null; top_headline: string | null }[];
    holding: string[];
    rejected: { symbol: string; reason: string }[];
  } | null;
  news_vs_core: { news: Totals; core: Totals; news_wrong: number };
  changes: { key: string; effective: string; title: string; watch: string; before: Totals; after: Totals }[];
};

async function loadScoreboard(): Promise<LearningScoreboard> {
  const response = await authenticatedFetch(`${API_BASE_URL}/stock-paper/learning-scoreboard`, { cache: "no-store" });
  if (!response.ok) throw new Error(`Learning scoreboard unavailable (${response.status})`);
  return response.json();
}

function label(value: string) { return value.replaceAll("_", " "); }
function perTrade(value: number | null) { return value === null ? "n/a" : `${value > 0 ? "+" : ""}${(value * 100).toFixed(2)}%`; }
function et(value: string | null) { return value ? new Date(value).toLocaleString("en-US", { timeZone: "America/New_York", dateStyle: "medium", timeStyle: "short" }) : "n/a"; }
function summary(t: Totals) { return t.trades ? `${t.trades} trades · win ${formatPct(t.win_rate)} · ${perTrade(t.avg_return)} per trade` : "no trades yet"; }

const CAUSE_NAMES: Record<string, string> = {
  signal_wrong: "The strategy's signal was wrong",
  missed_market_move: "Missed a market drop",
  missed_sector_move: "Missed a sector drop",
  missed_earnings: "Held through earnings",
  news_wrong: "The news call was wrong",
  noise: "Noise (under 0.5%)",
  unknown_context: "Not enough data to tell",
  edge: "Win the strategy earned",
  market_tailwind: "Win the market carried",
};

export function LearningScoreboardView({ board }: { board: LearningScoreboard }) {
  const { health, progress, causes, news, news_vs_core: split } = board;
  const failing = health.checks.filter(check => check.status === "failing").length;
  const losses = Object.entries(causes.by_cause).filter(([cause]) => !["edge", "market_tailwind"].includes(cause));
  const wins = Object.entries(causes.by_cause).filter(([cause]) => ["edge", "market_tailwind"].includes(cause));
  return <div className="space-y-8">
    <section>
      <div className="section-heading"><h2><GraduationCap size={17} className="mr-2 inline" />Is it working?</h2>
        <span className="section-meta">{health.session ? `Session ${health.session.date}${health.session.in_progress ? " (in progress)" : ""}` : "No session yet"} · updated {et(board.generated_at)} ET</span></div>
      <p className="section-meta">{failing ? `${failing} check${failing > 1 ? "s" : ""} failing.` : "Nothing failing."} "Waiting" means the event has not happened yet this session (for example no BUY signal yet), not a fault.</p>
      <div className="short-monitor-table"><table><thead><tr><th>Check</th><th>Status</th><th>What we saw</th></tr></thead>
        <tbody>{health.checks.map(check => <tr key={check.key}><th scope="row">{check.label}</th><td><StatusPill status={check.status} /></td><td>{check.detail}</td></tr>)}</tbody></table></div>
    </section>

    <section>
      <div className="section-heading"><h2>Are we improving?</h2><span className="section-meta">Closed paper trades · goal win {formatPct(progress.goal.target_win_rate)} while making money</span></div>
      <dl className="short-monitor-metrics">
        <div><dt>All closed trades</dt><dd>{progress.overall.trades}<small>win {formatPct(progress.overall.win_rate)}</small></dd></div>
        <div><dt>Average per trade</dt><dd>{perTrade(progress.overall.avg_return)}<small>net return (expectancy)</small></dd></div>
        <div><dt>Net P&amp;L</dt><dd>{formatUsd(progress.overall.net_pnl)}<small>costs known trades</small></dd></div>
        <div><dt>Explained</dt><dd>{causes.diagnosed}<small>trades with a cause, last 30 days</small></dd></div>
      </dl>
      <div className="short-monitor-table"><table><thead><tr><th>Week</th><th>Trades</th><th>Win rate</th><th>Avg per trade</th><th>Net P&amp;L</th></tr></thead>
        <tbody>{progress.weeks.map(week => <tr key={week.week}><th scope="row">{week.week}</th><td>{week.trades}</td><td>{formatPct(week.win_rate)}</td><td>{perTrade(week.avg_return)}</td><td>{formatUsd(week.net_pnl)}</td></tr>)}</tbody></table></div>
    </section>

    <section>
      <div className="section-heading"><h2>By strategy</h2><span className="section-meta">Last 10 trades against the ones before · judged after {progress.goal.min_trades_for_evidence} trades</span></div>
      {progress.strategies.length ? <div className="short-monitor-table"><table><thead><tr><th>Strategy</th><th>Trades</th><th>Win rate</th><th>Avg per trade</th><th>Last 10</th><th>Before</th><th>Evidence</th></tr></thead>
        <tbody>{progress.strategies.map(row => <tr key={row.strategy_type}><th scope="row">{label(row.strategy_type)}</th><td>{row.trades}</td><td>{formatPct(row.win_rate)}</td><td>{perTrade(row.avg_return)}</td>
          <td>{formatPct(row.recent.win_rate)}<small>{perTrade(row.recent.avg_return)}</small></td><td>{row.earlier.trades ? <>{formatPct(row.earlier.win_rate)}<small>{perTrade(row.earlier.avg_return)}</small></> : "n/a"}</td><td>{row.evidence}</td></tr>)}</tbody></table></div>
        : <p className="section-meta">No closed trades yet.</p>}
    </section>

    <section>
      <div className="section-heading"><h2>Why trades lost</h2><span className="section-meta">Last 30 days · {causes.diagnosed} explained</span></div>
      {losses.length || wins.length ? <div className="short-monitor-table"><table><thead><tr><th>Cause</th><th>Trades</th></tr></thead>
        <tbody>{[...losses, ...wins].map(([cause, count]) => <tr key={cause}><th scope="row">{CAUSE_NAMES[cause] ?? label(cause)}</th><td>{count}</td></tr>)}
          {Object.entries(causes.contributing).map(([flag, count]) => <tr key={flag}><th scope="row"><small>also: {label(flag)}</small></th><td>{count}</td></tr>)}</tbody></table></div>
        : <p className="section-meta">No explained trades yet.</p>}
    </section>

    <section>
      <div className="section-heading"><h2>Lessons that keep repeating</h2><span className="section-meta">A cause behind at least 3 losses and 40% of a strategy's recent losses</span></div>
      {board.repeating_failures.length ? <div className="short-monitor-table"><table><thead><tr><th>Strategy</th><th>Cause</th><th>Losses</th><th>Proposed experiment</th></tr></thead>
        <tbody>{board.repeating_failures.map(item => <tr key={`${item.strategy_type}-${item.cause}`}><th scope="row">{label(item.strategy_type)}</th><td>{CAUSE_NAMES[item.cause] ?? label(item.cause)}<small>{item.symbols.join(", ")}</small></td><td>{item.count}<small>{formatPct(item.share_of_losses)} of losses</small></td><td>{item.proposal ?? item.lesson ?? "n/a"}</td></tr>)}</tbody></table></div>
        : <p className="section-meta">No repeating failure yet. It takes at least 3 losses with the same cause.</p>}
    </section>

    <section>
      <div className="section-heading"><h2>Symbols from the news</h2><span className="section-meta">{news ? `Last run ${et(news.observed_at)} ET · ${news.articles_fetched ?? 0} articles read` : "No discovery run yet"}</span></div>
      {news?.error && <p role="alert" className="workspace-error">{news.error}</p>}
      <dl className="short-monitor-metrics">
        <div><dt>News-found trades</dt><dd>{split.news.trades}<small>{summary(split.news)}</small></dd></div>
        <div><dt>Core-universe trades</dt><dd>{split.core.trades}<small>{summary(split.core)}</small></dd></div>
        <div><dt>News call wrong</dt><dd>{split.news_wrong}<small>losses diagnosed news_wrong</small></dd></div>
      </dl>
      {news && news.admitted.length ? <div className="short-monitor-table"><table><thead><tr><th>Symbol</th><th>Stories</th><th>Tone</th><th>Top headline</th><th>Admitted until</th></tr></thead>
        <tbody>{news.admitted.map(row => <tr key={row.symbol}><th scope="row">{row.symbol}<small>{row.name ?? ""}</small></th><td>{row.articles ?? "n/a"}</td><td>{row.avg_sentiment === null ? "n/a" : row.avg_sentiment.toFixed(2)}</td><td>{row.top_headline ?? "n/a"}</td><td>{et(row.expires_at)}</td></tr>)}</tbody></table></div>
        : <p className="section-meta">No symbols admitted right now.</p>}
      {news && news.holding.length > 0 && <p className="section-meta">Admission ended, still held (exits only): {news.holding.join(", ")}</p>}
      {news && news.rejected.length > 0 && <p className="section-meta">Rejected last run: {news.rejected.map(row => `${row.symbol} (${row.reason})`).join("; ")}</p>}
    </section>

    <section>
      <div className="section-heading"><h2>What changed</h2><span className="section-meta">Trades opened before and after each change</span></div>
      <div className="short-monitor-table"><table><thead><tr><th>Change</th><th>Live from</th><th>Before</th><th>After</th></tr></thead>
        <tbody>{board.changes.map(change => <tr key={change.key}><th scope="row">{change.title}<small>{change.watch}</small></th><td>{change.effective}</td><td>{summary(change.before)}</td><td>{summary(change.after)}</td></tr>)}</tbody></table></div>
    </section>
  </div>;
}

export function LearningScoreboardPanel() {
  const query = useQuery({ queryKey: ["learning-scoreboard"], queryFn: loadScoreboard, refetchInterval: 120_000, retry: 1 });
  if (query.error) return <p role="alert" className="workspace-error">{getErrorMessage(query.error, "Learning scoreboard unavailable")}</p>;
  if (!query.data) return <p role="status">Loading learning scoreboard...</p>;
  return <LearningScoreboardView board={query.data} />;
}
