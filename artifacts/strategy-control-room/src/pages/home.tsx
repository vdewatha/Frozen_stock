import { useEffect, useState, type ReactNode } from "react";
import { useAuth } from "@clerk/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "wouter";
import { Activity, ArrowRight, Bell, BookOpen, ChartNoAxesCombined, ChevronRight, FlaskConical, Gauge, LayoutDashboard, Menu, RefreshCw, Settings2, ShieldCheck, TrendingUp, Wallet, X } from "lucide-react";
import { AccessRoleProvider, RoleGate } from "@/components/access-control";
import { AuditHistoryPanel } from "@/components/audit-history-panel";
import { BrokerSafetyLab } from "@/components/broker-safety-lab";
import { PaperVenueGovernancePanel } from "@/components/paper-venue-governance-panel";
import { DeploymentMonitorPanel } from "@/components/deployment-monitor-panel";
import { EconomicContextLab } from "@/components/economic-context-lab";
import { EquityChart } from "@/components/equity-chart";
import { ExperimentManagerLab } from "@/components/experiment-manager-lab";
import { MarketLab } from "@/components/market-lab";
import { IexResearchPanel } from "@/components/iex-research-panel";
import { DelayedSipPanel } from "@/components/delayed-sip-panel";
import { MemoryReplayPanel } from "@/components/memory-replay-panel";
import { ModelLab } from "@/components/model-lab";
import { ModelPerformanceLab } from "@/components/model-performance-lab";
import { NewsSentimentLab } from "@/components/news-sentiment-lab";
import { NotificationCenter } from "@/components/notification-center";
import { OpportunityRadar } from "@/components/opportunity-radar";
import { ForwardPaperEvaluationPanel } from "@/components/forward-paper-evaluation-panel";
import { PredictionScanner } from "@/components/prediction-scanner";
import { ResearchControlRoom } from "@/components/research-control-room";
import { ReadinessChecklist } from "@/components/readiness-checklist";
import { RegimeMonitorLab } from "@/components/regime-monitor-lab";
import { RiskSettingsPanel } from "@/components/risk-settings-panel";
import { StatusPill } from "@/components/status-pill";
import { StockPaperLedgerPanel } from "@/components/stock-paper-ledger-panel";
import { StockTrainingLab } from "@/components/stock-training-lab";
import { StockMonitoringPanel } from "@/components/stock-monitoring-panel";
import { StockRecoveryPanel } from "@/components/stock-recovery-panel";
import { StockLearningCyclePanel } from "@/components/stock-learning-cycle-panel";
import { OperationalHardeningPanel } from "@/components/operational-hardening-panel";
import { LiveOperationsPanel } from "@/components/live-operations-panel";
import { LivePilotPanel } from "@/components/live-pilot-panel";
import { getAuthConfig, getAuthSession, getDashboard, getErrorMessage, setAccessToken, setAuthMode, type AuthSession, type DashboardSnapshot } from "@/lib/api";
import { workspacePages, resolveWorkspacePage } from "@/lib/workspace-navigation";

const icons = [LayoutDashboard, ChartNoAxesCombined, Wallet, FlaskConical, BookOpen, ShieldCheck, Activity, Settings2];
const localPaperAuth = import.meta.env.VITE_LOCAL_PAPER_AUTH !== "false";
const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const money = (value: number | null) => value === null ? "Unavailable" : currency.format(value);

export default function DashboardHome() {
  return localPaperAuth ? <Workspace /> : <IdentityWorkspace />;
}

function IdentityWorkspace() {
  const { isLoaded, isSignedIn } = useAuth();
  if (!isLoaded) return <div className="workspace-loading">Opening workspace...</div>;
  if (!isSignedIn) return <main className="workspace-loading"><a href={`${import.meta.env.BASE_URL}sign-in`}>Continue with your organization</a></main>;
  return <Workspace />;
}

