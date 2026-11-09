import assert from "node:assert/strict";
import { test } from "node:test";

import { createStderrRouter, isRoutineLogLine } from "./log-lines";

test("INFO and DEBUG records from Celery, Python logging and uvicorn are routine", () => {
  for (const line of [
    "[2026-11-09 16:07:30,767: INFO/MainProcess] stock paper cycle: symbols=48 submitted=0 blocked=277",
    "[2026-11-09 16:07:30,767: DEBUG/ForkPoolWorker-2] detail",
    "2026-11-09 22:31:20,194 INFO stock-monitor-loop status=warning snapshot_id=17204",
    'INFO:     127.0.0.1:52338 - "GET /api/health HTTP/1.1" 200 OK',
  ]) {
    assert.equal(isRoutineLogLine(line), true, line);
  }
});

test("warnings, errors, tracebacks and unmarked lines stay on stderr", () => {
  for (const line of [
    "[2026-11-09 15:07:27,624: ERROR/MainProcess] Task app.tasks.jobs.x raised unexpected: ValueError('Unknown strategy')",
    "[2026-11-09 15:19:18,440: WARNING/MainProcess] Scheduled job deadlock victim; retrying",
    "2026-11-09 21:10:49,698 WARNING stock-watchdog Ended an idle transaction",
    "ERROR:    Exception in ASGI application",
    "Traceback (most recent call last):",
    '  File "/home/runner/workspace/app/tasks/jobs.py", line 307, in _run_job',
    "KeyError: 'passive_arm'",
    "some unmarked output mentioning INFO later on",
  ]) {
    assert.equal(isRoutineLogLine(line), false, line);
  }
});

test("chunks are split into lines across boundaries and routed per line", () => {
  const out: string[] = [];
  const err: string[] = [];
  const router = createStderrRouter((t) => out.push(t), (t) => err.push(t));
  router.write("[2026-11-09 16:07:30,767: INFO/MainProcess] one\n[2026-11-09 16:07:31,000: ERR");
  router.write("OR/MainProcess] two\nTraceback (most recent call last):\n");
  router.write("2026-11-09 22:31:20,194 INFO three\npartial");
  assert.deepEqual(out.join(""), "[2026-11-09 16:07:30,767: INFO/MainProcess] one\n2026-11-09 22:31:20,194 INFO three\n");
  assert.deepEqual(err.join(""), "[2026-11-09 16:07:31,000: ERROR/MainProcess] two\nTraceback (most recent call last):\n");
  router.flush();
  assert.equal(err.join("").endsWith("partial"), true);
});
