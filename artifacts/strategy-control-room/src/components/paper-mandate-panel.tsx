import { useCallback, useEffect, useState } from "react";
import { FileCheck2, ShieldOff } from "lucide-react";

import { RoleGate } from "@/components/access-control";
import {
  createPaperMandate,
  getErrorMessage,
  getPaperMandate,
  revokePaperMandate,
  type PaperMandateRequest,
  type PaperMandateStatus,
} from "@/lib/api";

function formatTime(value: string | null | undefined): string {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function statusClass(status: string | undefined): string {
  if (status === "active") return "border-emerald-200 bg-emerald-50 text-emerald-900";
  if (status === "pending") return "border-line bg-slate-50 text-slate-700";
  return "border-amber-200 bg-amber-50 text-amber-900";
}

function defaultExpiry(): string {
  const date = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);
  return date.toISOString().slice(0, 10);
}

type Draft = {
  symbols: string;
  maxNotional: string;
  maxRiskPerTrade: string;
  maxLoss: string;
  maxSessionsPerCycle: string;
  maxSessions: string;
  expiresOn: string;
  allowReduce: boolean;
  allowFlatten: boolean;
  reason: string;
};

const INITIAL_DRAFT: Draft = {
  symbols: "SPY, QQQ",
  maxNotional: "10000",
  maxRiskPerTrade: "0.0025",
  maxLoss: "0.02",
  maxSessionsPerCycle: "20",
  maxSessions: "60",
  expiresOn: defaultExpiry(),
  allowReduce: false,
  allowFlatten: false,
  reason: "",
};

export function mandateRequestFromDraft(draft: Draft): PaperMandateRequest {
  const policies: PaperMandateRequest["remaining_position_policies"] = ["hold"];
  if (draft.allowReduce) policies.push("reduce");
  if (draft.allowFlatten) policies.push("flatten");
  return {
    symbols: draft.symbols.split(/[\s,]+/).map((symbol) => symbol.trim().toUpperCase()).filter(Boolean),
    exposure_limits: { max_allocated_notional: draft.maxNotional.trim(), max_risk_per_trade: draft.maxRiskPerTrade.trim() },
    loss_limits: { max_loss: draft.maxLoss.trim(), unit: "fraction_of_baseline_equity", currency: "USD" },
    triggers: ["scheduled"],
    remaining_position_policies: policies,
    max_sessions_per_cycle: Number(draft.maxSessionsPerCycle),
    max_sessions: Number(draft.maxSessions),
    expires_at: new Date(`${draft.expiresOn}T21:00:00Z`).toISOString(),
    reason: draft.reason.trim(),
  };
}

/**
 * Standing paper mandate: one admin decision that lets the scheduler mint each
 * cycle's exact paper-run approval. Cycles outside the mandate still defer.
 */
