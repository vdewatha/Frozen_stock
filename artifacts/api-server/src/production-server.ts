import { createHmac, randomBytes } from "node:crypto";
import { spawn, type ChildProcess } from "node:child_process";
import { dirname, join } from "node:path";
import { request as httpRequest } from "node:http";
import { fileURLToPath } from "node:url";

import { clerkMiddleware, getAuth } from "@clerk/express";
import { publishableKeyFromHost } from "@clerk/shared/keys";
import express from "express";
import { createProxyMiddleware } from "http-proxy-middleware";

import {
  CLERK_PROXY_PATH,
  clerkProxyMiddleware,
  getClerkProxyHost,
} from "./middlewares/clerkProxyMiddleware";

const publicPort = Number(process.env.PORT);
if (!Number.isInteger(publicPort) || publicPort <= 0) {
  throw new Error("PORT must be a positive integer");
}

const internalPort = 8090;
const internalSecret = randomBytes(32).toString("hex");
const internalTarget = `http://127.0.0.1:${internalPort}`;
const buildId = process.env.PAPER_BUILD_ID?.trim() || "unknown";
const allowedRoles = new Set(["viewer", "researcher", "operator", "admin"]);
const clerkConfigured = Boolean(process.env.CLERK_SECRET_KEY?.trim());
// Autoscale is a gateway host, not a durable worker host. Workers must be
// explicitly enabled on a single-instance deployment with shared Redis;
// otherwise every scaled instance could create duplicate schedulers.
const paperWorkersEnabled = process.env.PAPER_WORKERS_ENABLED === "true";
// Replit Autoscale has a small per-instance memory budget.  A dozen separate
// prefork workers can evict the API before its health probe succeeds, so the
// hosted paper deployment uses one bounded, thread-based worker pool.  Local
// Docker deployments keep their queue-isolated services unchanged.
const compactPaperWorker = process.env.PAPER_COMPACT_WORKER === "true";
const embeddedRedisPort = 6380;
const configuredRedisUrl = process.env.REDIS_URL?.trim();
const researchLoopsEnabled =
  process.env.PAPER_DIRECT_RESEARCH_LOOP === "true" ||
  process.env.PAPER_DIRECT_SCALP_RESEARCH_LOOP === "true";
// Research-only Autoscale instances may use a private Redis sidecar when no
// shared REDIS_URL is configured. This is deliberately separate from the
// paper-worker Redis switch and cannot start an execution worker.
const researchEmbeddedRedis =
  researchLoopsEnabled && process.env.PAPER_RESEARCH_EMBEDDED_REDIS === "true";
const useEmbeddedRedis =
  (paperWorkersEnabled && process.env.PAPER_EMBEDDED_REDIS === "true") ||
  researchEmbeddedRedis;
const redisUrl = useEmbeddedRedis
  ? `redis://127.0.0.1:${embeddedRedisPort}/0`
  : configuredRedisUrl;

const redisProcess = useEmbeddedRedis
  ? spawn("redis-server", [
      "--bind", "127.0.0.1",
      "--port", String(embeddedRedisPort),
      "--save", "",
      "--appendonly", "no",
      "--protected-mode", "yes",
    ], { stdio: "inherit" })
  : undefined;

function boundedConcurrency(name: string, fallback: number, maximum: number): number {
  const parsed = Number.parseInt(process.env[name] ?? "", 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(maximum, Math.max(1, parsed));
}

const learningConcurrency = boundedConcurrency("PAPER_LEARNING_CONCURRENCY", 2, 8);
const learningReplicas = boundedConcurrency("PAPER_LEARNING_REPLICAS", 3, 8);
const scalpResearchReplicas = boundedConcurrency("PAPER_SCALP_RESEARCH_REPLICAS", 1, 4);

let stopping = false;
let backendReady = false;
let startupState: "migrating" | "starting" | "ready" | "failed" = "migrating";
let startupError: string | undefined;
let migrationProcess: ChildProcess | undefined;
let lastBackendProbeError: string | undefined;
let backendDiagnostic = "";
// Hosted instances can spend several minutes importing the paper API's large
// SQLAlchemy/Pydantic route graph. Keep the gateway alive long enough for the
// child to finish its bounded background import instead of recycling it every
// 150 seconds and creating an endless cold-start loop. This remains finite so
// a genuinely failed child is still recycled and reported as failed.
const backendHealthMaxAttempts = 1200;

function appendBackendDiagnostic(chunk: Buffer | string): void {
  const text = String(chunk)
    .replace(/(?:[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|URL|ACCOUNT_ID)[A-Z0-9_]*\s*[=:]\s*)[^\s,}]+/gi, "<redacted>")
    .replace(/(?:Bearer\s+)[^\s]+/gi, "Bearer <redacted>")
    .slice(-2000);
  backendDiagnostic = `${backendDiagnostic}${text}`.slice(-4000);
}

