

import { useEffect, useState } from "react";
import { Save, ShieldCheck } from "lucide-react";

import { RiskRule, RiskSettingsUpdate, SafetyControlResponse, updateRiskSettings } from "@/lib/api";
import { RoleGate } from "@/components/access-control";
import { SafetyControlBar, type SafetyApi } from "@/components/safety-control-bar";

export type FieldKey = Exclude<keyof RiskSettingsUpdate, "reason">;

type RiskField = {
  key: FieldKey;
  label: string;
  step: string;
  min: number;
  max: number;
  integer?: boolean;
  hint?: string;
};

export const fields: RiskField[] = [
  { key: "min_confidence", label: "Minimum confidence", step: "0.01", min: 0, max: 1 },
  { key: "max_daily_drawdown", label: "Max daily drawdown", step: "0.005", min: 0, max: 1 },
  { key: "max_strategy_drawdown", label: "Max strategy drawdown", step: "0.005", min: 0, max: 1 },
  { key: "max_open_positions", label: "Max open positions", step: "1", min: 1, max: 100, integer: true },
  { key: "max_open_positions_per_strategy", label: "Max per strategy", step: "1", min: 1, max: 100, integer: true },
  { key: "max_symbol_exposure", label: "Max symbol exposure", step: "0.01", min: 0, max: 1 },
  { key: "max_risk_per_trade", label: "Max risk per trade", step: "0.001", min: 0, max: 1 },
  {
    key: "position_stop_loss_pct",
    label: "Position stop-loss",
    step: "0.005",
    min: 0,
    max: 0.5,
    hint: "Fraction below the entry price. It sets the broker stop leg and the app-side protective stop. 0 disables both."
  },
  {
    key: "position_take_profit_pct",
    label: "Position take-profit",
    step: "0.005",
    min: 0,
    max: 0.5,
    hint: "Fraction above the entry price for the broker take-profit leg. 0 means a stop-only exit."
  },
  { key: "stop_after_consecutive_losses", label: "Loss pause count", step: "1", min: 1, max: 100, integer: true },
  { key: "candidate_review_score_threshold", label: "Review score gate", step: "0.01", min: 0, max: 2 },
  { key: "activation_score_threshold", label: "Activation score gate", step: "0.01", min: 0, max: 2 },
  { key: "journal_feedback_review_threshold_cap", label: "Memory review cap", step: "0.005", min: 0, max: 0.25 },
  { key: "journal_feedback_allocation_multiplier_cap", label: "Memory sizing cap", step: "0.01", min: 0, max: 1 },
  { key: "memory_replay_min_complete_samples", label: "Replay sample gate", step: "1", min: 0, max: 1000, integer: true },
  { key: "memory_replay_min_avg_return_delta", label: "Replay return gate", step: "0.001", min: -1, max: 1 },
  { key: "memory_replay_min_hit_rate_delta", label: "Replay hit-rate gate", step: "0.01", min: -1, max: 1 }
];

type RiskForm = Required<Omit<RiskSettingsUpdate, "reason">>;

const defaults: RiskForm = {
  min_confidence: 0.58,
  max_daily_drawdown: 0.03,
  max_strategy_drawdown: 0.1,
  max_open_positions: 10,
  max_open_positions_per_strategy: 5,
  max_symbol_exposure: 0.1,
  max_risk_per_trade: 0.01,
  position_stop_loss_pct: 0.03,
  position_take_profit_pct: 0.03,
  stop_after_consecutive_losses: 5,
  candidate_review_score_threshold: 0.7,
  activation_score_threshold: 0.72,
  journal_feedback_review_threshold_cap: 0.03,
  journal_feedback_allocation_multiplier_cap: 0.2,
  memory_replay_min_complete_samples: 10,
  memory_replay_min_avg_return_delta: 0,
  memory_replay_min_hit_rate_delta: 0
};

function numberFromRule(rule: RiskRule | null, key: FieldKey): number {
  const value = rule?.value?.[key];
  if (typeof value === "number") return value;
  const parsed = Number(value ?? defaults[key]);
  return Number.isFinite(parsed) ? parsed : defaults[key];
}

export function formFromRule(rule: RiskRule | null): RiskForm {
  return Object.fromEntries(fields.map((field) => [field.key, numberFromRule(rule, field.key)])) as RiskForm;
}

