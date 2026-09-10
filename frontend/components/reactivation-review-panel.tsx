"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, CirclePause, RefreshCw, ShieldAlert, XCircle } from "lucide-react";

import {
  StrategyReactivationCandidate,
  StrategyReactivationQueue,
  StrategyReactivationReviewResponse,
  getStrategyReactivationQueue,
  reviewStrategyReactivation
} from "@/lib/api";
import { StatusPill } from "@/components/status-pill";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

function metric(value: number | undefined): string {
  return typeof value === "number" ? value.toFixed(2) : "0.00";
}

export function ReactivationReviewPanel() {
  const [queue, setQueue] = useState<StrategyReactivationQueue | null>(null);
  const [status, setStatus] = useState("Loading reactivation queue");
  const [isBusy, setIsBusy] = useState(true);
  const [lastReview, setLastReview] = useState<StrategyReactivationReviewResponse | null>(null);

  async function refresh() {
    setIsBusy(true);
    try {
      const data = await getStrategyReactivationQueue();
      setQueue(data);
      setStatus(`${data.candidates.length} strategies awaiting review`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Reactivation queue failed");
    } finally {
      setIsBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    getStrategyReactivationQueue().then((data) => {
      if (!active) return;
      setQueue(data);
      setStatus(`${data.candidates.length} strategies awaiting review`);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Reactivation queue failed"); })
      .finally(() => { if (active) setIsBusy(false); });
    return () => { active = false; };
  }, []);

  async function submitReview(candidate: StrategyReactivationCandidate, decision: "approve" | "reject" | "hold") {
    setIsBusy(true);
    try {
      const response = await reviewStrategyReactivation(
        candidate.strategy_id,
        decision,
        `${decision} decision from reactivation review panel.`
      );
      setLastReview(response);
      setStatus(`${response.strategy_name}: ${response.status} (${response.old_status} -> ${response.new_status})`);
      await refresh();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Review failed");
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <ShieldAlert size={19} className={queue?.kill_switch_enabled ? "text-coral" : "text-mint"} />
            <h2 className="text-base font-semibold">Reactivation Review</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={refresh} type="button">
          <RefreshCw size={16} />
          Refresh
        </button>
      </div>

      {queue?.kill_switch_enabled ? (
        <div className="border-b border-line bg-red-50 px-4 py-3 text-sm font-medium text-coral">
          Global kill switch is enabled. Approval is blocked until it is reset.
        </div>
      ) : null}

      <div className="grid gap-3 p-4">
        {queue?.candidates.length ? queue.candidates.map((candidate) => (
          <div className="rounded-md border border-line" key={candidate.strategy_id}>
            <div className="flex flex-col gap-3 border-b border-line p-3 md:flex-row md:items-center md:justify-between">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-sm font-semibold">{candidate.strategy_name}</h3>
                  <StatusPill status={candidate.current_status} />
                  <span className={candidate.eligible ? "rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 text-xs font-semibold text-mint" : "rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-xs font-semibold text-amber-700"}>
                    {candidate.eligible ? "Eligible" : "Blocked"}
                  </span>
                </div>
                <div className="mt-1 text-xs text-slate-500">{candidate.strategy_type} | {candidate.memory.symbol ?? "no symbol"} | {candidate.memory.market_regime ?? "unclassified"}</div>
              </div>
              <div className="flex flex-wrap gap-2">
                <button className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => submitReview(candidate, "hold")} type="button">
                  <CirclePause size={15} />
                  Hold
                </button>
                <button className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-coral/30 bg-red-50 px-3 text-sm font-semibold text-coral" disabled={isBusy} onClick={() => submitReview(candidate, "reject")} type="button">
                  <XCircle size={15} />
                  Reject
                </button>
                <button className="focus-ring inline-flex h-9 items-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white disabled:opacity-60" disabled={isBusy || !candidate.eligible} onClick={() => submitReview(candidate, "approve")} type="button">
                  <CheckCircle2 size={15} />
                  Approve
                </button>
              </div>
            </div>

            <div className="grid gap-3 p-3 lg:grid-cols-[1fr_1.2fr]">
              <div className="grid grid-cols-5 gap-2 text-xs text-slate-600">
                <span>Samples {candidate.memory.sample_size ?? 0}</span>
                <span>Win {percent.format(candidate.memory.win_rate ?? 0)}</span>
                <span>PF {metric(candidate.memory.profit_factor)}</span>
                <span>Draw {percent.format(candidate.memory.avg_drawdown ?? 0)}</span>
                <span>Conf {metric(candidate.memory.confidence_score)}</span>
              </div>
              <div className="grid gap-1 text-xs text-slate-600">
                {candidate.blockers.length ? candidate.blockers.map((blocker) => <span key={blocker}>{blocker}</span>) : <span>No review blockers.</span>}
              </div>
            </div>
          </div>
        )) : (
          <div className="rounded-md border border-line bg-panel p-4 text-sm text-slate-600">No paused or candidate strategies require review.</div>
        )}
      </div>

      {lastReview ? (
        <div className="border-t border-line bg-panel px-4 py-3 text-sm text-slate-700">
          Last review: {lastReview.strategy_name} {lastReview.decision} | {lastReview.status}
        </div>
      ) : null}
    </section>
  );
}
