"""Run exactly one Celery beat process while advertising a short Redis lease."""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time

import redis

from app.tasks.celery_app import celery_app


LEASE_KEY = "celery:beat:lease:primary"
LEASE_SECONDS = 15
STARTUP_REFRESH_KEY = "trading:startup-refresh:market-data"
STARTUP_REFRESH_TTL_SECONDS = 20 * 60 * 60


def _queue_startup_refresh(client: redis.Redis) -> None:
    """Queue one immediate daily import after a deployment.

    Beat's 24-hour interval would otherwise wait until its next scheduled
    firing, leaving a newly promoted deployment with stale but otherwise valid
    historical data. The Redis marker prevents multiple beat leaders from
    queueing duplicate refreshes during a restart race.
    """
    if not client.set(STARTUP_REFRESH_KEY, "queued", nx=True, ex=STARTUP_REFRESH_TTL_SECONDS):
        return
    try:
        celery_app.send_task(
            "app.tasks.jobs.daily_market_data_import",
            queue="market_data",
            expires=15 * 60,
        )
        print("Queued startup market-data refresh.", flush=True)
    except Exception:
        # A later scheduled run can recover from a transient broker/import
        # failure. Remove the marker so the next beat leader may retry.
        try:
            client.delete(STARTUP_REFRESH_KEY)
        except redis.RedisError:
            pass
        raise


def main() -> int:
    client = redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=2, socket_timeout=2)
    owner = secrets.token_hex(16)
    if not client.set(LEASE_KEY, owner, nx=True, ex=LEASE_SECONDS):
        print("Another Celery beat scheduler already holds the lease.", file=sys.stderr)
        return 1

    _queue_startup_refresh(client)

    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.tasks.celery_app:celery_app",
            "beat",
            "--loglevel=INFO",
            f"--schedule={os.environ.get('CELERY_BEAT_SCHEDULE_FILE', '/tmp/frozen-stock-celerybeat-schedule')}",
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
            try:
                refreshed = refresh(keys=[LEASE_KEY], args=[owner, LEASE_SECONDS])
            except redis.RedisError:
                child.terminate()
                print("Celery beat lease coordination is unavailable.", file=sys.stderr)
                return 1
            if not refreshed:
                child.terminate()
                print("Celery beat lease was lost.", file=sys.stderr)
                return 1
        return child.returncode or 0
    finally:
        # Do not release leadership until the old scheduler has stopped.
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        try:
            release(keys=[LEASE_KEY], args=[owner])
        except redis.RedisError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
