#!/usr/bin/env bash
set -euo pipefail

base_url="${1:?usage: $0 https://host.example release-id}"
expected_release="${2:?usage: $0 https://host.example release-id}"
startup_url="${base_url%/}/api/startup"
payload="$(curl --fail --silent --show-error "$startup_url")"

PAYLOAD="$payload" EXPECTED_RELEASE="$expected_release" node <<'NODE'
const payload = JSON.parse(process.env.PAYLOAD);
const expected = process.env.EXPECTED_RELEASE;
const problems = [];
if (payload.status !== "ready") problems.push(`status=${payload.status}`);
if (payload.build_id !== expected) problems.push(`build_id=${payload.build_id ?? "missing"}`);
if (payload.paper_only !== true) problems.push("paper_only is not true");
if (payload.live_trading !== false) problems.push("live_trading is not false");
if (problems.length) {
  console.error(`Hosted release verification failed: ${problems.join(", ")}`);
  process.exit(1);
}
console.log(`Hosted release verified: ${payload.build_id}`);
NODE