/** Field errors for values the API would reject (it re-validates on save). */
export function validateRiskForm(form: RiskForm): string[] {
  return fields.flatMap((field) => {
    const value = form[field.key];
    if (!Number.isFinite(value) || value < field.min || value > field.max) {
      return [`${field.label} must be between ${field.min} and ${field.max}`];
    }
    return [];
  });
}

/** The PATCH body: integer fields rounded, reason attached. */
export function buildRiskSettingsPayload(form: RiskForm, reason = "Risk settings updated from dashboard."): RiskSettingsUpdate {
  const payload: RiskSettingsUpdate = { ...form, reason };
  for (const field of fields) {
    if (field.integer) payload[field.key] = Math.round(form[field.key]);
  }
  return payload;
}

/** Applies a safety-control response to the locally held risk rule. */
export function ruleAfterSafetyAction(rule: RiskRule | null, response: SafetyControlResponse): RiskRule | null {
  if (response.risk_rule) return response.risk_rule;
  if (!rule) return rule;
  return { ...rule, value: { ...rule.value, kill_switch_enabled: response.kill_switch_enabled } };
}

export function RiskSettingsPanel({
  initialRule,
  onSafetyAction,
  safetyApi
}: {
  initialRule: RiskRule | null;
  onSafetyAction?: (response: SafetyControlResponse) => void;
  safetyApi?: SafetyApi;
}) {
  const [rule, setRule] = useState<RiskRule | null>(initialRule);
  const [form, setForm] = useState<RiskForm>(() => formFromRule(initialRule));
  const [status, setStatus] = useState("Risk settings ready");
  const [isSaving, setIsSaving] = useState(false);

  // Keep the displayed rule (kill-switch label) in step with a refetched
  // workspace snapshot. The editable form is left alone so in-progress edits survive.
  useEffect(() => {
    if (initialRule) setRule(initialRule);
  }, [initialRule]);

  function setField(key: FieldKey, value: string) {
    const parsed = Number(value);
    setForm((current) => ({ ...current, [key]: Number.isFinite(parsed) ? parsed : 0 }));
  }

  async function saveSettings() {
    const errors = validateRiskForm(form);
    if (errors.length) {
      setStatus(errors.join("; "));
      return;
    }
    setIsSaving(true);
    setStatus("Saving risk settings...");
    try {
      // updateRiskSettings goes through authenticatedFetch, which sends the
      // x-action-confirmation, x-idempotency-key and x-action-reason headers.
      const saved = await updateRiskSettings(buildRiskSettingsPayload(form));
      setRule(saved);
      setForm(formFromRule(saved));
      setStatus("Saved and audit logged");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Save failed");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <div className="rounded-md border border-line bg-white p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <ShieldCheck size={19} className="text-mint" />
          <h2 className="text-base font-semibold">Risk Settings</h2>
        </div>
        <RoleGate requires="admin"><button
          className="focus-ring inline-flex h-9 items-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white disabled:opacity-60"
          disabled={isSaving}
          onClick={saveSettings}
          type="button"
        >
          <Save size={16} />
          Save
        </button></RoleGate>
      </div>
      <RoleGate requires="admin"><div className="grid gap-3 sm:grid-cols-2">
        {fields.map((field) => (
          <label className="grid gap-1 text-sm" key={field.key}>
            <span className="font-medium">{field.label}</span>
            {field.hint ? <span className="text-xs text-slate-500">{field.hint}</span> : null}
            <input
              className="focus-ring h-10 rounded-md border border-line px-3"
              max={field.max}
              min={field.min}
              onChange={(event) => setField(field.key, event.target.value)}
              step={field.step}
              type="number"
              value={form[field.key]}
            />
          </label>
        ))}
      </div></RoleGate>
      <div className="mt-3 grid gap-2 text-sm">
        <div className="flex items-center justify-between rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-mint">
          <span className="font-semibold">Paper-only lock</span>
          <span>{rule?.value?.paper_only === false ? "Forced on save" : "Enabled"}</span>
        </div>
        <div className="flex items-center justify-between rounded-md border border-line bg-panel px-3 py-2 text-slate-600">
          <span>{status}</span>
          <span data-testid="kill-switch-state">{rule?.value?.kill_switch_enabled ? "Kill switch on" : "Kill switch off"}</span>
        </div>
        <SafetyControlBar
          api={safetyApi}
          onAction={(response) => {
            setRule((current) => ruleAfterSafetyAction(current, response));
            onSafetyAction?.(response);
          }}
        />
      </div>
    </div>
  );
}
