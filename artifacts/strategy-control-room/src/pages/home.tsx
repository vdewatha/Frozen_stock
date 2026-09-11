

import { useState } from "react";
import { Activity, BarChart3, BrainCircuit, FlaskConical, ShieldCheck } from "lucide-react";

import { AuditHistoryPanel } from "@/components/audit-history-panel";
import { AccessRoleProvider, RoleGate } from "@/components/access-control";
import { BrokerSafetyLab } from "@/components/broker-safety-lab";
import { DeploymentMonitorPanel } from "@/components/deployment-monitor-panel";
import { EconomicContextLab } from "@/components/economic-context-lab";
import { EquityChart } from "@/components/equity-chart";
import { ExperimentManagerLab } from "@/components/experiment-manager-lab";
import { GovernanceLab } from "@/components/governance-lab";
import { MarketLab } from "@/components/market-lab";
import { MemoryReplayPanel } from "@/components/memory-replay-panel";
import { MetricCard } from "@/components/metric-card";
import { ModelLab } from "@/components/model-lab";
import { ModelPerformanceLab } from "@/components/model-performance-lab";
import { NewsSentimentLab } from "@/components/news-sentiment-lab";
import { NotificationCenter } from "@/components/notification-center";
import { OpportunityRadar } from "@/components/opportunity-radar";
import { PaperTradingLab } from "@/components/paper-trading-lab";
import { PortfolioRiskPanel } from "@/components/portfolio-risk-panel";
import { PredictionScanner } from "@/components/prediction-scanner";
import { ReactivationReviewPanel } from "@/components/reactivation-review-panel";
import { ResearchRunsPanel } from "@/components/research-runs-panel";
import { ReadinessChecklist } from "@/components/readiness-checklist";
import { RegimeMonitorLab } from "@/components/regime-monitor-lab";
import { RiskSettingsPanel } from "@/components/risk-settings-panel";
import { SafetyControlBar } from "@/components/safety-control-bar";
import { StatusPill } from "@/components/status-pill";
import { StrategyImprovementQueuePanel } from "@/components/strategy-improvement-queue";
import { TradeScorecard } from "@/components/trade-scorecard";
import { getAuthSession, getDashboard, getErrorMessage, setAccessToken, type AccessRole, type DashboardSnapshot } from "@/lib/api";

const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

export default function Login() {
  const [token, setToken] = useState("");
  const [dashboard, setDashboard] = useState<DashboardSnapshot | null>(null);
  const [role, setRole] = useState<AccessRole | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  if (dashboard && role) return <AccessRoleProvider role={role}><Home dashboard={dashboard} role={role} onSignOut={() => { setAccessToken(""); setDashboard(null); setRole(null); setToken(""); }} /></AccessRoleProvider>;
  return <main className="mx-auto max-w-lg p-8"><h1>Trading research sign in</h1>
    <p>Enter your API access key. It is held only in this tab’s memory and cleared when you sign out or reload.</p>
    <form onSubmit={async event => { event.preventDefault(); setBusy(true); setError(""); setAccessToken(token.trim());
      try { const [nextDashboard, session] = await Promise.all([getDashboard(), getAuthSession()]); setDashboard(nextDashboard); setRole(session.role); setToken(""); } catch (failure) { setAccessToken(""); setError(getErrorMessage(failure, "Sign in failed")); }
      finally { setBusy(false); }
    }}><label>Access key<input className="m-4 border p-2" type="password" autoComplete="off" value={token} onChange={event => setToken(event.target.value)} /></label>
    <button disabled={busy || !token.trim()} type="submit">{busy ? "Connecting…" : "Sign in"}</button></form>
    {error && <p role="alert">{error}</p>}</main>;
}

