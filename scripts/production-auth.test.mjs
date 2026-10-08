import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";

const root = new URL("../", import.meta.url);
const scripts = (artifact) =>
  JSON.parse(readFileSync(new URL(`artifacts/${artifact}/package.json`, root), "utf8")).scripts;

// Execute the actual package commands against harmless stand-ins. These tests
// never start workers, contact an identity provider, or access credentials.
function commandEnvironment(command, executable, variable, inheritedValue) {
  const directory = mkdtempSync(path.join(tmpdir(), "production-auth-"));
  try {
    writeFileSync(
      path.join(directory, executable),
      `#!/bin/sh\nprintf '%s\\n' "$${variable}"\nprintf '%s\\n' "$@"\n`,
      { mode: 0o755 },
    );
    const result = spawnSync("/bin/sh", ["-c", command], {
      encoding: "utf8",
      env: { PATH: directory, [variable]: inheritedValue },
    });
    assert.equal(result.status, 0, result.stderr);
    return result.stdout.trim().split("\n");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test("published frontend always selects Clerk despite a shared local-paper flag", () => {
  for (const inheritedValue of ["true", "false", ""]) {
    assert.deepEqual(
      commandEnvironment(scripts("strategy-control-room").build, "vite", "VITE_LOCAL_PAPER_AUTH", inheritedValue),
      ["false", "build", "--config", "vite.config.ts"],
    );
  }
});

test("production gateway enables the Clerk proxy despite an inherited development environment", () => {
  for (const inheritedValue of ["development", "production", ""]) {
    assert.deepEqual(
      commandEnvironment(scripts("api-server").start, "node", "NODE_ENV", inheritedValue),
      ["production", "dist/production-server.js"],
    );
  }
});

test("preview commands retain local-paper access without forcing production mode", () => {
  assert.equal(scripts("strategy-control-room").dev, "vite --config vite.config.ts --host 0.0.0.0");
  assert.equal(scripts("api-server").dev, "cd backend && bash scripts/run_local_stack.sh");
});
