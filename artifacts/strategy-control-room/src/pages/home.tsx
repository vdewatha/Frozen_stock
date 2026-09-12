import { useState } from "react";
import { Activity, AlertTriangle, BarChart3, BrainCircuit, FlaskConical, ShieldCheck } from "lucide-react";

import { AuditHistoryPanel } from "@/components/audit-history-panel";
import { AccessRoleProvider, RoleGate } from "@/components/access-control";
import { BrokerSafetyLab } from "@/components/broker-safety-lab";
import { DeploymentMonitorPanel } from "@/components/deployment-monitor-panel";
import { EconomicContextLab } from "@/components/economic-context-lab";
import { EquityChart } from "@/components/equity-chart";
import { ExperimentManagerLab } from "@/components/experiment-manager-lab";
import { MarketLab } from "@/components/market-lab";
import { MemoryReplayPanel } from "@/components/memory-replay-panel";
import { MetricCard } from "@/components/metric-card";
import { ModelLab } from "@/components/model-lab";
import { ModelPerformanceLab } from "@/components/model-performance-lab";
import { NewsSentimentLab } from "@/components/news-sentiment-lab";
import { NotificationCenter } from "@/components/notification-center";
import { OpportunityRadar } from "@/components/opportunity-radar";
import { ForwardPaperEvaluationPanel } from "@/components/forward-paper-evaluation-panel";
import { PredictionScanner } from "@/components/prediction-scanner";
import { ResearchRunsPanel } from "@/components/research-runs-panel";
import { ReadinessChecklist } from "@/components/readiness-checklist";
import { RegimeMonitorLab } from "@/components/regime-monitor-lab";
import { RiskSettingsPanel } from "@/components/risk-settings-panel";
import { SafetyControlBar } from "@/components/safety-control-bar";
import { StatusPill } from "@/components/status-pill";
import { StockPaperLedgerPanel } from "@/components/stock-paper-ledger-panel";
import { getAuthSession, getDashboard, getErrorMessage, setAccessToken, type AccessRole, type DashboardSnapshot } from "@/lib/api";
import { StockTrainingLab } from "@/components/stock-training-lab";
import { StockMonitoringPanel } from "@/components/stock-monitoring-panel";
import { StockRecoveryPanel } from "@/components/stock-recovery-panel";
import { OperationalHardeningPanel } from "@/components/operational-hardening-panel";

const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