function Home({ dashboard, role, onSignOut }: { dashboard: DashboardSnapshot; role: AccessRole; onSignOut: () => void }) {

  return (
    <main className="min-h-screen">
      <header className="border-b border-line bg-white">
        <div className="mx-auto flex max-w-7xl flex-col gap-4 px-4 py-4 md:flex-row md:items-center md:justify-between">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold text-mint">
              <ShieldCheck size={18} />
              Paper-only research system
            </div>
            <h1 className="mt-1 text-2xl font-semibold text-ink">Strategy Control Room</h1>
          </div>
          <div className="grid gap-2 md:justify-items-end">
            <div className="flex items-center gap-2">
              <span className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-1 text-sm font-semibold capitalize text-mint">{role}</span>
              <button className="focus-ring rounded-md border border-line px-3 py-1 text-sm font-medium" onClick={onSignOut}>Sign out</button>
            </div>
            <SafetyControlBar />
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-7xl gap-4 px-4 py-4">
        <section className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard label="Paper Account" value={dashboard.paper_account_value === null ? "Unavailable" : currency.format(dashboard.paper_account_value)} delta={dashboard.risk_state} />
          <MetricCard label="Daily P/L" value={dashboard.daily_pl === null ? "Unavailable" : currency.format(dashboard.daily_pl)} delta={dashboard.performance_note} />
          <MetricCard label="Total P/L" value={dashboard.total_pl === null ? "Unavailable" : currency.format(dashboard.total_pl)} delta={dashboard.performance_note} />
          <MetricCard label="Open Trades" value={String(dashboard.open_paper_trades)} delta={`${dashboard.active_strategies} active strategies`} />
        </section>

        <section className="grid gap-4 lg:grid-cols-[1.7fr_1fr]">
          <div className="rounded-md border border-line bg-white p-4">
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <BarChart3 size={19} className="text-mint" />
                <h2 className="text-base font-semibold">Paper Equity Curve</h2>
              </div>
              <span className="text-xs text-slate-500">Generated {dashboard.generated_at}</span>
            </div>
            <EquityChart data={dashboard.equity_curve} />
          </div>

          <RiskSettingsPanel initialRule={dashboard.risk_rules[0] ?? null} />
        </section>

        <ReadinessChecklist />

        <DeploymentMonitorPanel />

        <RoleGate requires="researcher"><PredictionScanner /></RoleGate>

        <RoleGate requires="researcher"><OpportunityRadar /></RoleGate>

        <RoleGate requires="researcher"><TradeScorecard /></RoleGate>

        <RoleGate requires="researcher"><MemoryReplayPanel /></RoleGate>

        <NotificationCenter />

        <MarketLab />

        <RoleGate requires="researcher"><ModelLab /></RoleGate>

        <ResearchRunsPanel />

        <ModelPerformanceLab />

        <NewsSentimentLab />

        <EconomicContextLab />

        <RegimeMonitorLab />

        <BrokerSafetyLab />

        <RoleGate requires="researcher"><PortfolioRiskPanel /></RoleGate>

        <PaperTradingLab />

        <ExperimentManagerLab />

        <RoleGate requires="researcher"><GovernanceLab /></RoleGate>

        <RoleGate requires="researcher"><StrategyImprovementQueuePanel /></RoleGate>

        <RoleGate requires="researcher"><ReactivationReviewPanel /></RoleGate>

        <AuditHistoryPanel />

        <section className="grid gap-4 xl:grid-cols-[1.4fr_1fr]">
          <div className="rounded-md border border-line bg-white">
            <div className="flex items-center gap-2 border-b border-line p-4">
              <Activity size={19} className="text-mint" />
              <h2 className="text-base font-semibold">Strategy Library</h2>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] border-collapse text-sm">
                <thead className="bg-panel text-left text-xs uppercase text-slate-500">
                  <tr>
                    <th className="px-4 py-3">Strategy</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3 text-right">Score</th>
                    <th className="px-4 py-3 text-right">Win Rate</th>
                    <th className="px-4 py-3 text-right">Drawdown</th>
                    <th className="px-4 py-3 text-right">Profit Factor</th>
                  </tr>
                </thead>
                <tbody>
                  {dashboard.strategies.map((strategy) => (
                    <tr className="border-t border-line" key={strategy.id}>
                      <td className="px-4 py-3 font-medium">{strategy.name}</td>
                      <td className="px-4 py-3"><StatusPill status={strategy.status} /></td>
                      <td className="px-4 py-3 text-right">{strategy.score?.toFixed(2) ?? "Unavailable"}</td>
                      <td className="px-4 py-3 text-right">{strategy.win_rate === null ? "Unavailable" : percent.format(strategy.win_rate)}</td>
                      <td className="px-4 py-3 text-right">{strategy.drawdown === null ? "Unavailable" : percent.format(strategy.drawdown)}</td>
                      <td className="px-4 py-3 text-right">{strategy.profit_factor?.toFixed(2) ?? "Unavailable"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="rounded-md border border-line bg-white">
            <div className="flex items-center gap-2 border-b border-line p-4">
              <FlaskConical size={19} className="text-mint" />
              <h2 className="text-base font-semibold">Learning Lab</h2>
            </div>
            <div className="divide-y divide-line">
              {dashboard.recent_trades.length === 0 && <p className="p-4 text-sm text-slate-500">No recorded paper trades.</p>}
              {dashboard.experiments.slice(0, 3).map((experiment) => (
                <div className="p-4" key={experiment.experiment_name}>
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="text-sm font-semibold">{experiment.experiment_name}</h3>
                    <StatusPill status={experiment.decision} />
                  </div>
                  <p className="mt-2 text-sm leading-5 text-slate-600">{experiment.hypothesis}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="grid gap-4 lg:grid-cols-2">
          <div className="rounded-md border border-line bg-white">
            <div className="flex items-center gap-2 border-b border-line p-4">
              <BrainCircuit size={19} className="text-mint" />
              <h2 className="text-base font-semibold">AI Review Boundary</h2>
            </div>
            <div className="grid gap-3 p-4 text-sm text-slate-700">
              <div className="rounded-md border border-line bg-panel p-3">Allowed: summarize news, explain performance, suggest controlled experiments.</div>
              <div className="rounded-md border border-line bg-panel p-3">Blocked: direct order placement, bypassing risk rules, changing strategy code without tests.</div>
            </div>
          </div>

          <div className="rounded-md border border-line bg-white">
            <div className="flex items-center gap-2 border-b border-line p-4">
              <Activity size={19} className="text-mint" />
              <h2 className="text-base font-semibold">Recent Paper Trades</h2>
            </div>
            <div className="divide-y divide-line">
              {dashboard.recent_trades.map((trade, index) => (
                <div className="grid grid-cols-[72px_1fr_auto] gap-3 p-4 text-sm" key={trade.id ?? `${trade.symbol}-${index}`}>
                  <div>
                    <div className="font-semibold">{trade.symbol}</div>
                    <div className="text-slate-500">{trade.side}</div>
                  </div>
                  <div>
                    <div className="font-medium">{trade.reason}</div>
                    <div className="text-slate-500">Confidence {trade.confidence === null ? "unavailable" : percent.format(trade.confidence)}</div>
                  </div>
                  <div className={trade.profit_loss === null ? "text-slate-500" : trade.profit_loss >= 0 ? "font-semibold text-mint" : "font-semibold text-coral"}>
                    {trade.profit_loss === null ? "Unavailable" : currency.format(trade.profit_loss)}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}
