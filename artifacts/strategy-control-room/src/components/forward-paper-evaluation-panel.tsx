import { useEffect, useState, useCallback } from "react";
import { useForm } from "react-hook-form";
import { Activity, Beaker, Check, ChevronDown, ChevronRight, CircleOff, FileText, Pause, Play, Square, ShieldCheck, ShieldAlert } from "lucide-react";
import { RoleGate } from "@/components/access-control";
import { StatusPill } from "@/components/status-pill";
import {
  getForwardTrials,
  getForwardTrialsEligibleBindings,
  createForwardTrial,
  getForwardTrialDetail,
  getForwardTrialDecisions,
  getForwardTrialMetrics,
  startForwardTrial,
  pauseForwardTrial,
  resumeForwardTrial,
  stopForwardTrial,
  type ForwardTrial,
  type ForwardTrialBindingEligible,
  type ForwardTrialDecision,
  type ForwardTrialMetricHistoryItem
} from "@/lib/api";

const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });
const decimal = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

function formatNull(val: number | null | undefined, formatter: Intl.NumberFormat): string {
  if (val === null || val === undefined) return "Unavailable";
  return formatter.format(val);
}

function TrialDetailView({ trial, onActionComplete }: { trial: ForwardTrial; onActionComplete: () => void }) {
  const [latestMetric, setLatestMetric] = useState<ForwardTrialMetricHistoryItem | null>(null);
  const [decisions, setDecisions] = useState<ForwardTrialDecision[]>([]);
  const [loading, setLoading] = useState(true);
  const [isBusy, setIsBusy] = useState(false);
  const [pauseReason, setPauseReason] = useState("");
  const [showPauseForm, setShowPauseForm] = useState(false);

  const fetchDetails = useCallback(async () => {
    try {
      setLoading(true);
      const [m, d] = await Promise.all([
        getForwardTrialMetrics(trial.id),
        getForwardTrialDecisions(trial.id)
      ]);
      setLatestMetric(m.length > 0 ? m[m.length - 1] : null);
      setDecisions(d);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }, [trial.id]);

  useEffect(() => {
    void fetchDetails();
  }, [fetchDetails]);

  const handleAction = async (action: () => Promise<unknown>) => {
    try {
      setIsBusy(true);
      await action();
      onActionComplete();
    } catch (e) {
      console.error(e);
    } finally {
      setIsBusy(false);
      setShowPauseForm(false);
      setPauseReason("");
    }
  };

  const payload = latestMetric?.payload || {};
  
  // Extract fields from policy/lineage if they exist
  const lineage = trial.lineage || {};
  const policy = trial.policy || {};
  const modelHash = lineage.model_hash || "Unavailable";
  const modelCutoff = lineage.model_cutoff || lineage.cutoff || "Unavailable";
  const universe = lineage.universe || "Unavailable";
  const paperOnly = policy.paper_only !== false;
  const liveDisabled = policy.live_disabled !== false && policy.live_authorized !== true;

  return (
    <div className="grid gap-4 border-t border-line bg-slate-50 p-4">
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        <div className="rounded-md border border-line bg-white p-3 text-sm">
          <div className="font-semibold text-slate-800">Configuration</div>
          <div className="mt-2 grid gap-1 text-xs text-slate-600">
            <div>Binding ID: <span className="font-medium text-slate-800">{trial.binding_id}</span></div>
            <div>Policy: <span className="font-medium text-slate-800 break-words">{JSON.stringify(trial.policy)}</span></div>
            <div>Lineage: <span className="font-medium text-slate-800 break-words">{JSON.stringify(trial.lineage)}</span></div>
          </div>
        </div>

        <div className="rounded-md border border-line bg-white p-3 text-sm">
          <div className="font-semibold text-slate-800">Fixed Criteria</div>
          <div className="mt-2 grid gap-1 text-xs text-slate-600">
            <div>Model Hash: <span className="font-medium text-slate-800">{modelHash as string}</span></div>
            <div>Cutoff: <span className="font-medium text-slate-800">{modelCutoff as string}</span></div>
            <div>Universe: <span className="font-medium text-slate-800">{universe as string}</span></div>
            <div className="mt-1 flex gap-2">
              <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 ${paperOnly ? "border-emerald-200 bg-emerald-50 text-mint" : "border-slate-200 bg-slate-100"}`}>
                {paperOnly ? <ShieldCheck size={12} /> : <ShieldAlert size={12} />}
                Paper Only
              </span>
              <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 ${liveDisabled ? "border-emerald-200 bg-emerald-50 text-mint" : "border-slate-200 bg-slate-100"}`}>
                {liveDisabled ? <ShieldCheck size={12} /> : <ShieldAlert size={12} />}
                Live Disabled
              </span>
            </div>
          </div>
        </div>

        <div className="rounded-md border border-line bg-white p-3 text-sm">
          <div className="font-semibold text-slate-800">Lifecycle</div>
          <div className="mt-2 grid gap-1 text-xs text-slate-600">
            <div>Started: <span className="font-medium text-slate-800">{trial.started_at ? new Date(trial.started_at).toLocaleString() : "Not started"}</span></div>
            <div>Stopped: <span className="font-medium text-slate-800">{trial.stopped_at ? new Date(trial.stopped_at).toLocaleString() : "-"}</span></div>
            {trial.blocked_reason && <div>Blocked: <span className="font-medium text-coral">{trial.blocked_reason}</span></div>}
            {trial.pause_reason && <div>Pause Reason: <span className="font-medium text-amber-600">{trial.pause_reason}</span></div>}
          </div>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="rounded-md border border-line bg-white p-3">
          <div className="flex items-center justify-between mb-2">
            <div className="font-semibold text-sm text-slate-800">Performance Metrics</div>
            {latestMetric && <div className="text-xs text-slate-500">As of {new Date(latestMetric.as_of).toLocaleDateString()} • {latestMetric.classification}</div>}
          </div>
          
          {loading ? (
            <div className="mt-2 text-xs text-slate-500">Loading metrics...</div>
          ) : latestMetric ? (
            <div className="grid grid-cols-2 gap-2 text-xs xl:grid-cols-3">
              <div className="rounded bg-panel p-2">
                <div className="text-slate-500">Closed Trades</div>
                <div className="mt-1 font-medium text-slate-800">{payload.closed_trades as number ?? "0"}</div>
              </div>
              <div className="rounded bg-panel p-2">
                <div className="text-slate-500">Win Rate</div>
                <div className="mt-1 font-medium text-slate-800">{formatNull(payload.win_rate == null ? null : Number(payload.win_rate), percent)}</div>
              </div>
              <div className="rounded bg-panel p-2">
                <div className="text-slate-500">Net PnL</div>
                <div className="mt-1 font-medium text-slate-800">{formatNull(payload.net_pnl as number, currency)}</div>
              </div>
              <div className="rounded bg-panel p-2">
                <div className="text-slate-500">Gross PnL (Prov)</div>
                <div className="mt-1 font-medium text-slate-800">{formatNull((payload.provisional_gross_pnl ?? payload.gross_pnl) as number, currency)}</div>
              </div>
              <div className="rounded bg-panel p-2">
                <div className="text-slate-500">Expectancy</div>
                <div className="mt-1 font-medium text-slate-800">{formatNull(payload.expectancy as number, currency)}</div>
              </div>
              <div className="rounded bg-panel p-2">
                <div className="text-slate-500">Max Drawdown</div>
                <div className="mt-1 font-medium text-slate-800">{formatNull(payload.max_drawdown as number, percent)}</div>
              </div>
              <div className="rounded bg-panel p-2">
                <div className="text-slate-500">Bmk Buy & Hold</div>
                <div className="mt-1 font-medium text-slate-800">{formatNull(payload.benchmark_buy_hold as number, percent)}</div>
              </div>
              <div className="rounded bg-panel p-2">
                <div className="text-slate-500">Obs / Exp</div>
                <div className="mt-1 font-medium text-slate-800">{payload.observed_observations as number ?? 0} / {payload.expected_observations as number ?? 0}</div>
              </div>
              <div className="rounded bg-panel p-2">
                <div className="text-slate-500">Sessions</div>
                <div className="mt-1 font-medium text-slate-800">{payload.observed_sessions as number ?? 0}</div>
              </div>
              <div className="rounded bg-panel p-2">
                <div className="text-slate-500">Decision Coverage</div>
                <div className="mt-1 font-medium text-slate-800">{formatNull(payload.decision_coverage == null ? null : Number(payload.decision_coverage), percent)}</div>
              </div>
              {payload.costs_known === false && (
                <div className="col-span-2 xl:col-span-3 text-amber-600 text-[11px] bg-amber-50 border border-amber-200 p-1.5 rounded mt-1">
                  Costs are unknown; performance may be optimistic.
                </div>
              )}
            </div>
          ) : (
            <div className="mt-2 text-xs text-slate-500">Metrics unavailable.</div>
          )}
        </div>

        <div className="rounded-md border border-line bg-white p-3">
          <div className="font-semibold text-sm text-slate-800">Recent Decisions</div>
          {loading ? (
            <div className="mt-2 text-xs text-slate-500">Loading decisions...</div>
          ) : decisions.length > 0 ? (
            <div className="mt-2 flex max-h-48 flex-col gap-2 overflow-y-auto pr-1">
              {decisions.map(d => (
                <div key={d.id} className="rounded border border-line p-2 text-xs">
                  <div className="flex justify-between font-medium">
                    <span className={!d.qualifying ? "text-coral" : "text-slate-800"}>{d.action} {d.symbol}</span>
                    <span className="text-slate-500">{new Date(d.bar_timestamp).toLocaleString()}</span>
                  </div>
                  {d.rejection_reason && <div className="mt-0.5 text-slate-600">Rejection: {d.rejection_reason}</div>}
                  {d.order_id && <div className="mt-0.5 text-slate-500">Order: {d.order_id}</div>}
                </div>
              ))}
            </div>
          ) : (
            <div className="mt-2 text-xs text-slate-500">No decisions recorded.</div>
          )}
        </div>
      </div>

      <RoleGate requires="operator">
        <div className="flex flex-wrap gap-2 pt-2">
          {trial.status === "approved" && (
            <button
              disabled={isBusy}
              onClick={() => handleAction(() => startForwardTrial(trial.id))}
              className="inline-flex items-center gap-1.5 rounded bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
            >
              <Play size={14} /> Start Trial
            </button>
          )}
          {trial.status === "running" && !showPauseForm && (
            <button
              disabled={isBusy}
              onClick={() => setShowPauseForm(true)}
              className="inline-flex items-center gap-1.5 rounded border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-semibold text-amber-800 disabled:opacity-50"
            >
              <Pause size={14} /> Pause
            </button>
          )}
          {showPauseForm && trial.status === "running" && (
            <div className="flex items-center gap-2 rounded border border-amber-200 bg-amber-50 p-2">
              <input 
                type="text"
                placeholder="Reason for pause..."
                value={pauseReason}
                onChange={e => setPauseReason(e.target.value)}
                className="h-7 w-48 rounded border border-amber-200 px-2 text-xs text-amber-900 placeholder:text-amber-600/50"
              />
              <button
                disabled={isBusy || !pauseReason.trim()}
                onClick={() => handleAction(() => pauseForwardTrial(trial.id, pauseReason))}
                className="rounded bg-amber-600 px-2 py-1 text-xs font-semibold text-white disabled:opacity-50"
              >
                Confirm Pause
              </button>
              <button
                disabled={isBusy}
                onClick={() => { setShowPauseForm(false); setPauseReason(""); }}
                className="rounded border border-amber-300 px-2 py-1 text-xs font-medium text-amber-700 disabled:opacity-50"
              >
                Cancel
              </button>
            </div>
          )}
          {(trial.status === "paused" || trial.status === "blocked") && (
            <button
              disabled={isBusy}
              onClick={() => handleAction(() => resumeForwardTrial(trial.id))}
              className="inline-flex items-center gap-1.5 rounded border border-emerald-300 bg-emerald-50 px-3 py-1.5 text-xs font-semibold text-mint disabled:opacity-50"
            >
              <Play size={14} /> Resume
            </button>
          )}
          {(trial.status === "running" || trial.status === "paused" || trial.status === "approved" || trial.status === "blocked") && (
            <button
              disabled={isBusy}
              onClick={() => handleAction(() => stopForwardTrial(trial.id))}
              className="inline-flex items-center gap-1.5 rounded border border-red-200 bg-red-50 px-3 py-1.5 text-xs font-semibold text-coral disabled:opacity-50"
            >
              <Square size={14} /> Stop
            </button>
          )}
        </div>
      </RoleGate>
    </div>
  );
}


