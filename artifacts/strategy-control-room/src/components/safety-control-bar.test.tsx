import React from "react";
import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import type { SafetyControlResponse } from "@/lib/api";
import { AccessRoleProvider } from "./access-control";
import { RiskSettingsPanel, ruleAfterSafetyAction } from "./risk-settings-panel";
import {
  SAFETY_ACTIONS,
  SafetyControlBar,
  confirmSafetyAction,
  idleSafetyFlow,
  safetyFlowReducer,
  type SafetyActionId,
  type SafetyApi
} from "./safety-control-bar";
import { recoveryKillSwitchGuidance } from "./stock-recovery-panel";

function response(killSwitch: boolean): SafetyControlResponse {
  return {
    action: "kill_switch",
    status: "ok",
    message: "done",
    kill_switch_enabled: killSwitch,
    paused_strategies: 0,
    affected_strategy_ids: [],
    risk_rule: null
  };
}

function fakeApi() {
  const calls: Array<{ action: SafetyActionId; reason: string }> = [];
  const make = (action: SafetyActionId) => async (reason: string) => {
    calls.push({ action, reason });
    return response(action === "kill");
  };
  const api: SafetyApi = { pause: make("pause"), resume: make("resume"), kill: make("kill"), reset: make("reset") };
  return { api, calls };
}

/** Fails the test if anything reaches for a native prompt dialog. */
function withPromptTrap<T>(run: () => T): { result: T; promptCalls: number } {
  const holder = globalThis as unknown as { window?: unknown };
  const previous = holder.window;
  let promptCalls = 0;
  holder.window = { prompt: () => { promptCalls += 1; return "from prompt"; } };
  try {
    return { result: run(), promptCalls };
  } finally {
    holder.window = previous;
  }
}

test("clicking an action opens the inline form prefilled with a default reason", () => {
  const open = safetyFlowReducer(idleSafetyFlow, { type: "open", action: "reset" });
  assert.equal(open.pending, "reset");
  assert.equal(open.reason, SAFETY_ACTIONS.reset.defaultReason);
  assert.ok(open.reason.trim().length > 0);
});

test("the pending form renders an input, Confirm and Cancel without window.prompt", () => {
  const { result: markup, promptCalls } = withPromptTrap(() =>
    renderToStaticMarkup(
      <AccessRoleProvider role="admin">
        <SafetyControlBar initialFlow={safetyFlowReducer(idleSafetyFlow, { type: "open", action: "reset" })} />
      </AccessRoleProvider>
    )
  );
  assert.equal(promptCalls, 0);
  assert.match(markup, /data-testid="safety-reason-form"/);
  assert.match(markup, /data-testid="safety-reason-input"/);
  assert.match(markup, new RegExp(`value="${SAFETY_ACTIONS.reset.defaultReason}"`));
  assert.match(markup, />Confirm</);
  assert.match(markup, />Cancel</);
});

test("no form is shown until an action is chosen", () => {
  const markup = renderToStaticMarkup(
    <AccessRoleProvider role="admin"><SafetyControlBar /></AccessRoleProvider>
  );
  assert.doesNotMatch(markup, /safety-reason-form/);
  for (const id of ["pause", "resume", "kill", "reset"]) assert.match(markup, new RegExp(`safety-action-${id}`));
});

test("confirm calls the API with the entered reason and never prompts", async () => {
  const { api, calls } = fakeApi();
  let flow = safetyFlowReducer(idleSafetyFlow, { type: "open", action: "reset" });
  flow = safetyFlowReducer(flow, { type: "edit", reason: "  Reviewed broker state, clearing kill switch  " });
  const { result, promptCalls } = withPromptTrap(() => confirmSafetyAction(flow, api));
  const outcome = await result;
  assert.equal(promptCalls, 0);
  assert.deepEqual(calls, [{ action: "reset", reason: "Reviewed broker state, clearing kill switch" }]);
  assert.equal(outcome?.kill_switch_enabled, false);
});

