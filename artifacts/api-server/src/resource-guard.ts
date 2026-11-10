// Resource recorder and memory guard for the whole deployment VM.
//
// The deployment froze three times in two days (2026-11-08 10:17 ET,
// 11-09 11:08 ET, 11-09 19:18 ET): every process stopped logging at once, no
// crash, no OOM kill, no restart, and even the gateway's stall watchdog never
// fired. Each freeze came ~17 minutes into a heavy backtest (Sunday 10:00
// strategy discovery, the 19:00 nightly backtest, learning backtests under a
// QA sweep) on a 2 vCPU / 8 GiB VM running about ten Python processes: the
// signature of memory thrashing rather than an application deadlock. Nothing
// recorded memory, so no freeze left evidence.
//
// Every SAMPLE_SECONDS the gateway reads the VM's memory (cgroup v2 when
// present, else /proc/meminfo), memory/CPU pressure (PSI), load and each
// child's RSS. It logs a compact line each minute and at once when memory is
// tight; Replit keeps logs off the VM, so the record survives a freeze. When
// available memory stays critically low the guard SIGKILLs the largest
// research worker (the one running backtests; it is restarted by its
// supervisor) before the whole VM locks up. Trading, broker, monitor,
// watchdog, beat and API processes are never killed.
import { readFileSync } from "node:fs";

export type MemoryReading = {
  totalBytes: number;
  usedBytes: number;
  availableBytes: number;
  source: "cgroup" | "meminfo";
  swapTotalBytes: number | null;
  swapFreeBytes: number | null;
};

export type Pressure = { some10: number | null; full10: number | null };

export type ChildUsage = { label: string; pid: number; rssBytes: number };

export type Sample = {
  at: string;
  memory: MemoryReading | null;
  memoryPressure: Pressure;
  cpuPressure: Pressure;
  load1: number | null;
  children: ChildUsage[];
};

export type GuardConfig = {
  minAvailableFraction: number;
  maxFullPressure10: number;
  consecutiveSamples: number;
  cooldownMs: number;
  killableLabels: RegExp;
};

export const defaultGuardConfig = (env: NodeJS.ProcessEnv = process.env): GuardConfig => ({
  // About 650 MiB on the 8 GiB VM.
  minAvailableFraction: Number(env.MEMORY_GUARD_MIN_AVAILABLE_FRACTION ?? 0.08),
  // Share of the last 10 s in which every task was stalled on memory.
  maxFullPressure10: Number(env.MEMORY_GUARD_MAX_FULL_PRESSURE ?? 25),
  consecutiveSamples: Math.max(1, Number(env.MEMORY_GUARD_CONSECUTIVE_SAMPLES ?? 2)),
  cooldownMs: Math.max(60_000, Number(env.MEMORY_GUARD_COOLDOWN_SECONDS ?? 300) * 1000),
  // Research and learning lanes only: compact "paper-core" and the
  // non-compact learning-N / research / scalp-research workers.
  killableLabels: /^(paper-core|learning-\d+|research|scalp-research-\d+)$/,
});

// ------------------------------------------------------------ parsing

function kib(text: string, field: string): number | null {
  const match = new RegExp(`^${field}:\\s+(\\d+)\\s+kB`, "m").exec(text);
  return match ? Number(match[1]) * 1024 : null;
}

export function parseMeminfo(text: string): MemoryReading | null {
  const total = kib(text, "MemTotal");
  const available = kib(text, "MemAvailable");
  if (total === null || available === null) return null;
  return {
    totalBytes: total,
    availableBytes: available,
    usedBytes: total - available,
    source: "meminfo",
    swapTotalBytes: kib(text, "SwapTotal"),
    swapFreeBytes: kib(text, "SwapFree"),
  };
}

// cgroup v2: memory.current counts page cache too, so reclaimable file pages
// (inactive_file) are subtracted, as the kernel's own OOM accounting does.
export function parseCgroup(current: string, max: string, stat: string): MemoryReading | null {
  const used = Number(current.trim());
  const limitText = max.trim();
  if (!Number.isFinite(used) || limitText === "" || limitText === "max") return null;
  const limit = Number(limitText);
  if (!Number.isFinite(limit) || limit <= 0) return null;
  const inactive = /^inactive_file (\d+)$/m.exec(stat);
  const reclaimable = inactive ? Number(inactive[1]) : 0;
  const working = Math.max(0, used - reclaimable);
  return {
    totalBytes: limit,
    usedBytes: working,
    availableBytes: Math.max(0, limit - working),
    source: "cgroup",
    swapTotalBytes: null,
    swapFreeBytes: null,
  };
}

