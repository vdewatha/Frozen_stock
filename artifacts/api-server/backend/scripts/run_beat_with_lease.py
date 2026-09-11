"""Run exactly one Celery beat process while advertising a short Redis lease."""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time

import redis


LEASE_KEY = "celery:beat:lease:primary"
LEASE_SECONDS = 15


def main() -> int:
    client = redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=2, socket_timeout=2)
    owner = secrets.token_hex(16)
    if not client.set(LEASE_KEY, owner, nx=True, ex=LEASE_SECONDS):
        print("Another Celery beat scheduler already holds the lease.", file=sys.stderr)
        return 1

    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.tasks.celery_app:celery_app",
            "beat",
            "--loglevel=INFO",
            "--schedule=/tmp/frozen-stock-celerybeat-schedule",
        ]
    )

    def stop(_signum: int, _frame: object) -> None:
        if child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    refresh = client.register_script(
        """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('expire', KEYS[1], ARGV[2])
        end
        return 0
        """
    )
    release = client.register_script(
        """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('del', KEYS[1])
        end
        return 0
        """
    )

    try:
        while child.poll() is None:
            time.sleep(LEASE_SECONDS / 3)
            if not refresh(keys=[LEASE_KEY], args=[owner, LEASE_SECONDS]):
                child.terminate()
                print("Celery beat lease was lost.", file=sys.stderr)
                return 1
        return child.returncode or 0
    finally:
        try:
            release(keys=[LEASE_KEY], args=[owner])
        except redis.RedisError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())