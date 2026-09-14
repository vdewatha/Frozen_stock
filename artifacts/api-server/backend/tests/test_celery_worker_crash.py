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


class CeleryWorkerCrashIntegrationTests(unittest.TestCase):
    def test_intraday_lease_expires_after_worker_termination(self):
        redis_server = shutil.which("redis-server")
        if redis_server is None:
            self.skipTest("redis-server is required for this integration scenario")

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
            stderr=subprocess.DEVNULL,
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
                _wait_for(
                    lambda: _ping_redis(client),
                    timeout=5,
                    description="ephemeral Redis",
                )
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
                self.assertIsNone(client.get(f"celery-task-meta-{first_task_id}"))

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


def _start_worker(environment: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "tests/celery_crash_worker.py"],
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