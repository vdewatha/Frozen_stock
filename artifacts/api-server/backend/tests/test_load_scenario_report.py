import importlib
import os
import re
import subprocess
from pathlib import Path

import pytest

from scripts.report_load_scenario import (
    _lock_waits,
    _memory,
    _percentile,
    _queue_delay,
    _recovery,
    _tasks,
    build_report,
    compare,
)

BACKEND = Path(__file__).resolve().parents[1]
RUNNER = BACKEND / "scripts" / "run_load_scenario.sh"
SERVER = BACKEND.parent / "src" / "production-server.ts"


def _sent(task_id, queue, sent_at, task="load_scenario.probe"):
    return {"kind": "sent", "id": task_id, "task": task, "queue": queue, "sent_at": sent_at, "at": sent_at}


def _started(task_id, queue, sent_at, delay, *, pid=10, worker="paper-core@host", task="load_scenario.probe"):
    return {"kind": "started", "id": task_id, "task": task, "queue": queue, "worker": worker, "pid": pid,
            "sent_at": sent_at, "started_at": sent_at + delay, "at": sent_at + delay, "queue_delay_seconds": delay}


def _finished(task_id, runtime, *, task="load_scenario.probe", status="complete"):
    return {"kind": "finished", "id": task_id, "task": task, "state": "SUCCESS", "result_status": status,
            "runtime_seconds": runtime, "at": 0}


def test_percentile_is_nearest_rank_and_empty_is_none():
    assert _percentile([], 0.95) is None
    assert _percentile([4.0, 1.0, 3.0, 2.0], 0.50) == 2.0
    assert _percentile([4.0, 1.0, 3.0, 2.0], 0.95) == 4.0


def test_queue_delay_is_measured_per_queue_from_sent_to_started():
    events = [
        _sent("a", "learning", 100.0), _started("a", "learning", 100.0, 2.0),
        _sent("b", "learning", 101.0), _started("b", "learning", 101.0, 40.0),
        _sent("c", "learning_fast", 102.0), _started("c", "learning_fast", 102.0, 0.05),
    ]
    queues = _queue_delay(events, ended_epoch=200.0)
    assert queues["learning"]["seconds"] == {"count": 2, "p50": 2.0, "p95": 40.0, "max": 40.0, "mean": 21.0}
    assert queues["learning_fast"]["seconds"]["max"] == 0.05
    assert queues["learning"]["never_started"] == 0


def test_a_task_that_never_started_is_counted_not_treated_as_no_delay():
    events = [
        _sent("a", "market_data", 100.0),
        _sent("b", "market_data", 130.0),
        _sent("c", "market_data", 140.0),
        {"kind": "revoked", "id": "c", "task": "load_scenario.probe", "expired": True},
    ]
    queue = _queue_delay(events, ended_epoch=190.0)["market_data"]
    assert queue["sent"] == 3 and queue["started"] == 0
    assert queue["never_started"] == 2
    assert queue["expired_before_start"] == 1
    assert queue["oldest_unstarted_wait_seconds"] == 90.0
    assert queue["seconds"]["count"] == 0 and queue["seconds"]["p95"] is None


def test_task_summary_separates_skips_failures_and_unfinished_work():
    name = "load_scenario.research"
    events = [
        _sent("a", "learning", 1.0, task=name), _started("a", "learning", 1.0, 0.1, task=name),
        _finished("a", 30.0, task=name),
        _sent("b", "learning", 1.0, task=name), _started("b", "learning", 1.0, 0.2, task=name),
        _finished("b", 0.01, task=name, status="skipped"),
        _sent("c", "learning", 1.0, task=name), _started("c", "learning", 1.0, 31.0, task=name),
        {"kind": "failed", "id": "d", "task": name, "error_type": "OperationalError", "sqlstate": "55P03"},
    ]
    summary = _tasks(events)[name]
    assert summary["sent"] == 3 and summary["started"] == 3
    assert summary["result_status"] == {"complete": 1, "skipped": 1}
    assert summary["failed"] == {"OperationalError": 1}
    assert summary["started_not_finished"] == 1
    assert summary["runtime_seconds"]["max"] == 30.0


