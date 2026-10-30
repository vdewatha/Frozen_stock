import { Fragment, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "wouter";
import {
  Activity, ArrowDownRight, ArrowRight, ArrowUpRight, Bell, BookOpen, ChartNoAxesCombined,
  ChevronRight, CircleHelp, Clock3, FlaskConical, RefreshCw, ShieldCheck, Wallet,
} from "lucide-react";
import { getErrorMessage } from "@/lib/api";
import { getPaperOverview, type OverviewPosition, type PaperOverview } from "@/lib/paper-overview-api";

const numberFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const moneyFormat = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });

function decimal(value: string | null): number | null {
  if (value === null || value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function money(value: string | null): string {
  const amount = decimal(value);
  return amount === null ? "Unknown" : moneyFormat.format(amount);
}

function percentage(value: number | null): string {
  return value === null || !Number.isFinite(value) ? "Unknown" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function dateTime(value: string | null): string {
  if (!value) return "Unknown";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "Unknown" : parsed.toLocaleString("en-US", {
    year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    timeZone: "America/New_York", timeZoneName: "short",
  });
}

function dateOnly(value: string | null): string {
  if (!value) return "Unknown";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "Unknown" : parsed.toLocaleDateString("en-US", { dateStyle: "medium", timeZone: "America/New_York" });
}

function timeOnly(value: string | null): string {
  if (!value || Number.isNaN(new Date(value).getTime())) return "Unknown";
  return new Date(value).toLocaleTimeString("en-US", {
    hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/New_York",
  });
}

function positionOwner(position: OverviewPosition, overview: PaperOverview): string {
  if (position.attribution === "unknown") return "Unattributed";
  if (position.attribution === "multiple") {
    const names = position.strategy_ids
      .map(id => overview.strategies.find(strategy => strategy.id === id)?.name)
      .filter((name): name is string => Boolean(name));
    return names.length ? `Multiple strategies: ${names.join(", ")}` : "Multiple strategies";
  }
  return position.strategy || "Unknown";
}

function quantity(value: string): string {
  const parsed = decimal(value);
  return parsed === null ? "Unknown" : numberFormat.format(parsed);
}

function PositionExtraDetails({ position, overview }: { position: OverviewPosition; overview: PaperOverview }) {
  const owner = positionOwner(position, overview);
  const risk = money(position.stop_price);
  const signal = overview.strategies
    .filter(strategy => position.strategy_ids.includes(strategy.id) && strategy.last_signal)
    .map(strategy => `${strategy.name}: ${strategy.last_signal?.action} ${strategy.last_signal?.symbol}${strategy.last_signal?.withheld ? ` · Withheld${strategy.last_signal.reason ? `: ${strategy.last_signal.reason}` : ""}` : ""}`)
    .join("; ") || "No signal available";
  return <div className="ov-expanded-detail">
    <strong>{position.symbol} position details</strong>
    <span>Latest strategy signal (not entry evidence): {signal}</span>
    <span>Risk / stop: {risk}</span>
    <span>Attribution: {position.attribution === "unknown" ? "Unattributed — fills do not explain the broker quantity" : owner}</span>
    <span>Last observed: {dateTime(position.observed_at)}</span>
  </div>;
}

function StatusStrip({ overview }: { overview: PaperOverview }) {
  const { session, bot, bounds, account } = overview;
  const minutesToClose = session.closes_at ? Math.ceil((new Date(session.closes_at).getTime() - new Date(overview.generated_at).getTime()) / 60_000) : null;
  const countdown = minutesToClose !== null && minutesToClose > 0 ? ` · ${Math.floor(minutesToClose / 60)}h ${minutesToClose % 60}m to close` : "";
  const state = session.state === "open" ? `Open${countdown}` : "Closed";
  const activeMandate = bot.mandate_status ? `Mandate: ${bot.mandate_status}` : "Mandate status unknown";
  const switchState = bot.kill_switch === null ? "Kill switch state unknown" : bot.kill_switch ? "Kill switch on" : "Kill switch off";
  const boundsSummary = bounds.starts_at ? `${bounds.max_positions ?? "Unknown"} positions · ${money(bounds.max_gross)} gross` : "No current session approval";
  const stopFraction = decimal(bounds.stop_fraction);
  const boundsDetails = `Per symbol ${money(bounds.per_symbol)} · Stop ${stopFraction === null ? "Unknown" : `${(stopFraction * 100).toFixed(2)}%`}`;
  return <section className={`ov-status-strip${account.stale ? " is-stale" : ""}`} aria-label="Paper operating status">
    <div className="ov-state-cell"><span className="ov-state-icon session"><Clock3 size={16} /></span><div><small>MARKET SESSION</small><strong>{state}</strong><span>NYSE · {dateOnly(overview.generated_at)}</span><span>Entry {timeOnly(session.entry_start)} – {timeOnly(session.entry_end)} ET</span></div></div>
    <div className="ov-state-cell"><span className="ov-state-icon bot"><Activity size={16} /></span><div><small>PAPER BOT</small><strong>{bot.state || "Unknown"}</strong><span>{switchState} · {activeMandate}</span><span>{bot.reason || "Runtime health unknown"}</span></div></div>
    <div className="ov-state-cell"><span className="ov-state-icon bounds"><ShieldCheck size={16} /></span><div><small>APPROVED BOUNDS</small><strong>{boundsSummary}</strong><span>{boundsDetails}</span><span title={`${dateTime(bounds.starts_at)} – ${dateTime(bounds.ends_at)}`}>{timeOnly(bounds.starts_at)} – {timeOnly(bounds.ends_at)} ET</span></div></div>
    <div className="ov-state-cell reconcile"><span className="ov-state-icon time"><Clock3 size={16} /></span><div><small>LAST RECONCILIATION</small><strong>{timeOnly(account.last_reconciled_at)} ET</strong><span>{account.stale ? "Last known snapshot · values may have changed" : `Account status: ${account.status}`}</span><span>{dateOnly(account.last_reconciled_at)} · paper only</span></div></div>
  </section>;
}

function PositionsPanel({ overview }: { overview: PaperOverview }) {
  const [filter, setFilter] = useState("All strategies");
  const [expanded, setExpanded] = useState<string | null>(null);
  const filters = useMemo(() => {
    const names = overview.strategies.map(strategy => strategy.name);
    return [...new Set(names)];
  }, [overview.strategies]);
  const filtered = overview.positions.filter(position => {
    if (filter === "All strategies") return true;
    if (filter === "Unattributed") return position.attribution === "unknown";
    if (filter === "Multiple strategies") return position.attribution === "multiple";
    const strategy = overview.strategies.find(item => item.name === filter);
    return position.strategy === filter || Boolean(strategy && position.strategy_ids.includes(strategy.id));
  });

  return <section className="ov-positions">
    <div className="ov-section-head"><div><div className="ov-kicker">CURRENT PAPER BOOK</div><h2>Open positions <span>{overview.positions.length}</span></h2></div><div className="ov-filter-wrap"><label htmlFor="strategy-filter">Strategy</label><select id="strategy-filter" value={filter} onChange={event => { setFilter(event.target.value); setExpanded(null); }}><option>All strategies</option>{filters.map(name => <option key={name}>{name}</option>)}<option>Unattributed</option><option>Multiple strategies</option></select></div></div>
    {overview.account.stale && <div className="ov-stale-note"><Clock3 size={14} />Showing last known holdings. Reconciliation is stale; values may have changed.</div>}
    {filtered.length === 0 ? <div className="ov-empty"><div className="ov-empty-mark"><Wallet size={22} /></div><div><strong>{overview.positions.length ? "No positions match this strategy" : overview.account.last_reconciled_at ? "No open positions" : "Positions unavailable"}</strong><p>{overview.positions.length ? "Choose another strategy filter to see holdings." : overview.account.last_reconciled_at ? `Broker snapshot ${dateTime(overview.account.last_reconciled_at)}.` : "No broker snapshot has been imported yet."}</p></div></div> :
      <>
        <div className="ov-table-wrap"><table className="ov-table"><thead><tr><th>POSITION</th><th>OWNER / ENTRY</th><th className="numeric">QTY</th><th className="numeric">MARK</th><th className="numeric">UNREALIZED P/L</th><th>HORIZON</th><th>EXIT INTENT</th><th aria-label="Details" /></tr></thead><tbody>{filtered.map(position => {
          const pnl = decimal(position.unrealized_pl);
          const held = position.sessions_held;
          const horizon = position.horizon_sessions;
          const progress = held !== null && horizon !== null && horizon > 0 ? Math.min(100, Math.max(0, held / horizon * 100)) : null;
           const unknownAttribution = position.attribution === "unknown";
          const isExpanded = expanded === position.symbol;
          return <Fragment key={position.symbol}>
            <tr className={isExpanded ? "is-expanded" : ""}>
              <td><button className="ov-position-button" onClick={() => setExpanded(isExpanded ? null : position.symbol)} aria-expanded={isExpanded}><span className="ov-expand">{isExpanded ? "−" : "+"}</span><span><strong>{position.symbol}</strong></span></button></td>
               <td><strong className={unknownAttribution ? "ov-unattributed" : "ov-owner"} title={unknownAttribution ? "Ownership not confirmed: fills do not explain the broker quantity" : undefined}>{unknownAttribution && <CircleHelp size={13} />}{positionOwner(position, overview)}</strong><small className="ov-sub">{position.entry_at ? dateOnly(position.entry_at) : "Date unknown"} · {money(position.entry_price)} entry</small></td>
              <td className="numeric ov-number">{quantity(position.quantity)}</td>
              <td className="numeric ov-number">{money(position.current_price)}</td>
              <td className="numeric"><strong className={pnl === null ? "ov-unknown" : pnl >= 0 ? "ov-positive" : "ov-negative"}>{pnl === null ? "Unverified" : `${pnl >= 0 ? "+" : ""}${money(position.unrealized_pl)}`}</strong><small className={`ov-sub ${pnl === null ? "ov-unknown" : pnl >= 0 ? "ov-positive" : "ov-negative"}`}>{percentage(position.unrealized_pl_pct)}</small></td>
              <td><strong className="ov-horizon">{held === null ? "Unknown" : horizon === null ? `${held} held · horizon Unknown` : `${held} / ${horizon} sessions`}</strong>{progress !== null && <div className="ov-progress"><i style={{ width: `${progress}%` }} /></div>}</td>
              <td><span className={`ov-intent ${position.exit_intent ? "" : "unknown"}`}>{position.exit_intent || "Unknown"}</span></td>
              <td><button className="ov-row-toggle" aria-label={`${isExpanded ? "Hide" : "Show"} ${position.symbol} details`} aria-expanded={isExpanded} onClick={() => setExpanded(isExpanded ? null : position.symbol)}><ChevronRight size={16} /></button></td>
            </tr>
            {isExpanded && <tr className="ov-detail-row"><td colSpan={8}><PositionExtraDetails position={position} overview={overview} /></td></tr>}
          </Fragment>;
        })}</tbody></table></div>
        <div className="ov-mobile-positions">{filtered.map(position => {
          const pnl = decimal(position.unrealized_pl);
          const held = position.sessions_held;
          const horizon = position.horizon_sessions;
          const progress = held !== null && horizon !== null && horizon > 0 ? Math.min(100, Math.max(0, held / horizon * 100)) : null;
           const unknownAttribution = position.attribution === "unknown";
          const isExpanded = expanded === position.symbol;
          return <article className="ov-mobile-card" key={position.symbol}>
            <button className="ov-mobile-card-head" onClick={() => setExpanded(isExpanded ? null : position.symbol)} aria-expanded={isExpanded}><span><strong>{position.symbol}</strong></span><span className={pnl === null ? "ov-unknown" : pnl >= 0 ? "ov-positive" : "ov-negative"}>{pnl === null ? "Unverified" : `${pnl >= 0 ? "+" : ""}${money(position.unrealized_pl)}`}<small>{percentage(position.unrealized_pl_pct)}</small></span><ChevronRight size={16} /></button>
            <div className="ov-mobile-owner">{unknownAttribution ? <span className="ov-unattributed"><CircleHelp size={13} />{positionOwner(position, overview)}</span> : positionOwner(position, overview)}<span>{quantity(position.quantity)} shares</span></div>
            <div className="ov-mobile-grid">
              <div><small>ENTRY</small><strong>{money(position.entry_price)}</strong><em>{position.entry_at ? dateOnly(position.entry_at) : "Unknown"}</em></div>
              <div><small>MARK</small><strong>{money(position.current_price)}</strong><em>Current paper price</em></div>
              <div><small>HORIZON</small><strong>{held === null ? "Unknown" : horizon === null ? `${held} held · Unknown horizon` : `${held} / ${horizon} sessions`}</strong>{progress !== null && <div className="ov-progress"><i style={{ width: `${progress}%` }} /></div>}</div>
              <div><small>EXIT INTENT</small><strong className="ov-card-intent">{position.exit_intent || "Unknown"}</strong></div>
            </div>
            {isExpanded && <div className="ov-detail"><span>Latest strategy signal <b>{overview.strategies.filter(strategy => position.strategy_ids.includes(strategy.id) && strategy.last_signal).map(strategy => `${strategy.last_signal?.action} ${strategy.last_signal?.symbol}`).join(", ") || "Unknown"}</b></span><span>Risk / stop <b>{money(position.stop_price)}</b></span><span>Attribution <b>{positionOwner(position, overview)}</b></span><span>Observed <b>{dateTime(position.observed_at)}</b></span></div>}
          </article>;
        })}</div>
      </>}
    <div className="ov-table-foot"><span>Values as reported · USD · generated {dateTime(overview.generated_at)}</span><span>Position values are not orders</span></div>
  </section>;
}

function StrategiesPanel({ overview }: { overview: PaperOverview }) {
  const nextEvaluation = overview.strategies.map(strategy => strategy.next_evaluation_at).filter((value): value is string => value !== null).sort()[0] ?? null;
  return <section className="ov-strategies">
    <div className="ov-section-head compact"><div><div className="ov-kicker">PAPER STRATEGIES</div><h2>Strategies today <span>{overview.strategies.length}</span></h2></div><span className="ov-next-eval">Next eval · {nextEvaluation ? dateTime(nextEvaluation) : "Unknown"}</span></div>
    {overview.strategies.length === 0 ? <p className="ov-empty-inline">No strategy data available.</p> : <div className="ov-strategy-list">{overview.strategies.map(strategy => {
      const signal = strategy.last_signal;
       const tone = strategy.status === "paper_trading_active" ? "active" : strategy.status === "paper_trading_candidate" ? "candidate" : "paused";
      return <article className="ov-strategy-row" key={strategy.id}>
        <div className="ov-strategy-name"><span className={`ov-strategy-dot ${tone}`} /><div><strong>{strategy.name}</strong><small>{strategy.open_positions} attributed / {strategy.position_limit ?? "Unknown"} max</small></div><span className={`ov-status-tag ${tone}`}>{({paper_trading_active: "Active", paper_trading_candidate: "Candidate", paused: "Paused"} as Record<string, string>)[strategy.status] || strategy.status.replaceAll("_", " ")}</span></div>
        <div className="ov-strategy-meta">
          <span><small>LAST SIGNAL</small><b>{signal ? `${signal.action} · ${signal.symbol}` : "Unknown"}</b><em>{signal ? dateTime(signal.at) : "No signal recorded"}</em></span>
          <span><small>ENTRY STATUS</small><b className={signal?.withheld === true ? "ov-withheld" : signal?.withheld === false ? "ov-clear" : "ov-unknown"} title={signal?.reason ?? undefined}>{signal?.withheld === true ? "BUY withheld · entry window" : signal?.withheld === false ? "Not withheld" : "Unknown"}</b></span>
          <span><small>NEXT EVALUATION</small><b title="Only a persisted next-run timestamp is shown. Cadence does not guarantee dispatch.">{strategy.next_evaluation_at ? dateTime(strategy.next_evaluation_at) : "Unknown"}</b></span>
        </div>
      </article>;
    })}</div>}
    <Link className="ov-research-link" href="/research/library"><FlaskConical size={15} />Research library <span>Strategy details</span><ArrowRight size={14} /></Link>
  </section>;
}

function AccountPanel({ overview }: { overview: PaperOverview }) {
  const { account } = overview;
  return <section className="ov-account">
    <div className="ov-section-head compact"><div><div className="ov-kicker">ACCOUNT SNAPSHOT</div><h2>Paper account</h2></div></div>
    {account.stale && <p className="ov-account-warning">Stale account values shown as last known; they may have changed.</p>}
    <div className="ov-account-grid">
      <div><small>ACCOUNT EQUITY</small><strong>{money(account.equity)}</strong></div>
      <div><small>CASH AVAILABLE</small><strong>{money(account.cash)}</strong></div>
      <div><small>GROSS EXPOSURE</small><strong>{money(account.exposure)}</strong><em>{account.exposure_pct === null ? "Unknown % of equity" : `${account.exposure_pct.toFixed(1)}% of equity`}</em></div>
      <div><small>REALIZED P/L</small><strong className="ov-unverified">{account.pnl_verified && account.realized_pl !== null ? money(account.realized_pl) : "Unverified"}</strong><em>{account.pnl_verified && account.realized_pl !== null ? "Reported verified value" : "P/L verification unavailable"}</em></div>
    </div>
    <Link href="/portfolio/ledger" className="ov-ledger-link">Account ledger <ArrowRight size={14} /></Link>
  </section>;
}

function ActivityPanel({ overview }: { overview: PaperOverview }) {
  const icon = (kind: "buy" | "sell" | "withheld") => kind === "buy" ? <ArrowUpRight size={14} /> : kind === "sell" ? <ArrowDownRight size={14} /> : <ShieldCheck size={14} />;
  return <section className="ov-activity">
    <div className="ov-section-head compact"><div><div className="ov-kicker">PAPER TAPE</div><h2>Recent activity</h2></div><span className="ov-live-dot">LATEST</span></div>
    {overview.activity.length === 0 ? <p className="ov-empty-inline">No recent activity available.</p> : <div className="ov-activity-list">{overview.activity.map((item, index) => <div key={item.id || `${item.symbol}-${index}`}>
      <span className={`ov-activity-mark ${item.kind}`}>{icon(item.kind)}</span>
      <p><strong>{item.symbol} · {item.kind === "withheld" ? "action withheld" : item.kind === "buy" ? "entry" : "exit"}</strong><small>{item.strategy ?? "Strategy unknown"} · {dateTime(item.at)}</small></p>
      <b>{item.kind === "withheld" ? item.detail || "Withheld" : item.detail || item.kind}</b>
    </div>)}</div>}
    <Link className="ov-activity-link" href="/activity/notifications"><Bell size={14} />Notifications and activity <ArrowRight size={14} /></Link>
  </section>;
}

export function PaperOverviewPanel() {
  const { data, error, isPending, refetch, isFetching } = useQuery({
    queryKey: ["paper-overview"],
    queryFn: getPaperOverview,
    refetchInterval: 30_000,
    retry: 1,
  });
  if (isPending && !data) return <section className="ov-state-message" aria-live="polite"><h2>Loading paper overview</h2><p>Retrieving the latest account and strategy snapshot.</p></section>;
  if (error && !data) return <section className="ov-state-message is-error" role="alert"><h2>Paper overview unavailable</h2><p>{getErrorMessage(error, "The paper overview could not be loaded.")}</p><button type="button" onClick={() => void refetch()} disabled={isFetching}>Retry</button></section>;
  if (!data) return <section className="ov-state-message"><h2>No overview data</h2><p>The service returned no paper overview.</p></section>;
  return <>
    <div className="ov-heading"><div><div className="eyebrow">FROZEN STOCK / PAPER BOOK</div><h1>Overview</h1><p>Holdings, ownership and today’s operating state in one view.</p></div><div className="ov-heading-actions"><span className="ov-authority">READ ONLY</span><button className="icon-button refresh-button" aria-label="Refresh overview" title="Refresh overview" disabled={isFetching} onClick={() => void refetch()}><RefreshCw size={18} className={isFetching ? "animate-spin" : ""} /></button></div></div>
    {error && <div className="ov-refresh-warning" role="status">The latest refresh failed. Showing the last successfully loaded snapshot. <button type="button" onClick={() => void refetch()}>Retry</button></div>}
    <StatusStrip overview={data} />
    <PositionsPanel overview={data} />
    <div className="ov-lower-grid">
      <StrategiesPanel overview={data} />
      <aside className="ov-side-column"><AccountPanel overview={data} /><ActivityPanel overview={data} /></aside>
    </div>
    <nav className="ov-related-links" aria-label="Related workspace pages"><Link href="/portfolio">Portfolio</Link><Link href="/research/library"><BookOpen size={14} />Research library</Link><Link href="/risk/recovery"><ShieldCheck size={14} />Recovery</Link><Link href="/activity/notifications"><Bell size={14} />Notifications</Link><Link href="/activity"><Activity size={14} />Activity</Link><Link href="/portfolio/ledger"><ChartNoAxesCombined size={14} />Ledger</Link></nav>
  </>;
}

