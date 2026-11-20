import { request as httpRequest } from "node:http";

export type HealthProbeResult =
  | { ok: true; statusCode: number; body: string }
  | { ok: false; error: string };

/**
 * One GET against the API's health route that reports exactly once.
 *
 * On 2026-11-19 the startup probe sent its last request at 14:26:12 UTC and
 * never retried: the API answered with headers, the body arrived after the
 * timeout, and `request.destroy()` with no error fires neither the response's
 * `end` nor the request's `error`. Only those two scheduled the next probe,
 * so the gateway stayed in "starting" and never started the paper workers.
 * Here every path (body read, request error, timeout, a response or socket
 * that closes early) ends in `done`, and `done` runs once.
 */
export function probeHealth(
  url: string,
  timeoutMs: number,
  done: (result: HealthProbeResult) => void,
  maxBodyChars = 512,
): void {
  let settled = false;
  const settle = (result: HealthProbeResult) => {
    if (settled) return;
    settled = true;
    done(result);
  };
  // agent: false gives every probe a fresh connection, as the liveness probe
  // does: a reused keep-alive socket can race uvicorn's idle close.
  const probe = httpRequest(url, { method: "GET", timeout: timeoutMs, agent: false }, (response) => {
    let body = "";
    response.setEncoding("utf8");
    response.on("data", (chunk: string) => {
      if (body.length < maxBodyChars) body += chunk;
    });
    response.on("end", () => settle({ ok: true, statusCode: response.statusCode ?? 0, body }));
    response.on("error", (error) => settle({ ok: false, error: error.message }));
    response.on("close", () => {
      if (!response.complete) settle({ ok: false, error: "response closed before the body was complete" });
    });
  });
  probe.on("timeout", () => probe.destroy(new Error(`no complete answer in ${timeoutMs}ms`)));
  probe.on("error", (error) => settle({ ok: false, error: error.message }));
  probe.on("close", () => settle({ ok: false, error: "connection closed without an answer" }));
  probe.end();
}
