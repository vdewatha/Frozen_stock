#!/usr/bin/env python3
"""Write the JSON report for one load-scenario run, or compare two reports.

The report is built only from the files the driver left in the run directory
(``meta.json``, ``events.jsonl``, ``samples.jsonl``, ``drill.json``). It
measures; it sets no pass mark. A section with no evidence is reported as
missing under ``coverage`` and never as a clean result.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

LOCK_NOT_AVAILABLE = "55P03"
DEADLOCK_DETECTED = "40P01"
SECTIONS = ("queue_delay", "lock_waits", "memory", "recovery")


def _percentile(values: Iterable[float], fraction: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _spread(values: list[float], digits: int = 3) -> dict:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "p50": round(_percentile(values, 0.50), digits),
        "p95": round(_percentile(values, 0.95), digits),
        "max": round(max(values), digits),
        "mean": round(sum(values) / len(values), digits),
    }


def _by_kind(events: list[dict], kind: str) -> list[dict]:
    return [event for event in events if event.get("kind") == kind]


def _queue_delay(events: list[dict], ended_epoch: float | None) -> dict:
    """Seconds from publish to ``task_prerun`` per queue.

    A task that never started is not a zero: it is counted, with how long the
    oldest one had waited when the run ended, unless its expiry dropped it.
    """
    sent = {event["id"]: event for event in _by_kind(events, "sent") if event.get("id")}
    started_ids = {event.get("id") for event in _by_kind(events, "started")}
    expired_ids = {event.get("id") for event in _by_kind(events, "revoked") if event.get("expired")}
    delays: dict[str, list[float]] = defaultdict(list)
    for event in _by_kind(events, "started"):
        queue = event.get("queue") or (sent.get(event.get("id")) or {}).get("queue")
        if queue and event.get("queue_delay_seconds") is not None:
            delays[queue].append(float(event["queue_delay_seconds"]))
    queues: dict[str, dict] = {}
    for queue in sorted({*delays, *(event.get("queue") for event in sent.values() if event.get("queue"))}):
        of_queue = [event for event in sent.values() if event.get("queue") == queue]
        waiting = [event for event in of_queue if event["id"] not in started_ids and event["id"] not in expired_ids]
        oldest = min((float(event["sent_at"]) for event in waiting if event.get("sent_at") is not None), default=None)
        queues[queue] = {
            "sent": len(of_queue),
            "started": len(delays.get(queue, [])),
            "expired_before_start": sum(1 for event in of_queue if event["id"] in expired_ids),
            "never_started": len(waiting),
            "oldest_unstarted_wait_seconds": (
                round(ended_epoch - oldest, 3) if waiting and oldest is not None and ended_epoch is not None else None
            ),
            "seconds": _spread(delays.get(queue, [])),
        }
    return queues


def _tasks(events: list[dict]) -> dict:
    names = sorted({str(event.get("task")) for event in events if event.get("task")})
    finished_ids = {event.get("id") for event in _by_kind(events, "finished")}
    summary: dict[str, dict] = {}
    for name in names:
        of_task = [event for event in events if event.get("task") == name]
        finished = _by_kind(of_task, "finished")
        started = _by_kind(of_task, "started")
        summary[name] = {
            "sent": len(_by_kind(of_task, "sent")),
            "started": len(started),
            "finished": dict(sorted(Counter(str(event.get("state")) for event in finished).items())),
            "result_status": dict(sorted(Counter(
                str(event.get("result_status")) for event in finished if event.get("result_status")).items())),
            "failed": dict(sorted(Counter(str(event.get("error_type")) for event in _by_kind(of_task, "failed")).items())),
            "expired_before_start": sum(1 for event in _by_kind(of_task, "revoked") if event.get("expired")),
            # Started and never finished: in flight when its worker was killed
            # or when the run ended.
            "started_not_finished": sum(1 for event in started if event.get("id") not in finished_ids),
            "runtime_seconds": _spread([
                float(event["runtime_seconds"]) for event in finished if event.get("runtime_seconds") is not None]),
        }
    return summary


def _sqlstate_counts(events: list[dict]) -> dict:
    counts = Counter(str(event.get("sqlstate")) for event in _by_kind(events, "db_error") if event.get("sqlstate"))
    by_task = Counter(
        f"{event.get('sqlstate')}:{event.get('task') or 'outside a task'}"
        for event in _by_kind(events, "db_error")
        if event.get("sqlstate") in {LOCK_NOT_AVAILABLE, DEADLOCK_DETECTED}
    )
    return {
        LOCK_NOT_AVAILABLE: counts.get(LOCK_NOT_AVAILABLE, 0),
        DEADLOCK_DETECTED: counts.get(DEADLOCK_DETECTED, 0),
        "other": {state: count for state, count in sorted(counts.items())
                  if state not in {LOCK_NOT_AVAILABLE, DEADLOCK_DETECTED}},
        "by_task": dict(sorted(by_task.items())),
    }


def _lock_waits(samples: list[dict], events: list[dict], meta: dict) -> dict:
    sampled = [sample for sample in samples if "lock_waits" in sample]
    waits = [wait for sample in sampled for wait in sample["lock_waits"]]
    before, after = meta.get("postgres_deadlocks_before"), meta.get("postgres_deadlocks_after")
    return {
        "samples": len(sampled),
        "samples_failed": len(samples) - len(sampled),
        "samples_with_a_waiting_session": sum(1 for sample in sampled if sample["lock_waits"]),
        "max_waiting_sessions": max((len(sample["lock_waits"]) for sample in sampled), default=0),
        # How long the waiting statement had been running when sampled: an
        # upper bound on the wait itself.
        "longest_wait_seconds": round(max((float(wait.get("waited_seconds") or 0) for wait in waits), default=0.0), 3),
        "by_wait_event": dict(sorted(Counter(str(wait.get("wait_event")) for wait in waits).items())),
        "max_idle_in_transaction": max((int(sample.get("idle_in_transaction") or 0) for sample in sampled), default=0),
        "max_sessions": max((int(sample.get("sessions") or 0) for sample in sampled), default=0),
        "sqlstate_counts": _sqlstate_counts(events),
        "postgres_deadlocks": None if before is None or after is None else int(after) - int(before),
    }


def _memory(samples: list[dict], meta: dict) -> dict:
    series: dict[str, list[float]] = defaultdict(list)
    totals: list[float] = []
    for sample in samples:
        values = {label: value for label, value in (sample.get("memory_mb") or {}).items() if value is not None}
        for label, value in values.items():
            series[label].append(float(value))
        if values:
            totals.append(sum(values.values()))
    return {
        "source": meta.get("memory_source"),
        "processes": {
            label: {
                "samples": len(values), "first_mb": round(values[0], 1), "peak_mb": round(max(values), 1),
                "last_mb": round(values[-1], 1), "mean_mb": round(sum(values) / len(values), 1),
            }
            for label, values in sorted(series.items())
        },
        "peak_total_mb": round(max(totals), 1) if totals else None,
    }


def _recovery(drill: dict, events: list[dict]) -> dict:
    result = dict(drill or {"status": "not_run", "reason": "no drill record"})
    if "@" in str(result.get("heartbeat_key") or ""):
        # The worker label is the evidence; the machine's name is not.
        result["heartbeat_key"] = result["heartbeat_key"].split("@", 1)[0] + "@<host>"
    killed_at, killed_pid = result.get("killed_at"), result.get("killed_pid")
    if killed_at is None:
        return result
    finished_ids = {event.get("id") for event in _by_kind(events, "finished")}
    on_killed = [event for event in _by_kind(events, "started") if event.get("pid") == killed_pid]
    in_flight = [
        event for event in on_killed
        if event.get("id") not in finished_ids and float(event.get("at") or 0) <= float(killed_at)
    ]
    # A task acknowledged when it started is gone with its worker; one that
    # acknowledges late (the intraday import) is delivered again.
    again = {
        event.get("id") for event in _by_kind(events, "started")
        if event.get("pid") != killed_pid and float(event.get("at") or 0) > float(killed_at)
    }
    result["tasks_in_flight_at_kill"] = sorted(str(event.get("task")) for event in in_flight)
    result["tasks_redelivered_after_kill"] = sorted(str(event.get("task")) for event in in_flight if event.get("id") in again)
    result["tasks_lost_at_kill"] = sorted(str(event.get("task")) for event in in_flight if event.get("id") not in again)
    worker = next((event.get("worker") for event in on_killed if event.get("worker")), None)
    after = sorted(
        float(event["at"]) for event in _by_kind(events, "started")
        if event.get("worker") == worker and event.get("pid") != killed_pid and float(event.get("at") or 0) > float(killed_at)
    ) if worker else []
    result["first_task_started_seconds"] = round(after[0] - float(killed_at), 2) if after else None
    return result


def build_report(meta: dict, events: list[dict], samples: list[dict], drill: dict) -> dict:
    report = {
        "report": "load_scenario",
        "paper_only": True,
        "live_trading": False,
        "schema": meta.get("schema"),
        "started_at": meta.get("started_at"),
        "elapsed_seconds": (
            round(float(meta["ended_epoch"]) - float(meta["started_epoch"]), 1)
            if meta.get("ended_epoch") is not None and meta.get("started_epoch") is not None else None
        ),
        "config": meta.get("config") or {},
        "layout": meta.get("layout") or {},
        "environment": meta.get("environment") or {},
        "queue_delay": _queue_delay(events, meta.get("ended_epoch")),
        "tasks": _tasks(events),
        "lock_waits": _lock_waits(samples, events, meta),
        "memory": _memory(samples, meta),
        "recovery": _recovery(drill, events),
    }
    report["coverage"] = _coverage(report)
    return report


def _coverage(report: dict) -> dict:
    measured = {
        "queue_delay": any(queue["started"] for queue in report["queue_delay"].values()),
        "lock_waits": report["lock_waits"]["samples"] > 0,
        "memory": bool(report["memory"]["processes"]),
        "recovery": report["recovery"].get("heartbeat_restored_seconds") is not None,
    }
    return {
        "complete": all(measured.values()),
        "measured": measured,
        "missing": [section for section in SECTIONS if not measured[section]],
    }


def _number(value: Any) -> str:
    return "-" if value is None else (f"{value:.2f}" if isinstance(value, float) else str(value))


def compare(first: dict, second: dict) -> list[str]:
    """Side-by-side lines for two reports: the numbers a rerun should reproduce."""
    lines = ["metric | first | second"]

    def row(label: str, left: Any, right: Any) -> None:
        lines.append(f"{label} | {_number(left)} | {_number(right)}")

    for queue in sorted({*first["queue_delay"], *second["queue_delay"]}):
        for field in ("p50", "p95", "max"):
            row(f"queue delay {queue} {field} (s)",
                (first["queue_delay"].get(queue) or {}).get("seconds", {}).get(field),
                (second["queue_delay"].get(queue) or {}).get("seconds", {}).get(field))
        for field in ("started", "expired_before_start", "never_started"):
            row(f"queue {queue} {field}",
                (first["queue_delay"].get(queue) or {}).get(field), (second["queue_delay"].get(queue) or {}).get(field))
    for field in ("samples_with_a_waiting_session", "max_waiting_sessions", "longest_wait_seconds", "postgres_deadlocks"):
        row(f"lock waits {field}", first["lock_waits"].get(field), second["lock_waits"].get(field))
    for state in (LOCK_NOT_AVAILABLE, DEADLOCK_DETECTED):
        row(f"sqlstate {state}", first["lock_waits"]["sqlstate_counts"].get(state),
            second["lock_waits"]["sqlstate_counts"].get(state))
    for label in sorted({*first["memory"]["processes"], *second["memory"]["processes"]}):
        for field in ("first_mb", "peak_mb", "last_mb"):
            row(f"memory {label} {field}", (first["memory"]["processes"].get(label) or {}).get(field),
                (second["memory"]["processes"].get(label) or {}).get(field))
    row("memory peak_total_mb", first["memory"].get("peak_total_mb"), second["memory"].get("peak_total_mb"))
    for field in ("process_restarted_seconds", "heartbeat_restored_seconds", "first_task_started_seconds"):
        row(f"recovery {field}", first["recovery"].get(field), second["recovery"].get(field))
    return lines


def _read_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, help="Directory the driver wrote meta.json, events.jsonl, samples.jsonl and drill.json to.")
    parser.add_argument("--output", type=Path, help="Where to write the JSON report.")
    parser.add_argument("--compare", nargs=2, type=Path, metavar="REPORT", help="Print two reports side by side.")
    args = parser.parse_args()
    if args.compare:
        print("\n".join(compare(*(json.loads(path.read_text(encoding="utf-8")) for path in args.compare))))
        return 0
    if not args.run_dir or not args.output:
        parser.error("--run-dir and --output are required unless --compare is given")
    report = build_report(
        _read_json(args.run_dir / "meta.json", {}),
        _read_lines(args.run_dir / "events.jsonl"),
        _read_lines(args.run_dir / "samples.jsonl"),
        _read_json(args.run_dir / "drill.json", {}),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"load_scenario_report={args.output} complete={report['coverage']['complete']} missing={report['coverage']['missing']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
