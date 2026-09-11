

import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, ListChecks, MinusCircle, Play, RefreshCw, Scale, ShieldCheck, TrendingDown, X } from "lucide-react";

import { AllocationReviewQueue, PortfolioAllocationExecution, PortfolioAllocationPlan, PortfolioRiskActionResponse, PortfolioRiskSnapshot, dryRunApprovedAllocationReview, executePortfolioAllocationPlan, getAllocationReviewQueue, getPortfolioAllocationPlan, getPortfolioRisk, reducePaperTrade, reviewAllocationQueueItem, runPortfolioRiskActions } from "@/lib/api";

const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

function alertClasses(severity: string): string {
  if (severity === "breach") {
    return "border-coral/30 bg-red-50 text-coral";
  }
  if (severity === "warning") {
    return "border-amber-200 bg-amber-50 text-amber-700";
  }
  return "border-emerald-200 bg-emerald-50 text-mint";
}

function barColor(utilization: number): string {
  if (utilization >= 1) {
    return "bg-coral";
  }
  if (utilization >= 0.85) {
    return "bg-amber-500";
  }
  return "bg-mint";
}

function ExposureBar({ label, value, limit, utilization }: { label: string; value: number; limit?: number; utilization: number }) {
  return (
    <div className="grid gap-1 text-sm">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium">{label}</span>
        <span className="text-slate-500">{percent.format(value)}{typeof limit === "number" ? ` / ${percent.format(limit)}` : ""}</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-panel">
        <div className={`h-full ${barColor(utilization)}`} style={{ width: `${Math.min(utilization, 1) * 100}%` }} />
      </div>
    </div>
  );
}

function recommendationClasses(recommendation: string): string {
  if (recommendation === "add" || recommendation === "activate_candidate") {
    return "border-emerald-200 bg-emerald-50 text-mint";
  }
  if (recommendation === "trim" || recommendation === "wait_for_room") {
    return "border-amber-200 bg-amber-50 text-amber-700";
  }
  return "border-line bg-panel text-slate-700";
}

