// Last-resort watchdog for the gateway's own event loop.
//
// The main thread stamps a shared heartbeat every second. A worker thread,
// which keeps running while the main thread is stuck, checks it; if the
// heartbeat is older than GATEWAY_STALL_SECONDS (default 180) it SIGKILLs
// every tracked child process and then the gateway itself, so the hosting
// platform restarts the deployment instead of leaving it wedged and silent.
// Children are killed first so none is orphaned holding a port or a lease.
import { Worker } from "node:worker_threads";

const SLOTS = 128;
const stallSeconds = Math.max(30, Number(process.env.GATEWAY_STALL_SECONDS ?? 180));
const shared = new SharedArrayBuffer(4 * (SLOTS + 1));
const cells = new Int32Array(shared);

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

export function trackChildPid(pid: number | undefined): void {
  if (!pid) return;
  for (let index = 1; index <= SLOTS; index += 1) {
    if (Atomics.compareExchange(cells, index, 0, pid) === 0) return;
  }
}

export function untrackChildPid(pid: number | undefined): void {
  if (!pid) return;
  for (let index = 1; index <= SLOTS; index += 1) {
    Atomics.compareExchange(cells, index, pid, 0);
  }
}

const WATCHDOG_SOURCE = `
const { workerData } = require("node:worker_threads");
const fs = require("node:fs");
const cells = new Int32Array(workerData.shared);
const stall = workerData.stallSeconds;
setInterval(() => {
  const last = Atomics.load(cells, 0);
  const now = Math.floor(Date.now() / 1000);
  if (last === 0 || now - last <= stall) return;
  try {
    fs.writeSync(2, "[gateway] event loop stalled for " + (now - last) + "s; killing children and exiting so the deployment restarts\\n");
  } catch {}
  for (let index = 1; index < cells.length; index += 1) {
    const pid = Atomics.load(cells, index);
    if (pid > 0) { try { process.kill(pid, "SIGKILL"); } catch {} }
  }
  try { process.kill(process.pid, "SIGKILL"); } catch {}
}, 5000);
`;

export function startStallWatchdog(): void {
  if (process.env.GATEWAY_STALL_WATCHDOG === "false") return;
  Atomics.store(cells, 0, nowSeconds());
  setInterval(() => Atomics.store(cells, 0, nowSeconds()), 1000).unref();
  const worker = new Worker(WATCHDOG_SOURCE, { eval: true, workerData: { shared, stallSeconds } });
  worker.unref();
  worker.on("error", (error: Error) => console.error(`Gateway stall watchdog failed: ${error.message}`));
}
