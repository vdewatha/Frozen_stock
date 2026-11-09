// Non-blocking log output for the production gateway.
//
// On 2026-11-08 the published deployment went silent at 10:17 ET and stopped
// answering every request for a day, with no crash, OOM or restart and the VM
// still up: every process stopped logging at the same instant. The likeliest
// cause is the deployment's stdout pipe no longer draining. Every Python
// worker wrote straight into that pipe (stdio "inherit"), and the gateway
// copied the API's output into it with process.stdout.write, which on Linux is
// synchronous for pipes, so one stalled reader blocks every writer, including
// the gateway's event loop and therefore every HTTP request.
//
// This module makes the gateway's own stdout/stderr non-blocking and bounds
// what it buffers: once LOG_BUFFER_LIMIT bytes are waiting for the pipe,
// further log writes are dropped (and counted) instead of blocking or growing
// memory. Children write to pipes the gateway always reads, so a stalled log
// collector can no longer stop a worker or the API either.

const LOG_BUFFER_LIMIT = Number(process.env.GATEWAY_LOG_BUFFER_BYTES ?? 4 * 1024 * 1024);

type Guarded = NodeJS.WriteStream & { __logRelayGuarded?: boolean };

export const logRelayStats = { droppedBytes: 0, droppedWrites: 0, lastDropAt: undefined as string | undefined };

function chunkLength(chunk: unknown): number {
  if (typeof chunk === "string") return Buffer.byteLength(chunk);
  if (chunk instanceof Uint8Array) return chunk.byteLength;
  return 0;
}

function guard(stream: Guarded): void {
  if (stream.__logRelayGuarded) return;
  stream.__logRelayGuarded = true;
  // Pipes are written synchronously on Linux; make this fd non-blocking so a
  // full pipe queues in memory (bounded below) instead of stalling the loop.
  try {
    (stream as unknown as { _handle?: { setBlocking?: (b: boolean) => unknown } })._handle?.setBlocking?.(false);
  } catch {
    // Not a pipe/TTY handle (for example a file): writes are already safe.
  }
  // A write to a pipe whose reader has gone away must not crash the gateway.
  stream.on("error", () => {});
  const original = stream.write.bind(stream) as (...args: unknown[]) => boolean;
  let reportedDrops = 0;
  (stream as unknown as { write: (...args: unknown[]) => boolean }).write = (...args: unknown[]): boolean => {
    if (stream.writableLength > LOG_BUFFER_LIMIT) {
      logRelayStats.droppedBytes += chunkLength(args[0]);
      logRelayStats.droppedWrites += 1;
      logRelayStats.lastDropAt = new Date().toISOString();
      const callback = args.find((arg) => typeof arg === "function") as (() => void) | undefined;
      callback?.();
      return false;
    }
    if (logRelayStats.droppedWrites > reportedDrops) {
      const dropped = logRelayStats.droppedWrites - reportedDrops;
      reportedDrops = logRelayStats.droppedWrites;
      original(`[gateway] log output was backed up; dropped ${dropped} write(s) (total ${logRelayStats.droppedBytes} bytes)\n`);
    }
    return original(...args);
  };
}

export function installNonBlockingStdio(): void {
  guard(process.stdout as Guarded);
  guard(process.stderr as Guarded);
}
