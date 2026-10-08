import { createHmac, randomBytes } from "node:crypto";
import { spawn, type ChildProcess } from "node:child_process";

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
const allowedRoles = new Set(["viewer", "researcher", "operator", "admin"]);
// This entrypoint is only used for the paper deployment. Keep the worker stack
// on by default so a missing deployment variable cannot silently publish an API
// with no scheduler or learning workers.
const paperWorkersEnabled = process.env.PAPER_WORKERS_ENABLED !== "false";
const embeddedRedisPort = 6380;
const configuredRedisUrl = process.env.REDIS_URL?.trim();
const useEmbeddedRedis =
  paperWorkersEnabled &&
  process.env.PAPER_EMBEDDED_REDIS === "true";
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

let stopping = false;

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
  PAPER_DIRECT_MONITOR_LOOP: "true",
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

const python = spawn(
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
    stdio: "inherit",
  },
);

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
  child.on("exit", (code, signal) => {
    if (stopping) return;
    console.error(`Paper worker exited; restarting ${label}: code=${code} signal=${signal}`);
    setTimeout(() => {
      if (!stopping) supervisePaperWorker(args, label);
    }, 2000);
  });
  return child;
}

if (paperWorkersEnabled) {
  supervisePaperWorker(["-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker", "--loglevel=INFO", "--hostname=intraday@%h", "--queues=intraday_market_data", "--concurrency=1"], "intraday");
  supervisePaperWorker(["-m", "celery", "-A", "app.tasks.celery_app:celery_app", "worker", "--loglevel=INFO", "--hostname=research@%h", "--queues=research_market_data", "--concurrency=1"], "research");
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

const app = express();
app.use(CLERK_PROXY_PATH, clerkProxyMiddleware());

// Replit probes both the deployment root and this artifact's `/api` mount.
// Keep both responses dependency-free and immediate; detailed readiness checks
// remain under `/api/health` and `/api/ready`.
app.get(["/", "/api"], (_req, res) => {
  res.status(200).json({
    status: "ok",
    service: "frozen-stock-paper-gateway",
    paper_only: true,
    live_trading: false,
  });
});

app.use(
  clerkMiddleware((req) => ({
    publishableKey: publishableKeyFromHost(
      getClerkProxyHost(req) ?? "",
      process.env.CLERK_PUBLISHABLE_KEY,
    ),
  })),
);

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

    const auth = getAuth(req);
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

const server = app.listen(publicPort, "0.0.0.0", () => {
  console.log(`Production authentication gateway listening on ${publicPort}`);
});

function stop(signal: NodeJS.Signals, exitCode?: number) {
  if (stopping) return;
  stopping = true;
  server.close(() => {
    for (const worker of workerProcesses) {
      if (!worker.killed) worker.kill(signal);
    }
    if (redisProcess && !redisProcess.killed) redisProcess.kill(signal);
    if (!python.killed) python.kill(signal);
    if (exitCode !== undefined) process.exit(exitCode);
  });
}

process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));
python.on("exit", (code, signal) => {
  console.error(`FastAPI exited before gateway shutdown: code=${code} signal=${signal}`);
  stop("SIGTERM", code ?? 1);
});

redisProcess?.on("error", (error) => {
  console.error(`Embedded Redis failed to start: ${error.message}`);
});
