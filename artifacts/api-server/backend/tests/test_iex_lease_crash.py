"""Real process death and full production-duration expiry on disposable Redis."""
import os
import shutil
import socket
import subprocess
import sys
import time
from unittest.mock import patch

import pytest
import redis

from app.tasks import jobs


def test_crashed_iex_owner_blocks_duplicates_until_real_lease_expiry(tmp_path):
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("Requires disposable redis-server binary")
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    url = f"redis://127.0.0.1:{port}/0"
    server = subprocess.Popen([executable, "--bind", "127.0.0.1", "--port", str(port),
        "--save", "", "--appendonly", "no", "--dir", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    owner = None
    client = redis.Redis.from_url(url, socket_timeout=2)
    key = "trading:scheduled-job:iex_research_collection_job"
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except redis.RedisError:
                time.sleep(.05)
        else:
            pytest.fail("Disposable Redis failed to start")
        owner = subprocess.Popen([sys.executable, "-c",
            "import time; from app.tasks import jobs; "
            "lock=jobs._acquire_job_lock('iex_research_collection_job'); "
            "assert lock is not None; time.sleep(300)"],
            env={**os.environ, "REDIS_URL": url},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(200):
            if client.exists(key):
                break
            assert owner.poll() is None, "Lease owner exited before acquiring"
            time.sleep(.05)
        else:
            pytest.fail("Lease owner did not acquire")
        assert 175_000 < client.pttl(key) <= jobs.IEX_JOB_LOCK_TTL_SECONDS * 1000
        with patch.object(jobs.settings, "redis_url", url):
            assert jobs._acquire_job_lock("iex_research_collection_job") is None
            owner.kill()
            owner.wait(timeout=10)
            assert jobs._acquire_job_lock("iex_research_collection_job") is None
            # Do not delete or shorten the lease: observe its configured expiry.
            deadline = time.monotonic() + jobs.IEX_JOB_LOCK_TTL_SECONDS + 10
            while client.exists(key) and time.monotonic() < deadline:
                time.sleep(1)
            assert not client.exists(key), "Crashed-owner lease exceeded its expiry budget"
            replacement = jobs._acquire_job_lock("iex_research_collection_job")
            assert replacement is not None
            replacement.release()
            assert not client.exists(key)
    finally:
        if owner is not None and owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
        client.close()
        server.terminate()
        server.wait(timeout=10)