export function PaperMandatePanel() {
  const [mandate, setMandate] = useState<PaperMandateStatus | null>(null);
  const [message, setMessage] = useState("Loading standing paper mandate");
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<Draft>(INITIAL_DRAFT);
  const [revokeReason, setRevokeReason] = useState("");

  const refresh = useCallback(async () => {
    try {
      setMandate(await getPaperMandate());
      setMessage("Mandate status updated");
    } catch (error) {
      setMessage(getErrorMessage(error, "Standing mandate status is unavailable."));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 60_000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  async function submit(action: () => Promise<PaperMandateStatus>, done: string) {
    setBusy(true);
    try {
      setMandate(await action());
      setMessage(done);
    } catch (error) {
      setMessage(getErrorMessage(error, "Mandate action failed."));
    } finally {
      setBusy(false);
    }
  }

  const current = mandate?.current ?? null;
  const next = mandate?.next_auto_minted_approval ?? null;
  const update = (patch: Partial<Draft>) => setDraft((value) => ({ ...value, ...patch }));
  const field = "focus-ring h-8 rounded border border-line px-2 text-xs";

  return (
    <div className="mt-4 rounded border border-line p-3" data-testid="paper-mandate-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <FileCheck2 size={16} className="text-mint" />
          <div>
            <div className="text-sm font-semibold text-ink">Standing paper mandate</div>
            <div className="text-xs text-slate-500">The scheduler mints each cycle's exact paper approval only while the cycle fits inside this mandate. Paper only; never live.</div>
          </div>
        </div>
        <span className={`rounded border px-2 py-1 text-xs font-semibold capitalize ${statusClass(current?.status)}`} data-testid="paper-mandate-status">
          {current ? current.status : "none recorded"}
        </span>
      </div>

      {current ? (
        <div className="mt-3 grid gap-2 text-xs md:grid-cols-4">
          <div><span className="font-semibold">Symbols:</span> {current.symbols.join(", ")}</div>
          <div><span className="font-semibold">Notional cap:</span> {current.exposure_limits.max_allocated_notional ?? "—"}</div>
          <div><span className="font-semibold">Loss cap:</span> {current.loss_limits.max_loss ?? "—"} of baseline equity</div>
          <div><span className="font-semibold">Expires:</span> {formatTime(current.expires_at)}</div>
          <div><span className="font-semibold">Sessions:</span> {current.sessions_used} used / {current.max_sessions} ({current.sessions_remaining} left)</div>
          <div><span className="font-semibold">Per cycle:</span> up to {current.max_sessions_per_cycle} sessions</div>
          <div><span className="font-semibold">Position policies:</span> {current.remaining_position_policies.join(", ")}</div>
          <div><span className="font-semibold">Recorded by:</span> {current.created_by}</div>
          {current.reason ? <div className="md:col-span-4 text-amber-800">{current.reason}</div> : null}
          {current.revoked_at ? (
            <div className="md:col-span-4 text-amber-800">Revoked by {current.revoked_by} at {formatTime(current.revoked_at)}: {current.revocation_reason}</div>
          ) : null}
        </div>
      ) : (
        <p className="mt-3 text-xs text-slate-600">Without a mandate, each scheduled handoff waits for a manually recorded paper-run approval.</p>
      )}

      <div className="mt-3 rounded border border-line bg-slate-50 p-2 text-xs" data-testid="paper-mandate-next-mint">
        <div className="font-semibold">Next auto-minted approval</div>
        {next ? (
          <div className="mt-1 text-slate-700">
            Cycle {next.cycle_id.slice(0, 12)} ({next.cycle_status.replaceAll("_", " ")}) ·{" "}
            {next.approval_status === "pass"
              ? "approval already in place"
              : next.would_mint
                ? "fits the mandate; the scheduler will mint it at handoff"
                : `will defer: ${next.reasons.join("; ")}`}
          </div>
        ) : (
          <div className="mt-1 text-slate-500">No cycle is currently waiting for a paper handoff.</div>
        )}
        {mandate && mandate.minted_approvals.length ? (
          <ul className="mt-2 grid gap-0.5 text-slate-600">
            {mandate.minted_approvals.slice(0, 5).map((row) => (
              <li key={row.approval_id}>
                Approval #{row.approval_id} for cycle {row.cycle_id.slice(0, 12)} · {row.duration_sessions} sessions · {formatTime(row.created_at)}
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      <RoleGate requires="admin" className="mt-3 grid gap-3">
        {current && (current.status === "active" || current.status === "pending") ? (
          <div className="flex flex-wrap items-center gap-2">
            <input className={`${field} min-w-64 flex-1`} placeholder="Reason for revoking" value={revokeReason} onChange={(event) => setRevokeReason(event.target.value)} />
            <button
              className="focus-ring inline-flex h-8 items-center gap-1 rounded border border-coral/30 bg-red-50 px-2.5 text-xs font-semibold text-coral disabled:opacity-50"
              disabled={busy || revokeReason.trim().length < 3}
              onClick={() => void submit(() => revokePaperMandate(current.id, revokeReason.trim()), "Mandate revoked; scheduled handoffs will defer again")}
              type="button"
            >
              <ShieldOff size={13} /> Revoke mandate
            </button>
          </div>
        ) : null}
        <details className="text-xs">
          <summary className="cursor-pointer font-semibold">{current ? "Record a replacement mandate" : "Record a standing mandate"}</summary>
          <div className="mt-2 grid gap-2 md:grid-cols-4">
            <label className="grid gap-1 md:col-span-2">Symbols<input className={field} value={draft.symbols} onChange={(event) => update({ symbols: event.target.value })} /></label>
            <label className="grid gap-1">Max notional (USD)<input className={field} value={draft.maxNotional} onChange={(event) => update({ maxNotional: event.target.value })} /></label>
            <label className="grid gap-1">Max risk per trade<input className={field} value={draft.maxRiskPerTrade} onChange={(event) => update({ maxRiskPerTrade: event.target.value })} /></label>
            <label className="grid gap-1">Max loss (fraction)<input className={field} value={draft.maxLoss} onChange={(event) => update({ maxLoss: event.target.value })} /></label>
            <label className="grid gap-1">Sessions per cycle<input className={field} inputMode="numeric" value={draft.maxSessionsPerCycle} onChange={(event) => update({ maxSessionsPerCycle: event.target.value })} /></label>
            <label className="grid gap-1">Total sessions<input className={field} inputMode="numeric" value={draft.maxSessions} onChange={(event) => update({ maxSessions: event.target.value })} /></label>
            <label className="grid gap-1">Expires on<input className={field} type="date" value={draft.expiresOn} onChange={(event) => update({ expiresOn: event.target.value })} /></label>
            <label className="flex items-center gap-1"><input type="checkbox" checked={draft.allowReduce} onChange={(event) => update({ allowReduce: event.target.checked })} /> Allow reduce at end</label>
            <label className="flex items-center gap-1"><input type="checkbox" checked={draft.allowFlatten} onChange={(event) => update({ allowFlatten: event.target.checked })} /> Allow flatten at end</label>
            <label className="grid gap-1 md:col-span-2">Reason<input className={field} value={draft.reason} onChange={(event) => update({ reason: event.target.value })} /></label>
          </div>
          <button
            className="focus-ring mt-2 rounded bg-mint px-2.5 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
            disabled={busy || draft.reason.trim().length < 3}
            onClick={() => void submit(() => createPaperMandate(mandateRequestFromDraft(draft)), "Standing mandate recorded")}
            type="button"
          >
            Record mandate
          </button>
        </details>
      </RoleGate>
      <div className="mt-2 text-xs text-slate-500">{message}</div>
    </div>
  );
}
