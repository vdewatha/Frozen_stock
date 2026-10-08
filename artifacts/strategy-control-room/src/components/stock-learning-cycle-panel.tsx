import { useEffect, useState } from "react";
import { CheckCircle2, GitBranch, RefreshCw, ShieldAlert, Workflow } from "lucide-react";

import { RoleGate } from "@/components/access-control";
import { LaunchPrerequisiteResults } from "@/components/launch-prerequisites";
import { StatusPill } from "@/components/status-pill";
import {
  actOnStockLearningCycle,
  createStockLearningCycleApproval,
  getErrorMessage,
  getLaunchPrerequisites,
  type LaunchPrerequisites,
  getStockLearningCycles,
  getStockLearningScheduleControl,
  launchLearningWorkerBatch,
  launchLearningWorkerScope,
  reviewStockLearningCycle,
  updateStockLearningScheduleControl,
  type StockLearningScheduleControl,
  type StockLearningCycle,
  type StockLearningCycleAction,
  type CreatePaperRunApprovalRequest,
} from "@/lib/api";

function gateClass(status: string): string {
  if (status === "pass") return "text-mint";
  if (status === "fail" || status === "blocked") return "text-coral";
  return "text-amber-700";
}

function ComparisonSummary({ cycle }: { cycle: StockLearningCycle }) {
  const gate = cycle.gates.challenger_comparison;
  if (!gate) return null;
  const evidence = (gate.evidence ?? {}) as {
    challenger_model_run_id?: string;
    incumbent_model_run_id?: string | null;
    minimum_sample_count?: number;
  };
  return (
    <div className="mt-3 rounded border border-slate-200 bg-white p-2 text-xs" data-testid="learning-cycle-comparison">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold text-ink">Challenger comparison</span>
        <span className={gateClass(gate.status)}>{gate.status}</span>
      </div>
      <div className="mt-1 text-slate-600">
        {gate.reason ?? "Accuracy, calibration, cost, risk, and sample gates passed independently."}
      </div>
      <div className="mt-1 flex flex-wrap gap-3 text-slate-500">
        <span>Challenger: {evidence.challenger_model_run_id ?? cycle.model_run_id ?? "unavailable"}</span>
        <span>Incumbent: {evidence.incumbent_model_run_id ?? "baseline only"}</span>
        <span>Minimum samples: {evidence.minimum_sample_count ?? "unknown"}</span>
      </div>
    </div>
  );
}

