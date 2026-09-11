import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { AccessRoleProvider, canAccess, RoleGate } from "./access-control";
import type { AccessRole } from "@/lib/api";

const roles: AccessRole[] = ["viewer", "researcher", "operator", "admin"];

test("role boundaries follow Viewer, Researcher, Operator, Admin order", () => {
  for (const [activeIndex, active] of roles.entries()) {
    for (const [requiredIndex, required] of roles.entries()) {
      assert.equal(canAccess(active, required), activeIndex >= requiredIndex, `${active} -> ${required}`);
    }
  }
});

test("restricted controls are not mounted and show the required role", () => {
  let mounted = 0;
  function RestrictedControl() {
    mounted += 1;
    return <button>Dangerous action</button>;
  }

  const markup = renderToStaticMarkup(
    <AccessRoleProvider role="viewer">
      <RoleGate requires="operator"><RestrictedControl /></RoleGate>
    </AccessRoleProvider>,
  );

  assert.equal(mounted, 0);
  assert.doesNotMatch(markup, /Dangerous action/);
  assert.match(markup, /Requires Operator access/);
});

test("each control group mounts at and above its required role", () => {
  for (const required of roles) {
    for (const active of roles) {
      const markup = renderToStaticMarkup(
        <AccessRoleProvider role={active}>
          <RoleGate requires={required}><button>{required} control</button></RoleGate>
        </AccessRoleProvider>,
      );
      assert.equal(markup.includes(`${required} control`), canAccess(active, required));
    }
  }
});