def test_lock_waits_count_sampled_waits_and_both_sqlstates():
    samples = [
        {"lock_waits": [], "sessions": 6, "idle_in_transaction": 0},
        {"lock_waits": [{"pid": 1, "wait_event": "transactionid", "waited_seconds": 2.5},
                        {"pid": 2, "wait_event": "tuple", "waited_seconds": 0.4}],
         "sessions": 9, "idle_in_transaction": 2},
        {"database_error": "OperationalError"},
    ]
    events = [
        {"kind": "db_error", "sqlstate": "55P03", "task": "load_scenario.signal_pass"},
        {"kind": "db_error", "sqlstate": "55P03", "task": "load_scenario.signal_pass"},
        {"kind": "db_error", "sqlstate": "40P01", "task": None},
        {"kind": "db_error", "sqlstate": "23505", "task": "load_scenario.research"},
    ]
    result = _lock_waits(samples, events, {"postgres_deadlocks_before": 3, "postgres_deadlocks_after": 4})
    assert result["samples"] == 2 and result["samples_failed"] == 1
    assert result["samples_with_a_waiting_session"] == 1
    assert result["max_waiting_sessions"] == 2
    assert result["longest_wait_seconds"] == 2.5
    assert result["by_wait_event"] == {"transactionid": 1, "tuple": 1}
    assert result["max_idle_in_transaction"] == 2
    assert result["sqlstate_counts"]["55P03"] == 2
    assert result["sqlstate_counts"]["40P01"] == 1
    assert result["sqlstate_counts"]["other"] == {"23505": 1}
    assert result["sqlstate_counts"]["by_task"] == {
        "40P01:outside a task": 1, "55P03:load_scenario.signal_pass": 2}
    assert result["postgres_deadlocks"] == 1


def test_lock_sqlstates_are_reported_as_zero_not_omitted_when_none_occurred():
    result = _lock_waits([{"lock_waits": []}], [], {})
    assert result["sqlstate_counts"]["55P03"] == 0 and result["sqlstate_counts"]["40P01"] == 0
    assert result["postgres_deadlocks"] is None


def test_memory_is_reported_per_process_and_skips_a_process_that_was_down():
    samples = [
        {"memory_mb": {"paper-core": 300.0, "paper-fast": 200.0}},
        {"memory_mb": {"paper-core": 900.0, "paper-fast": 210.0}},
        {"memory_mb": {"paper-core": None, "paper-fast": 205.0}},
        {"memory_mb": {"paper-core": 310.0, "paper-fast": 205.0}},
    ]
    result = _memory(samples, {"memory_source": "proc"})
    assert result["source"] == "proc"
    assert result["processes"]["paper-core"] == {
        "samples": 3, "first_mb": 300.0, "peak_mb": 900.0, "last_mb": 310.0, "mean_mb": 503.3}
    assert result["processes"]["paper-fast"]["samples"] == 4
    assert result["peak_total_mb"] == 1110.0


def test_recovery_times_the_first_task_and_separates_lost_from_redelivered_work():
    drill = {"worker": "paper-core", "status": "recovered", "killed_pid": 10, "killed_at": 160.0,
             "heartbeat_restored_seconds": 11.5, "process_restarted_seconds": 2.1,
             "heartbeat_key": "celery:worker:heartbeat:paper-core@build-machine.local"}
    events = [
        _started("lost", "learning", 100.0, 1.0, pid=10, task="load_scenario.research"),
        _started("again", "intraday_market_data", 158.0, 0.1, pid=10, task="load_scenario.intraday_import"),
        _started("done", "learning", 100.0, 1.0, pid=10), _finished("done", 5.0),
        _started("again", "intraday_market_data", 158.0, 14.0, pid=11, task="load_scenario.intraday_import"),
        _started("next", "learning", 100.0, 73.5, pid=11, task="load_scenario.research"),
    ]
    result = _recovery(drill, events)
    assert result["tasks_in_flight_at_kill"] == ["load_scenario.intraday_import", "load_scenario.research"]
    assert result["tasks_lost_at_kill"] == ["load_scenario.research"]
    assert result["tasks_redelivered_after_kill"] == ["load_scenario.intraday_import"]
    assert result["first_task_started_seconds"] == 12.0
    assert result["heartbeat_key"] == "celery:worker:heartbeat:paper-core@<host>"
    assert result["heartbeat_restored_seconds"] == 11.5


