import { useEffect, useState } from "react";
import { CircleHelp, RefreshCw, ShieldOff, Timer } from "lucide-react";

import { RoleGate } from "@/components/access-control";
import {
  createLiveMandate,
  getErrorMessage,
  getLiveMandate,
  recordLiveMandateComplianceBlock,
  reinstateLiveMandate,
  revokeLiveMandate,
  type LiveMandateEvent,
  type LiveMandateSnapshot,
} from "@/lib/api";

function time(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "Not set";
}

const STATE_TONE: Record<string, string> = {
  active: "border-mint/50 text-mint",
  pending: "border-line text-slate-600",
  suspended: "border-coral/50 text-coral",
  revoked: "border-coral/50 text-coral",
  expired: "border-line text-slate-500",
  superseded: "border-line text-slate-500",
};

function EventRow({ event }: { event: LiveMandateEvent }) {
  const detail = event.triggers?.join(", ") || event.failing_gates?.join(", ");
  return (
    <li className="border-b border-line py-2 last:border-b-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-semibold">{event.action.replace(/_/g, " ")}</span>
        <span className="text-xs text-slate-500">{time(event.created_at)}</span>
      </div>
      <div className="text-slate-600">{event.reason}</div>
      <div className="text-xs text-slate-500">
        Mandate {event.mandate_id} · {event.actor}{event.secondary_actor ? ` + ${event.secondary_actor}` : ""}
        {detail ? ` · ${detail}` : ""}
      </div>
    </li>
  );
}