function Workspace() {
  const [location] = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const client = useQueryClient();
  const { data, error, isFetching, refetch } = useQuery({
    queryKey: ["workspace-session"],
    queryFn: async () => {
      const config = await getAuthConfig();
      setAuthMode(config.mode);
      const [session, dashboard] = await Promise.all([getAuthSession(), getDashboard()]);
      return { session, dashboard };
    },
    refetchInterval: 60000, retry: 1,
  });
  useEffect(() => { setMenuOpen(false); window.scrollTo(0, 0); }, [location]);
  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") setMenuOpen(false); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [menuOpen]);
  const resolved = resolveWorkspacePage(location);
  const page = resolved?.page;
  const title = page?.label ?? "Page not found";
  return <div className="workspace control-room">
    {menuOpen && <button className="nav-backdrop" aria-label="Close navigation" onClick={() => setMenuOpen(false)} />}
    <aside className={`workspace-sidebar ${menuOpen ? "is-open" : ""}`} aria-label="Workspace navigation">
      <Link href="/" className="workspace-brand"><span className="brand-icon"><ChartNoAxesCombined size={23} /></span><span>Frozen<span className="brand-light"> Stock</span><small>RESEARCH WORKSPACE</small></span></Link>
      <button className="mobile-nav-close icon-button" aria-label="Close menu" onClick={() => setMenuOpen(false)}><X size={20} /></button>
      <div className="nav-caption">WORKSPACE</div>
      <nav aria-label="Main navigation">{workspacePages.map((item, index) => {
        const Icon = icons[index];
        return <Link key={item.id} href={item.path} onClick={() => setMenuOpen(false)} className={`nav-link ${page?.id === item.id ? "active" : ""}`} aria-current={page?.id === item.id ? "page" : undefined}><Icon size={18} /><span>{item.label}</span>{page?.id === item.id && <ChevronRight size={15} className="nav-chevron" />}</Link>;
      })}</nav>
      <div className="sidebar-footer"><span className="environment-dot" />Paper environment<small>Live trading disabled</small><Link href="/system/access" className="access-link">{data?.session.role ?? "Connecting"} access <ArrowRight size={14} /></Link></div>
    </aside>
    <div className="workspace-body">
      <header className="workspace-topbar"><div className="breadcrumb"><button className="mobile-menu icon-button" aria-label="Open navigation" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}><Menu size={20} /></button><span>Workspace</span><ChevronRight size={14} /><strong>{title}</strong></div><div className="topbar-actions"><span className="paper-label"><ShieldCheck size={14} />Paper only</span><Link href="/activity" className="icon-button" aria-label="Notifications" title="Notifications"><Bell size={18} /></Link></div></header>
      <main id="main-content" className="workspace-content">
        <div className="page-heading"><div><div className="eyebrow">FROZEN STOCK / WORKSPACE</div><h1>{title}</h1><p>{page?.description ?? "This page is not available."}</p></div><button className="icon-button refresh-button" aria-label="Refresh data" title="Refresh data" disabled={isFetching} onClick={() => void client.invalidateQueries()}><RefreshCw size={18} className={isFetching ? "animate-spin" : ""} /></button></div>
        {page && page.tabs.length > 0 && <nav className="page-tabs" aria-label={`${page.label} sections`}>{page.tabs.map(tab => <Link key={tab.id} href={`${page.path}/${tab.id}`} aria-current={resolved?.section === tab.id ? "page" : undefined} className={resolved?.section === tab.id ? "selected" : ""}>{tab.label}</Link>)}</nav>}
        {error && <div className="workspace-error" role="alert">{getErrorMessage(error, "Workspace unavailable")} <button onClick={() => void refetch()}>Retry</button></div>}
        {!page ? <Link className="text-link" href="/">Return to overview <ArrowRight size={16} /></Link> : !data ? !error && <div className="loading-grid" aria-label="Loading workspace">{[0, 1, 2].map(i => <div key={i} className="loading-block" />)}</div> : <AccessRoleProvider role={data.session.role}><div className="page-panels" key={`${page.id}/${resolved?.section}`}><PageContent page={page.id} section={resolved?.section ?? ""} dashboard={data.dashboard} session={data.session} /></div></AccessRoleProvider>}
        <footer className="workspace-footer"><span>Frozen Stock</span><span>{data ? `Snapshot ${new Date(data.dashboard.generated_at).toLocaleString()}` : "Connecting to local service"}</span></footer>
      </main>
    </div>
  </div>;
}