export function PortfolioRiskPanel() {
  const [snapshot, setSnapshot] = useState<PortfolioRiskSnapshot | null>(null);
  const [allocation, setAllocation] = useState<PortfolioAllocationPlan | null>(null);
  const [reviewQueue, setReviewQueue] = useState<AllocationReviewQueue | null>(null);
  const [lastAllocationRun, setLastAllocationRun] = useState<PortfolioAllocationExecution | null>(null);
  const [status, setStatus] = useState("Loading portfolio risk");
  const [isBusy, setIsBusy] = useState(true);
  const [lastAction, setLastAction] = useState<PortfolioRiskActionResponse | null>(null);

  async function refresh() {
    setIsBusy(true);
    try {
      const [data, plan, queue] = await Promise.all([getPortfolioRisk(), getPortfolioAllocationPlan(), getAllocationReviewQueue()]);
      setSnapshot(data);
      setAllocation(plan);
      setReviewQueue(queue);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Portfolio risk refresh failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function evaluateActions(dryRun: boolean) {
    setIsBusy(true);
    try {
      const response = await runPortfolioRiskActions(true, dryRun);
      setLastAction(response);
      setSnapshot(response.snapshot);
      const [plan, queue] = await Promise.all([getPortfolioAllocationPlan(), getAllocationReviewQueue()]);
      setAllocation(plan);
      setReviewQueue(queue);
      setStatus(`${response.status}: ${response.message}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Risk action evaluation failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function reducePosition(tradeId: number, reducePct: number) {
    setIsBusy(true);
    try {
      const response = await reducePaperTrade(
        tradeId,
        reducePct,
        reducePct >= 1 ? "Manual paper position close from portfolio risk monitor." : "Manual 50% paper exposure reduction from portfolio risk monitor."
      );
      const data = await getPortfolioRisk();
      const plan = await getPortfolioAllocationPlan();
      const queue = await getAllocationReviewQueue();
      setSnapshot(data);
      setAllocation(plan);
      setReviewQueue(queue);
      setStatus(
        response.status === "closed"
          ? `Closed paper trade ${tradeId} at ${currency.format(response.exit_price)}`
          : `Reduced trade ${tradeId}; ${response.remaining_quantity.toFixed(4)} shares remain`
      );
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Paper position reduction failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function runAllocationExecutor(dryRun: boolean) {
    setIsBusy(true);
    try {
      const response = await executePortfolioAllocationPlan(dryRun, 3, 20);
      const [data, plan, queue] = await Promise.all([getPortfolioRisk(), getPortfolioAllocationPlan(), getAllocationReviewQueue()]);
      setLastAllocationRun(response);
      setSnapshot(data);
      setAllocation(plan);
      setReviewQueue(queue);
      setStatus(`${response.status}: ${response.message}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Allocation executor failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function reviewQueueItem(item: AllocationReviewQueue["items"][number], decision: "approve" | "skip") {
    setIsBusy(true);
    try {
      const response = await reviewAllocationQueueItem(
        item,
        decision,
        `${decision === "approve" ? "Approved" : "Skipped"} replay-approved allocation queue item for ${item.symbol} ${item.strategy}.`
      );
      const [plan, queue] = await Promise.all([getPortfolioAllocationPlan(), getAllocationReviewQueue()]);
      setAllocation(plan);
      setReviewQueue(queue);
      setStatus(`${response.status}: ${response.message}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Allocation queue review failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function approveAndDryRunQueueItem(item: AllocationReviewQueue["items"][number]) {
    setIsBusy(true);
    try {
      const review = await reviewAllocationQueueItem(
        item,
        "approve",
        `Approved replay-approved allocation queue item for ${item.symbol} ${item.strategy} before single-item dry run.`
      );
      const dryRun = await dryRunApprovedAllocationReview(item, review.journal_entry.id, 20);
      const [data, plan, queue] = await Promise.all([getPortfolioRisk(), getPortfolioAllocationPlan(), getAllocationReviewQueue()]);
      setLastAllocationRun(dryRun);
      setSnapshot(data);
      setAllocation(plan);
      setReviewQueue(queue);
      setStatus(`${dryRun.status}: ${dryRun.message}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Approved allocation dry run failed");
    } finally {
      setIsBusy(false);
    }
  }

  useEffect(() => {
    let active = true;
    Promise.all([getPortfolioRisk(), getPortfolioAllocationPlan(), getAllocationReviewQueue()]).then(([data, plan, queue]) => {
      if (!active) return;
      setSnapshot(data);
      setAllocation(plan);
      setReviewQueue(queue);
      setStatus(`Updated ${new Date(data.generated_at).toLocaleTimeString()}`);
    }).catch(error => { if (active) setStatus(error instanceof Error ? error.message : "Portfolio risk refresh failed"); })
      .finally(() => { if (active) setIsBusy(false); });
    return () => { active = false; };
  }, []);

  const riskTone = snapshot?.alerts.some((alert) => alert.severity === "breach")
    ? "bad"
    : snapshot?.alerts.some((alert) => alert.severity === "warning")
      ? "warn"
      : "good";

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <ShieldCheck size={19} className={riskTone === "bad" ? "text-coral" : riskTone === "warn" ? "text-amber-600" : "text-mint"} />
            <h2 className="text-base font-semibold">Portfolio Risk Monitor</h2>
          </div>
          <div className="mt-1 text-sm text-slate-500">{status}</div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={refresh} type="button">
            <RefreshCw size={16} />
            Refresh
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={isBusy} onClick={() => evaluateActions(true)} type="button">
            <Play size={16} />
            Dry Run
          </button>
          <button className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-md bg-coral px-3 text-sm font-semibold text-white disabled:opacity-60" disabled={isBusy} onClick={() => evaluateActions(false)} type="button">
            <AlertTriangle size={16} />
            Run Actions
          </button>
        </div>
      </div>

      <div className="border-b border-line p-4">
        <div className="mb-4 rounded-md border border-line bg-panel p-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <ListChecks size={17} className="text-mint" />
              <div className="text-sm font-semibold">Replay Approval Queue</div>
            </div>
            <div className="text-xs text-slate-500">
              {reviewQueue?.open_gate_alerts ?? 0} open gate alert{(reviewQueue?.open_gate_alerts ?? 0) === 1 ? "" : "s"} | {reviewQueue?.actionable_items ?? 0} dry-run ready
            </div>
          </div>
          {reviewQueue?.items.length ? (
            <div className="grid gap-2 lg:grid-cols-3">
              {reviewQueue.items.slice(0, 3).map((item) => (
                <div className="rounded-md border border-emerald-200 bg-white p-3 text-xs" key={`${item.notification_id}-${item.symbol}-${item.strategy}`}>
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <div className="font-semibold text-ink">{item.symbol} | {item.strategy_name}</div>
                      <div className="mt-1 uppercase text-mint">{item.review_status.replaceAll("_", " ")}</div>
                    </div>
                    <span className="rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1 font-semibold text-mint">{item.recommendation.replaceAll("_", " ")}</span>
                  </div>
                  <div className="mt-2 grid gap-1 text-slate-600">
                    <span>Gate: {item.gate_label}</span>
                    <span>{item.gate_complete_samples ?? 0} / {item.gate_min_complete_samples ?? 0} replay samples</span>
                    <span>Target {percent.format(item.target_exposure_pct)} | multiplier {item.memory_allocation_multiplier.toFixed(2)}x</span>
                    <span>Up {percent.format(item.probability_up)} | ER {percent.format(item.expected_return)}</span>
                  </div>
                  <div className="mt-2 text-slate-700">{item.reason}</div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button className="focus-ring inline-flex h-8 items-center gap-1 rounded-md bg-mint px-2 text-xs font-semibold text-white disabled:opacity-60" disabled={isBusy} onClick={() => reviewQueueItem(item, "approve")} type="button">
                      <CheckCircle2 size={13} />
                      Approve
                    </button>
                    {item.dry_run_available ? (
                      <button className="focus-ring inline-flex h-8 items-center gap-1 rounded-md border border-emerald-200 bg-emerald-50 px-2 text-xs font-semibold text-mint disabled:opacity-60" disabled={isBusy} onClick={() => approveAndDryRunQueueItem(item)} type="button">
                        <Play size={13} />
                        Approve + Dry Run
                      </button>
                    ) : null}
                    <button className="focus-ring inline-flex h-8 items-center gap-1 rounded-md border border-line px-2 text-xs font-medium text-slate-700 disabled:opacity-60" disabled={isBusy} onClick={() => reviewQueueItem(item, "skip")} type="button">
                      <X size={13} />
                      Skip
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="rounded-md border border-line bg-white p-3 text-sm text-slate-600">{reviewQueue?.message ?? "Loading replay approval queue"}</div>
          )}
        </div>

        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Scale size={17} className="text-mint" />
            <div className="text-sm font-semibold">Allocation Plan</div>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <div className="text-xs text-slate-500">{allocation?.positive_candidates ?? 0} positive candidates</div>
            <button className="focus-ring inline-flex h-8 items-center justify-center gap-1 rounded-md border border-line px-2 text-xs font-medium disabled:opacity-60" disabled={isBusy} onClick={() => runAllocationExecutor(true)} type="button">
              <Play size={13} />
              Dry Run
            </button>
            <button className="focus-ring inline-flex h-8 items-center justify-center gap-1 rounded-md bg-mint px-2 text-xs font-semibold text-white disabled:opacity-60" disabled={isBusy} onClick={() => runAllocationExecutor(false)} type="button">
              <CheckCircle2 size={13} />
              Execute
            </button>
          </div>
        </div>
        {lastAllocationRun ? (
          <div className="mb-3 rounded-md border border-line bg-panel p-3 text-xs text-slate-700">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-semibold">Last Allocation {lastAllocationRun.dry_run ? "Dry Run" : "Execution"}</span>
              <span className="uppercase text-slate-500">{lastAllocationRun.status}</span>
            </div>
            <div className="mt-1">{lastAllocationRun.message}</div>
            <div className="mt-1 text-slate-500">{lastAllocationRun.actions.length} action(s), {lastAllocationRun.skipped.length} skipped</div>
          </div>
        ) : null}
        {allocation?.recommendations.length ? (
          <div className="grid gap-2 xl:grid-cols-2">
            {allocation.recommendations.slice(0, 4).map((row) => (
              <div className={`rounded-md border p-3 text-sm ${recommendationClasses(row.recommendation)}`} key={`${row.symbol}-${row.strategy}`}>
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="font-semibold">{row.symbol} | {row.strategy_name}</div>
                    <div className="mt-1 text-xs uppercase">{row.recommendation.replaceAll("_", " ")}</div>
                  </div>
                  <div className="text-right text-xs">
                    <div>{percent.format(row.current_exposure_pct)} now</div>
                    <div>{percent.format(row.target_exposure_pct)} target</div>
                  </div>
                </div>
                <div className="mt-2 text-xs">{row.reason}</div>
                {row.memory_allocation_context.sample_size ? (
                  <div className="mt-2 rounded-md border border-line bg-white/70 p-2 text-xs">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold uppercase">Journal sizing</span>
                      <span>{row.memory_allocation_multiplier.toFixed(2)}x</span>
                    </div>
                    <div className="mt-1 text-slate-600">
                      Base {percent.format(row.base_target_exposure_pct)} | {row.memory_allocation_context.status.replaceAll("_", " ")}
                      {row.memory_allocation_context.gate_limited ? ` | requested ${row.memory_allocation_context.requested_multiplier.toFixed(2)}x` : ""}
                    </div>
                    <div className="mt-1 text-slate-600">
                      Gate: {row.memory_allocation_context.replay_gate_scope_label}
                    </div>
                  </div>
                ) : null}
                <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                  <span>Up {percent.format(row.probability_up)}</span>
                  <span>ER {percent.format(row.expected_return)}</span>
                  <span>Score {row.rank_score.toFixed(2)}</span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="rounded-md border border-line bg-panel p-3 text-sm text-slate-600">{allocation?.message ?? "Loading allocation plan"}</div>
        )}
        {allocation?.memory_replay_gate ? (
          <div className={`mt-3 rounded-md border p-3 text-xs ${allocation.memory_replay_gate.allows_memory_increase ? "border-emerald-200 bg-emerald-50 text-mint" : "border-amber-200 bg-amber-50 text-amber-700"}`}>
            <div className="font-semibold">Memory replay gate {allocation.memory_replay_gate.status}</div>
            <div className="mt-1">
              {allocation.memory_replay_gate.allows_memory_increase
                ? "Memory-based allocation increases are enabled."
                : allocation.memory_replay_gate.blockers[0] ?? "Memory-based allocation increases are paused."}
            </div>
          </div>
        ) : null}
      </div>

      <div className="grid gap-4 p-4 xl:grid-cols-[1fr_1fr]">
        <div className="grid gap-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="rounded-md border border-line bg-panel p-3">
              <div className="text-xs font-semibold uppercase text-slate-500">Gross Exposure</div>
              <div className="mt-1 text-xl font-semibold">{percent.format(snapshot?.gross_exposure ?? 0)}</div>
              <div className="text-xs text-slate-500">{currency.format(snapshot?.total_notional ?? 0)} notional</div>
            </div>
            <div className="rounded-md border border-line bg-panel p-3">
              <div className="text-xs font-semibold uppercase text-slate-500">Open Positions</div>
              <div className="mt-1 text-xl font-semibold">{snapshot?.open_positions ?? 0}</div>
              <div className="text-xs text-slate-500">Limit {snapshot?.risk_limits.max_open_positions ?? 0}</div>
            </div>
            <div className="rounded-md border border-line bg-panel p-3">
              <div className="text-xs font-semibold uppercase text-slate-500">Unrealized P/L</div>
              <div className={(snapshot?.total_unrealized_pl ?? 0) >= 0 ? "mt-1 text-xl font-semibold text-mint" : "mt-1 text-xl font-semibold text-coral"}>{currency.format(snapshot?.total_unrealized_pl ?? 0)}</div>
              <div className="text-xs text-slate-500">{percent.format(snapshot?.total_unrealized_pl_pct ?? 0)}</div>
            </div>
          </div>

          <div className="rounded-md border border-line">
            <div className="border-b border-line p-3 text-sm font-semibold">Risk Alerts</div>
            <div className="grid gap-2 p-3">
              {snapshot?.alerts.map((alert) => (
                <div className={`flex items-start gap-2 rounded-md border p-3 text-sm ${alertClasses(alert.severity)}`} key={`${alert.label}-${alert.message}`}>
                  {alert.severity === "clear" ? <ShieldCheck size={17} /> : <AlertTriangle size={17} />}
                  <div>
                    <div className="font-semibold">{alert.label}</div>
                    <div>{alert.message}</div>
                  </div>
                </div>
              )) ?? <div className="text-sm text-slate-600">Loading alerts</div>}
            </div>
          </div>

          {lastAction ? (
            <div className="rounded-md border border-line bg-panel p-3 text-sm">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold">Last Automated Evaluation</span>
                <span className="text-xs uppercase text-slate-500">{lastAction.status}</span>
              </div>
              <div className="mt-2 text-slate-700">{lastAction.message}</div>
              <div className="mt-2 grid gap-1 text-xs text-slate-500 md:grid-cols-3">
                <span>Breaches {lastAction.breach_labels.length}</span>
                <span>Persistent {lastAction.persistent_labels.length}</span>
                <span>Actions {lastAction.actions_taken.length}</span>
              </div>
            </div>
          ) : null}
        </div>

        <div className="grid gap-4">
          <div className="rounded-md border border-line p-3">
            <div className="mb-3 text-sm font-semibold">Symbol Exposure</div>
            <div className="grid gap-3">
              {snapshot?.symbol_exposure.length ? snapshot.symbol_exposure.slice(0, 5).map((row) => (
                <ExposureBar key={row.key} label={row.key} limit={row.limit_pct} utilization={row.utilization} value={row.exposure_pct} />
              )) : <div className="text-sm text-slate-600">No open symbol exposure.</div>}
            </div>
          </div>

          <div className="rounded-md border border-line p-3">
            <div className="mb-3 text-sm font-semibold">Strategy Concentration</div>
            <div className="grid gap-3">
              {snapshot?.strategy_exposure.length ? snapshot.strategy_exposure.slice(0, 5).map((row) => (
                <div className="grid gap-1" key={row.key}>
                  <ExposureBar label={row.key} utilization={row.utilization} value={row.exposure_pct} />
                  <div className="text-xs text-slate-500">{row.open_positions} / {row.position_limit} open positions</div>
                </div>
              )) : <div className="text-sm text-slate-600">No open strategy exposure.</div>}
            </div>
          </div>
        </div>
      </div>

      <div className="border-t border-line">
        <div className="flex items-center gap-2 border-b border-line p-3 text-sm font-semibold">
          <TrendingDown size={17} className="text-mint" />
          Open Position Ledger
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[900px] border-collapse text-sm">
            <thead className="bg-panel text-left text-xs uppercase text-slate-500">
              <tr>
                <th className="px-3 py-2">Symbol</th>
                <th className="px-3 py-2">Strategy</th>
                <th className="px-3 py-2 text-right">Qty</th>
                <th className="px-3 py-2 text-right">Exposure</th>
                <th className="px-3 py-2 text-right">Entry</th>
                <th className="px-3 py-2 text-right">Latest</th>
                <th className="px-3 py-2 text-right">Unrealized</th>
                <th className="px-3 py-2 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {snapshot?.positions.length ? snapshot.positions.map((position) => (
                <tr className="border-t border-line" key={position.paper_trade_id}>
                  <td className="px-3 py-2 font-semibold">{position.symbol}</td>
                  <td className="px-3 py-2">{position.strategy}</td>
                  <td className="px-3 py-2 text-right">{position.quantity.toFixed(4)}</td>
                  <td className="px-3 py-2 text-right">{percent.format(position.exposure_pct)}</td>
                  <td className="px-3 py-2 text-right">{currency.format(position.entry_price)}</td>
                  <td className="px-3 py-2 text-right">{currency.format(position.latest_price)}</td>
                  <td className={(position.unrealized_pl >= 0 ? "px-3 py-2 text-right font-semibold text-mint" : "px-3 py-2 text-right font-semibold text-coral")}>{currency.format(position.unrealized_pl)}</td>
                  <td className="px-3 py-2">
                    <div className="flex justify-end gap-2">
                      <button className="focus-ring inline-flex h-8 items-center justify-center gap-1 rounded-md border border-line px-2 text-xs font-medium disabled:opacity-60" disabled={isBusy} onClick={() => reducePosition(position.paper_trade_id, 0.5)} title="Reduce paper position by 50%" type="button">
                        <MinusCircle size={14} />
                        50%
                      </button>
                      <button className="focus-ring inline-flex h-8 items-center justify-center gap-1 rounded-md bg-coral px-2 text-xs font-semibold text-white disabled:opacity-60" disabled={isBusy} onClick={() => reducePosition(position.paper_trade_id, 1)} title="Close paper position" type="button">
                        <X size={14} />
                        Close
                      </button>
                    </div>
                  </td>
                </tr>
              )) : (
                <tr className="border-t border-line">
                  <td className="px-3 py-3 text-slate-600" colSpan={8}>No open paper positions.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