function observeChildOutput(child: ChildProcess, label: string): void {
  child.stdout?.on("data", (chunk) => {
    appendBackendDiagnostic(`[${label}] ${chunk}`);
    process.stdout.write(chunk);
  });
  child.stderr?.on("data", (chunk) => {
    appendBackendDiagnostic(`[${label} stderr] ${chunk}`);
    process.stderr.write(chunk);
  });
}

const runtimeEnv = {
  ...process.env,
  ...(redisUrl ? { REDIS_URL: redisUrl } : {}),
  AUTH_MODE: "clerk_gateway",
  INTERNAL_AUTH_SECRET: internalSecret,
  ALLOW_LIVE_TRADING: "false",
  // Seed the trusted research cache once after a production restart. The beat
  // wrapper deduplicates this through Redis and this task never authorizes
  // orders; operators can explicitly disable it when needed.
  PAPER_STARTUP_REFRESH: process.env.PAPER_STARTUP_REFRESH ?? "true",
  // The direct monitor owns the monitoring cadence in this supervisor. Keep
  // Celery beat from running a second copy of the same task.
  PAPER_DIRECT_MONITOR_LOOP: process.env.PAPER_DIRECT_MONITOR_LOOP ?? "true",
  // Let the worker heartbeat identify the bounded hosted queue topology.
  // Readiness still requires a live expiring heartbeat; this is not a static
  // configuration grant.
  PAPER_WORKER_QUEUE_MODE:
    process.env.PAPER_WORKER_QUEUE_MODE ??
    (process.env.PAPER_COMPACT_WORKER === "true" ? "compact" : "dedicated"),
  PYTHONPATH: ".",
};

function roleMappings(): Record<string, string> {
  try {
    const parsed = JSON.parse(process.env.CLERK_ROLE_MAPPINGS || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const mappings: Record<string, string> = {};
    for (const [subject, role] of Object.entries(parsed)) {
      if (subject.trim() && typeof role === "string" && allowedRoles.has(role)) {
        mappings[subject] = role;
      }
    }
    return mappings;
  } catch {
    return {};
  }
}

const app = express();
app.use(CLERK_PROXY_PATH, clerkProxyMiddleware());

// Serve the frontend from the same immutable release as the API. Replit's
// deployment build produces this directory before starting the gateway.
const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), "../../strategy-control-room/dist/public");
app.use(express.static(frontendRoot, { index: "index.html" }));

// Bind the public paper-only gateway before database migration or worker
// startup. Hosting probes must get an immediate response during boot.
app.get(["/", "/api", "/api/health"], (_req, res) => {
  res.status(200).json({
    status: "ok",
    service: "frozen-stock-paper-gateway",
    build_id: buildId,
    paper_only: true,
    live_trading: false,
  });
});

// Keep deployment diagnostics separate from liveness. Replit can reach this
// process while migrations or FastAPI startup are still in progress, but it
// must not receive a proxy timeout that hides the actual startup state.
app.get("/api/startup", (_req, res) => {
  const ready = backendReady && clerkConfigured;
  res.status(ready ? 200 : 503).json({
    status: backendReady && !clerkConfigured ? "auth_missing" : startupState,
    build_id: buildId,
    paper_only: true,
    live_trading: false,
    auth_configured: clerkConfigured,
    ...(startupError ? { error: startupError } : {}),
    // Keep hosted startup failures diagnosable without exposing credentials.
    // appendBackendDiagnostic redacts secret-shaped assignments before this
    // bounded output is retained.
    ...(!ready && lastBackendProbeError ? { backend_probe: lastBackendProbeError } : {}),
    ...(!ready && backendDiagnostic.trim() ? { backend_output: backendDiagnostic.trim() } : {}),
  });
});

app.use("/api", (req, res, next) => {
  if (req.path === "/health" || req.path === "/startup") {
    next();
    return;
  }
  if (!backendReady) {
    res.status(503).json({
      status: startupState,
      detail: "Paper API is not ready; inspect /api/startup",
      paper_only: true,
      live_trading: false,
    });
    return;
  }
  next();
});