function ResearchOnly({ children }: { children: ReactNode }) { return <RoleGate requires="researcher">{children}</RoleGate>; }

function PageContent({ page, section, dashboard, session }: { page: string; section: string; dashboard: DashboardSnapshot; session: AuthSession }) {
  if (page === "overview") return <Overview dashboard={dashboard} />;
  const views: Record<string, ReactNode> = {
    "markets/iex": <IexResearchPanel />, "markets/consolidated": <DelayedSipPanel />, "markets/history": <MarketLab />, "markets/context": <><RegimeMonitorLab /><EconomicContextLab /><NewsSentimentLab /></>,
    "portfolio/ledger": <StockPaperLedgerPanel />, "portfolio/equity": <EquitySection dashboard={dashboard} />,
    "learning/cycles": <StockLearningCyclePanel />, "learning/training": <StockTrainingLab />, "learning/evaluation": <ForwardPaperEvaluationPanel />, "learning/performance": <ModelPerformanceLab />,
    "research/runs": <ResearchControlRoom />, "research/experiments": <ExperimentManagerLab />, "research/library": <StrategyLibrary dashboard={dashboard} />, "research/models": <ResearchOnly><ModelLab /></ResearchOnly>, "research/signals": <ResearchOnly><PredictionScanner /><OpportunityRadar /></ResearchOnly>, "research/memory": <ResearchOnly><MemoryReplayPanel /></ResearchOnly>,
    "risk/readiness": <ReadinessChecklist />, "risk/limits": <RiskSettingsPanel initialRule={dashboard.risk_rules[0] ?? null} />, "risk/broker": <><PaperVenueGovernancePanel /><BrokerSafetyLab /></>, "risk/recovery": <StockRecoveryPanel />,
    "activity/notifications": <NotificationCenter />, "activity/audit": <AuditHistoryPanel />,
    "system/health": <DeploymentMonitorPanel />, "system/monitoring": <StockMonitoringPanel />, "system/hardening": <OperationalHardeningPanel />, "system/live": <><LiveOperationsPanel /><LivePilotPanel /></>, "system/access": <AccessSettings session={session} />,
  };
  return views[`${page}/${section}`] ?? <p role="status">Section not found.</p>;
}

function Overview({ dashboard }: { dashboard: DashboardSnapshot }) {
  const metrics = [["Account equity", money(dashboard.paper_account_value), "Broker-reconciled balance"], ["Daily P/L", money(dashboard.daily_pl), "Verified costs and cash flows"], ["Total P/L", money(dashboard.total_pl), "Verified performance only"], ["Open positions", dashboard.paper_account_value === null ? "Unavailable" : String(dashboard.open_paper_trades), "Paper account holdings"]];
  return <>
    <div className="overview-notice"><ShieldCheck size={18} /><span><strong>Paper workspace</strong> <span className="notice-detail">{dashboard.risk_state}. Live orders are disabled.</span></span><Link href="/risk">Review readiness <ArrowRight size={15} /></Link></div>
    <div className="overview-metrics">{metrics.map(([label, value, note]) => <div className="overview-metric" key={label}><span>{label}</span><strong className={value === "Unavailable" ? "metric-unavailable" : ""}>{value}</strong><small>{note}</small></div>)}</div>
    <div className="overview-split"><EquitySection dashboard={dashboard} /><section className="research-summary"><div className="section-heading"><h2>Research snapshot</h2><FlaskConical size={18} /></div><dl><div><dt>Active strategies</dt><dd>{dashboard.active_strategies}</dd></div><div><dt>Paused strategies</dt><dd>{dashboard.paused_strategies}</dd></div><div><dt>Recent experiments</dt><dd>{dashboard.experiments.length}</dd></div></dl><Link href="/learning" className="text-link">Learning cycles <ArrowRight size={16} /></Link></section></div>
    <div className="overview-split bottom-split"><StrategyLibrary dashboard={dashboard} compact /><section className="workspace-shortcuts"><div className="section-heading"><h2>Explore your data</h2></div>{[{path:"/markets", label:"Market data", detail:"IEX, delayed SIP & price history", icon:ChartNoAxesCombined}, {path:"/research",label:"Research",detail:"Models, experiments & evaluations",icon:FlaskConical}, {path:"/system",label:"System health",detail:"Workers, collectors & recovery",icon:Gauge}].map(({path,label,detail,icon:Icon}) => <Link key={path} href={path} className="shortcut"><Icon size={20}/><span><strong>{label}</strong><small>{detail}</small></span><ArrowRight size={16}/></Link>)}</section></div>
  </>;
}

