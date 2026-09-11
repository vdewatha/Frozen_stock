

import { useEffect, useState } from "react";
import { BarChart3, Database, Newspaper, RefreshCw, Target } from "lucide-react";
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { CompanyOpportunityRadar, getLatestTradeCandidateRefreshJob, getOpportunityRadar, getWatchlistDiscovery, importWatchlist, startTradeCandidateRefreshJob, WatchlistDiscovery } from "@/lib/api";

const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

function toneClass(value: number): string {
  if (value > 0.05) {
    return "text-mint";
  }
  if (value < -0.05) {
    return "text-coral";
  }
  return "text-slate-600";
}

function badgeClass(value: string): string {
  if (value === "paper_trade_candidate") {
    return "border-emerald-200 bg-emerald-50 text-mint";
  }
  return "border-line bg-panel text-slate-600";
}

export function OpportunityRadar() {
  const [radar, setRadar] = useState<CompanyOpportunityRadar | null>(null);
  const [discovery, setDiscovery] = useState<WatchlistDiscovery | null>(null);
  const [status, setStatus] = useState("Loading company opportunity radar");
  const [isBusy, setIsBusy] = useState(true);

  async function refresh(refreshNews = false) {
    setIsBusy(true);
    try {
      const [response, discovered] = await Promise.all([getOpportunityRadar(8, refreshNews, "auto"), getWatchlistDiscovery(10)]);
      setRadar(response);
      setDiscovery(discovered);
      const importNote = response.news_imports.length ? `; refreshed ${response.news_imports.length} news feeds` : "";
      setStatus(`Ranked ${response.asset_count} symbols from charts, news, and scanner evidence${importNote}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Opportunity radar refresh failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function importLargeCaps() {
    setIsBusy(true);
    try {
      setStatus("Importing large caps and queueing scanner refresh");
      const response = await importWatchlist(6, "auto", false);
      const job = await startTradeCandidateRefreshJob("watchlist_import", { symbols: response.activated_symbols, surface: "opportunity_radar" });
      setStatus(job.message ?? "Scanner refresh queued.");
      let completedJob = job;
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, attempt === 0 ? 800 : 3000));
        const latestJob = await getLatestTradeCandidateRefreshJob();
        if (latestJob?.id === job.id) {
          completedJob = latestJob;
          setStatus(latestJob.message ?? `Scanner refresh ${latestJob.status}`);
          if (latestJob.status === "complete" || latestJob.status === "failed") {
            break;
          }
        }
      }
      const radarResponse = await getOpportunityRadar(8, false, "auto");
      const discovered = await getWatchlistDiscovery(10);
      setRadar(radarResponse);
      setDiscovery(discovered);
      const scanNote = completedJob.status === "complete"
        ? `; scanner refreshed ${completedJob.payload.candidate_count ?? 0} strategy checks with ${completedJob.payload.positive_count ?? 0} positives`
        : "";
      setStatus(`Activated ${response.activated_symbols.join(", ")} with market data and news context${scanNote}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Watchlist import failed");
    } finally {
      setIsBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    Promise.all([getOpportunityRadar(8, false, "auto"), getWatchlistDiscovery(10)]).then(([response, discovered]) => {
      if (!active) return;
      setRadar(response);
      setDiscovery(discovered);
      const importNote = response.news_imports.length ? `; refreshed ${response.news_imports.length} news feeds` : "";
      setStatus(`Ranked ${response.asset_count} symbols from charts, news, and scanner evidence${importNote}`);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Opportunity radar refresh failed"); })
      .finally(() => { if (active) setIsBusy(false); });
    return () => { active = false; };
  }, []);

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Target size={19} className="text-mint" />
            <h2 className="text-base font-semibold">Company Opportunity Radar</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line px-3 text-sm font-medium disabled:opacity-60" disabled={isBusy} onClick={() => refresh(true)} type="button">
            <RefreshCw size={16} />
            Refresh News
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white disabled:opacity-60" disabled={isBusy} onClick={importLargeCaps} type="button">
            <Database size={16} />
            Import Large Caps
          </button>
        </div>
      </div>

      {discovery ? (
        <div className="border-b border-line px-4 py-3 text-sm text-slate-600">
          Discovery queue: {discovery.candidates.filter((candidate) => !candidate.already_active).slice(0, 6).map((candidate) => candidate.symbol).join(", ") || "all curated large caps are active"}
        </div>
      ) : null}

      <div className="grid gap-3 p-4 xl:grid-cols-2">
        {radar?.opportunities.length ? radar.opportunities.map((row) => (
          <div className="rounded-md border border-line p-3" key={row.symbol}>
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <h3 className="font-semibold">{row.symbol}</h3>
                  <span className={`rounded-md border px-2 py-1 text-xs font-semibold ${badgeClass(row.recommendation)}`}>{row.recommendation.replaceAll("_", " ")}</span>
                </div>
                <div className="mt-1 text-xs text-slate-500">{row.price.trend_label} | {row.data_source} | score {row.opportunity_score.toFixed(2)}</div>
              </div>
              <div className="text-right">
                <div className="font-semibold">{currency.format(row.price.latest_close)}</div>
                <div className={`text-xs ${toneClass(row.price.return_20d)}`}>{percent.format(row.price.return_20d)} 20d</div>
              </div>
            </div>

            <div className="mt-3 h-28 rounded-md bg-panel p-2">
              <ResponsiveContainer>
                <LineChart data={row.price.chart} margin={{ left: 0, right: 4, top: 8, bottom: 0 }}>
                  <XAxis dataKey="date" hide />
                  <YAxis hide domain={["dataMin", "dataMax"]} />
                  <Tooltip />
                  <Line type="monotone" dataKey="close" dot={false} stroke="#0f8b6f" strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            </div>

            <div className="mt-3 grid gap-2 text-sm lg:grid-cols-2">
              <div className="rounded-md border border-line bg-panel p-2">
                <div className="mb-1 flex items-center gap-1 text-xs font-semibold uppercase text-slate-500">
                  <BarChart3 size={13} />
                  Best Trade Signal
                </div>
                {row.best_candidate ? (
                  <div>
                    <div className="font-semibold">{row.best_candidate.strategy_name}</div>
                    <div className="mt-1 text-xs text-slate-600">{row.best_candidate.action} | up {percent.format(row.best_candidate.probability_up)} | ER {percent.format(row.best_candidate.expected_return)}</div>
                    <div className="mt-1 text-xs text-slate-500">{row.best_candidate.strategy_research.source_label}</div>
                  </div>
                ) : (
                  <div className="text-xs text-slate-600">No scanner candidate yet.</div>
                )}
              </div>
              <div className="rounded-md border border-line bg-panel p-2">
                <div className="mb-1 flex items-center gap-1 text-xs font-semibold uppercase text-slate-500">
                  <Newspaper size={13} />
                  News Tone
                </div>
                <div className={`font-semibold ${toneClass(row.news.average_sentiment)}`}>{row.news.sentiment_label}</div>
                <div className="mt-1 text-xs text-slate-600">{row.news.article_count} items | avg {row.news.average_sentiment.toFixed(2)}</div>
              </div>
            </div>
          </div>
        )) : (
          <div className="rounded-md border border-line bg-panel p-4 text-sm text-slate-600">No active company opportunities loaded yet.</div>
        )}
      </div>
    </section>
  );
}
