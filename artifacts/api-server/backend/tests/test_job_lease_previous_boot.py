"""Scheduled-job leases left by a previous boot are released at once on disposable Redis."""
import shutil
import socket
import subprocess
import time
from unittest.mock import patch

import pytest
import redis

from app.tasks import jobs

JOB = "stock_forward_trial_reconcile_job"
KEY = f"trading:scheduled-job:{JOB}"


@pytest.fixture
def redis_url(tmp_path):
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("Requires disposable redis-server binary")
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    server = subprocess.Popen([executable, "--bind", "127.0.0.1", "--port", str(port),
        "--save", "", "--appendonly", "no", "--dir", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"redis://127.0.0.1:{port}/0"
    client = redis.Redis.from_url(url, socket_timeout=2)
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except redis.RedisError:
                time.sleep(.05)
        else:
            pytest.fail("Disposable Redis failed to start")
        with patch.object(jobs.settings, "redis_url", url):
            yield client
    finally:
        client.close()
        server.kill()
        server.wait(timeout=10)


def test_a_lease_from_the_previous_boot_is_taken_over(redis_url, monkeypatch):
    redis_url.set(KEY, "old-boot:abc", px=900_000)
    monkeypatch.setenv("PAPER_BOOT_ID", "new-boot")
    lock = jobs._acquire_job_lock(JOB)
    assert lock is not None
    assert redis_url.get(KEY).decode().startswith("new-boot:")
    assert redis_url.pttl(KEY) > 800_000  # a fresh full-length lease
    lock.release()
    assert not redis_url.exists(KEY)


def test_a_lease_from_before_boot_tokens_existed_is_taken_over(redis_url, monkeypatch):
    redis_url.set(KEY, "9f0c2d7e5b1a4c3d8e6f", px=900_000)  # pre-fix uuid token
    monkeypatch.setenv("PAPER_BOOT_ID", "new-boot")
    lock = jobs._acquire_job_lock(JOB)
    assert lock is not None
    lock.release()


def test_a_lease_held_in_this_boot_still_blocks_duplicates(redis_url, monkeypatch):
    monkeypatch.setenv("PAPER_BOOT_ID", "same-boot")
    first = jobs._acquire_job_lock(JOB)
    assert first is not None
    assert jobs._acquire_job_lock(JOB) is None
    first.release()
    assert jobs._acquire_job_lock(JOB) is not None


def test_without_a_boot_id_a_foreign_lease_waits_for_its_expiry(redis_url, monkeypatch):
    monkeypatch.delenv("PAPER_BOOT_ID", raising=False)
    redis_url.set(KEY, "old-boot:abc", px=900_000)
    assert jobs._acquire_job_lock(JOB) is None
    assert redis_url.get(KEY) == b"old-boot:abc"
