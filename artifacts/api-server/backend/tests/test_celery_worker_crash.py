from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import redis
from celery import Celery

from app.tasks import jobs


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for(predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {description}")


def _require_redis_server() -> str:
    redis_server = shutil.which("redis-server")
    if redis_server is None:
        raise RuntimeError(
            "Redis-backed crash-recovery validation requires redis-server; "
            "the integration test cannot be skipped."
        )
    return redis_server


def _wait_for_redis(client: redis.Redis, redis_process: subprocess.Popen):
    def redis_ready():
        if _ping_redis(client):
            return True
        if redis_process.poll() is not None:
            details = ""
            if redis_process.stderr is not None:
                details = redis_process.stderr.read().strip()
            raise RuntimeError(
                "Ephemeral Redis exited before becoming ready "
                f"(exit code {redis_process.returncode})."
                + (f" stderr: {details}" if details else "")
            )
        return False

    _wait_for(redis_ready, timeout=5, description="ephemeral Redis")


class CeleryWorkerCrashIntegrationTests(unittest.TestCase):
    def test_intraday_lease_expires_after_worker_termination(self):
        redis_server = _require_redis_server()

        port = _free_tcp_port()
        redis_url = f"redis://127.0.0.1:{port}/0"
        client = redis.Redis.from_url(redis_url)
        redis_process = subprocess.Popen(
            [
                redis_server,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--save",
                "",
                "--appendonly",
                "no",
                "--daemonize",
                "no",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        worker_processes: list[subprocess.Popen] = []
        with tempfile.TemporaryDirectory(prefix="intraday-crash-test-") as temp_dir:
            phase_key = f"intraday-crash-test:{os.getpid()}:phase"
            release_key = f"intraday-crash-test:{os.getpid()}:release"
            database_url = f"sqlite:///{Path(temp_dir) / 'worker.sqlite'}"
            lock_key = "trading:scheduled-job:intraday_market_data_import"
            lock_ttl = 2
            worker_environment = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).parents[1]),
                "REDIS_URL": redis_url,
                "DATABASE_URL": database_url,
                "INTRADAY_TEST_PHASE_KEY": phase_key,
                "INTRADAY_TEST_RELEASE_KEY": release_key,
                "INTRADAY_TEST_LOCK_TTL": str(lock_ttl),
                "INTRADAY_TEST_BLOCK": "1",
            }

            try:
                _wait_for_redis(client, redis_process)
                client.delete(phase_key, release_key, lock_key)

                test_celery_app = Celery(
                    "intraday-crash-test",
                    broker=redis_url,
                    backend=redis_url,
                )

                first_worker = _start_worker(worker_environment)
                worker_processes.append(first_worker)
                first_result = test_celery_app.send_task(
                    "app.tasks.jobs.intraday_market_data_import",
                    queue="intraday_market_data"
                )
                first_task_id = first_result.id

                _wait_for(
                    lambda: client.get(phase_key) == b"started",
                    timeout=10,
                    description="intraday import to start",
                )
                lease_ttl = client.ttl(lock_key)
                self.assertGreater(lease_ttl, 0)
                self.assertLessEqual(lease_ttl, lock_ttl)
                started_payload = client.get(f"celery-task-meta-{first_task_id}")
                self.assertIsNotNone(started_payload)
                self.assertEqual(json.loads(started_payload)["status"], "STARTED")

                first_worker.kill()
                first_worker.wait(timeout=5)
                self.assertIsNotNone(first_worker.returncode)
                first_result = None

                _wait_for(
                    lambda: not client.exists(lock_key),
                    timeout=lock_ttl + 3,
                    description="the crashed worker lease to expire",
                )

                replacement_worker = _start_worker(
                    {
                        **worker_environment,
                        "INTRADAY_TEST_BLOCK": "0",
                    }
                )
                worker_processes.append(replacement_worker)
                replacement_result = test_celery_app.send_task(
                    "app.tasks.jobs.intraday_market_data_import",
                    queue="intraday_market_data"
                )
                replacement_task_id = replacement_result.id
                replacement_result = None

                self.assertEqual(
                    _wait_for(
                        lambda: _completed_task_result(client, replacement_task_id),
                        timeout=10,
                        description="replacement intraday import to complete",
                    ),
                    {
                        "job": "intraday_market_data_import",
                        "status": "complete",
                        "repair": "bounded",
                        "worker_phase": "replacement",
                    },
                )
                self.assertEqual(client.get(phase_key), b"resumed")
                self.assertFalse(client.exists(lock_key))
                self.assertTrue(
                    first_worker.returncode is not None,
                    "the replacement poll must run after the interrupted worker is gone",
                )
            finally:
                client.set(release_key, "cleanup", ex=10)
                first_result = None
                replacement_result = None
                for worker_process in worker_processes:
                    if worker_process.poll() is None:
                        worker_process.send_signal(signal.SIGTERM)
                        try:
                            worker_process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            worker_process.kill()
                            worker_process.wait(timeout=5)
                redis_process.terminate()
                try:
                    redis_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    redis_process.kill()
                    redis_process.wait(timeout=5)

    def test_scheduled_intraday_poll_resumes_after_worker_termination(self):
        redis_server = _require_redis_server()

        port = _free_tcp_port()
        redis_url = f"redis://127.0.0.1:{port}/0"
        client = redis.Redis.from_url(redis_url)
        redis_process = subprocess.Popen(
            [
                redis_server,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--save",
                "",
                "--appendonly",
                "no",
                "--daemonize",
                "no",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        processes: list[subprocess.Popen] = []
        with tempfile.TemporaryDirectory(prefix="intraday-beat-crash-test-") as temp_dir:
            phase_key = f"intraday-beat-crash-test:{os.getpid()}:phase"
            release_key = f"intraday-beat-crash-test:{os.getpid()}:release"
            database_url = f"sqlite:///{Path(temp_dir) / 'worker.sqlite'}"
            schedule_path = str(Path(temp_dir) / "celerybeat-schedule")
            lock_key = "trading:scheduled-job:intraday_market_data_import"
            lock_ttl = 2
            worker_environment = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).parents[1]),
                "REDIS_URL": redis_url,
                "DATABASE_URL": database_url,
                "INTRADAY_TEST_PHASE_KEY": phase_key,
                "INTRADAY_TEST_RELEASE_KEY": release_key,
                "INTRADAY_TEST_LOCK_TTL": str(lock_ttl),
                "INTRADAY_TEST_BLOCK": "1",
            }

            try:
                _wait_for_redis(client, redis_process)
                client.delete(phase_key, release_key, lock_key)

                first_worker = _start_worker(worker_environment)
                processes.append(first_worker)
                beat_process = _start_beat(worker_environment, schedule_path)
                processes.append(beat_process)

                _wait_for(
                    lambda: client.get(phase_key) == b"started",
                    timeout=75,
                    description="scheduled intraday import to start",
                )
                lease_ttl = client.ttl(lock_key)
                self.assertGreater(lease_ttl, 0)
                self.assertLessEqual(lease_ttl, lock_ttl)

                first_worker.kill()
                first_worker.wait(timeout=5)
                self.assertIsNotNone(first_worker.returncode)

                _wait_for(
                    lambda: not client.exists(lock_key),
                    timeout=lock_ttl + 3,
                    description="the crashed worker lease to expire",
                )

                replacement_worker = _start_worker(
                    {
                        **worker_environment,
                        "INTRADAY_TEST_BLOCK": "0",
                    }
                )
                processes.append(replacement_worker)

                scheduled_result = _wait_for(
                    lambda: _completed_result_matching(
                        client,
                        lambda result: result.get("worker_phase") == "replacement",
                    ),
                    timeout=75,
                    description="the next scheduled intraday import to complete",
                )
                self.assertEqual(
                    scheduled_result,
                    {
                        "job": "intraday_market_data_import",
                        "status": "complete",
                        "repair": "bounded",
                        "worker_phase": "replacement",
                    },
                )
                self.assertEqual(client.get(phase_key), b"resumed")
                self.assertFalse(client.exists(lock_key))
                self.assertIsNone(
                    beat_process.poll(),
                    "Celery beat must remain alive while the replacement poll runs",
                )
                self.assertTrue(
                    first_worker.returncode is not None,
                    "the scheduled replacement poll must run after the interrupted worker is gone",
                )
            finally:
                client.set(release_key, "cleanup", ex=10)
                for process in processes:
                    if process.poll() is None:
                        process.send_signal(signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                redis_process.terminate()
                try:
                    redis_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    redis_process.kill()
                    redis_process.wait(timeout=5)


def _start_worker(environment: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "tests/celery_crash_worker.py"],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _start_beat(environment: dict[str, str], schedule_path: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.tasks.celery_app:celery_app",
            "beat",
            "--loglevel=WARNING",
            f"--schedule={schedule_path}",
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ping_redis(client: redis.Redis) -> bool:
    try:
        return bool(client.ping())
    except redis.RedisError:
        return False


def _completed_task_result(client: redis.Redis, task_id: str):
    payload = client.get(f"celery-task-meta-{task_id}")
    if payload is None:
        return None
    result = json.loads(payload)
    if result.get("status") != "SUCCESS":
        return None
    return result.get("result")


def _completed_result_matching(client: redis.Redis, predicate):
    for key in client.scan_iter(match="celery-task-meta-*"):
        payload = client.get(key)
        if payload is None:
            continue
        result = json.loads(payload)
        if result.get("status") == "SUCCESS" and predicate(result.get("result") or {}):
            return result.get("result")
    return None
