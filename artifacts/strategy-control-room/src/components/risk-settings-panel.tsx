

import { useState } from "react";
import { Save, ShieldCheck } from "lucide-react";

import { RiskRule, RiskSettingsUpdate, updateRiskSettings } from "@/lib/api";

type FieldKey = Exclude<keyof RiskSettingsUpdate, "reason">;

type RiskField = {
  key: FieldKey;
  label: string;
  step: string;
  min: number;
  max: number;
};

const fields: RiskField[] = [
  { key: "min_confidence", label: "Minimum confidence", step: "0.01", min: 0, max: 1 },
  { key: "max_daily_drawdown", label: "Max daily drawdown", step: "0.005", min: 0, max: 1 },
  { key: "max_strategy_drawdown", label: "Max strategy drawdown", step: "0.005", min: 0, max: 1 },
  { key: "max_open_positions", label: "Max open positions", step: "1", min: 1, max: 100 },
  { key: "max_open_positions_per_strategy", label: "Max per strategy", step: "1", min: 1, max: 100 },
  { key: "max_symbol_exposure", label: "Max symbol exposure", step: "0.01", min: 0, max: 1 },
  { key: "max_risk_per_trade", label: "Max risk per trade", step: "0.001", min: 0, max: 1 },
  { key: "stop_after_consecutive_losses", label: "Loss pause count", step: "1", min: 1, max: 100 },
  { key: "candidate_review_score_threshold", label: "Review score gate", step: "0.01", min: 0, max: 2 },
  { key: "activation_score_threshold", label: "Activation score gate", step: "0.01", min: 0, max: 2 },
  { key: "journal_feedback_review_threshold_cap", label: "Memory review cap", step: "0.005", min: 0, max: 0.25 },
  { key: "journal_feedback_allocation_multiplier_cap", label: "Memory sizing cap", step: "0.01", min: 0, max: 1 },
  { key: "memory_replay_min_complete_samples", label: "Replay sample gate", step: "1", min: 0, max: 1000 },
  { key: "memory_replay_min_avg_return_delta", label: "Replay return gate", step: "0.001", min: -1, max: 1 },
  { key: "memory_replay_min_hit_rate_delta", label: "Replay hit-rate gate", step: "0.01", min: -1, max: 1 }
];

const defaults: Required<Omit<RiskSettingsUpdate, "reason">> = {
  min_confidence: 0.58,
  max_daily_drawdown: 0.03,
  max_strategy_drawdown: 0.1,
  max_open_positions: 10,
  max_open_positions_per_strategy: 5,
  max_symbol_exposure: 0.1,
  max_risk_per_trade: 0.01,
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
  return typeof value === "number" ? value : Number(value ?? defaults[key]);
}

export function RiskSettingsPanel({ initialRule }: { initialRule: RiskRule | null }) {
  const [rule, setRule] = useState<RiskRule | null>(initialRule);
  const [form, setForm] = useState<Required<Omit<RiskSettingsUpdate, "reason">>>(() => ({
    min_confidence: numberFromRule(initialRule, "min_confidence"),
    max_daily_drawdown: numberFromRule(initialRule, "max_daily_drawdown"),
    max_strategy_drawdown: numberFromRule(initialRule, "max_strategy_drawdown"),
    max_open_positions: numberFromRule(initialRule, "max_open_positions"),
    max_open_positions_per_strategy: numberFromRule(initialRule, "max_open_positions_per_strategy"),
    max_symbol_exposure: numberFromRule(initialRule, "max_symbol_exposure"),
    max_risk_per_trade: numberFromRule(initialRule, "max_risk_per_trade"),
    stop_after_consecutive_losses: numberFromRule(initialRule, "stop_after_consecutive_losses"),
    candidate_review_score_threshold: numberFromRule(initialRule, "candidate_review_score_threshold"),
    activation_score_threshold: numberFromRule(initialRule, "activation_score_threshold"),
    journal_feedback_review_threshold_cap: numberFromRule(initialRule, "journal_feedback_review_threshold_cap"),
    journal_feedback_allocation_multiplier_cap: numberFromRule(initialRule, "journal_feedback_allocation_multiplier_cap"),
    memory_replay_min_complete_samples: numberFromRule(initialRule, "memory_replay_min_complete_samples"),
    memory_replay_min_avg_return_delta: numberFromRule(initialRule, "memory_replay_min_avg_return_delta"),
    memory_replay_min_hit_rate_delta: numberFromRule(initialRule, "memory_replay_min_hit_rate_delta")
  }));
  const [status, setStatus] = useState("Risk settings ready");
  const [isSaving, setIsSaving] = useState(false);

  function setField(key: FieldKey, value: string) {
    const parsed = Number(value);
    setForm((current) => ({ ...current, [key]: Number.isFinite(parsed) ? parsed : 0 }));
  }

  async function saveSettings() {
    setIsSaving(true);
    setStatus("Saving risk settings...");
    try {
      const saved = await updateRiskSettings({
        ...form,
        max_open_positions: Math.round(form.max_open_positions),
        max_open_positions_per_strategy: Math.round(form.max_open_positions_per_strategy),
        stop_after_consecutive_losses: Math.round(form.stop_after_consecutive_losses),
        memory_replay_min_complete_samples: Math.round(form.memory_replay_min_complete_samples),
        reason: "Risk settings updated from dashboard."
      });
      setRule(saved);
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
        <button
          className="focus-ring inline-flex h-9 items-center gap-2 rounded-md bg-mint px-3 text-sm font-semibold text-white disabled:opacity-60"
          disabled={isSaving}
          onClick={saveSettings}
          type="button"
        >
          <Save size={16} />
          Save
        </button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {fields.map((field) => (
          <label className="grid gap-1 text-sm" key={field.key}>
            <span className="font-medium">{field.label}</span>
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
      </div>
      <div className="mt-3 grid gap-2 text-sm">
        <div className="flex items-center justify-between rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-mint">
          <span className="font-semibold">Paper-only lock</span>
          <span>{rule?.value?.paper_only === false ? "Forced on save" : "Enabled"}</span>
        </div>
        <div className="flex items-center justify-between rounded-md border border-line bg-panel px-3 py-2 text-slate-600">
          <span>{status}</span>
          <span>{rule?.value?.kill_switch_enabled ? "Kill switch on" : "Kill switch off"}</span>
        </div>
      </div>
    </div>
  );
}