def test_report_states_what_was_not_measured_instead_of_passing_it():
    report = build_report({"started_epoch": 0.0, "ended_epoch": 10.0}, [], [], {})
    assert report["coverage"]["complete"] is False
    assert report["coverage"]["missing"] == ["queue_delay", "lock_waits", "memory", "recovery"]
    assert report["recovery"]["status"] == "not_run"
    assert report["paper_only"] is True and report["live_trading"] is False


def test_report_is_complete_only_with_all_four_measurements():
    meta = {"started_epoch": 0.0, "ended_epoch": 200.0, "memory_source": "proc",
            "postgres_deadlocks_before": 0, "postgres_deadlocks_after": 0}
    events = [_sent("a", "learning", 100.0), _started("a", "learning", 100.0, 2.0), _finished("a", 1.0)]
    samples = [{"lock_waits": [], "memory_mb": {"paper-core": 300.0}}]
    drill = {"worker": "paper-core", "status": "recovered", "killed_pid": 10, "killed_at": 160.0,
             "heartbeat_restored_seconds": 11.5}
    report = build_report(meta, events, samples, drill)
    assert report["coverage"] == {
        "complete": True, "missing": [],
        "measured": {"queue_delay": True, "lock_waits": True, "memory": True, "recovery": True},
    }
    assert report["elapsed_seconds"] == 200.0
    not_recovered = build_report(meta, events, samples, {**drill, "heartbeat_restored_seconds": None})
    assert not_recovered["coverage"]["missing"] == ["recovery"]


def test_compare_lines_up_two_reports():
    meta = {"started_epoch": 0.0, "ended_epoch": 200.0}
    events = [_sent("a", "learning", 100.0), _started("a", "learning", 100.0, 2.0)]
    first = build_report(meta, events, [{"lock_waits": [], "memory_mb": {"paper-core": 300.0}}], {})
    second = build_report(meta, [_sent("a", "learning", 100.0), _started("a", "learning", 100.0, 3.5)],
                          [{"lock_waits": [], "memory_mb": {"paper-core": 320.0}}], {})
    lines = compare(first, second)
    assert lines[0] == "metric | first | second"
    assert "queue delay learning p95 (s) | 2.00 | 3.50" in lines
    assert "memory paper-core peak_mb | 300.00 | 320.00" in lines
    assert "sqlstate 55P03 | 0 | 0" in lines
    assert "recovery heartbeat_restored_seconds | - | -" in lines


def test_resident_mb_reads_another_process_or_reports_none():
    from app.core import memory

    own = memory.resident_mb(os.getpid())
    assert own is None or own > 0
    # Off Linux there is no /proc for either form; the driver has its own fallbacks.
    assert (own is None) == (memory.resident_mb() is None)
    assert memory.resident_mb(2**31 - 1) is None


def test_scenario_tasks_refuse_to_load_outside_the_scenario(monkeypatch):
    monkeypatch.delenv("LOAD_SCENARIO", raising=False)
    with pytest.raises(RuntimeError, match="run_load_scenario.sh"):
        importlib.import_module("scripts.load_scenario_tasks")


def _run(env: dict) -> subprocess.CompletedProcess:
    clean = {key: value for key, value in os.environ.items() if not key.startswith("LOAD_")}
    return subprocess.run(["bash", str(RUNNER)], capture_output=True, text=True, timeout=60, env={**clean, **env})


def test_runner_refuses_sqlite_and_a_schema_that_is_not_disposable():
    result = _run({"DATABASE_URL": "sqlite:///./trading_app.db"})
    assert result.returncode == 2 and "never runs against SQLite" in result.stderr
    result = _run({"DATABASE_URL": "postgresql://user@127.0.0.1:5432/app", "LOAD_SCHEMA": "public"})
    assert result.returncode == 2 and "load_scenario_" in result.stderr


def test_runner_refuses_a_database_that_is_not_on_this_machine():
    result = _run({"DATABASE_URL": "postgresql://user:secret@db.example.com:5432/app"})
    assert result.returncode != 0
    assert "only against a PostgreSQL server on this machine" in result.stderr
    assert result.stdout == ""


