import {useMemo, useState, type MouseEvent} from 'react';
import {Activity, ArrowDownRight, ArrowRight, ArrowUpRight, Bell, BookOpen, ChartNoAxesCombined, ChevronRight, CircleHelp, Clock3, FlaskConical, LayoutDashboard, Menu, ShieldCheck, Wallet, X} from 'lucide-react';
import {workspacePages} from './_navigation';
import './_group.css';
import './_overview.css';

type Position = {symbol:string; company:string; strategy:string|null; qty:number; entry:number; entryDate:string; price:number; pnl:number; pct:number; held:number; horizon:number|null; intent:string; stop:string; signal:string};
const originalPositions:Position[]=[
  {symbol:'NVDA',company:'NVIDIA Corporation',strategy:'Channel Breakout',qty:32,entry:118.42,entryDate:'Oct 21, 2026',price:124.86,pnl:206.08,pct:5.43,held:7,horizon:20,intent:'Horizon exit · day 20',stop:'Stop at $113.68',signal:'Long breakout'},
  {symbol:'MSFT',company:'Microsoft Corporation',strategy:'Bollinger Mean Reversion',qty:18,entry:514.20,entryDate:'Oct 27, 2026',price:508.72,pnl:-98.64,pct:-1.07,held:3,horizon:8,intent:'Mean reversion · day 8',stop:'Stop at $493.63',signal:'Lower band reclaim'},
  {symbol:'AMD',company:'Advanced Micro Devices',strategy:null,qty:45,entry:186.35,entryDate:'Oct 22, 2026',price:191.08,pnl:212.85,pct:2.54,held:6,horizon:null,intent:'Exit intent unavailable',stop:'Risk metadata unavailable',signal:'Attribution pending'},
  {symbol:'COST',company:'Costco Wholesale',strategy:'IBS Mean Reversion',qty:8,entry:982.10,entryDate:'Oct 29, 2026',price:976.44,pnl:-45.28,pct:-0.58,held:1,horizon:5,intent:'Horizon exit · day 5',stop:'Stop at $952.64',signal:'IBS threshold'},
];
const strategies=[
  {name:'Channel Breakout',state:'Active',tone:'active',open:1,limit:3,signal:'BUY · NVDA',when:'09:42 ET',withheld:'No',next:'10:35 ET'},
  {name:'Bollinger Mean Reversion',state:'Active',tone:'active',open:1,limit:2,signal:'HOLD · MSFT',when:'09:38 ET',withheld:'No',next:'10:35 ET'},
  {name:'IBS Mean Reversion',state:'Candidate',tone:'candidate',open:1,limit:2,signal:'BUY · COST',when:'09:36 ET',withheld:'No',next:'11:00 ET'},
  {name:'MACD Momentum',state:'Paused',tone:'paused',open:0,limit:2,signal:'BUY · AAPL',when:'09:32 ET',withheld:'Yes · entry window',next:'13:00 ET'},
];
const money=(n:number)=>`${n<0?'−':''}$${Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}`;
const noNav=(event:MouseEvent<HTMLAnchorElement>)=>event.preventDefault();