export function parsePressure(text: string): Pressure {
  const pick = (kind: string): number | null => {
    const match = new RegExp(`^${kind} avg10=([\\d.]+)`, "m").exec(text);
    return match ? Number(match[1]) : null;
  };
  return { some10: pick("some"), full10: pick("full") };
}

export function parseRss(status: string): number | null {
  return kib(status, "VmRSS");
}

// The tighter of the cgroup limit and the VM's own view.
export function tighter(a: MemoryReading | null, b: MemoryReading | null): MemoryReading | null {
  if (!a) return b;
  if (!b) return a;
  return a.availableBytes <= b.availableBytes ? { ...a, swapTotalBytes: b.swapTotalBytes, swapFreeBytes: b.swapFreeBytes } : b;
}

// ------------------------------------------------------------ decision

export type GuardState = { strikes: number; lastKillAt: number | null };

export type GuardDecision =
  | { action: "none"; critical: boolean; reason: string }
  | { action: "kill"; critical: true; reason: string; victim: ChildUsage };

export function assess(sample: Sample, state: GuardState, config: GuardConfig, nowMs: number): GuardDecision {
  const memory = sample.memory;
  const lowMemory = memory !== null && memory.totalBytes > 0
    && memory.availableBytes / memory.totalBytes < config.minAvailableFraction;
  const full = sample.memoryPressure.full10;
  const thrashing = full !== null && full >= config.maxFullPressure10;
  if (!lowMemory && !thrashing) {
    state.strikes = 0;
    return { action: "none", critical: false, reason: "ok" };
  }
  state.strikes += 1;
  const reason = [
    lowMemory && memory ? `available ${mib(memory.availableBytes)} of ${mib(memory.totalBytes)}` : null,
    thrashing ? `memory full pressure ${full}%` : null,
  ].filter(Boolean).join(", ");
  if (state.strikes < config.consecutiveSamples) {
    return { action: "none", critical: true, reason: `${reason} (sample ${state.strikes}/${config.consecutiveSamples})` };
  }
  if (state.lastKillAt !== null && nowMs - state.lastKillAt < config.cooldownMs) {
    return { action: "none", critical: true, reason: `${reason}; in cooldown after the last kill` };
  }
  const victim = sample.children
    .filter((child) => config.killableLabels.test(child.label))
    .sort((left, right) => right.rssBytes - left.rssBytes)[0];
  if (!victim) {
    return { action: "none", critical: true, reason: `${reason}; no research worker to stop` };
  }
  state.strikes = 0;
  state.lastKillAt = nowMs;
  return { action: "kill", critical: true, reason, victim };
}

// ------------------------------------------------------------ formatting

export function mib(bytes: number): string {
  return `${Math.round(bytes / (1024 * 1024))}MiB`;
}

export function formatSample(sample: Sample): string {
  const memory = sample.memory;
  const parts = [
    memory
      ? `mem_used=${mib(memory.usedBytes)}/${mib(memory.totalBytes)} avail=${mib(memory.availableBytes)} src=${memory.source}`
      : "mem=unknown",
    memory && memory.swapTotalBytes !== null
      ? `swap=${mib((memory.swapTotalBytes ?? 0) - (memory.swapFreeBytes ?? 0))}/${mib(memory.swapTotalBytes ?? 0)}`
      : null,
    `psi_mem_some10=${sample.memoryPressure.some10 ?? "na"} full10=${sample.memoryPressure.full10 ?? "na"}`,
    `psi_cpu_some10=${sample.cpuPressure.some10 ?? "na"}`,
    `load1=${sample.load1 ?? "na"}`,
    `top=${[...sample.children].sort((a, b) => b.rssBytes - a.rssBytes).slice(0, 6)
      .map((child) => `${child.label}:${mib(child.rssBytes)}`).join(",") || "-"}`,
  ];
  return `[resources] ${parts.filter(Boolean).join(" ")}`;
}