export function ForwardPaperEvaluationPanel() {
  const [trials, setTrials] = useState<ForwardTrial[]>([]);
  const [bindings, setBindings] = useState<ForwardTrialBindingEligible[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [expandedTrial, setExpandedTrial] = useState<string | null>(null);
  
  const [isCreating, setIsCreating] = useState(false);
  const [createBindingId, setCreateBindingId] = useState<number | "">("");

  const loadData = useCallback(async () => {
    try {
      const [tRes, bRes] = await Promise.all([
        getForwardTrials(),
        getForwardTrialsEligibleBindings().catch(() => []) // Viewer might not have access or it might be empty
      ]);
      setTrials(tRes);
      setBindings(bRes);
      setError("");
    } catch (e) {
      setError("Failed to load forward paper evaluation data.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadData();
    const interval = setInterval(() => {
      // Poll if we have any running trials
      setTrials(current => {
        if (current.some(t => t.status === "running")) {
          void loadData();
        }
        return current;
      });
    }, 15000);
    return () => clearInterval(interval);
  }, [loadData]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!createBindingId) return;
    try {
      setIsCreating(true);
      await createForwardTrial(Number(createBindingId));
      setCreateBindingId("");
      await loadData();
    } catch (e) {
      console.error(e);
    } finally {
      setIsCreating(false);
    }
  };

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex items-center justify-between border-b border-line p-4">
        <div className="flex items-center gap-2">
          <Beaker size={19} className="text-mint" />
          <h2 className="text-base font-semibold">Forward Paper Evaluation</h2>
        </div>
        <RoleGate requires="admin">
          <div className="text-xs font-semibold text-mint bg-emerald-50 border border-emerald-200 px-2 py-1 rounded">
            Admin Approved Surface
          </div>
        </RoleGate>
      </div>

      <div className="p-4 text-sm text-slate-600 border-b border-line bg-slate-50">
        Controlled environment for gathering verifiable forward evidence on strategy models.
        Trials execute isolated paper logic with fixed lineage criteria.
      </div>

      {error && <div className="p-4 text-sm text-coral bg-red-50">{error}</div>}

      <RoleGate requires="admin">
        {bindings.length > 0 && (
          <div className="border-b border-line p-4">
            <h3 className="text-sm font-semibold text-slate-800 mb-3">Approve New Trial</h3>
            <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-3">
              <label className="grid gap-1.5 text-xs font-semibold text-slate-700">
                Eligible Binding
                <select 
                  className="h-9 w-64 rounded-md border border-line bg-white px-3 text-sm focus-ring"
                  value={createBindingId}
                  onChange={e => setCreateBindingId(e.target.value ? Number(e.target.value) : "")}
                  disabled={isCreating}
                >
                  <option value="">Select a binding...</option>
                  {bindings.filter(b => b.eligible).map(b => (
                    <option key={b.binding_id} value={b.binding_id}>
                      Binding {b.binding_id} (Model {b.model_run_id || "?"})
                    </option>
                  ))}
                </select>
              </label>

              <button
                type="submit"
                disabled={!createBindingId || isCreating}
                className="h-9 rounded-md bg-mint px-4 text-sm font-semibold text-white disabled:opacity-50 focus-ring"
              >
                {isCreating ? "Approving..." : "Approve Trial"}
              </button>
            </form>
          </div>
        )}
      </RoleGate>

      <div className="divide-y divide-line">
        {loading && trials.length === 0 ? (
          <div className="p-8 text-center text-sm text-slate-500">Loading trials...</div>
        ) : trials.length === 0 ? (
          <div className="p-8 text-center">
            <CircleOff className="mx-auto mb-2 text-slate-400" size={24} />
            <div className="text-sm font-medium text-slate-600">No forward trials active</div>
            <div className="mt-1 text-xs text-slate-500">Admin must approve a binding to begin.</div>
          </div>
        ) : (
          trials.map(trial => {
            const isExpanded = expandedTrial === trial.id;
            return (
              <div key={trial.id} className="group flex flex-col">
                <button
                  onClick={() => setExpandedTrial(isExpanded ? null : trial.id)}
                  className="flex items-center justify-between p-4 hover:bg-slate-50 transition-colors text-left"
                >
                  <div className="flex items-center gap-4">
                    {isExpanded ? <ChevronDown size={16} className="text-slate-400" /> : <ChevronRight size={16} className="text-slate-400" />}
                    <div className="flex flex-col gap-1">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-sm text-slate-900">{trial.id}</span>
                        <span className="text-sm text-slate-500">Binding {trial.binding_id}</span>
                      </div>
                      <div className="flex gap-3 text-xs text-slate-500">
                        {trial.policy?.paper_only !== false && <span className="text-mint flex items-center gap-1"><ShieldCheck size={12}/> Paper</span>}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-4">
                    <div className="text-xs text-right mr-2">
                      <div className="text-slate-500">Started</div>
                      <div className="font-medium text-slate-700">{trial.started_at ? new Date(trial.started_at).toLocaleDateString() : "-"}</div>
                    </div>
                    <StatusPill status={trial.status} />
                  </div>
                </button>
                {isExpanded && <TrialDetailView trial={trial} onActionComplete={loadData} />}
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}