export function Overview(){
  const [menuOpen,setMenuOpen]=useState(false);
  const [expanded,setExpanded]=useState<string|null>(null);
  const [filter,setFilter]=useState('All strategies');
  const [scenario,setScenario]=useState('populated');
  const positions=useMemo(()=>{
    if(scenario==='empty') return [];
    if(scenario==='stale') return originalPositions.slice(0,2);
    return originalPositions;
  },[scenario]);
  const accountEquity=50842.16;
  const exposure=positions.reduce((sum,p)=>sum+p.qty*p.price,0);
  const cash=accountEquity-exposure;
  const visible=positions.filter(p=>filter==='All strategies'||(filter==='Unattributed'?p.strategy===null:p.strategy===filter));
  const Icons=[LayoutDashboard,ChartNoAxesCombined,Wallet,FlaskConical,BookOpen,ShieldCheck,Activity];
  return <div className="workspace control-room ov-shell">
    {menuOpen&&<button className="nav-backdrop" aria-label="Close navigation" onClick={()=>setMenuOpen(false)}/>}
    <aside className={`workspace-sidebar ${menuOpen?'is-open':''}`} aria-label="Workspace navigation">
      <a href="#" onClick={noNav} className="workspace-brand"><span className="brand-icon"><ChartNoAxesCombined size={23}/></span><span>Frozen<span className="brand-light"> Stock</span><small>RESEARCH WORKSPACE</small></span></a>
      <button className="mobile-nav-close icon-button" aria-label="Close menu" onClick={()=>setMenuOpen(false)}><X size={20}/></button>
      <div className="nav-caption">WORKSPACE</div><nav aria-label="Main navigation">{workspacePages.slice(0,7).map((item,index)=>{const Icon=Icons[index]||CircleHelp;return <a key={item.id} href="#" onClick={noNav} className={`nav-link ${index===0?'active':''}`} aria-current={index===0?'page':undefined}><Icon size={18}/><span>{item.label}</span>{index===0&&<ChevronRight size={15} className="nav-chevron"/>}</a>})}</nav>
      <div className="sidebar-footer"><span className="environment-dot"/>Paper environment<small>Live trading disabled</small><a href="#" onClick={noNav} className="access-link">Viewer access <ArrowRight size={14}/></a></div>
    </aside>
    <div className="workspace-body">
      <header className="workspace-topbar"><div className="breadcrumb"><button className="mobile-menu icon-button" aria-label="Open navigation" aria-expanded={menuOpen} onClick={()=>setMenuOpen(true)}><Menu size={20}/></button><span>Workspace</span><ChevronRight size={14}/><strong>Overview</strong></div><div className="topbar-actions"><span className="paper-label"><ShieldCheck size={14}/>Paper only</span><span className="ov-authority">READ ONLY</span><button className="icon-button" aria-label="Notifications preview only" title="Notifications are not interactive in this preview"><Bell size={18}/></button></div></header>
      <main className="workspace-content ov-content">
        <div className="ov-preview-label"><span className="ov-preview-dot"/>Design preview · illustrative data</div>
        <div className="ov-heading"><div><div className="eyebrow">FROZEN STOCK / PAPER BOOK</div><h1>Overview</h1><p>Holdings, ownership and today’s operating state in one view.</p></div><label className="ov-scenario">Preview scenario<select value={scenario} onChange={e=>{setScenario(e.target.value);setExpanded(null)}} aria-label="Preview scenario"><option value="populated">Populated book</option><option value="empty">No open positions</option><option value="stale">Stale snapshot</option></select></label></div>
        <section className={`ov-status-strip ${scenario==='stale'?'is-stale':''}`} aria-label="Paper operating status">
          <div className="ov-state-cell"><span className="ov-state-icon session"><Clock3 size={16}/></span><div><small>MARKET SESSION</small><strong>{scenario==='stale'?'Snapshot stale · 18m':'Open · 5h 30m to close'}</strong><span>NYSE · Friday, Oct 30 · 09:35–15:45 ET</span></div></div>
          <div className="ov-state-cell"><span className="ov-state-icon bot"><Activity size={16}/></span><div><small>PAPER BOT</small><strong>Mandate active</strong><span>Kill switch off · no live authority</span></div></div>
          <div className="ov-state-cell"><span className="ov-state-icon bounds"><ShieldCheck size={16}/></span><div><small>APPROVED BOUNDS</small><strong>4 positions · $40k gross</strong><span>Per name ≤ $12,000 · 1.0% stop</span></div></div>
          <div className="ov-state-cell reconcile"><span className="ov-state-icon time"><Clock3 size={16}/></span><div><small>LAST RECONCILIATION</small><strong>{scenario==='stale'?'10:12 ET · stale':'10:30:14 ET'}</strong><span>Broker snapshot · paper only</span></div></div>
        </section>
        <section className="ov-positions">
          <div className="ov-section-head"><div><div className="ov-kicker">CURRENT PAPER BOOK</div><h2>Open positions <span>{positions.length}</span></h2></div><div className="ov-filter-wrap"><label htmlFor="strategy-filter">Strategy</label><select id="strategy-filter" value={filter} onChange={e=>setFilter(e.target.value)}><option>All strategies</option><option>Channel Breakout</option><option>Bollinger Mean Reversion</option><option>IBS Mean Reversion</option><option>Unattributed</option></select></div></div>
          {scenario==='stale'&&<div className="ov-stale-note"><Clock3 size={14}/>Showing last known holdings. Reconciliation is 18 minutes old; values may have changed.</div>}
          {visible.length===0?<div className="ov-empty"><div className="ov-empty-mark"><Wallet size={22}/></div><div><strong>{positions.length?'No positions match this strategy':'No open positions'}</strong><p>{positions.length?'Choose another strategy filter to see holdings.':`Broker snapshot checked ${scenario==='stale'?'10:12':'10:30'} ET · paper account.`}</p></div></div>:
          <><div className="ov-table-wrap"><table className="ov-table"><thead><tr><th>POSITION</th><th>OWNER / ENTRY</th><th className="numeric">QTY</th><th className="numeric">MARK</th><th className="numeric">UNREALIZED P/L</th><th>HORIZON</th><th>EXIT INTENT</th><th aria-label="Details"/></tr></thead><tbody>{visible.map(p=><tr key={p.symbol} className={expanded===p.symbol?'is-expanded':''}><td><button className="ov-position-button" onClick={()=>setExpanded(expanded===p.symbol?null:p.symbol)} aria-expanded={expanded===p.symbol}><span className="ov-expand">{expanded===p.symbol?'−':'+'}</span><span><strong>{p.symbol}</strong><small>{p.company}</small></span></button></td><td>{p.strategy?<><strong className="ov-owner">{p.strategy}</strong><small className="ov-sub">{p.entryDate} · {money(p.entry)} entry</small></>:<><strong className="ov-unattributed"><CircleHelp size={13}/> Unattributed</strong><small className="ov-sub">Ownership not confirmed · {p.entryDate} entry</small></>}</td><td className="numeric ov-number">{p.qty}</td><td className="numeric ov-number">{money(p.price)}</td><td className="numeric"><strong className={p.pnl>=0?'ov-positive':'ov-negative'}>{p.pnl>=0?'+':''}{money(p.pnl)}</strong><small className={`ov-sub ${p.pnl>=0?'ov-positive':'ov-negative'}`}>{p.pct>=0?'+':''}{p.pct.toFixed(2)}%</small></td><td><strong className="ov-horizon">{p.horizon===null?`${p.held} held · horizon unknown`:`${p.held} / ${p.horizon} days`}</strong>{p.horizon!==null&&<div className="ov-progress"><i style={{width:`${Math.min(100,p.held/p.horizon*100)}%`}}/></div>}</td><td><span className={`ov-intent ${p.strategy?'':'unknown'}`}>{p.intent}</span></td><td><button className="ov-row-toggle" aria-label={`${expanded===p.symbol?'Hide':'Show'} ${p.symbol} details`} onClick={()=>setExpanded(expanded===p.symbol?null:p.symbol)}><ChevronRight size={16}/></button></td></tr>)}</tbody></table></div>
          <div className="ov-mobile-positions">{visible.map(p=><article className="ov-mobile-card" key={p.symbol}><button className="ov-mobile-card-head" onClick={()=>setExpanded(expanded===p.symbol?null:p.symbol)} aria-expanded={expanded===p.symbol}><span><strong>{p.symbol}</strong><small>{p.company}</small></span><span className={p.pnl>=0?'ov-positive':'ov-negative'}>{p.pnl>=0?'+':''}{money(p.pnl)}<small>{p.pct>=0?'+':''}{p.pct.toFixed(2)}%</small></span><ChevronRight size={16}/></button><div className="ov-mobile-owner">{p.strategy||<span className="ov-unattributed"><CircleHelp size={13}/> Unattributed</span>}<span>{p.qty} shares</span></div><div className="ov-mobile-grid"><div><small>ENTRY</small><strong>{money(p.entry)}</strong><em>{p.entryDate}</em></div><div><small>MARK</small><strong>{money(p.price)}</strong><em>Current paper price</em></div><div><small>HORIZON</small><strong>{p.horizon===null?`${p.held} held · Unknown horizon`:`${p.held} / ${p.horizon} days`}</strong>{p.horizon!==null&&<div className="ov-progress"><i style={{width:`${Math.min(100,p.held/p.horizon*100)}%`}}/></div>}</div><div><small>EXIT INTENT</small><strong className="ov-card-intent">{p.intent}</strong></div></div>{expanded===p.symbol&&<div className="ov-detail"><span>Signal <b>{p.signal}</b></span><span>Risk <b>{p.stop}</b></span></div>}</article>)}</div>
          {expanded&&visible.find(p=>p.symbol===expanded)&&<div className="ov-expanded-detail"><strong>{expanded} position details</strong><span>Entry signal: {visible.find(p=>p.symbol===expanded)?.signal}</span><span>Risk plan: {visible.find(p=>p.symbol===expanded)?.stop}</span><span>Attribution: {visible.find(p=>p.symbol===expanded)?.strategy||'Unknown — fills do not confirm one owner'}</span></div>}
          </>}
          <div className="ov-table-foot"><span>Illustrative marks · USD · {scenario==='stale'?'last snapshot 10:12 ET':'snapshot 10:30 ET'}</span><span>Position values are not orders</span></div>
        </section>
        <div className="ov-lower-grid">
          <section className="ov-strategies"><div className="ov-section-head compact"><div><div className="ov-kicker">ADMITTED TO PAPER</div><h2>Strategies today <span>4</span></h2></div><span className="ov-next-eval">Next eval · 10:35 ET</span></div>
            <div className="ov-strategy-list">{strategies.map(s=>{const openCount=positions.filter(p=>p.strategy===s.name).length;return <article className="ov-strategy-row" key={s.name}><div className="ov-strategy-name"><span className={`ov-strategy-dot ${s.tone}`}/><div><strong>{s.name}</strong><small>{openCount} open / {s.limit} max</small></div><span className={`ov-status-tag ${s.tone}`}>{s.state}</span></div><div className="ov-strategy-meta"><span><small>LAST SIGNAL</small><b>{s.signal}</b><em>{s.when}</em></span><span><small>ENTRY WINDOW</small><b className={s.withheld.startsWith('Yes')?'ov-withheld':'ov-clear'}>{s.withheld==='No'?'Clear':s.withheld}</b></span><span><small>NEXT EVALUATION</small><b>{s.next}</b></span></div></article>})}</div>
            <a className="ov-research-link" href="#" onClick={noNav}><FlaskConical size={15}/>Research library <span>5 strategies · preview destination</span><ArrowRight size={14}/></a>
          </section>
          <aside className="ov-side-column">
            <section className="ov-account"><div className="ov-section-head compact"><div><div className="ov-kicker">SECONDARY VIEW</div><h2>Paper account</h2></div></div><div className="ov-account-grid"><div><small>ACCOUNT EQUITY</small><strong>{money(accountEquity)}</strong></div><div><small>CASH AVAILABLE</small><strong>{money(cash)}</strong></div><div><small>GROSS EXPOSURE</small><strong>{money(exposure)}</strong><em>{(exposure/accountEquity*100).toFixed(1)}% of equity</em></div><div><small>TODAY REALIZED P/L</small><strong className="ov-unverified">Unverified</strong><em>Ledger validation pending</em></div></div><a href="#" onClick={noNav} className="ov-ledger-link">Account ledger · preview destination <ArrowRight size={14}/></a></section>
            <section className="ov-activity"><div className="ov-section-head compact"><div><div className="ov-kicker">PAPER TAPE</div><h2>Recent activity</h2></div><span className="ov-live-dot">LATEST</span></div><div className="ov-activity-list"><div><span className="ov-activity-mark withheld"><ShieldCheck size={14}/></span><p><strong>AAPL · entry withheld</strong><small>MACD Momentum · Oct 30, 09:32 ET</small></p><b>Window closed</b></div><div><span className="ov-activity-mark exit"><ArrowDownRight size={14}/></span><p><strong>TSLA · position exited</strong><small>Channel Breakout · Oct 29, 15:48</small></p><b>Horizon exit</b></div><div><span className="ov-activity-mark buy"><ArrowUpRight size={14}/></span><p><strong>NVDA · entry filled</strong><small>Channel Breakout · Oct 21, 09:42:08 ET</small></p><b>32 @ $118.42</b></div></div></section>
          </aside>
        </div>
        <footer className="workspace-footer"><span>Frozen Stock · read-only paper workspace</span><span>Live authority disabled · illustrative data only</span></footer>
      </main>
    </div>
  </div>
}

export default Overview;