def test_scenario_workers_use_the_hosted_queue_layout():
    source = SERVER.read_text()
    workers = re.findall(r'^\s*"(paper-[a-z]+)\|([a-z_,]+)\|([a-z]+)\|(\d+)"$', RUNNER.read_text(), flags=re.MULTILINE)
    assert [label for label, _queues, _pool, _count in workers] == [
        "paper-core", "paper-research", "paper-fast", "paper-intraday", "paper-broker"]
    for label, queues, pool, count in workers:
        match = re.search(
            rf'--hostname={label}@%h",\s*"--queues=([^"]+)",.*?"--pool=([a-z]+)", "--concurrency=(\d+)", '
            rf'"--prefetch-multiplier=1"(, "--max-tasks-per-child=1")?',
            source, flags=re.DOTALL,
        )
        assert match, label
        assert (queues, pool, count) == match.groups()[:3], label
        # One task per process is what makes the prefork worker the research lane.
        assert bool(match.group(4)) == (pool == "prefork"), label
    assert dict((label, pool) for label, _queues, pool, _count in workers)["paper-research"] == "prefork"
    runner = RUNNER.read_text()
    assert "--prefetch-multiplier=1" in runner and "--max-tasks-per-child=1" in runner


def test_recovery_reports_a_task_process_kill_and_the_recorded_losses():
    # The research worker (pid 20) stays up; its task process (pid 21) was killed mid-job.
    events = [
        _sent("r1", "learning", 100.0, task="load_scenario.research"),
        _started("r1", "learning", 100.0, 1.0, pid=21, worker="paper-research@host", task="load_scenario.research"),
        _sent("r2", "learning", 100.0, task="load_scenario.research"),
        _started("r2", "learning", 100.0, 62.5, pid=22, worker="paper-research@host", task="load_scenario.research"),
    ]
    drill = {"worker": "paper-research", "status": "recovered", "killed": "task_process", "killed_pid": 21,
             "worker_pid": 20, "killed_at": 160.0, "process_restarted_seconds": None, "worker_restarted": False,
             "task_process_replaced_seconds": 0.4, "heartbeat_restored_seconds": 1.9,
             "heartbeat_key": "celery:worker:heartbeat:paper-research@build-machine.local"}
    lost = [{"job": "research", "cause": "Worker exited prematurely: signal 9 (SIGKILL)", "task_id": "r1",
             "message": "The process running research was stopped"}]
    result = _recovery(drill, events, lost)
    assert result["tasks_in_flight_at_kill"] == ["load_scenario.research"]
    assert result["tasks_lost_at_kill"] == ["load_scenario.research"]
    assert result["first_task_started_seconds"] == 2.5
    assert result["lost_jobs_recorded"] == lost
    assert _recovery({}, [])["lost_jobs_recorded"] == []
    # A task-process kill has no heartbeat to restore; the replacement is the evidence.
    replaced = {**drill, "heartbeat_restored_seconds": None}
    report = build_report({}, events, [{"lock_waits": [], "memory_mb": {"paper-research": 200.0}}], replaced)
    assert report["coverage"]["measured"]["recovery"] is True
    unreplaced = {**replaced, "task_process_replaced_seconds": None}
    report = build_report({}, events, [{"lock_waits": [], "memory_mb": {"paper-research": 200.0}}], unreplaced)
    assert report["coverage"]["missing"] == ["recovery"]

    # The whole worker killed instead: its task process (pid 21) dies with it.
    worker_kill = {**drill, "killed": "worker", "killed_pid": 20, "process_restarted_seconds": 2.1,
                   "worker_restarted": True, "task_process_replaced_seconds": None, "heartbeat_restored_seconds": 4.7}
    result = _recovery(worker_kill, events, lost)
    assert result["tasks_lost_at_kill"] == ["load_scenario.research"]
    assert result["first_task_started_seconds"] == 2.5

    meta = {"started_epoch": 100.0, "ended_epoch": 300.0, "lost_jobs_recorded": lost}
    report = build_report(meta, events, [{"lock_waits": [], "memory_mb": {"paper-research:task": 1410.0}}], drill)
    lines = compare(report, report)
    assert "recovery task_process_replaced_seconds | 0.40 | 0.40" in lines
    assert "recovery tasks_lost_at_kill | 1 | 1" in lines
    assert "recovery lost_jobs_recorded | 1 | 1" in lines
    assert "memory paper-research:task peak_mb | 1410.00 | 1410.00" in lines
