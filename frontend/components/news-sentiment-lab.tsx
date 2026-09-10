"use client";

import { useEffect, useMemo, useState } from "react";
import { Newspaper, RefreshCw, UploadCloud } from "lucide-react";

import { NewsArticle, NewsSentimentSummary, getNewsArticles, getNewsSummary, importNews } from "@/lib/api";
import { StatusPill } from "@/components/status-pill";

function numberValue(value: string | number | null | undefined) {
  return Number(value ?? 0);
}

function sentimentTone(value: number) {
  if (value >= 0.2) {
    return "text-mint";
  }
  if (value <= -0.2) {
    return "text-coral";
  }
  return "text-slate-600";
}

export function NewsSentimentLab() {
  const [symbol, setSymbol] = useState("SPY");
  const [provider, setProvider] = useState<"auto" | "yfinance" | "nasdaq_rss" | "mock">("auto");
  const [status, setStatus] = useState("Ready");
  const [isBusy, setIsBusy] = useState(false);
  const [summary, setSummary] = useState<NewsSentimentSummary | null>(null);
  const [articles, setArticles] = useState<NewsArticle[]>([]);

  const latestArticles = useMemo(() => articles.slice(0, 6), [articles]);

  async function refreshNews() {
    const [summaryResponse, articleRows] = await Promise.all([getNewsSummary(symbol), getNewsArticles(symbol)]);
    setSummary(summaryResponse);
    setArticles(articleRows);
  }

  useEffect(() => {
    let active = true;
    Promise.all([getNewsSummary(symbol), getNewsArticles(symbol)]).then(([summaryResponse, articleRows]) => {
      if (!active) return;
      setSummary(summaryResponse);
      setArticles(articleRows);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "News refresh failed"); });
    return () => { active = false; };
  }, [symbol]);

  async function runAction(action: () => Promise<void>) {
    setIsBusy(true);
    try {
      await action();
      await refreshNews();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "News action failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Newspaper size={19} className="text-mint" />
            <h2 className="text-base font-semibold">News Sentiment Context</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="grid gap-2 sm:grid-cols-[120px_150px_auto_auto]">
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Symbol</span>
            <input className="focus-ring h-10 rounded-md border border-line px-3 uppercase" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
          </label>
          <label className="grid gap-1 text-sm">
            <span className="font-medium">Provider</span>
            <select className="focus-ring h-10 rounded-md border border-line px-3" value={provider} onChange={(event) => setProvider(event.target.value as "auto" | "yfinance" | "nasdaq_rss" | "mock")}>
              <option value="auto">Auto</option>
              <option value="yfinance">YFinance</option>
              <option value="nasdaq_rss">Nasdaq RSS</option>
              <option value="mock">Fallback</option>
            </select>
          </label>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md bg-mint px-3 text-sm font-semibold text-white" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus(`Importing ${provider} news`);
            const response = await importNews(symbol, provider);
            setStatus(`Stored ${response.article_ids.length} ${response.source} items${response.error ? " with fallback" : ""}`);
          })}>
            <UploadCloud size={16} />
            Import
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 self-end rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => runAction(async () => {
            setStatus("Refreshing news context");
          })}>
            <RefreshCw size={16} />
            Refresh
          </button>
        </div>
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[0.85fr_1.15fr]">
        <div className="rounded-md border border-line p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold">Sentiment Summary</div>
            <StatusPill status={summary?.sentiment_label ?? "neutral"} />
          </div>
          <div className="mt-3 grid gap-3 text-sm">
            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-md bg-panel p-2">
                <div className="text-xs font-semibold uppercase text-slate-500">Articles</div>
                <div className="mt-1 text-lg font-semibold">{summary?.article_count ?? 0}</div>
              </div>
              <div className="rounded-md bg-panel p-2">
                <div className="text-xs font-semibold uppercase text-slate-500">Avg Sentiment</div>
                <div className={`mt-1 text-lg font-semibold ${sentimentTone(summary?.average_sentiment ?? 0)}`}>{(summary?.average_sentiment ?? 0).toFixed(2)}</div>
              </div>
            </div>
            <div className="rounded-md border border-line bg-panel p-3 text-slate-700">{summary?.summary ?? "No news context loaded yet"}</div>
            <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber">News informs explanations only. Deterministic risk rules still approve or block paper trades.</div>
          </div>
        </div>

        <div className="rounded-md border border-line">
          <div className="border-b border-line p-3 text-sm font-semibold">Stored Headlines</div>
          <div className="max-h-72 overflow-auto">
            {latestArticles.length ? latestArticles.map((article) => (
              <div className="grid grid-cols-[1fr_auto] gap-3 border-b border-line p-3 text-sm" key={article.id}>
                <div>
                  <div className="font-medium">{article.title}</div>
                  <div className="mt-1 text-xs text-slate-500">{article.summary}</div>
                  <div className="mt-1 text-xs text-slate-500">{article.source} | {article.published_at ? new Date(article.published_at).toLocaleString() : "no date"}</div>
                </div>
                <div className="text-right">
                  <div className={`font-semibold ${sentimentTone(numberValue(article.sentiment_score))}`}>{numberValue(article.sentiment_score).toFixed(2)}</div>
                  <div className="text-xs text-slate-500">Rel {numberValue(article.relevance_score).toFixed(2)}</div>
                </div>
              </div>
            )) : (
              <div className="p-4 text-sm text-slate-600">No stored headlines yet</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