// ------------------------------------------------------------ live sampler

function read(path: string): string | null {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return null;
  }
}

export function readSample(children: Map<number, string>, now: Date = new Date()): Sample {
  const meminfo = read("/proc/meminfo");
  const current = read("/sys/fs/cgroup/memory.current");
  const max = read("/sys/fs/cgroup/memory.max");
  const stat = read("/sys/fs/cgroup/memory.stat");
  const memory = tighter(
    current !== null && max !== null ? parseCgroup(current, max, stat ?? "") : null,
    meminfo !== null ? parseMeminfo(meminfo) : null,
  );
  const memoryPressure = parsePressure(read("/sys/fs/cgroup/memory.pressure") ?? read("/proc/pressure/memory") ?? "");
  const cpuPressure = parsePressure(read("/sys/fs/cgroup/cpu.pressure") ?? read("/proc/pressure/cpu") ?? "");
  const loadText = read("/proc/loadavg");
  const load1 = loadText ? Number(loadText.split(" ")[0]) : null;
  const usage: ChildUsage[] = [];
  for (const [pid, label] of children) {
    const status = read(`/proc/${pid}/status`);
    const rss = status ? parseRss(status) : null;
    if (rss !== null) usage.push({ label, pid, rssBytes: rss });
  }
  return { at: now.toISOString(), memory, memoryPressure, cpuPressure, load1: Number.isFinite(load1) ? load1 : null, children: usage };
}

export type ResourceGuardStats = {
  enabled: boolean;
  last_sample: Sample | null;
  peak_used_bytes: number | null;
  peak_at: string | null;
  kills: { at: string; label: string; pid: number; rss: string; reason: string }[];
};

export function startResourceGuard(options: {
  children: Map<number, string>;
  log?: (line: string) => void;
  warn?: (line: string) => void;
  kill?: (pid: number) => void;
  env?: NodeJS.ProcessEnv;
}): ResourceGuardStats {
  const env = options.env ?? process.env;
  const stats: ResourceGuardStats = { enabled: env.MEMORY_GUARD !== "false", last_sample: null, peak_used_bytes: null, peak_at: null, kills: [] };
  if (env.RESOURCE_SAMPLER === "false") return stats;
  const log = options.log ?? ((line) => console.log(line));
  const warn = options.warn ?? ((line) => console.error(line));
  const kill = options.kill ?? ((pid) => process.kill(pid, "SIGKILL"));
  const config = defaultGuardConfig(env);
  const sampleMs = Math.max(5_000, Number(env.RESOURCE_SAMPLE_SECONDS ?? 10) * 1000);
  const logEvery = Math.max(1, Math.round(60_000 / sampleMs));
  const state: GuardState = { strikes: 0, lastKillAt: null };
  let tick = 0;
  const timer = setInterval(() => {
    try {
      const sample = readSample(options.children);
      stats.last_sample = sample;
      if (sample.memory && (stats.peak_used_bytes === null || sample.memory.usedBytes > stats.peak_used_bytes)) {
        stats.peak_used_bytes = sample.memory.usedBytes;
        stats.peak_at = sample.at;
      }
      const decision = stats.enabled
        ? assess(sample, state, config, Date.now())
        : { action: "none" as const, critical: false, reason: "guard disabled" };
      tick += 1;
      if (decision.critical) warn(`${formatSample(sample)} critical: ${decision.reason}`);
      else if (tick % logEvery === 0) log(formatSample(sample));
      if (decision.action === "kill") {
        const { victim } = decision;
        warn(`[memory-guard] stopping ${victim.label} (pid ${victim.pid}, ${mib(victim.rssBytes)}) before the VM runs out of memory: ${decision.reason}; its supervisor restarts it`);
        stats.kills = [...stats.kills, { at: sample.at, label: victim.label, pid: victim.pid, rss: mib(victim.rssBytes), reason: decision.reason }].slice(-10);
        try {
          kill(victim.pid);
        } catch (error) {
          warn(`[memory-guard] could not stop ${victim.label}: ${(error as Error).message}`);
        }
      }
    } catch (error) {
      warn(`[resources] sample failed: ${(error as Error).message}`);
    }
  }, sampleMs);
  timer.unref();
  return stats;
}
