import { useEffect, useState } from "react";
import { CircleHelp, ShieldAlert, ShieldCheck } from "lucide-react";

import { RoleGate } from "@/components/access-control";
import {
  activateLivePilot,
  getErrorMessage,
  getLivePilot,
  promoteLivePilot,
  rollbackLivePilot,
  reviewLivePilot,
  stopLivePilot,
  type LivePilot,
} from "@/lib/api";

function time(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : "Not set";
}

export function LivePilotPanel() {
  const [pilot, setPilot] = useState<LivePilot | null>(null);
  const [status, setStatus] = useState("Loading pilot controls");
  const [busy, setBusy] = useState(false);
  const [secondary, setSecondary] = useState("");
  const [model, setModel] = useState("");
  const [reason, setReason] = useState("");

  async function refresh() {
    try {
      setPilot(await getLivePilot());
      setStatus("Pilot state refreshed");
    } catch (error) {
      setStatus(getErrorMessage(error, "Pilot state unavailable"));
    }
  }

  useEffect(() => { void refresh(); }, []);

  async function act(action: () => Promise<LivePilot>, message: string) {
    setBusy(true);
    try {
      setPilot(await action());
      setStatus(message);
    } catch (error) {
      setStatus(getErrorMessage(error, "Pilot action blocked"));
    } finally {
      setBusy(false);
    }
  }

  const launchChecklist = {
    drills: { emergency_stop: true, rollback: true, broker_uncertainty: true, worker_loss: true, model_demotion: true },
    evidence_references: ["control-room-operator-checklist"],
  };

  return (
    <section className="rounded-md border border-line bg-white" data-testid="live-pilot-panel">
      <div className="flex items-start justify-between gap-3 border-b border-line p-4">
        <div>
          <div className="flex items-center gap-2">
            {pilot?.active ? <ShieldAlert size={18} className="text-coral" /> : <ShieldCheck size={18} className="text-mint" />}
            <h2 className="text-base font-semibold">Controlled Live Pilot</h2>
          </div>
          <p className="mt-1 text-sm text-slate-500">{status}</p>
        </div>
        <span className="rounded-md border border-line px-2 py-1 text-xs font-semibold uppercase">{pilot?.status ?? "unknown"}</span>
      </div>
      <div className="grid gap-3 p-4 text-sm md:grid-cols-2">
        <div className="rounded-md border border-line p-3">
          <div className="font-semibold">Exposure boundary</div>
          <div className="mt-1 text-slate-600">Symbols: {pilot?.symbols.join(", ") || "none"}</div>
          <div className="text-slate-600">Budget: {pilot?.max_notional ?? "0"} · Order cap: {pilot?.max_order_notional ?? "0"}</div>
          <div className="text-slate-600">Regular sessions · limit/day only</div>
          <div className="text-slate-600">Window: {time(pilot?.starts_at)} → {time(pilot?.expires_at)}</div>
        </div>
        <div className="rounded-md border border-line p-3">
          <div className="font-semibold">Separation of duties</div>
          <div className="mt-1 text-slate-600">Primary: {pilot?.primary_approval_actor ?? "not approved"}</div>
          <div className="text-slate-600">Secondary: {pilot?.secondary_approval_actor ?? "not approved"}</div>
          <div className="text-slate-600">Rollback target: {pilot?.rollback_target ?? "paper"}</div>
          <div className="text-slate-600">Live orders remain blocked until approved-live.</div>
        </div>
      </div>
      <RoleGate requires="admin" className="border-t border-line p-4">
        <div className="mb-2 text-sm font-semibold">Two-person pilot controls</div>
        <div className="grid gap-2 md:grid-cols-3">
          <input className="focus-ring rounded-md border border-line px-3 py-2 text-sm" placeholder="Model run id" value={model} onChange={event => setModel(event.target.value)} />
          <input className="focus-ring rounded-md border border-line px-3 py-2 text-sm" placeholder="Second approver identity" value={secondary} onChange={event => setSecondary(event.target.value)} />
          <input className="focus-ring rounded-md border border-line px-3 py-2 text-sm" placeholder="Reason" value={reason} onChange={event => setReason(event.target.value)} />
        </div>
        <div className="mt-2 flex flex-wrap gap-2">
          <button className="focus-ring rounded-md border border-line px-3 py-2 text-sm font-semibold" disabled={busy || !model || !secondary || !reason} onClick={() => {
            const starts = new Date();
            const expires = new Date(starts.getTime() + 7 * 24 * 60 * 60 * 1000);
            void act(() => activateLivePilot({
              symbols: ["SPY"], max_notional: "10000", max_order_notional: "1000",
              starts_at: starts.toISOString(), expires_at: expires.toISOString(),
              observation_window_sessions: 5, rollback_target: "paper", model_run_id: model,
              paper_expectations: { preregistered: true }, checklist: launchChecklist,
              secondary_approval_actor: secondary, reason,
            }), "Activation request recorded or blocked by the launch gate");
          }} type="button">Start canary</button>
          <button className="focus-ring rounded-md border border-line px-3 py-2 text-sm font-semibold" disabled={busy || pilot?.status !== "canary" || !secondary || !reason} onClick={() => void act(() => promoteLivePilot({ secondary_approval_actor: secondary, reason }), "Promotion request recorded or blocked")}>Approve pilot</button>
        </div>
      </RoleGate>
      <RoleGate requires="operator" className="border-t border-line p-4">
        {pilot?.status === "halted" ? (
          <input className="focus-ring mb-2 w-full rounded-md border border-line px-3 py-2 text-sm" placeholder="Second approver for emergency-stop reset" value={secondary} onChange={event => setSecondary(event.target.value)} />
        ) : null}
        <div className="flex flex-wrap gap-2">
          <button className="focus-ring rounded-md border border-coral/40 px-3 py-2 text-sm font-semibold text-coral" disabled={busy || !reason || !pilot?.active} onClick={() => void act(() => stopLivePilot(reason), "Emergency stop recorded")}>Emergency stop</button>
          <button className="focus-ring rounded-md border border-line px-3 py-2 text-sm font-semibold" disabled={busy || !reason || (pilot?.status === "halted" && !secondary) || !["active", "canary", "halted", "expired"].includes(pilot?.status ?? "")} onClick={() => void act(() => rollbackLivePilot(reason, secondary || undefined), "Pilot rolled back to paper/shadow")}>Rollback</button>
          <button className="focus-ring rounded-md border border-line px-3 py-2 text-sm font-semibold" disabled={busy || !reason || !pilot} onClick={() => void act(() => reviewLivePilot(reason), "Pilot review recorded without changing model or budget")}>Record review</button>
        </div>
      </RoleGate>
      {!pilot ? <div className="border-t border-line p-4 text-sm text-slate-500"><CircleHelp size={14} className="mr-1 inline" />No pilot evidence is available.</div> : null}
    </section>
  );
}