test("each action routes to its own API call", async () => {
  for (const id of ["pause", "resume", "kill", "reset"] as SafetyActionId[]) {
    const { api, calls } = fakeApi();
    await confirmSafetyAction(safetyFlowReducer(idleSafetyFlow, { type: "open", action: id }), api);
    assert.deepEqual(calls, [{ action: id, reason: SAFETY_ACTIONS[id].defaultReason }]);
  }
});

test("cancel closes the form and calls nothing", async () => {
  const { api, calls } = fakeApi();
  const open = safetyFlowReducer(idleSafetyFlow, { type: "open", action: "kill" });
  const cancelled = safetyFlowReducer(open, { type: "cancel" });
  assert.deepEqual(cancelled, idleSafetyFlow);
  assert.equal(await confirmSafetyAction(cancelled, api), null);
  assert.equal(calls.length, 0);
});

test("a blank reason is rejected without calling the API", async () => {
  const { api, calls } = fakeApi();
  let flow = safetyFlowReducer(idleSafetyFlow, { type: "open", action: "pause" });
  flow = safetyFlowReducer(flow, { type: "edit", reason: "   " });
  assert.equal(await confirmSafetyAction(flow, api), null);
  assert.equal(calls.length, 0);
});

test("role gates stay: viewers see no safety buttons, operators cannot reset", () => {
  const viewer = renderToStaticMarkup(<AccessRoleProvider role="viewer"><SafetyControlBar /></AccessRoleProvider>);
  assert.doesNotMatch(viewer, /safety-action-/);
  assert.match(viewer, /Requires Operator access/);
  const operator = renderToStaticMarkup(<AccessRoleProvider role="operator"><SafetyControlBar /></AccessRoleProvider>);
  assert.match(operator, /safety-action-kill/);
  assert.match(operator, /safety-action-pause/);
  assert.doesNotMatch(operator, /safety-action-reset/);
  assert.doesNotMatch(operator, /safety-action-resume/);
});

test("the risk settings page renders the safety controls beside the kill switch label", () => {
  const rule = { name: "default_paper_risk", is_active: true, value: { kill_switch_enabled: true, paper_only: true } };
  const markup = renderToStaticMarkup(<AccessRoleProvider role="admin"><RiskSettingsPanel initialRule={rule} /></AccessRoleProvider>);
  assert.match(markup, /Kill switch on/);
  assert.match(markup, /safety-action-reset/);
});

test("a safety response updates the kill switch label state", () => {
  const rule = { name: "default_paper_risk", is_active: true, value: { kill_switch_enabled: true, max_open_positions: 5 } };
  const next = ruleAfterSafetyAction(rule, response(false));
  assert.equal(next?.value.kill_switch_enabled, false);
  assert.equal(next?.value.max_open_positions, 5);
  const fromServer = { id: 1, name: "default_paper_risk", is_active: true, value: { kill_switch_enabled: false } };
  assert.equal(ruleAfterSafetyAction(rule, { ...response(false), risk_rule: fromServer }), fromServer);
});

test("recovery tab: resume clears the kill switch; reset only once recovery is armed or resumable", () => {
  const waiting = recoveryKillSwitchGuidance("cooldown", true);
  assert.equal(waiting.canResume, true);
  assert.equal(waiting.offerReset, false);
  assert.match(waiting.hint, /clears the global kill switch/);

  const armed = recoveryKillSwitchGuidance("armed", true);
  assert.equal(armed.canResume, false);
  assert.equal(armed.offerReset, true);

  const resumable = recoveryKillSwitchGuidance("resumable", true);
  assert.equal(resumable.canResume, true);
  assert.equal(resumable.offerReset, true);

  assert.equal(recoveryKillSwitchGuidance("armed", false).offerReset, false);
  assert.equal(recoveryKillSwitchGuidance("paused", true).offerReset, false);
  assert.equal(recoveryKillSwitchGuidance("armed", null).offerReset, false);
});