export function LiveMandatePanel() {
  const [snapshot, setSnapshot] = useState<LiveMandateSnapshot | null>(null);
  const [status, setStatus] = useState("Loading standing live mandate");
  const [busy, setBusy] = useState(false);
  const [secondary, setSecondary] = useState("");
  const [assertion, setAssertion] = useState("");
  const [reason, setReason] = useState("");
  const [model, setModel] = useState("");
  const [symbols, setSymbols] = useState("SPY");
  const [budget, setBudget] = useState("5000");
  const [orderCap, setOrderCap] = useState("500");
  const [lossCap, setLossCap] = useState("500");
  const [drawdown, setDrawdown] = useState("0.05");
  const [pilotDays, setPilotDays] = useState(7);
  const [outerDays, setOuterDays] = useState(90);
  const [compliance, setCompliance] = useState("");

  async function refresh() {
    try {
      setSnapshot(await getLiveMandate());
      setStatus("Mandate state refreshed");
    } catch (error) {
      setStatus(getErrorMessage(error, "Mandate state unavailable"));
    }
  }

  useEffect(() => { void refresh(); }, []);

  async function act(action: () => Promise<LiveMandateSnapshot>, message: string) {
    setBusy(true);
    try {
      setSnapshot(await action());
      setStatus(message);
    } catch (error) {
      setStatus(getErrorMessage(error, "Mandate action blocked"));
    } finally {
      setBusy(false);
    }
  }

  function submitMandate(amend: boolean) {
    let record: Record<string, unknown>;
    try {
      record = JSON.parse(compliance) as Record<string, unknown>;
    } catch {
      setStatus("The compliance record must be valid JSON");
      return;
    }
    const expires = new Date(Date.now() + outerDays * 24 * 60 * 60 * 1000);
    void act(() => createLiveMandate({
      symbols: symbols.split(",").map(item => item.trim()).filter(Boolean),
      max_notional: budget,
      max_order_notional: orderCap,
      max_pilot_days: pilotDays,
      renewal_lead_hours: 24,
      observation_window_sessions: 5,
      max_cumulative_loss: lossCap,
      max_drawdown: drawdown,
      rollback_target: "paper",
      model_run_id: model,
      paper_expectations: { preregistered: true },
      checklist: {
        drills: { emergency_stop: true, rollback: true, broker_uncertainty: true, worker_loss: true, model_demotion: true },
        evidence_references: ["control-room-operator-checklist"],
        compliance_readiness: record,
      },
      expires_at: expires.toISOString(),
      secondary_approval_actor: secondary,
      reason,
    }, amend, assertion), amend ? "Mandate amendment recorded" : "Standing mandate recorded");
  }

  const current = snapshot?.current;
  const last = snapshot?.last_renewal_evaluation;
  const canWrite = !busy && !!secondary && reason.length >= 3;

  return (
    <section className="rounded-md border border-line bg-white" data-testid="live-mandate-panel">
      <div className="flex items-start justify-between gap-3 border-b border-line p-4">
        <div>
          <div className="flex items-center gap-2">
            <Timer size={18} className="text-slate-600" />
            <h2 className="text-base font-semibold">Standing Live Mandate</h2>
          </div>
          <p className="mt-1 text-sm text-slate-500">{status}</p>
        </div>
        <div className="flex items-center gap-2">
          <button className="focus-ring rounded-md border border-line p-1.5" onClick={() => void refresh()} type="button" aria-label="Refresh mandate">
            <RefreshCw size={14} />
          </button>
          <span className={`rounded-md border px-2 py-1 text-xs font-semibold uppercase ${STATE_TONE[current?.state ?? ""] ?? "border-line"}`} data-testid="live-mandate-state">
            {current?.state ?? "none"}
          </span>
        </div>
      </div>

      {current ? (
        <div className="grid gap-3 p-4 text-sm md:grid-cols-3">
          <div className="rounded-md border border-line p-3">
            <div className="font-semibold">Mandate {current.id} bounds</div>
            <div className="mt-1 text-slate-600">Symbols: {current.symbols.join(", ")}</div>
            <div className="text-slate-600">Budget {current.max_notional} · order cap {current.max_order_notional} · limit only</div>
            <div className="text-slate-600">Loss cap {current.max_cumulative_loss} · drawdown cap {current.max_drawdown}</div>
            <div className="text-slate-600">Pilot window ≤ {current.max_pilot_days} days · rollback to {current.rollback_target}</div>
            <div className="text-slate-600">Outer expiry: {time(current.expires_at)}</div>
            <div className="text-slate-600">Approvers: {current.primary_approval_actor} + {current.secondary_approval_actor}</div>
            {current.state_reason ? <div className="mt-1 text-coral">{current.state_reason}</div> : null}
          </div>
          <div className="rounded-md border border-line p-3">
            <div className="font-semibold">Renewal</div>
            <div className="mt-1 text-slate-600">Pilot: {snapshot?.pilot.status} {snapshot?.pilot.mandate_id ? `(mandate ${snapshot.pilot.mandate_id})` : ""}</div>
            <div className="text-slate-600">Window ends: {time(snapshot?.pilot.expires_at)}</div>
            <div className="text-slate-600" data-testid="live-mandate-next-renewal">
              Next renewal: {snapshot?.next_renewal_at ? time(snapshot.next_renewal_at) : `evaluated every ${snapshot?.evaluation_cadence_minutes ?? 5} minutes`}
            </div>
            {snapshot?.pilot.completed_observation_sessions != null ? (
              <div className="text-slate-600">Canary sessions: {snapshot.pilot.completed_observation_sessions} / {current.observation_window_sessions}</div>
            ) : null}
            <div className="text-slate-600">
              Cumulative loss: {snapshot?.cumulative_loss?.status === "measured" ? `${snapshot.cumulative_loss.loss} of ${snapshot.cumulative_loss.cap}` : "unknown"}
            </div>
            <div className="text-slate-600">
              Drawdown: {snapshot?.drawdown?.status === "measured" ? `${snapshot.drawdown.worst} of ${snapshot.drawdown.cap}` : "unknown"}
            </div>
          </div>
          <div className="rounded-md border border-line p-3">
            <div className="font-semibold">Last renewal evaluation</div>
            {last ? (
              <>
                <div className="mt-1 text-slate-600">{last.action.replace(/_/g, " ")} · {time(last.created_at)}</div>
                <div className="text-slate-600">{last.reason}</div>
                {last.failing_gates?.length ? <div className="text-coral">Not green: {last.failing_gates.join(", ")}</div> : null}
              </>
            ) : <div className="mt-1 text-slate-500">No evaluation recorded yet.</div>}
            {snapshot?.active_triggers.length ? (
              <div className="mt-2 text-coral">
                <ShieldOff size={14} className="mr-1 inline" />
                {snapshot.active_triggers.map(item => item.trigger).join(", ")}
              </div>
            ) : null}
          </div>
        </div>
      ) : (
        <div className="p-4 text-sm text-slate-500">
          <CircleHelp size={14} className="mr-1 inline" />
          No standing mandate. Live authority exists only through a manual two-person pilot.
        </div>
      )}

      <div className="border-t border-line p-4 text-sm">
        <div className="mb-1 font-semibold">Revocation history</div>
        {snapshot?.revocation_history.length ? (
          <ul data-testid="live-mandate-revocations">
            {snapshot.revocation_history.map(event => <EventRow key={event.id} event={event} />)}
          </ul>
        ) : <div className="text-slate-500">No suspensions, revocations or lapses recorded.</div>}
      </div>

      <RoleGate requires="operator" className="border-t border-line p-4">
        <div className="mb-2 text-sm font-semibold">Compliance stop</div>
        <div className="flex flex-wrap gap-2">
          <input className="focus-ring min-w-64 flex-1 rounded-md border border-line px-3 py-2 text-sm" placeholder="Reason" value={reason} onChange={event => setReason(event.target.value)} />
          <button className="focus-ring rounded-md border border-coral/40 px-3 py-2 text-sm font-semibold text-coral" disabled={busy || reason.length < 3 || !current || !["active", "pending", "suspended"].includes(current.state)} onClick={() => void act(() => recordLiveMandateComplianceBlock(reason), "Compliance block recorded; live authority ended")} type="button">
            Record compliance blocked
          </button>
        </div>
      </RoleGate>

      <RoleGate requires="admin" className="border-t border-line p-4">
        <div className="mb-2 text-sm font-semibold">Two-person mandate controls</div>
        <div className="grid gap-2 md:grid-cols-4">
          <input className="focus-ring rounded-md border border-line px-3 py-2 text-sm" placeholder="Second approver identity" value={secondary} onChange={event => setSecondary(event.target.value)} />
          <input className="focus-ring rounded-md border border-line px-3 py-2 text-sm" placeholder="Model run id" value={model} onChange={event => setModel(event.target.value)} />
          <input className="focus-ring rounded-md border border-line px-3 py-2 text-sm" placeholder="Symbols (comma separated)" value={symbols} onChange={event => setSymbols(event.target.value)} />
          <input className="focus-ring rounded-md border border-line px-3 py-2 text-sm" placeholder="Reason" value={reason} onChange={event => setReason(event.target.value)} />
          <label className="text-xs text-slate-500">Budget<input className="focus-ring mt-1 w-full rounded-md border border-line px-3 py-2 text-sm text-ink" value={budget} onChange={event => setBudget(event.target.value)} /></label>
          <label className="text-xs text-slate-500">Per-order cap<input className="focus-ring mt-1 w-full rounded-md border border-line px-3 py-2 text-sm text-ink" value={orderCap} onChange={event => setOrderCap(event.target.value)} /></label>
          <label className="text-xs text-slate-500">Cumulative loss cap<input className="focus-ring mt-1 w-full rounded-md border border-line px-3 py-2 text-sm text-ink" value={lossCap} onChange={event => setLossCap(event.target.value)} /></label>
          <label className="text-xs text-slate-500">Drawdown cap (fraction)<input className="focus-ring mt-1 w-full rounded-md border border-line px-3 py-2 text-sm text-ink" value={drawdown} onChange={event => setDrawdown(event.target.value)} /></label>
          <label className="text-xs text-slate-500">Days per pilot window<input type="number" min={1} max={30} className="focus-ring mt-1 w-full rounded-md border border-line px-3 py-2 text-sm text-ink" value={pilotDays} onChange={event => setPilotDays(Number(event.target.value))} /></label>
          <label className="text-xs text-slate-500">Outer expiry (days, ≤180)<input type="number" min={1} max={180} className="focus-ring mt-1 w-full rounded-md border border-line px-3 py-2 text-sm text-ink" value={outerDays} onChange={event => setOuterDays(Number(event.target.value))} /></label>
        </div>
        <input type="password" autoComplete="off" className="focus-ring mt-2 w-full rounded-md border border-line px-3 py-2 text-sm" placeholder="Second approver's signed identity assertion (required in production and approved-live)" value={assertion} onChange={event => setAssertion(event.target.value)} />
        <textarea className="focus-ring mt-2 h-20 w-full rounded-md border border-line px-3 py-2 font-mono text-xs" placeholder="Human-determined compliance record (JSON)" value={compliance} onChange={event => setCompliance(event.target.value)} />
        <div className="mt-2 flex flex-wrap gap-2">
          <button className="focus-ring rounded-md border border-line px-3 py-2 text-sm font-semibold" disabled={!canWrite || !model || !compliance} onClick={() => submitMandate(false)} type="button">Approve mandate</button>
          <button className="focus-ring rounded-md border border-line px-3 py-2 text-sm font-semibold" disabled={!canWrite || !model || !compliance || !current || !["active", "pending"].includes(current.state)} onClick={() => submitMandate(true)} type="button">Amend mandate</button>
          <button className="focus-ring rounded-md border border-line px-3 py-2 text-sm font-semibold" disabled={!canWrite || current?.state !== "suspended"} onClick={() => void act(() => reinstateLiveMandate({ secondary_approval_actor: secondary, reason }, assertion), "Mandate reinstated")} type="button">Reinstate</button>
          <button className="focus-ring rounded-md border border-coral/40 px-3 py-2 text-sm font-semibold text-coral" disabled={!canWrite || !current || !["active", "pending", "suspended"].includes(current.state)} onClick={() => void act(() => revokeLiveMandate({ secondary_approval_actor: secondary, reason }, assertion), "Mandate revoked; live authority ended")} type="button">Revoke</button>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          Approving a mandate never enables live trading by itself: the deployment must allow live trading and every launch gate must be green at each renewal.
        </p>
      </RoleGate>
    </section>
  );
}
