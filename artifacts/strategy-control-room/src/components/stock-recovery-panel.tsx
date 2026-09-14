import { useCallback, useEffect, useState } from "react";
import { RotateCcw, ShieldAlert, ShieldCheck, Square, Waves } from "lucide-react";

import { RoleGate } from "@/components/access-control";
import {
  acknowledgeStockPaperAccountingReview,
  cancelStockPaperRecovery,
  getErrorMessage,
  getStockPaperRecovery,
  resumeStockPaperAccount,
  rollbackStockPaperToLastKnownGood,
  type StockPaperRecoveryStatus,
} from "@/lib/api";

function formatTime(value: string | null): string {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function StockRecoveryPanel() {
  const [recovery, setRecovery] = useState<StockPaperRecoveryStatus | null>(null);
  const [message, setMessage] = useState("Loading recovery state");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setRecovery(await getStockPaperRecovery());
      setMessage("Recovery state updated");
    } catch (error) {
      setMessage(getErrorMessage(error, "Recovery state is unavailable."));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 30_000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  async function run(action: () => Promise<StockPaperRecoveryStatus>, pending: string) {
    setBusy(true);
    setMessage(pending);
    try {
      setRecovery(await action());
      setMessage("Recovery action recorded. Reconciliation and cooldown gates still apply.");
    } catch (error) {
      setMessage(getErrorMessage(error, "Recovery action failed."));
    } finally {
      setBusy(false);
    }
  }

  async function resume() {
    setBusy(true);
    setMessage("Checking cooldown, reconciliation, monitoring, and order gates before resume");
    try {
      await resumeStockPaperAccount();
      await refresh();
      setMessage("Paper recovery resumed after all safety gates passed");
    } catch (error) {
      setMessage(getErrorMessage(error, "Paper recovery could not resume."));
    } finally {
      setBusy(false);
    }
  }

  const accountHalted = recovery?.account_status === "halted" || recovery?.accounting_residual || recovery?.account_reconciliation_required;
  const needsRecovery = recovery && (accountHalted || (recovery.status !== "armed" && recovery.status !== "resumable"));
  const displayedStatus = accountHalted ? "halted" : recovery?.status ?? "loading";
  const monitoringPreflight = recovery?.last_monitoring_preflight ?? null;
  const monitoringPreflightCheckedAt = monitoringPreflight?.payload.checked_at ?? monitoringPreflight?.created_at ?? null;
  const monitoringPreflightClear = monitoringPreflight?.status === "clear";

  return (
    <section className="rounded-md border border-line bg-white" data-testid="panel-stock-recovery">
      <div className="flex flex-col gap-3 border-b border-line p-4 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            {needsRecovery ? <ShieldAlert size={19} className="text-coral" /> : <ShieldCheck size={19} className="text-mint" />}
            <h2 className="text-base font-semibold">Rollback and Recovery</h2>
            <span className="rounded border border-line bg-slate-50 px-2 py-1 text-xs font-semibold capitalize">{displayedStatus}</span>
          </div>
          <p className="mt-1 text-sm text-slate-500">
            Cancel in-flight paper orders, optionally flatten positions, and restore the recorded last-known-good model only after fresh evidence passes.
          </p>
          <p className="mt-1 text-xs text-slate-500">{message}</p>
        </div>
        <button className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-line px-3 text-sm font-medium" disabled={busy} onClick={() => void refresh()} type="button">
          <Waves size={15} /> Refresh
        </button>
      </div>
      <div className="grid gap-3 p-4 md:grid-cols-3">
        <div className="rounded-md border border-line p-3 text-xs">
          <div className="font-semibold">Cooldown</div>
          <div className="mt-1 text-slate-600">{formatTime(recovery?.cooldown_until ?? null)}</div>
        </div>
        <div className="rounded-md border border-line p-3 text-xs">
          <div className="font-semibold">Monitor heartbeat</div>
          <div className="mt-1 text-slate-600">{formatTime(recovery?.last_monitor_heartbeat_at ?? null)}</div>
        </div>
        <div className="rounded-md border border-line p-3 text-xs">
          <div className="font-semibold">Watchdog heartbeat</div>
          <div className="mt-1 text-slate-600">{formatTime(recovery?.last_watchdog_heartbeat_at ?? null)}</div>
        </div>
      </div>
      {recovery ? (
        <div className="grid gap-3 border-t border-line p-4 text-xs md:grid-cols-3">
          <div className={`rounded-md border p-3 ${recovery.accounting_residual ? "border-red-200 bg-red-50 text-red-900" : "border-emerald-200 bg-emerald-50 text-emerald-900"}`}>
            <div className="font-semibold">Accounting residual</div>
            <div className="mt-1">{recovery.accounting_residual ? "Unexplained residual remains" : recovery.accounting_verified ? "Broker accounting verified" : "No unresolved residual recorded"}</div>
          </div>
          <div className="rounded-md border border-line p-3">
            <div className="font-semibold">Account state</div>
            <div className="mt-1 text-slate-600">{recovery.account_status}</div>
            <div className="mt-1 text-slate-500">{recovery.account_reconciliation_required ? "Fresh reconciliation required" : recovery.costs_known ? "Reported costs complete" : "Reported costs remain unknown"}</div>
          </div>
          <div className="rounded-md border border-line p-3">
            <div className="font-semibold">Broker halt reason</div>
            <div className="mt-1 text-slate-600">{recovery.account_halt_reason ?? "None recorded"}</div>
          </div>
        </div>
      ) : null}
      <RoleGate requires="operator" className="flex flex-wrap gap-2 border-t border-line p-4">
        {monitoringPreflight ? (
          <div
            className={`w-full rounded-md border p-3 text-xs ${monitoringPreflightClear ? "border-emerald-300 bg-emerald-50 text-emerald-950" : "border-red-300 bg-red-50 text-red-950"}`}
            data-testid={monitoringPreflightClear ? "monitoring-preflight-clear" : "monitoring-preflight-block"}
          >
            <div className="flex items-center gap-2 font-semibold">
              {monitoringPreflightClear ? <ShieldCheck size={15} /> : <ShieldAlert size={15} />}
              {monitoringPreflightClear ? "Fresh monitoring evidence cleared the recovery block" : "Monitoring preflight blocked recovery"}
            </div>
            <div className="mt-2 grid gap-1 sm:grid-cols-3">
              <div><span className="font-semibold">Status:</span> {monitoringPreflight.status}</div>
              <div><span className="font-semibold">Checked:</span> {formatTime(monitoringPreflightCheckedAt)}</div>
              <div className="sm:col-span-3"><span className="font-semibold">Reason:</span> {monitoringPreflight.reason || "No reason recorded"}</div>
            </div>
            {monitoringPreflightClear ? <p className="mt-2">The monitoring gate is clear; remaining recovery safety gates and the server decision still control resume.</p> : null}
          </div>
        ) : null}
        {recovery?.accounting_review_required ? (
          <div className="w-full rounded-md border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950">
            <div className="font-semibold">Automatic accounting review is blocked</div>
            <p className="mt-1">Paper trading stays halted until a later, timestamped Alpaca fill with a known commission exactly explains the residual. Stable balances or operator confirmation do not bypass this gate.</p>
            {recovery.accounting_review_reason ? <p className="mt-2 text-amber-900">{recovery.accounting_review_reason}</p> : null}
          </div>
        ) : null}
        {!recovery?.accounting_review_required && recovery?.automatic_review_status === "complete" ? (
          <div className="w-full rounded-md border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-950">
            <div className="font-semibold">Automatic accounting review completed</div>
            <p className="mt-1">The residual was resolved from broker-complete evidence. Cooldown, fresh monitoring, reconciliation, and in-flight-order gates still control resume.</p>
          </div>
        ) : null}
        <button
          className="focus-ring inline-flex h-9 items-center gap-2 rounded-md bg-coral px-3 text-sm font-semibold text-white disabled:opacity-60"
          disabled={busy}
          onClick={() => void run(() => cancelStockPaperRecovery("none", "Operator requested atomic cancellation of in-flight stock-paper orders"), "Cancelling every known in-flight paper order")}
          type="button"
        >
          <Square size={14} /> Cancel orders
        </button>
        <button
          className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-coral/30 bg-red-50 px-3 text-sm font-semibold text-coral disabled:opacity-60"
          disabled={busy}
          onClick={() => void run(() => cancelStockPaperRecovery("positions", "Operator requested cancellation and position flattening"), "Cancelling orders and preparing position flattening")}
          type="button"
        >
          <ShieldAlert size={14} /> Cancel and flatten
        </button>
        <button
          className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 text-sm font-semibold text-mint disabled:opacity-60"
          disabled={busy || !recovery?.last_known_good_model_run_id}
          onClick={() => void run(() => rollbackStockPaperToLastKnownGood("Operator requested last-known-good model rollback"), "Restoring the last-known-good model binding")}
          type="button"
        >
          <RotateCcw size={14} /> Roll back model
        </button>
      </RoleGate>
      <RoleGate requires="admin" className="flex flex-wrap gap-2 border-t border-line p-4">
        {recovery && ["cooldown", "revalidation_required", "resumable"].includes(recovery.status) ? (
          <button
            className="focus-ring inline-flex h-9 items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 text-sm font-semibold text-mint disabled:opacity-60"
            disabled={busy}
            onClick={() => void resume()}
            type="button"
          >
            <ShieldCheck size={14} /> Resume paper recovery
          </button>
        ) : null}
      </RoleGate>
      {recovery?.pause_reason ? <div className="border-t border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900"><strong>Recovery reason:</strong> {recovery.pause_reason}</div> : null}
    </section>
  );
}