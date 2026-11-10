// Route child stderr lines by their own log level.
//
// Replit labels everything a deployment writes to stderr as ERROR. Python
// logging and Celery write every record, INFO included, to stderr, so on
// 2026-11-09 29,925 of 31,829 deployment log lines were labelled ERROR and
// the real failures could not be filtered from routine task chatter. The
// gateway relays each child's stderr; lines that carry an INFO or DEBUG
// level marker now go to stdout. Everything else (warnings, errors,
// tracebacks, unmarked lines) stays on stderr, so nothing that might be an
// error is ever relabelled as routine.

const ROUTINE_PATTERNS: RegExp[] = [
  // Celery: "[2026-11-09 16:07:30,767: INFO/MainProcess] ..."
  /^\[\d{4}-\d{2}-\d{2} [\d:,.]+: (?:INFO|DEBUG)\/[\w.-]+\]/,
  // Python logging: "2026-11-09 22:31:20,174 INFO paper-short ..."
  /^\d{4}-\d{2}-\d{2}[ T][\d:,.]+ (?:INFO|DEBUG) /,
  // Uvicorn: "INFO:     127.0.0.1:52338 - ..."
  /^(?:INFO|DEBUG):\s/,
];

export function isRoutineLogLine(line: string): boolean {
  return ROUTINE_PATTERNS.some((pattern) => pattern.test(line));
}

// A partial line longer than this is flushed as-is (to stderr) rather than
// buffered without bound.
const MAX_PENDING = 64 * 1024;

export type LineSink = (text: string) => void;

// Returns a chunk handler that splits a stream into lines and writes each to
// `routine` or `other`; `flush` writes any trailing partial line.
export function createStderrRouter(routine: LineSink, other: LineSink): {
  write: (chunk: Buffer | string) => void;
  flush: () => void;
} {
  let pending = "";
  const emit = (lines: string[]): void => {
    // Keep runs of the same destination together: one write per run.
    let run: string[] = [];
    let runRoutine = false;
    const send = (): void => {
      if (run.length) (runRoutine ? routine : other)(run.join(""));
      run = [];
    };
    for (const line of lines) {
      const routineLine = isRoutineLogLine(line);
      if (run.length && routineLine !== runRoutine) send();
      runRoutine = routineLine;
      run.push(line);
    }
    send();
  };
  return {
    write(chunk) {
      pending += String(chunk);
      const lastNewline = pending.lastIndexOf("\n");
      if (lastNewline === -1) {
        if (pending.length > MAX_PENDING) {
          other(pending);
          pending = "";
        }
        return;
      }
      const complete = pending.slice(0, lastNewline + 1);
      pending = pending.slice(lastNewline + 1);
      emit(complete.match(/[^\n]*\n/g) ?? []);
    },
    flush() {
      if (pending) other(pending);
      pending = "";
    },
  };
}
