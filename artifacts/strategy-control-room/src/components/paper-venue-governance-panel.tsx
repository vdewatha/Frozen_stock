import { useEffect, useState } from "react";
import { ClipboardCheck, ShieldCheck } from "lucide-react";

import { RoleGate } from "@/components/access-control";
import {
  activatePaperVenue,
  getErrorMessage,
  getPaperVenueQualification,
  getStockPaperStatus,
  submitPaperVenueQualification,
  type PaperVenueQualificationStatus,
  type StockPaperStatus,
} from "@/lib/api";

const evidenceTemplate = JSON.stringify({
  account_identity: { present: false, matches_configured_binding: false },
  orders: { complete: false },
  executions: { complete: false },
  commissions: {
    complete: false,
    policy_name: "alpaca_paper_commission_free_plus_regulatory_fees",
    policy_acknowledged: false,
    reported_per_fill: false,
    all_in_costs_verified: false,
    account_level_fees_recorded: false,
  },
  cash_activities: { complete: false },
  timestamps: { precise_utc: false },
  pagination: { orders_complete: false, activities_complete: false },
  delayed_events: { captured: false },
  restart_replay: { activities_preserved: false, no_duplicates: false },
  credential_replay: { activities_preserved: false, no_duplicates: false },
  report_period: { start: "", end: "" },
  provider_contract: { name: "alpaca-paper-api", read_paths: ["account", "orders", "activities"] },
}, null, 2);

export function PaperVenueGovernancePanel() {
  const [paper, setPaper] = useState<StockPaperStatus | null>(null);
  const [qualification, setQualification] = useState<PaperVenueQualificationStatus | null>(null);
  const [evidence, setEvidence] = useState(evidenceTemplate);
  const [activationReason, setActivationReason] = useState("");
  const [message, setMessage] = useState("Loading venue evidence");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const [paperStatus, venueStatus] = await Promise.all([
      getStockPaperStatus(),
      getPaperVenueQualification(),
    ]);
    setPaper(paperStatus);
    setQualification(venueStatus);
    setMessage(venueStatus.reason);
  }

  useEffect(() => {
    let active = true;
    refresh().catch(error => { if (active) setMessage(getErrorMessage(error, "Venue evidence unavailable")); });
    return () => { active = false; };
  }, []);

  async function submitQualification() {
    setBusy(true);
    try {
      const parsed = JSON.parse(evidence) as Record<string, unknown>;
      const result = await submitPaperVenueQualification({
        provider: "alpaca_paper",
        account_id: paper?.account?.account_id ?? "",
        evidence: parsed,
      });
      setMessage(`Qualification recorded as ${result.status}.`);
      await refresh();
    } catch (error) {
      setMessage(getErrorMessage(error, "Qualification could not be recorded"));
    } finally {
      setBusy(false);
    }
  }

  async function submitActivation() {
    if (!qualification?.qualification_id) return;
    setBusy(true);
    try {
      await activatePaperVenue({
        provider: "alpaca_paper",
        qualification_id: qualification.qualification_id,
        reason: activationReason.trim(),
      });
      setMessage("Separate paper activation recorded. Other readiness gates still apply.");
      await refresh();
    } catch (error) {
      setMessage(getErrorMessage(error, "Activation could not be recorded"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-md border border-line bg-white">
      <div className="flex flex-col gap-3 border-b border-line p-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2"><ClipboardCheck size={19} className="text-mint" /><h2 className="text-base font-semibold">Paper Venue Governance</h2></div>
          <div className="mt-1 text-sm text-slate-500">Account-specific qualification and separate paper activation.</div>
        </div>
        <button className="secondary-action" type="button" onClick={() => void refresh()} disabled={busy}>Refresh evidence</button>
      </div>
      <div className="grid gap-4 p-4">
        <div className="grid gap-2 text-sm md:grid-cols-3">
          <div><span className="text-slate-500">Provider</span><strong className="ml-2">{qualification?.provider ?? "alpaca_paper"}</strong></div>
          <div><span className="text-slate-500">Qualification</span><strong className="ml-2">{qualification?.qualification_status ?? "loading"}</strong></div>
          <div><span className="text-slate-500">Activation</span><strong className="ml-2">{qualification?.activation_authorized ? "authorized" : "missing"}</strong></div>
        </div>
        <p className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">{message}. The server validates every field and never treats this record as live authorization.</p>
        <RoleGate requires="operator">
          <div className="grid gap-2">
            <label className="text-sm font-semibold" htmlFor="paper-venue-evidence">Redacted qualification evidence JSON</label>
            <textarea id="paper-venue-evidence" className="min-h-64 rounded-md border border-line bg-slate-50 p-3 font-mono text-xs" value={evidence} onChange={event => setEvidence(event.target.value)} spellCheck={false} />
            <button className="primary-action w-fit" type="button" onClick={() => void submitQualification()} disabled={busy || !paper?.account?.account_id}>Submit qualification</button>
          </div>
        </RoleGate>
        <RoleGate requires="admin">
          <div className="grid gap-2 border-t border-line pt-4">
            <label className="text-sm font-semibold" htmlFor="paper-activation-reason">Separate activation reason</label>
            <input id="paper-activation-reason" className="rounded-md border border-line bg-slate-50 p-2 text-sm" value={activationReason} onChange={event => setActivationReason(event.target.value)} placeholder="Why this qualified paper venue may be activated" />
            <button className="secondary-action w-fit" type="button" onClick={() => void submitActivation()} disabled={busy || !qualification?.qualified || !qualification.qualification_id || !activationReason.trim()}><ShieldCheck size={15} />Submit separate activation</button>
          </div>
        </RoleGate>
      </div>
    </section>
  );
}
