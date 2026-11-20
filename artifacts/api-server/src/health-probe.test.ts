import assert from "node:assert/strict";
import { createServer, type RequestListener, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { test } from "node:test";

import { probeHealth, type HealthProbeResult } from "./health-probe";

async function withServer(handler: RequestListener, run: (url: string) => Promise<void>): Promise<void> {
  const server: Server = createServer(handler);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  try {
    await run(`http://127.0.0.1:${port}/api/health`);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

// Collect every report for longer than the probe can take, so a second
// report (or none) shows up as a failure instead of passing silently.
function probeAll(url: string, timeoutMs: number, waitMs: number): Promise<HealthProbeResult[]> {
  const results: HealthProbeResult[] = [];
  probeHealth(url, timeoutMs, (result) => results.push(result));
  return new Promise((resolve) => setTimeout(() => resolve(results), waitMs));
}

test("a complete answer is reported once with its status and body", async () => {
  await withServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end('{"routes_ready":true}');
  }, async (url) => {
    const results = await probeAll(url, 1000, 300);
    assert.deepEqual(results, [{ ok: true, statusCode: 200, body: '{"routes_ready":true}' }]);
  });
});

test("headers on time but the body after the timeout still report once (2026-11-19 outage)", async () => {
  await withServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.flushHeaders();
    setTimeout(() => res.end('{"routes_ready":false}'), 800);
  }, async (url) => {
    const results = await probeAll(url, 200, 1200);
    assert.equal(results.length, 1, JSON.stringify(results));
    assert.equal(results[0].ok, false);
  });
});

test("no answer at all reports one timeout", async () => {
  await withServer(() => {
    // Never respond.
  }, async (url) => {
    const results = await probeAll(url, 200, 700);
    assert.equal(results.length, 1, JSON.stringify(results));
    assert.deepEqual(results[0], { ok: false, error: "no complete answer in 200ms" });
  });
});

test("a refused connection reports one error", async () => {
  const server = createServer();
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  await new Promise<void>((resolve) => server.close(() => resolve()));
  const results = await probeAll(`http://127.0.0.1:${port}/api/health`, 500, 400);
  assert.equal(results.length, 1, JSON.stringify(results));
  assert.equal(results[0].ok, false);
});

test("a connection dropped mid-body reports once", async () => {
  await withServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json", "content-length": "100" });
    res.write('{"routes_');
    setTimeout(() => res.socket?.destroy(), 50);
  }, async (url) => {
    const results = await probeAll(url, 1000, 500);
    assert.equal(results.length, 1, JSON.stringify(results));
    assert.equal(results[0].ok, false);
  });
});

test("a long body is kept to the first chunk past the limit", async () => {
  await withServer((_req, res) => {
    res.writeHead(200);
    res.end("x".repeat(5000));
  }, async (url) => {
    const results = await probeAll(url, 1000, 300);
    assert.equal(results.length, 1);
    assert.ok(results[0].ok && results[0].body.length <= 5000 && results[0].body.length >= 512);
  });
});