export function StockLearningCyclePanel() {
  const [cycles, setCycles] = useState<StockLearningCycle[]>([]);
  const [scheduleControl, setScheduleControl] = useState<StockLearningScheduleControl | null>(null);
  const [reason, setReason] = useState("");
  const [status, setStatus] = useState("Loading governed learning cycles");
  const [busy, setBusy] = useState(false);
  const [prerequisites, setPrerequisites] = useState<Record<string, LaunchPrerequisites>>({});
  const [prerequisiteError, setPrerequisiteError] = useState<string>();
  const [refreshing, setRefreshing] = useState(true);
  const [approvalDrafts, setApprovalDrafts] = useState<Record<string, CreatePaperRunApprovalRequest>>({});
  const [workerSymbol, setWorkerSymbol] = useState("AAPL");
  const [workerStrategy, setWorkerStrategy] = useState("moving_average_crossover");
  const [workerStatus, setWorkerStatus] = useState("No learning worker launched from this control room yet.");

  async function refresh() {
    setRefreshing(true);
    setPrerequisiteError(undefined);
    try {
      const [nextCycles, nextControl] = await Promise.all([
        getStockLearningCycles(12),
        getStockLearningScheduleControl(),
      ]);
      setCycles(nextCycles);
      setScheduleControl(nextControl);
      setApprovalDrafts((current) => {
        const next = { ...current };
        for (const cycle of nextCycles) {
          if (!next[cycle.cycle_id]) {
            const expected = cycle.handoff.approval.expected;
            next[cycle.cycle_id] = {
              ...expected,
              duration_sessions: 1,
              loss_limits: expected.loss_limits ?? { max_loss: "0.02", unit: "fraction_of_baseline_equity", currency: "USD" },
              stop_authority: expected.stop_authority ?? "operator_and_system",
              pending_order_treatment: expected.pending_order_treatment ?? "cancel",
              remaining_position_policy: expected.remaining_position_policy ?? "hold",
              approving_actors: [],
            };
          }
        }
        return next;
      });
      const results = await Promise.all(
        nextCycles.length ? nextCycles.map((cycle) => getLaunchPrerequisites(cycle.cycle_id)) : [getLaunchPrerequisites()],
      );
      setPrerequisites(Object.fromEntries(results.map((result) => [result.cycle_id ?? "none", result])));
      setStatus("Cycle evidence refreshed");
    } catch (error) {
      setPrerequisiteError(getErrorMessage(error));
      setStatus(getErrorMessage(error));
    } finally {
      setRefreshing(false);
    }
  }

  async function updateSchedule(action: "pause" | "resume") {
    if (!reason.trim()) return;
    setBusy(true);
    try {
      setScheduleControl(await updateStockLearningScheduleControl(action, reason));
      setReason("");
      await refresh();
    } catch (error) {
      setStatus(getErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  async function review(cycle: StockLearningCycle) {
    if (!reason.trim()) return;
    setBusy(true);
    try {
      await reviewStockLearningCycle(cycle.cycle_id, reason);
      setReason("");
      await refresh();
    } catch (error) {
      setStatus(getErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function act(cycle: StockLearningCycle, action: StockLearningCycleAction) {
    if (!reason.trim()) return;
    setBusy(true);
    try {
      await actOnStockLearningCycle(cycle.cycle_id, action, reason);
      setReason("");
      await refresh();
    } catch (error) {
      setStatus(getErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function approvePaperRun(cycle: StockLearningCycle) {
    if (refreshing || prerequisiteError || !prerequisites[cycle.cycle_id]?.eligible_for_approval) return;
    const expected = approvalDrafts[cycle.cycle_id] ?? {
      ...cycle.handoff.approval.expected,
      duration_sessions: 1,
      approving_actors: [],
    };
    setBusy(true);
    try {
      const latest = await getLaunchPrerequisites(cycle.cycle_id);
      setPrerequisites((current) => ({ ...current, [cycle.cycle_id]: latest }));
      if (!latest.eligible_for_approval) {
        setStatus(latest.reason);
        return;
      }
      await createStockLearningCycleApproval(cycle.cycle_id, expected);
      setStatus("Paper run approval recorded");
      await refresh();
    } catch (error) {
      setStatus(getErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function launchBatchWorkers() {
    setBusy(true);
    try {
      const result = await launchLearningWorkerBatch(4, 8, 3);
      setWorkerStatus(`${result.message} Task ${result.task_id || "unavailable"}.`);
    } catch (error) {
      setWorkerStatus(getErrorMessage(error, "Learning worker batch could not be queued."));
    } finally {
      setBusy(false);
    }
  }

  async function launchScopeWorker() {
    if (!workerSymbol.trim()) return;
    setBusy(true);
    try {
      const result = await launchLearningWorkerScope(workerSymbol, workerStrategy, 3);
      setWorkerStatus(`${result.message} Task ${result.task_id || "unavailable"}.`);
    } catch (error) {
      setWorkerStatus(getErrorMessage(error, "Learning worker scope could not be queued."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white p-4" data-testid="stock-learning-cycle-panel">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <GitBranch size={18} className="text-mint" />
          <div>
            <h2 className="text-base font-semibold">Governed stock learning cycle</h2>
            <p className="text-xs text-slate-500">Research artifacts remain paper-only until forward evidence and explicit operator action pass.</p>
          </div>
        </div>
        <button disabled={refreshing || busy} className="focus-ring inline-flex h-8 items-center gap-1 rounded border border-line px-2.5 text-xs font-semibold" onClick={() => void refresh()} type="button">
          <RefreshCw size={13} /> Refresh
        </button>
      </div>

      <RoleGate requires="operator" className="mt-4">
        <div className="rounded border border-amber-200 bg-amber-50 p-3" data-testid="stock-learning-schedule-control">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-sm font-semibold text-ink">Scheduled learning decisions</div>
              <div className="text-xs text-slate-600">
                {scheduleControl?.paused
                  ? `Paused: ${scheduleControl.pause_reason ?? "operator maintenance"}`
                  : "Running on its normal schedule"}
              </div>
            </div>
            <span className={`text-xs font-semibold ${scheduleControl?.paused ? "text-amber-800" : "text-mint"}`}>
              {scheduleControl?.paused ? "Paused" : "Active"}
            </span>
          </div>
          <div className="mt-2 flex flex-wrap gap-2">
            {!scheduleControl?.paused ? (
              <button className="focus-ring rounded bg-amber-700 px-2 py-1.5 text-xs font-semibold text-white disabled:opacity-50" disabled={busy || !reason.trim()} onClick={() => void updateSchedule("pause")} type="button">
                Pause scheduled learning
              </button>
            ) : (
              <button className="focus-ring rounded bg-mint px-2 py-1.5 text-xs font-semibold text-white disabled:opacity-50" disabled={busy || !reason.trim()} onClick={() => void updateSchedule("resume")} type="button">
                Resume scheduled learning
              </button>
            )}
            <span className="self-center text-xs text-slate-600">Paper execution safeguards and recovery controls remain independent.</span>
          </div>
        </div>
      </RoleGate>

      <RoleGate requires="operator" className="mt-4">
        <div className="rounded border border-emerald-200 bg-emerald-50 p-3" data-testid="stock-learning-worker-control">
          <div className="flex items-center gap-2">
            <Workflow size={16} className="text-mint" />
            <div>
              <div className="text-sm font-semibold text-ink">Parallel research workers</div>
              <div className="text-xs text-slate-600">Fans out bounded symbol and strategy learning jobs. These workers create research evidence only; they never submit broker orders.</div>
            </div>
          </div>
          <div className="mt-3 flex flex-wrap items-end gap-2">
            <button className="focus-ring rounded bg-mint px-2.5 py-1.5 text-xs font-semibold text-white disabled:opacity-50" disabled={busy} onClick={() => void launchBatchWorkers()} type="button">
              Launch bounded batch
            </button>
            <label className="grid gap-1 text-[11px] text-slate-600">
              Symbol
              <input className="focus-ring h-8 w-20 rounded border border-emerald-200 bg-white px-2 text-xs" value={workerSymbol} onChange={(event) => setWorkerSymbol(event.target.value)} />
            </label>
            <label className="grid gap-1 text-[11px] text-slate-600">
              Strategy
              <select className="focus-ring h-8 rounded border border-emerald-200 bg-white px-2 text-xs" value={workerStrategy} onChange={(event) => setWorkerStrategy(event.target.value)}>
                <option value="moving_average_crossover">Moving average</option>
                <option value="rsi_reversion">RSI reversion</option>
                <option value="macd_momentum">MACD momentum</option>
                <option value="ensemble">Ensemble</option>
                <option value="model_predictive">Model predictive</option>
                <option value="bollinger_reversion">Bollinger reversion</option>
                <option value="channel_breakout">Channel breakout</option>
                <option value="trend_pullback">Trend pullback</option>
              </select>
            </label>
            <button className="focus-ring h-8 rounded border border-emerald-300 bg-white px-2.5 text-xs font-semibold text-emerald-900 disabled:opacity-50" disabled={busy || !workerSymbol.trim()} onClick={() => void launchScopeWorker()} type="button">
              Launch one scope
            </button>
          </div>
          <p className="mt-2 text-xs text-slate-600" role="status" data-testid="stock-learning-worker-status">{workerStatus}</p>
        </div>
      </RoleGate>

      {cycles.length === 0 ? (
        <div className="mt-4 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <LaunchPrerequisiteResults result={prerequisites.none} error={prerequisiteError} loading={refreshing} />
          No durable cycle run exists yet. Scheduled work will show blocked or deferred prerequisite evidence here rather than claiming coverage.
        </div>
      ) : (
        <div className="mt-4 grid gap-3">
          {cycles.map((cycle) => (
            <article className="rounded border border-line bg-panel p-3" data-testid={`stock-learning-cycle-${cycle.cycle_id}`} key={cycle.cycle_id}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <StatusPill status={cycle.status} />
                  <span className="text-sm font-semibold">{cycle.stage.replaceAll("_", " ")}</span>
                  <span className="text-xs text-slate-500">{cycle.trigger} · {cycle.symbols.join(", ")}</span>
                </div>
                <span className="text-xs text-slate-500">{new Date(cycle.updated_at).toLocaleString()}</span>
              </div>
              <div className="mt-2 grid gap-2 text-xs text-slate-600 sm:grid-cols-2">
                <div><span className="font-semibold text-ink">Dataset:</span> {cycle.snapshot_id ?? "not admitted"}</div>
                <div><span className="font-semibold text-ink">Model:</span> {cycle.model_run_id ?? "not registered"}</div>
                <div><span className="font-semibold text-ink">Paper binding:</span> {cycle.binding_id ?? "awaiting admission"}</div>
                <div><span className="font-semibold text-ink">Forward trial:</span> {cycle.trial_id ?? "awaiting admission"}</div>
                <div><span className="font-semibold text-ink">Monitor:</span> {cycle.monitoring.status}</div>
                <div><span className="font-semibold text-ink">Recovery:</span> {cycle.recovery.status}</div>
              </div>
              <div className="mt-3 rounded border border-line bg-white p-2 text-xs" data-testid="learning-cycle-handoff">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-semibold text-ink">Automatic paper handoff</span>
                  <span className={gateClass(cycle.handoff.status === "running_forward_trial" ? "pass" : cycle.handoff.status === "blocked" ? "blocked" : "unknown")}>
                    {cycle.handoff.status.replaceAll("_", " ")}
                  </span>
                </div>
                <div className="mt-1 text-slate-600">
                  {cycle.handoff.reason ?? "Waiting for scheduled training evidence"}
                </div>
                <div className="mt-1 text-slate-500">
                  Preflight: {cycle.handoff.preflight?.status ?? "not checked"}
                  {cycle.handoff.report_id ? ` · report ${cycle.handoff.report_id}` : ""}
                  {cycle.handoff.report_decision ? ` · ${cycle.handoff.report_decision}` : ""}
                </div>
                <div
                  className={`mt-2 rounded border p-2 ${
                    cycle.handoff.approval.status === "pass"
                      ? "border-emerald-200 bg-emerald-50"
                      : "border-amber-200 bg-amber-50"
                  }`}
                  data-testid={`learning-cycle-paper-approval-${cycle.cycle_id}`}
                >
                  <LaunchPrerequisiteResults result={prerequisites[cycle.cycle_id]} error={prerequisiteError} loading={refreshing} />
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-semibold">Paper run approval</span>
                    <span className={gateClass(cycle.handoff.approval.status === "pass" ? "pass" : "blocked")}>
                      {cycle.handoff.approval.status}
                    </span>
                  </div>
                  <div className="mt-1 text-slate-600">
                    {cycle.handoff.approval.reason ?? "Approval matches the requested paper runtime bounds."}
                  </div>
                  {cycle.handoff.approval.record ? (
                    <div className="mt-1 text-slate-500">
                      {cycle.handoff.approval.record.approving_actors.join(", ")} ·{" "}
                      {cycle.handoff.approval.record.execution_provider} ·{" "}
                      {cycle.handoff.approval.record.duration_sessions} sessions
                    </div>
                  ) : null}
                  {cycle.handoff.approval.status !== "pass" ? (
                    <RoleGate requires="operator" className="mt-2">
                      {(() => {
                        const draft = approvalDrafts[cycle.cycle_id];
                        if (!draft) return null;
                        const setDraft = (patch: Partial<CreatePaperRunApprovalRequest>) =>
                          setApprovalDrafts((current) => ({
                            ...current,
                            [cycle.cycle_id]: { ...draft, ...patch },
                          }));
                        return (
                          <div className="grid gap-2 rounded border border-amber-300 bg-white p-2">
                            <div className="font-semibold text-ink">Exact one-session bounds</div>
                            <div className="grid gap-2 sm:grid-cols-3">
                              {([
                                ["max_order_notional", "Per-order cap"],
                                ["max_symbol_notional", "Per-symbol cap"],
                                ["max_aggregate_notional", "Aggregate cap"],
                              ] as const).map(([key, label]) => (
                                <label className="grid gap-1 text-[11px] text-slate-600" key={key}>
                                  {label} (USD)
                                  <input
                                    className="focus-ring rounded border border-line px-2 py-1.5 text-xs text-ink"
                                    inputMode="decimal"
                                    value={String(draft.exposure_limits[key] ?? "")}
                                    onChange={(event) => setDraft({
                                      exposure_limits: { ...draft.exposure_limits, [key]: event.target.value },
                                    })}
                                  />
                                </label>
                              ))}
                            </div>
                            <div className="grid gap-2 sm:grid-cols-3">
                              <label className="grid gap-1 text-[11px] text-slate-600">
                                Max loss
                                <input
                                  className="focus-ring rounded border border-line px-2 py-1.5 text-xs text-ink"
                                  inputMode="decimal"
                                  value={String(draft.loss_limits.max_loss ?? "")}
                                  onChange={(event) => setDraft({
                                    loss_limits: { ...draft.loss_limits, max_loss: event.target.value },
                                  })}
                                />
                              </label>
                              <label className="grid gap-1 text-[11px] text-slate-600">
                                Loss unit
                                <input
                                  className="focus-ring rounded border border-line px-2 py-1.5 text-xs text-ink"
                                  value={String(draft.loss_limits.unit ?? "")}
                                  onChange={(event) => setDraft({
                                    loss_limits: { ...draft.loss_limits, unit: event.target.value },
                                  })}
                                />
                              </label>
                              <label className="grid gap-1 text-[11px] text-slate-600">
                                Remaining position
                                <select
                                  className="focus-ring rounded border border-line px-2 py-1.5 text-xs text-ink"
                                  value={draft.remaining_position_policy}
                                  onChange={(event) => setDraft({ remaining_position_policy: event.target.value })}
                                >
                                  <option value="hold">Hold</option>
                                  <option value="reduce">Reduce</option>
                                  <option value="flatten">Flatten</option>
                                </select>
                              </label>
                            </div>
                            <div className="grid gap-1 text-[11px] text-slate-500">
                              <span>
                                Session: {draft.schedule.session_date ?? "not configured"} ·{" "}
                                {draft.schedule.timezone ?? "America/New_York"}
                              </span>
                              <span>
                                {draft.schedule.start_at ?? "start unavailable"} → {draft.schedule.end_at ?? "end unavailable"}
                              </span>
                              <span>Pending orders: cancel at expiry · stop authority: operator and system</span>
                            </div>
                          </div>
                        );
                      })()}
                    </RoleGate>
                  ) : null}
                  {cycle.handoff.approval.status !== "pass" ? (
                    <RoleGate requires="operator">
                    <button
                      className="focus-ring mt-2 rounded bg-amber-700 px-2 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
                      disabled={busy || refreshing || !!prerequisiteError || !prerequisites[cycle.cycle_id]?.eligible_for_approval}
                      onClick={() => void approvePaperRun(cycle)}
                      type="button"
                    >
                      Record exact paper approval
                    </button>
                    </RoleGate>
                  ) : null}
                  <div className="mt-1 text-[11px] text-slate-500">
                    Paper-only authorization; it cannot grant live authority.
                  </div>
                </div>
              </div>
              <div className="mt-3 rounded border border-slate-200 bg-white p-2 text-xs" data-testid="learning-cycle-position-handling">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-semibold text-ink">Position handling</span>
                  <StatusPill status={cycle.position_handling.handling_status} />
                </div>
                <div className="mt-1 grid gap-1 text-slate-600 sm:grid-cols-2">
                  <span>
                    <span className="font-semibold text-ink">Approved remaining-position policy:</span>{" "}
                    {cycle.position_handling.approved_policy ?? "not approved"}
                  </span>
                  <span>
                    <span className="font-semibold text-ink">Expiry / operator stop:</span>{" "}
                    {cycle.position_handling.stop_status.replaceAll("_", " ")}
                    {cycle.position_handling.stop_reason ? ` · ${cycle.position_handling.stop_reason}` : ""}
                  </span>
                  <span>
                    <span className="font-semibold text-ink">New entries:</span>{" "}
                    {cycle.position_handling.new_entries_stopped ? "stopped" : "allowed"}
                  </span>
                  <span>
                    <span className="font-semibold text-ink">Monitoring / reconciliation:</span>{" "}
                    {cycle.monitoring.status} / {cycle.position_handling.reconciliation.status}
                    {cycle.position_handling.reconciliation.reconciliation_required ? " · review required" : ""}
                  </span>
                </div>
                {cycle.position_handling.remaining_positions.length > 0 ? (
                  <div className="mt-2 flex flex-wrap gap-2 text-slate-500">
                    <span className="font-semibold text-ink">Remaining paper positions:</span>
                    {cycle.position_handling.remaining_positions.map((position) => (
                      <span className="rounded bg-slate-100 px-1.5 py-0.5" key={position.symbol}>
                        {position.symbol} {position.quantity}
                      </span>
                    ))}
                  </div>
                ) : (
                  <div className="mt-2 text-slate-500">No remaining paper positions are recorded for this cycle universe.</div>
                )}
                {cycle.position_handling.managed_lots.length > 0 ? (
                  <div className="mt-3 rounded border border-slate-200 bg-slate-50 p-2" data-testid="learning-cycle-exit-progress">
                    <div className="font-semibold text-ink">Managed symbol exit progress</div>
                    <div className="mt-2 grid gap-2">
                      {cycle.position_handling.managed_lots.map((lot, index) => (
                        <div
                          className="rounded border border-slate-200 bg-white p-2"
                          key={`${lot.symbol}-${index}`}
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <span className="font-semibold text-ink">{lot.symbol}</span>
                            <StatusPill status={lot.exit_status ?? "not_started"} />
                          </div>
                          <div className="mt-1 grid gap-1 text-slate-600 sm:grid-cols-3">
                            <span>
                              <span className="font-semibold text-ink">Entry:</span> {lot.entry_quantity}
                            </span>
                            <span>
                              <span className="font-semibold text-ink">Exited:</span> {lot.exited_quantity}
                            </span>
                            <span>
                              <span className="font-semibold text-ink">Remaining:</span> {lot.remaining_quantity}
                            </span>
                          </div>
                          <div className="mt-1 text-slate-500">
                            <span className="font-semibold text-ink">Exit reason:</span>{" "}
                            {lot.exit_reason ? lot.exit_reason.replaceAll("_", " ") : "not recorded"}
                            {lot.exit_decided_at ? ` · decided ${new Date(lot.exit_decided_at).toLocaleString()}` : ""}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
              <div className="mt-3 rounded border border-slate-200 bg-white p-2 text-xs">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-semibold text-ink">Automatic paper promotion</span>
                  <span className={gateClass(cycle.automatic_promotion?.decision === "promoted" ? "pass" : cycle.automatic_promotion ? "blocked" : "unknown")}>
                    {cycle.automatic_promotion?.decision ?? "pending"}
                  </span>
                </div>
                {cycle.automatic_promotion ? (
                  <>
                    <div className="mt-1 text-slate-600">{cycle.automatic_promotion.reason}</div>
                    <div className="mt-1 text-slate-500">
                      {cycle.automatic_promotion.actor} · {cycle.automatic_promotion.source_job}
                      {cycle.automatic_promotion.after_binding_id ? ` · binding ${cycle.automatic_promotion.after_binding_id}` : ""}
                    </div>
                  </>
                ) : (
                  <div className="mt-1 text-slate-500">No scheduled decision has been recorded; missing evidence is not a pass.</div>
                )}
              </div>
              <ComparisonSummary cycle={cycle} />
              <div className="mt-2 grid gap-1 sm:grid-cols-2">
                {Object.entries(cycle.gates).map(([name, gate]) => (
                  <div className={`flex items-center gap-1 text-xs ${gateClass(gate.status)}`} key={name}>
                    {gate.status === "pass" ? <CheckCircle2 size={13} /> : <ShieldAlert size={13} />}
                    <span className="font-semibold">{name.replaceAll("_", " ")}</span>
                    <span>· {gate.status}</span>
                  </div>
                ))}
              </div>
              {cycle.last_reason ? <div className="mt-2 text-xs text-slate-600">{cycle.last_reason}</div> : null}
              <RoleGate requires="operator" className="mt-3">
                <div className="grid gap-2 rounded border border-amber-200 bg-amber-50 p-2">
                  <input className="focus-ring rounded border border-amber-300 bg-white px-2 py-1.5 text-xs" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Reason for review or lifecycle action" />
                  <div className="flex flex-wrap gap-2">
                    <button className="focus-ring rounded bg-mint px-2 py-1.5 text-xs font-semibold text-white disabled:opacity-50" disabled={busy || !reason.trim()} onClick={() => void review(cycle)} type="button">Review evidence</button>
                    {cycle.status === "operator_review" ? <button className="focus-ring rounded border border-mint px-2 py-1.5 text-xs font-semibold text-mint disabled:opacity-50" disabled={busy || !reason.trim()} onClick={() => void act(cycle, "mark_eligible")} type="button">Mark eligible</button> : null}
                    {cycle.status === "operator_review" ? <span className="self-center text-xs text-amber-800">Automatic promotion runs only for a completed, aligned paper canary; live trading remains disabled.</span> : null}
                  </div>
                </div>
              </RoleGate>
            </article>
          ))}
        </div>
      )}
      <div className="mt-3 text-xs text-slate-500">{status} · paper-only · live trading disabled</div>
    </section>
  );
}