// The hosted supervisor does not use the local Docker migration service. Apply
// the checked-in schema before starting FastAPI so a new release cannot boot
// against a database that is missing one of its required tables. The migration
// runs asynchronously after the gateway has bound its public port.
let python: ChildProcess | undefined;

function spawnPaperWorker(args: string[]): ChildProcess {
  return spawn("python3.11", args, {
        cwd: new URL("../backend", import.meta.url),
        env: runtimeEnv,
        stdio: "inherit",
  });
}

const workerProcesses: ChildProcess[] = [];

function supervisePaperWorker(args: string[], label: string): ChildProcess {
  const child = spawnPaperWorker(args);
  workerProcesses.push(child);
  let restartScheduled = false;
  const scheduleRestart = (reason: string): void => {
    if (stopping || restartScheduled) return;
    restartScheduled = true;
    console.error(`Paper worker unavailable; restarting ${label}: ${reason}`);
    setTimeout(() => {
      if (!stopping) supervisePaperWorker(args, label);
    }, 2000);
  };
  child.on("error", (error) => {
    scheduleRestart(`spawn error=${error.message}`);
  });
  child.on("exit", (code, signal) => {
    scheduleRestart(`exit code=${code} signal=${signal}`);
  });
  return child;
}

function startBackendAfterMigration(): void {
  startupState = "starting";
  startupError = undefined;
  backendDiagnostic = "";
  lastBackendProbeError = undefined;
  python = spawn(
    "python3.11",
    [
      "-m",
      "uvicorn",
      "app.main:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(internalPort),
    ],
    {
      cwd: new URL("../backend", import.meta.url),
      env: runtimeEnv,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  observeChildOutput(python, "fastapi");
  python.on("error", (error) => {
    backendReady = false;
    startupState = "failed";
    startupError = `paper API process error: ${error.message}${
      backendDiagnostic.trim() ? `; child_output=${backendDiagnostic.trim()}` : ""
    }`;
    console.error(startupError);
  });
  observePythonExit(python);
  waitForBackendHealth();
}

function startPaperWorkers(): void {
  if (!paperWorkersEnabled) return;
  if (compactPaperWorker) {
    supervisePaperWorker([
      "-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker",
      "--loglevel=INFO", "--hostname=paper-core@%h",
      "--queues=default,market_data,intraday_market_data,research_market_data,scalp_research,learning,paper_trading,risk,monitoring",
      // Only reserve tasks a free thread can start. With the default prefetch,
      // a core busy with long learning or import jobs held minutely intraday
      // imports until their 55-second expiry and they were discarded.
      "--pool=threads", "--concurrency=2", "--prefetch-multiplier=1",
    ], "paper-core");
    // Time-critical lane: long research and learning jobs must never starve
    // the minutely intraday import, paper signals, risk or monitoring.
    supervisePaperWorker([
      "-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker",
      "--loglevel=INFO", "--hostname=paper-fast@%h",
      "--queues=intraday_market_data,paper_trading,risk,monitoring",
      "--pool=threads", "--concurrency=2", "--prefetch-multiplier=1",
    ], "paper-fast");
    // Recovery admission requires an independent heartbeat. Keep this
    // process outside the Celery pool so a queue backlog cannot mask a
    // stalled paper worker.
    supervisePaperWorker(["scripts/run_stock_watchdog.py"], "watchdog");
    // The compact worker path still needs the independent monitor heartbeat.
    // Without it, the watchdog correctly halts paper admission forever after
    // the previous heartbeat expires.
    if (process.env.PAPER_DIRECT_MONITOR_LOOP !== "false") {
      supervisePaperWorker(["scripts/run_stock_monitor_loop.py"], "direct-monitor");
    }
    supervisePaperWorker(["scripts/run_beat_with_lease.py"], "beat");
    return;
  }
  supervisePaperWorker(["-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker", "--loglevel=INFO", "--hostname=intraday@%h", "--queues=intraday_market_data", "--concurrency=1"], "intraday");
  supervisePaperWorker(["-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker", "--loglevel=INFO", "--hostname=research@%h", "--queues=research_market_data", "--concurrency=1"], "research");
  // Keep the isolated scalp lane independent so its research jobs cannot sit
  // behind the broader market-data queue.
  for (let replica = 1; replica <= scalpResearchReplicas; replica += 1) {
    supervisePaperWorker([
      "-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker",
      "--loglevel=INFO", "--hostname=scalp-research-" + replica + "@%h",
      "--queues=scalp_research", "--concurrency=1",
    ], `scalp-research-${replica}`);
  }
  supervisePaperWorker(["-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker", "--loglevel=INFO", "--hostname=market@%h", "--queues=default,market_data", "--concurrency=1"], "market");
  for (let replica = 1; replica <= learningReplicas; replica += 1) {
    supervisePaperWorker([
      "-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker",
      "--loglevel=INFO", "--hostname=learning-" + replica + "@%h",
      "--queues=learning", `--concurrency=${learningConcurrency}`,
    ], `learning-${replica}`);
  }
  supervisePaperWorker(["-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker", "--loglevel=INFO", "--hostname=execution@%h", "--queues=paper_trading", "--concurrency=1"], "execution");
  supervisePaperWorker(["-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker", "--loglevel=INFO", "--hostname=risk@%h", "--queues=risk", "--concurrency=1"], "risk");
  supervisePaperWorker(["-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker", "--loglevel=INFO", "--hostname=monitor@%h", "--queues=monitoring", "--concurrency=1"], "monitor");
  supervisePaperWorker(["scripts/run_stock_monitor_loop.py"], "direct-monitor");
  supervisePaperWorker(["scripts/run_stock_watchdog.py"], "watchdog");
  supervisePaperWorker(["scripts/run_beat_with_lease.py"], "beat");
}

if (clerkConfigured) {
  app.use(
    clerkMiddleware((req) => ({
      publishableKey: publishableKeyFromHost(
        getClerkProxyHost(req) ?? "",
        process.env.CLERK_PUBLISHABLE_KEY,
      ),
    })),
  );
} else {
  // Do not let an incomplete Clerk deployment crash the gateway. The backend
  // still receives no internal identity headers and therefore rejects every
  // protected request; operators get a normal 401 plus /api/startup remains
  // useful for diagnosing the missing deployment secret.
  console.error("Clerk secret is not configured; protected requests remain unauthenticated");
}

const apiProxy = createProxyMiddleware({
  target: internalTarget,
  changeOrigin: false,
  pathRewrite: (path) => `/api${path}`,
});

app.use(
  "/api",
  (req, _res, next) => {
    for (const name of [
      "x-internal-auth-actor",
      "x-internal-auth-role",
      "x-internal-auth-timestamp",
      "x-internal-auth-signature",
    ]) {
      delete req.headers[name];
    }

    const auth = clerkConfigured ? getAuth(req) : { userId: null };
    if (auth.userId) {
      const role = roleMappings()[auth.userId] || "viewer";
      const timestamp = String(Math.floor(Date.now() / 1000));
      const payload = `${timestamp}\n${auth.userId}\n${role}`;
      const signature = createHmac("sha256", internalSecret)
        .update(payload)
        .digest("hex");
      req.headers["x-internal-auth-actor"] = auth.userId;
      req.headers["x-internal-auth-role"] = role;
      req.headers["x-internal-auth-timestamp"] = timestamp;
      req.headers["x-internal-auth-signature"] = signature;
    }
    next();
  },
  apiProxy,
);

// Client-side routes must resolve to the same built application without
// intercepting API requests or deployment diagnostics.
app.get("/{*splat}", (req, res, next) => {
  if (req.path.startsWith("/api") || req.path.startsWith(CLERK_PROXY_PATH)) {
    next();
    return;
  }
  res.sendFile(join(frontendRoot, "index.html"), (error) => {
    if (error) next(error);
  });
});

const server = app.listen(publicPort, "0.0.0.0", () => {
  console.log(`Production authentication gateway listening on ${publicPort}`);
});

function runMigration(attempt = 1): void {
  startupState = "migrating";
  migrationProcess = spawn(
    "python3.11",
    ["-m", "alembic", "upgrade", "head"],
    {
      cwd: new URL("../backend", import.meta.url),
      env: runtimeEnv,
      stdio: "inherit",
    },
  );
  migrationProcess.on("error", (error) => {
    console.error(`Database migration process failed: ${error.message}`);
  });
  migrationProcess.on("exit", (code, signal) => {
    migrationProcess = undefined;
    if (code === 0) {
      startBackendAfterMigration();
      return;
    }
    const maxAttempts = 5;
    if (!stopping && attempt < maxAttempts) {
      const delayMs = 2 ** (attempt - 1) * 1000;
      console.error(
        `Database migration failed; retrying in ${delayMs}ms ` +
        `(attempt ${attempt}/${maxAttempts}): code=${code} signal=${signal}`,
      );
      setTimeout(() => runMigration(attempt + 1), delayMs);
      return;
    }
    startupState = "failed";
    startupError = `database migration failed after ${attempt} attempt(s)`;
    console.error(`${startupError}: code=${code} signal=${signal}`);
  });
}

function waitForBackendHealth(attempt = 1): void {
  if (stopping || backendReady || !python) return;
  const probe = httpRequest(
    `${internalTarget}/api/health`,
    { method: "GET", timeout: 1000 },
    (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk: string) => {
        if (body.length < 512) body += chunk;
      });
      response.on("end", () => {
      let health: { routes_ready?: boolean; route_error?: string } = {};
      try {
        health = JSON.parse(body) as typeof health;
      } catch {
        // Keep retrying until the child returns a complete JSON health record.
      }
      if (response.statusCode === 200 && health.routes_ready === true) {
        backendReady = true;
        startupState = "ready";
        startupError = undefined;
        startPaperWorkers();
        return;
      }
        lastBackendProbeError =
          `status=${response.statusCode} routes_ready=${String(health.routes_ready ?? false)}` +
          `${health.route_error ? ` route_error=${health.route_error}` : ""}` +
          ` body=${body.trim() || "<empty>"}`;
        console.error(`Internal FastAPI health probe failed: ${lastBackendProbeError}`);
      retryBackendHealth(attempt);
      });
    },
  );
  probe.on("error", (error) => {
    lastBackendProbeError = `request=${error.message}`;
    console.error(`Internal FastAPI health probe errored: ${lastBackendProbeError}`);
    retryBackendHealth(attempt);
  });
  probe.on("timeout", () => probe.destroy());
  probe.end();
}

