import assert from "node:assert/strict";
import { test } from "node:test";

import {
  assess,
  defaultGuardConfig,
  formatSample,
  parseCgroup,
  parseMeminfo,
  parsePressure,
  parseRss,
  readSample,
  tighter,
  type GuardState,
  type Sample,
} from "./resource-guard";

const GiB = 1024 ** 3;
const MiB = 1024 ** 2;

test("meminfo, cgroup, pressure and RSS are parsed", () => {
  const meminfo = parseMeminfo("MemTotal:        8388608 kB\nMemFree:  100 kB\nMemAvailable:    1048576 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n");
  assert.equal(meminfo?.totalBytes, 8 * GiB);
  assert.equal(meminfo?.availableBytes, 1 * GiB);
  assert.equal(meminfo?.usedBytes, 7 * GiB);
  assert.equal(meminfo?.swapTotalBytes, 0);

  const cgroup = parseCgroup(String(6 * GiB), String(8 * GiB), `anon 123\ninactive_file ${1 * GiB}\nactive_file 5\n`);
  assert.equal(cgroup?.usedBytes, 5 * GiB);
  assert.equal(cgroup?.availableBytes, 3 * GiB);
  assert.equal(parseCgroup("100", "max", ""), null);

  assert.deepEqual(
    parsePressure("some avg10=12.50 avg60=3.00 avg300=1.00 total=1\nfull avg10=4.25 avg60=1.00 avg300=0.10 total=2\n"),
    { some10: 12.5, full10: 4.25 },
  );
  assert.deepEqual(parsePressure(""), { some10: null, full10: null });
  assert.equal(parseRss("Name:\tpython3.11\nVmRSS:\t  2048000 kB\n"), 2048000 * 1024);
});

test("the tighter of the cgroup and the VM view wins", () => {
  const vm = parseMeminfo("MemTotal: 8388608 kB\nMemAvailable: 4194304 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n");
  const cg = parseCgroup(String(7 * GiB), String(8 * GiB), "inactive_file 0\n");
  const result = tighter(cg, vm);
  assert.equal(result?.source, "cgroup");
  assert.equal(result?.availableBytes, 1 * GiB);
  assert.equal(result?.swapTotalBytes, 0);
  assert.equal(tighter(null, vm)?.source, "meminfo");
});

function sample(availableMiB: number, full10: number | null = 0): Sample {
  return {
    at: "2026-11-10T19:15:00Z",
    memory: {
      totalBytes: 8 * GiB, availableBytes: availableMiB * MiB, usedBytes: 8 * GiB - availableMiB * MiB,
      source: "meminfo", swapTotalBytes: 0, swapFreeBytes: 0,
    },
    memoryPressure: { some10: 50, full10 },
    cpuPressure: { some10: 80, full10: null },
    load1: 9.5,
    children: [
      { label: "fastapi", pid: 10, rssBytes: 3000 * MiB },
      { label: "paper-fast", pid: 11, rssBytes: 2500 * MiB },
      { label: "paper-core", pid: 12, rssBytes: 2200 * MiB },
      { label: "watchdog", pid: 13, rssBytes: 200 * MiB },
    ],
  };
}

test("the guard stops only the research worker, after consecutive low samples", () => {
  const config = defaultGuardConfig({});
  const state: GuardState = { strikes: 0, lastKillAt: null };
  // 600 MiB of 8 GiB is under the 8% line and above the 7% emergency floor.
  const first = assess(sample(600), state, config, 0);
  assert.equal(first.action, "none");
  assert.equal(first.critical, true);
  const second = assess(sample(600), state, config, 10_000);
  assert.equal(second.action, "kill");
  // The API and the trading lane are larger but never chosen.
  assert.equal(second.action === "kill" && second.victim.label, "paper-core");
  // Cooldown: no second stop within five minutes.
  assert.equal(assess(sample(600), state, config, 20_000).action, "none");
  const cooling = assess(sample(600), state, config, 30_000);
  assert.equal(cooling.action, "none");
  assert.match(cooling.reason, /cooldown/);
  // Still critical once the cooldown ends: it acts at once.
  assert.equal(assess(sample(600), state, config, 320_000).action, "kill");
});

test("below the emergency floor the guard acts on the first sample", () => {
  // 2026-11-11: 529 MiB and 184 MiB available were each "sample 1/2"; the VM froze before a second sample.
  const config = defaultGuardConfig({});
  for (const available of [529, 184]) {
    const state: GuardState = { strikes: 0, lastKillAt: null };
    const decision = assess(sample(available), state, config, 0);
    assert.equal(decision.action, "kill");
    assert.equal(decision.action === "kill" && decision.victim.label, "paper-core");
    assert.match(decision.reason, /emergency floor/);
    // The cooldown still applies afterwards.
    assert.equal(assess(sample(available), state, config, 5_000).action, "none");
  }
  // The floor can be moved or switched off by configuration.
  const relaxed = defaultGuardConfig({ MEMORY_GUARD_EMERGENCY_FRACTION: "0" });
  assert.equal(assess(sample(184), { strikes: 0, lastKillAt: null }, relaxed, 0).action, "none");
});

test("thrashing pressure alone also counts; healthy samples reset the count", () => {
  const config = defaultGuardConfig({});
  const state: GuardState = { strikes: 0, lastKillAt: null };
  assert.equal(assess(sample(3000, 40), state, config, 0).critical, true);
  assert.equal(assess(sample(3000, 1), state, config, 10_000).critical, false);
  assert.equal(state.strikes, 0);
  assert.equal(assess(sample(3000, 40), state, config, 20_000).action, "none");
  assert.equal(assess(sample(3000, 40), state, config, 30_000).action, "kill");
});

test("with no research worker running the guard only reports", () => {
  const config = defaultGuardConfig({});
  const state: GuardState = { strikes: 1, lastKillAt: null };
  const low = sample(300);
  low.children = low.children.filter((child) => child.label !== "paper-core");
  const decision = assess(low, state, config, 0);
  assert.equal(decision.action, "none");
  assert.match(decision.reason, /no research worker/);
});

test("non-compact research lanes are killable; trading lanes never are", () => {
  const { killableLabels } = defaultGuardConfig({});
  for (const label of ["paper-core", "learning-1", "learning-2", "research", "scalp-research-1"]) {
    assert.equal(killableLabels.test(label), true, label);
  }
  for (const label of ["paper-fast", "paper-broker", "paper-intraday", "watchdog", "direct-monitor", "beat",
    "fastapi", "redis", "short-pilot", "execution", "risk", "monitor", "market", "intraday"]) {
    assert.equal(killableLabels.test(label), false, label);
  }
});

test("the log line names memory, pressure, load and the largest processes", () => {
  const line = formatSample(sample(700));
  assert.match(line, /^\[resources\] mem_used=7492MiB\/8192MiB avail=700MiB src=meminfo/);
  assert.match(line, /psi_mem_some10=50 full10=0/);
  assert.match(line, /top=fastapi:3000MiB,paper-fast:2500MiB,paper-core:2200MiB,watchdog:200MiB/);
});

test("reading a live sample never throws, on Linux or elsewhere", () => {
  const live = readSample(new Map([[process.pid, "self"], [999_999_999, "gone"]]));
  assert.ok(live.at);
  assert.ok(live.children.every((child) => child.label !== "gone"));
});
