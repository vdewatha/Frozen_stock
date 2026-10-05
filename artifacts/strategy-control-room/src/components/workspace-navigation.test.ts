import assert from "node:assert/strict";
import test from "node:test";
import { resolveWorkspacePage, workspacePages } from "../lib/workspace-navigation";

test("every workspace page and section resolves to its own route", () => {
  for (const page of workspacePages) {
    assert.equal(resolveWorkspacePage(page.path)?.page.id, page.id);
    for (const tab of page.tabs) {
      assert.equal(resolveWorkspacePage(`${page.path}/${tab.id}`)?.section, tab.id);
      assert.equal(resolveWorkspacePage(`${page.path}/${tab.id}/`)?.page.id, page.id);
    }
  }
});
test("unknown routes never masquerade as valid pages", () => {
  for (const path of ["/nope", "/markets/nope", "/marketstuff", "/risk/limits/extra"]) assert.equal(resolveWorkspacePage(path), null);
});
test("local legacy sign-in links resolve to the overview", () => {
  assert.equal(resolveWorkspacePage("/sign-in")?.page.id, "overview");
  assert.equal(resolveWorkspacePage("/sign-up")?.page.id, "overview");
});