function LegacyEvidenceQuarantineCard({ title, endpoint, detail }: { title: string; endpoint: string; detail: string }) {
  return (
    <section className="rounded-md border border-amber-200 bg-amber-50 p-4" data-testid={`quarantine-${endpoint.replaceAll("/", "-").replace(/^-/, "")}`}>
      <div className="flex items-start gap-2 text-amber-950">
        <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-700" />
        <div>
          <h2 className="font-semibold">{title} unavailable</h2>
          <p className="mt-1 text-sm leading-5">{detail}</p>
          <p className="mt-2 text-xs font-semibold uppercase tracking-wide text-amber-800">Backend response: HTTP 409 — legacy evidence quarantine</p>
          <p className="mt-1 text-xs text-amber-800">Endpoint <code>{endpoint}</code> is intentionally not retried and no zero values are shown.</p>
        </div>
      </div>
    </section>
  );
}

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
          <MetricCard label="Reconciled Alpaca Paper Account" value={dashboard.paper_account_value === null ? "Unavailable" : currency.format(dashboard.paper_account_value)} delta={dashboard.risk_state} />
          <MetricCard label="Broker Daily P/L" value={dashboard.daily_pl === null ? "Unavailable" : currency.format(dashboard.daily_pl)} delta={dashboard.daily_pl === null ? "Intentionally withheld until costs and flows are verified" : dashboard.performance_note} />
          <MetricCard label="Broker Total P/L" value={dashboard.total_pl === null ? "Unavailable" : currency.format(dashboard.total_pl)} delta={dashboard.total_pl === null ? "Intentionally withheld until costs and flows are verified" : dashboard.performance_note} />
          <MetricCard label="Alpaca Paper Positions" value={dashboard.paper_account_value === null ? "Unavailable" : String(dashboard.open_paper_trades)} delta={dashboard.paper_account_value === null ? "Unavailable until broker reconciliation" : "broker-reported current positions"} />
        </section>

        <StockPaperLedgerPanel />

        <section className="grid gap-4 lg:grid-cols-[1.7fr_1fr]">
          <div className="rounded-md border border-line bg-white p-4">
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <BarChart3 size={19} className="text-mint" />
                <h2 className="text-base font-semibold">Reconciled Alpaca Paper Equity Curve</h2>
              </div>
              <span className="text-xs text-slate-500">Generated {dashboard.generated_at}</span>
            </div>
            <EquityChart data={dashboard.equity_curve} />
          </div>

          <RiskSettingsPanel initialRule={dashboard.risk_rules[0] ?? null} />
        </section>

        <ReadinessChecklist />

        <StockMonitoringPanel />

        <StockRecoveryPanel />

        <OperationalHardeningPanel />

        <DeploymentMonitorPanel />

        <RoleGate requires="researcher"><PredictionScanner /></RoleGate>

        <RoleGate requires="researcher"><OpportunityRadar /></RoleGate>

        <RoleGate requires="researcher">
          <LegacyEvidenceQuarantineCard
            title="Predictive trade scorecard"
            endpoint="/trade-scorecard"
            detail="Legacy simulator trade rows and StrategyMemory cannot be used as stock-paper performance evidence until a broker-complete accounting integration replaces them."
          />
        </RoleGate>

        <RoleGate requires="researcher"><MemoryReplayPanel /></RoleGate>

        <ForwardPaperEvaluationPanel />

        <NotificationCenter />

        <MarketLab />

        <RoleGate requires="researcher"><ModelLab /></RoleGate>

        <ResearchRunsPanel />

        <StockTrainingLab />

        <ModelPerformanceLab />

        <NewsSentimentLab />

        <EconomicContextLab />

        <RegimeMonitorLab />

        <BrokerSafetyLab />

        <RoleGate requires="researcher">
          <LegacyEvidenceQuarantineCard
            title="Portfolio risk and allocation"
            endpoint="/portfolio/risk"
            detail="Legacy simulator risk, allocation, and review actions are blocked. Use the Alpaca Paper Ledger for broker-reported positions and the signal-gated order path."
          />
        </RoleGate>

        <ExperimentManagerLab />

        <RoleGate requires="researcher">
          <LegacyEvidenceQuarantineCard
            title="Strategy governance"
            endpoint="/strategies/evaluate"
            detail="Governance scoring from legacy StrategyMemory is quarantined and cannot establish qualifying strategy evidence."
          />
        </RoleGate>

        <RoleGate requires="researcher">
          <LegacyEvidenceQuarantineCard
            title="Strategy improvement queue"
            endpoint="/strategies/improvement-queue"
            detail="The improvement queue depends on quarantined simulator evidence and is unavailable rather than rendered as an empty queue."
          />
        </RoleGate>

        <RoleGate requires="researcher">
          <LegacyEvidenceQuarantineCard
            title="Strategy reactivation"
            endpoint="/strategies/reactivation-queue"
            detail="Reactivation decisions cannot use quarantined legacy risk or scorecard evidence."
          />
        </RoleGate>

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
              {dashboard.recent_trades.length === 0 && <p className="p-4 text-sm text-slate-500">Legacy simulator trade rows are quarantined; no qualifying broker P/L is shown here.</p>}
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
              <h2 className="text-base font-semibold">Legacy Local Simulator Trades</h2>
            </div>
            <div className="border-b border-slate-200 bg-slate-50 px-4 py-2 text-xs font-semibold uppercase text-slate-500">Nonqualifying evidence — not Alpaca paper fills</div>
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