function EquitySection({ dashboard }: { dashboard: DashboardSnapshot }) {
  return <section className="equity-section"><div className="section-heading"><h2>Account equity</h2><span className="section-meta">USD · Paper account</span></div>{dashboard.equity_curve.length ? <EquityChart data={dashboard.equity_curve} /> : <div className="equity-empty"><TrendingUp size={34} strokeWidth={1.4} /><h3>No verified equity history yet</h3><p>Reconciled account snapshots will appear here.</p><Link href="/portfolio" className="text-link">View account ledger <ArrowRight size={15} /></Link></div>}</section>;
}

function StrategyLibrary({ dashboard, compact = false }: { dashboard: DashboardSnapshot; compact?: boolean }) {
  return <section className="strategy-section"><div className="section-heading"><h2>Strategy library</h2>{compact && <Link href="/research/library" className="text-link">View all <ArrowRight size={14}/></Link>}</div>{dashboard.strategies.length ? <div className="table-scroll"><table className="workspace-table"><thead><tr><th>Strategy</th><th>Status</th>{!compact && <><th>Score</th><th>Win rate</th><th>Drawdown</th></>}</tr></thead><tbody>{dashboard.strategies.slice(0, compact ? 5 : undefined).map(strategy => <tr key={strategy.id}><td>{strategy.name}</td><td><StatusPill status={strategy.status}/></td>{!compact && <><td>{strategy.score?.toFixed(2) ?? "Unavailable"}</td><td>{strategy.win_rate === null ? "Unavailable" : `${(strategy.win_rate * 100).toFixed(1)}%`}</td><td>{strategy.drawdown === null ? "Unavailable" : `${(strategy.drawdown * 100).toFixed(1)}%`}</td></>}</tr>)}</tbody></table></div> : <p className="empty-inline">No strategies available.</p>}</section>;
}

function AccessSettings({ session }: { session: AuthSession }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const client = useQueryClient();
  const changeAccess = async (token: string) => {
    setBusy(true); setError(""); setAccessToken(token);
    try { await getAuthSession(); setKey(""); await client.invalidateQueries(); }
    catch (failure) { setAccessToken(""); setError(getErrorMessage(failure, "Access key rejected")); await client.invalidateQueries(); }
    finally { setBusy(false); }
  };
  return <section className="access-settings"><div className="section-heading"><h2>Workspace access</h2><ShieldCheck size={20}/></div><dl className="access-details"><div><dt>Current role</dt><dd>{session.role}</dd></div><div><dt>Environment</dt><dd>Paper only</dd></div><div><dt>Live order authority</dt><dd>Disabled</dd></div></dl>{localPaperAuth && <form onSubmit={event => { event.preventDefault(); void changeAccess(key.trim()); }}><h3>Additional permissions</h3><p>Research and operator actions require a local role key. Access lasts until this tab reloads.</p><label htmlFor="role-key">Role access key</label><div className="access-form-row"><input id="role-key" type="password" autoComplete="off" value={key} onChange={event => setKey(event.target.value)}/><button className="primary-action" type="submit" disabled={busy || !key.trim()}>Apply key</button></div>{session.role !== "viewer" && <button className="text-link" type="button" disabled={busy} onClick={() => void changeAccess("")}>Return to read-only access</button>}{error && <p role="alert" className="text-red-700">{error}</p>}</form>}</section>;
}
