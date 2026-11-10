"""Outside uptime check for the deployed paper bot (run by .github/workflows/uptime.yml).

Opens one GitHub issue when the deployment stops working and closes it when it
recovers, so a freeze is noticed without anyone opening the dashboard:

- "down": /api/startup is not ready, or /api/heartbeat fails (worker, beat or
  the five-minute monitoring jobs stopped), on three tries about two minutes
  apart. The retries ride out a publish or a watchdog restart.
- "halted": the app is up but trading has been halted for 30+ minutes during
  regular market hours, which a short automatic pause never reaches.

With UPTIME_WATCH_SECONDS set, one run keeps checking every
UPTIME_INTERVAL_SECONDS until that much time has passed, so a freeze is
reported within minutes. GitHub's scheduled runs alone are far too sparse: on
2026-11-09 the */10 cron ran twice overnight and a 19:18 ET freeze was only
reported at 22:34. The workflow chains the next watch run itself.

Stdlib only. One request at a time, never concurrent, to keep load on the
small VM negligible. Uses the gh CLI with GH_TOKEN for issues.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

APP_URL = os.environ.get("APP_URL", "https://frozenstock.replit.app").rstrip("/")
MENTION = os.environ.get("ALERT_MENTION", "").strip()
TRIES = int(os.environ.get("UPTIME_TRIES", "3"))
RETRY_SECONDS = int(os.environ.get("UPTIME_RETRY_SECONDS", "120"))
WATCH_SECONDS = int(os.environ.get("UPTIME_WATCH_SECONDS", "0"))
INTERVAL_SECONDS = int(os.environ.get("UPTIME_INTERVAL_SECONDS", "120"))
TIMEOUT_SECONDS = 25
HALT_ALERT_SECONDS = 30 * 60
ET = ZoneInfo("America/New_York")

LABELS = {
    "down": ("uptime: down", "b60205", "The deployed bot is not responding or its workers stopped"),
    "halted": ("uptime: halted", "d93f0b", "Trading has been halted for 30+ minutes in market hours"),
}
TITLES = {
    "down": "Frozen_stock deployment is down or frozen",
    "halted": "Frozen_stock trading has been halted for 30+ minutes",
}


def fetch(path: str) -> tuple[int | None, dict | None, str | None]:
    request = urllib.request.Request(f"{APP_URL}{path}", headers={"User-Agent": "frozen-stock-uptime-check"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, _json(response.read()), None
    except urllib.error.HTTPError as exc:
        return exc.code, _json(exc.read()), None
    except Exception as exc:  # timeout, DNS, connection reset
        return None, None, exc.__class__.__name__


def _json(raw: bytes) -> dict | None:
    try:
        value = json.loads(raw or b"null")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def probe() -> dict:
    """One pass: gateway startup, then the backend heartbeat."""
    startup_code, startup, startup_error = fetch("/api/startup")
    result = {"startup_code": startup_code, "startup_status": (startup or {}).get("status"),
              "boot_at": (startup or {}).get("boot_at"), "startup_error": startup_error,
              "heartbeat_code": None, "heartbeat": None, "heartbeat_error": None, "problems": []}
    if startup_code != 200 or result["startup_status"] != "ready":
        result["problems"].append(f"startup:{startup_error or startup_code}:{result['startup_status']}")
        return result
    code, body, error = fetch("/api/heartbeat")
    result.update(heartbeat_code=code, heartbeat=body, heartbeat_error=error)
    if code in (401, 404):
        # A deployment from before the public heartbeat route (unknown paths
        # are refused by auth before routing): judge it on startup alone.
        return result
    if code != 200:
        detail = ",".join((body or {}).get("problems") or []) or error or str(code)
        result["problems"].append(f"heartbeat:{detail}")
    return result


def market_hours(now: datetime) -> bool:
    local = now.astimezone(ET)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60


def evaluate(attempts: list[dict], now: datetime) -> dict[str, bool]:
    """Which alerts should be open. ``down`` needs every try to fail."""
    down = bool(attempts) and all(item["problems"] for item in attempts)
    last = attempts[-1] if attempts else {}
    beat = last.get("heartbeat") or {}
    halted = (
        not down
        and not last.get("problems")
        and beat.get("tradable") is False
        and (beat.get("halted_for_seconds") or 0) >= HALT_ALERT_SECONDS
        and market_hours(now)
    )
    return {"down": down, "halted": bool(halted)}


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def open_issue(kind: str) -> int | None:
    rows = json.loads(gh("issue", "list", "--state", "open", "--label", LABELS[kind][0],
                         "--json", "number", "--limit", "1") or "[]")
    return rows[0]["number"] if rows else None


def summary(attempts: list[dict]) -> str:
    lines = [f"Checked {APP_URL} {len(attempts)} time(s):", ""]
    for index, item in enumerate(attempts, 1):
        beat = item.get("heartbeat") or {}
        lines.append(
            f"{index}. startup={item['startup_code']} ({item['startup_status']}), "
            f"heartbeat={item['heartbeat_code']}, problems={', '.join(item['problems']) or 'none'}"
        )
        if beat:
            lines.append(
                f"   worker={beat.get('worker_alive')} beat={beat.get('beat_alive')} "
                f"monitor_age={beat.get('monitor_age_seconds')}s watchdog_age={beat.get('watchdog_age_seconds')}s "
                f"recovery={beat.get('recovery_status')} halted_for={beat.get('halted_for_seconds')}s"
            )
    return "\n".join(lines)


def reconcile_issues(wanted: dict[str, bool], attempts: list[dict], now: datetime) -> None:
    stamp = now.astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    for kind, should_be_open in wanted.items():
        number = open_issue(kind)
        if should_be_open and number is None:
            label, color, description = LABELS[kind]
            gh("label", "create", label, "--color", color, "--description", description, "--force")
            hint = (
                "Check the Replit deployment logs, then `/api/startup`. The gateway's stall watchdog "
                "restarts a frozen backend after 3 minutes. If the logs have stopped entirely, the "
                "whole app is frozen and only a republish brings it back."
                if kind == "down" else
                "Open the dashboard's recovery panel to see the pause reason; most halts self-clear "
                "once the broker ledger matches again."
            )
            body = f"{(MENTION + ' ') if MENTION else ''}Detected at {stamp}.\n\n{summary(attempts)}\n\n{hint}\n\n" \
                   "This issue closes itself when the next check passes."
            gh("issue", "create", "--title", TITLES[kind], "--label", label, "--body", body)
            print(f"opened {kind} alert")
        elif not should_be_open and number is not None:
            gh("issue", "close", str(number), "--comment", f"Recovered at {stamp}.\n\n{summary(attempts)}")
            print(f"closed {kind} alert #{number}")


def check_round(sleep=time.sleep) -> list[dict]:
    """Up to TRIES probes; stops at the first one that passes."""
    attempts: list[dict] = []
    for index in range(TRIES):
        attempts.append(probe())
        print(json.dumps(attempts[-1], default=str), flush=True)
        if not attempts[-1]["problems"]:
            break
        if index + 1 < TRIES:
            sleep(RETRY_SECONDS)
    return attempts


def round_seconds() -> int:
    """Longest one round can take: every try timing out, plus the retries."""
    return TRIES * 2 * TIMEOUT_SECONDS + (TRIES - 1) * RETRY_SECONDS


def main(sleep=time.sleep, clock=time.monotonic) -> int:
    dry_run = os.environ.get("UPTIME_DRY_RUN") == "1"
    deadline = clock() + max(0, WATCH_SECONDS)
    last_wanted: dict[str, bool] | None = None
    rounds = 0
    while True:
        attempts = check_round(sleep)
        rounds += 1
        now = datetime.now(timezone.utc)
        wanted = evaluate(attempts, now)
        print(f"round {rounds} alerts: {wanted}", flush=True)
        # Issues only change when the verdict does, so a long watch costs two
        # GitHub API calls per change rather than per round.
        if not dry_run and wanted != last_wanted:
            try:
                reconcile_issues(wanted, attempts, now)
                last_wanted = wanted
            except subprocess.CalledProcessError as exc:  # retried next round
                print(f"issue update failed: {exc.stderr or exc}", flush=True)
        if clock() + INTERVAL_SECONDS + round_seconds() > deadline:
            return 0
        sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
