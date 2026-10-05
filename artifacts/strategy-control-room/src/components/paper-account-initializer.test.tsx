import React from "react";
import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { PaperAccountInitializer } from "./paper-account-initializer";

test("Alpaca offers explicit v2 opt-in without changing the legacy default", () => {
  const html = renderToStaticMarkup(<PaperAccountInitializer broker="alpaca_paper" busy={false} onInitialize={() => {}} />);
  assert.match(html, /value="alpaca-activities-v2"/);
  assert.match(html, /value="legacy-v1" selected/);
});
test("Tradier never offers Alpaca accounting", () => {
  const html = renderToStaticMarkup(<PaperAccountInitializer broker="tradier_sandbox" busy={false} onInitialize={() => {}} />);
  assert.doesNotMatch(html, /<select/);
});
test("Unknown brokers cannot be initialized", () => {
  const html = renderToStaticMarkup(<PaperAccountInitializer broker="unknown" busy={false} onInitialize={() => {}} />);
  assert.match(html, /disabled=""/);
});
