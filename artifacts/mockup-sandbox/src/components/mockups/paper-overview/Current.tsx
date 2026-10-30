// Extracted from the real home.tsx. Only APIs, routing and auth are stubbed.
import {useState, type ReactNode} from 'react';
import { Activity, ArrowRight, Bell, BookOpen, ChartNoAxesCombined, ChevronRight, FlaskConical, Gauge, LayoutDashboard, Menu, RefreshCw, Settings2, ShieldCheck, TrendingUp, Wallet, X } from "lucide-react";
import {StatusPill} from './_StatusPill';
import {workspacePages} from './_navigation';
import './_group.css';
const Link=({href,children,...rest}:any)=><a href={href.startsWith('/')?'#':href} {...rest}>{children}</a>;
const icons=[LayoutDashboard,ChartNoAxesCombined,Wallet,FlaskConical,BookOpen,ShieldCheck,Activity,Settings2];
const money=(v:number|null)=>v===null?'Unavailable':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:0}).format(v);
type Snapshot={paper_account_value:number|null;daily_pl:number|null;total_pl:number|null;open_paper_trades:number;risk_state:string;equity_curve:any[];active_strategies:number;paused_strategies:number;experiments:any[];strategies:{id:number;name:string;status:string;score:number|null;win_rate:number|null;drawdown:number|null}[];generated_at:string};
const dashboard:Snapshot={paper_account_value:null,daily_pl:null,total_pl:null,open_paper_trades:0,risk_state:'Kill switch enabled. Paper-only. Regime: bull_trend',equity_curve:[],active_strategies:0,paused_strategies:0,experiments:[1,2,3],generated_at:'2026-10-30T16:27:33Z',strategies:['Bollinger Mean Reversion','Channel Breakout','Ensemble Strategy','IBS Mean Reversion','MACD Momentum'].map((name,id)=>({id,name,status:id<3?'research':'paper_trading_candidate',score:null,win_rate:null,drawdown:null}))};
export function Current(){const [menuOpen,setMenuOpen]=useState(false);const data={dashboard,session:{role:'viewer'}};const page=workspacePages[0];const resolved={section:''};const title='Overview';const error=null;const isFetching=false;const refetch=()=>{};const client={invalidateQueries:()=>{}};const getErrorMessage=(_:unknown,f:string)=>f;
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
        {!page ? <Link className="text-link" href="/">Return to overview <ArrowRight size={16} /></Link> : !data ? !error && <div className="loading-grid" aria-label="Loading workspace">{[0, 1, 2].map(i => <div key={i} className="loading-block" />)}</div> : <><div className="page-panels" key={`${page.id}/${resolved?.section}`}><Overview dashboard={data.dashboard} /></div></>}
        <footer className="workspace-footer"><span>Frozen Stock</span><span>{data ? `Snapshot ${new Date(data.dashboard.generated_at).toLocaleString()}` : "Connecting to local service"}</span></footer>
      </main>
    </div>
  </div>;
}

function Overview({ dashboard }: { dashboard: Snapshot }) {
  const metrics = [["Account equity", money(dashboard.paper_account_value), "Broker-reconciled balance"], ["Daily P/L", money(dashboard.daily_pl), "Verified costs and cash flows"], ["Total P/L", money(dashboard.total_pl), "Verified performance only"], ["Open positions", dashboard.paper_account_value === null ? "Unavailable" : String(dashboard.open_paper_trades), "Paper account holdings"]];
  return <>
    <div className="overview-notice"><ShieldCheck size={18} /><span><strong>Paper workspace</strong> <span className="notice-detail">{dashboard.risk_state}. Live orders are disabled.</span></span><Link href="/risk">Review readiness <ArrowRight size={15} /></Link></div>
    <div className="overview-metrics">{metrics.map(([label, value, note]) => <div className="overview-metric" key={label}><span>{label}</span><strong className={value === "Unavailable" ? "metric-unavailable" : ""}>{value}</strong><small>{note}</small></div>)}</div>
    <div className="overview-split"><EquitySection dashboard={dashboard} /><section className="research-summary"><div className="section-heading"><h2>Research snapshot</h2><FlaskConical size={18} /></div><dl><div><dt>Active strategies</dt><dd>{dashboard.active_strategies}</dd></div><div><dt>Paused strategies</dt><dd>{dashboard.paused_strategies}</dd></div><div><dt>Recent experiments</dt><dd>{dashboard.experiments.length}</dd></div></dl><Link href="/learning" className="text-link">Learning cycles <ArrowRight size={16} /></Link></section></div>
    <div className="overview-split bottom-split"><StrategyLibrary dashboard={dashboard} compact /><section className="workspace-shortcuts"><div className="section-heading"><h2>Explore your data</h2></div>{[{path:"/markets", label:"Market data", detail:"IEX, delayed SIP & price history", icon:ChartNoAxesCombined}, {path:"/research",label:"Research",detail:"Models, experiments & evaluations",icon:FlaskConical}, {path:"/system",label:"System health",detail:"Workers, collectors & recovery",icon:Gauge}].map(({path,label,detail,icon:Icon}) => <Link key={path} href={path} className="shortcut"><Icon size={20}/><span><strong>{label}</strong><small>{detail}</small></span><ArrowRight size={16}/></Link>)}</section></div>
  </>;
}

function EquitySection({ dashboard }: { dashboard: Snapshot }) {
  return <section className="equity-section"><div className="section-heading"><h2>Account equity</h2><span className="section-meta">USD · Paper account</span></div>{dashboard.equity_curve.length ? <div /> : <div className="equity-empty"><TrendingUp size={34} strokeWidth={1.4} /><h3>No verified equity history yet</h3><p>Reconciled account snapshots will appear here.</p><Link href="/portfolio" className="text-link">View account ledger <ArrowRight size={15} /></Link></div>}</section>;
}

function StrategyLibrary({ dashboard, compact = false }: { dashboard: Snapshot; compact?: boolean }) {
  return <section className="strategy-section"><div className="section-heading"><h2>Strategy library</h2>{compact && <Link href="/research/library" className="text-link">View all <ArrowRight size={14}/></Link>}</div>{dashboard.strategies.length ? <div className="table-scroll"><table className="workspace-table"><thead><tr><th>Strategy</th><th>Status</th>{!compact && <><th>Score</th><th>Win rate</th><th>Drawdown</th></>}</tr></thead><tbody>{dashboard.strategies.slice(0, compact ? 5 : undefined).map(strategy => <tr key={strategy.id}><td>{strategy.name}</td><td><StatusPill status={strategy.status}/></td>{!compact && <><td>{strategy.score?.toFixed(2) ?? "Unavailable"}</td><td>{strategy.win_rate === null ? "Unavailable" : `${(strategy.win_rate * 100).toFixed(1)}%`}</td><td>{strategy.drawdown === null ? "Unavailable" : `${(strategy.drawdown * 100).toFixed(1)}%`}</td></>}</tr>)}</tbody></table></div> : <p className="empty-inline">No strategies available.</p>}</section>;
}