function retryBackendHealth(attempt: number): void {
  if (stopping || backendReady || !python) return;
  if (attempt >= backendHealthMaxAttempts) {
    startupState = "failed";
    startupError = `paper API did not become healthy after startup${
      lastBackendProbeError ? ` (${lastBackendProbeError})` : ""
    }${backendDiagnostic.trim() ? `; child_output=${backendDiagnostic.trim()}` : ""}`;
    // A child that remains present but never binds its port is not recoverable
    // by another probe. Recycle that child and let the supervisor start a
    // fresh API process; the gateway remains paper-only and fail-closed while
    // the replacement is coming up.
    const staleChild = python;
    python = undefined;
    if (staleChild && !staleChild.killed) staleChild.kill("SIGTERM");
    setTimeout(() => {
      if (!stopping && !python) startBackendAfterMigration();
    }, 5_000);
    return;
  }
  setTimeout(() => waitForBackendHealth(attempt + 1), 500);
}

runMigration();

function stop(signal: NodeJS.Signals, exitCode?: number) {
  if (stopping) return;
  stopping = true;
  server.close(() => {
    for (const worker of workerProcesses) {
      if (!worker.killed) worker.kill(signal);
    }
    if (redisProcess && !redisProcess.killed) redisProcess.kill(signal);
    if (migrationProcess && !migrationProcess.killed) migrationProcess.kill(signal);
    if (python && !python.killed) python.kill(signal);
    if (exitCode !== undefined) process.exit(exitCode);
  });
}

process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));
function observePythonExit(child: ChildProcess): void {
  child.on("exit", (code, signal) => {
    console.error(`FastAPI exited before gateway shutdown: code=${code} signal=${signal}`);
    backendReady = false;
    startupState = "failed";
    startupError = `paper API exited before gateway shutdown: code=${code} signal=${signal}${
      backendDiagnostic.trim() ? `; child_output=${backendDiagnostic.trim()}` : ""
    }${lastBackendProbeError ? `; last_probe=${lastBackendProbeError}` : ""}`;
    // Keep the gateway alive so hosted operators can read /api/startup and
    // repair the runtime configuration without receiving a generic 502.
    python = undefined;
    // A hosted supervisor can terminate only the child during a rolling
    // restart or resource reclaim. Restart it in place so a transient child
    // exit cannot leave the public gateway permanently wedged at 503.
    if (!stopping) {
      setTimeout(() => {
        if (!stopping && !python) startBackendAfterMigration();
      }, 5_000);
    }
  });
}

redisProcess?.on("error", (error) => {
  console.error(`Embedded Redis failed to start: ${error.message}`);
});
