import React from "react";
import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { AccessRoleProvider } from "./access-control";
import {
  RiskSettingsPanel,
  buildRiskSettingsPayload,
  fields,
  formFromRule,
  validateRiskForm
} from "./risk-settings-panel";

const rule = {
  name: "default_paper_risk",
  is_active: true,
  value: { position_stop_loss_pct: 0.05, position_take_profit_pct: 0.08, max_open_positions: 7, paper_only: true }
};

test("exit distances are editable risk fields bounded like the API", () => {
  const stop = fields.find((field) => field.key === "position_stop_loss_pct");
  const take = fields.find((field) => field.key === "position_take_profit_pct");
  assert.ok(stop && take);
  assert.equal(stop.max, 0.5);
  assert.equal(take.min, 0);
});

test("form loads saved exit distances and falls back to defaults", () => {
  const form = formFromRule(rule);
  assert.equal(form.position_stop_loss_pct, 0.05);
  assert.equal(form.position_take_profit_pct, 0.08);
  assert.equal(form.max_open_positions, 7);
  const empty = formFromRule(null);
  assert.equal(empty.position_stop_loss_pct, 0.03);
  assert.equal(empty.position_take_profit_pct, 0.03);
});

test("payload carries exit distances, rounds integers and keeps the reason", () => {
  const form = { ...formFromRule(rule), max_open_positions: 7.4 };
  const payload = buildRiskSettingsPayload(form);
  assert.equal(payload.position_stop_loss_pct, 0.05);
  assert.equal(payload.position_take_profit_pct, 0.08);
  assert.equal(payload.max_open_positions, 7);
  assert.equal(payload.reason, "Risk settings updated from dashboard.");
});

test("out-of-range exit distances are rejected before save", () => {
  const errors = validateRiskForm({ ...formFromRule(rule), position_stop_loss_pct: 0.9 });
  assert.equal(errors.length, 1);
  assert.match(errors[0], /Position stop-loss/);
  assert.deepEqual(validateRiskForm(formFromRule(rule)), []);
});

test("admins see the exit fields; viewers do not get inputs", () => {
  const admin = renderToStaticMarkup(
    <AccessRoleProvider role="admin"><RiskSettingsPanel initialRule={rule} /></AccessRoleProvider>
  );
  assert.match(admin, /Position stop-loss/);
  assert.match(admin, /Position take-profit/);
  const viewer = renderToStaticMarkup(
    <AccessRoleProvider role="viewer"><RiskSettingsPanel initialRule={rule} /></AccessRoleProvider>
  );
  assert.doesNotMatch(viewer, /Position stop-loss/);
  assert.match(viewer, /Requires Admin access/);
});
