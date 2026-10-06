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
# Version the bootstrap markers so a deployment carrying a scheduler fix can
# perform one fresh research refresh even when an older deployment left its
# deduplication keys in Redis.
STARTUP_REFRESH_KEY = "trading:startup-refresh:market-data:v3"
STARTUP_REFRESH_TTL_SECONDS = 20 * 60 * 60
STARTUP_FEATURE_REFRESH_KEY = "trading:startup-refresh:features:v3"
STARTUP_FEATURE_REFRESH_TTL_SECONDS = 6 * 60 * 60
STARTUP_REALIZATION_KEY = "trading:startup-refresh:realization:v2"
STARTUP_REALIZATION_TTL_SECONDS = 6 * 60 * 60
STARTUP_LEARNING_RECOVERY_KEY = "trading:startup-recovery:strategy-learning"
STARTUP_LEARNING_RECOVERY_TTL_SECONDS = 15 * 60


def _startup_refresh_enabled() -> bool:
    """Keep deployment bootstrap responsive unless an operator opts in."""
    return os.environ.get("PAPER_STARTUP_REFRESH", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _queue_startup_refresh(client: redis.Redis) -> None:
    """Queue one immediate import and a dependent research refresh.

    Beat's 24-hour interval would otherwise wait until its next scheduled
    firing, leaving a newly promoted deployment with stale but otherwise valid
    historical data. The feature task is delayed until the import has had time
    to commit. Redis markers prevent multiple beat leaders from queueing
    duplicate work during a restart race.
    """
    market_refresh_queued = client.set(
        STARTUP_REFRESH_KEY, "queued", nx=True, ex=STARTUP_REFRESH_TTL_SECONDS
    )
    try:
        if market_refresh_queued:
            celery_app.send_task(
                "app.tasks.jobs.daily_market_data_import",
                queue="market_data",
                expires=15 * 60,
            )
        if client.set(
            STARTUP_FEATURE_REFRESH_KEY,
            "queued",
            nx=True,
            ex=STARTUP_FEATURE_REFRESH_TTL_SECONDS,
        ):
            celery_app.send_task(
                "app.tasks.jobs.daily_feature_generation",
                queue="market_data",
                countdown=5 * 60,
                expires=15 * 60,
            )
        if client.set(
            STARTUP_REALIZATION_KEY,
            "queued",
            nx=True,
            ex=STARTUP_REALIZATION_TTL_SECONDS,
        ):
            celery_app.send_task(
                "app.tasks.jobs.model_realization_scoring_job",
                queue="learning",
                countdown=7 * 60,
                expires=20 * 60,
            )
        print("Queued startup research refresh stages.", flush=True)
    except Exception:
        # A later scheduled run can recover from a transient broker/import
        # failure. Remove the marker so the next beat leader may retry.
        try:
            client.delete(STARTUP_REFRESH_KEY)
            client.delete(STARTUP_FEATURE_REFRESH_KEY)
            client.delete(STARTUP_REALIZATION_KEY)
        except redis.RedisError:
            pass
        raise


def _queue_startup_learning_recovery(client: redis.Redis) -> None:
    """Give aged paper-learning failures one bounded recovery attempt at boot.

    Beat's periodic entry remains the primary mechanism. This boot probe is a
    small, deduplicated fallback for deployments where a persisted beat
    schedule starts after the first minute boundary.
    """
    try:
        should_queue = client.set(
            STARTUP_LEARNING_RECOVERY_KEY,
            "queued",
            nx=True,
            ex=STARTUP_LEARNING_RECOVERY_TTL_SECONDS,
        )
    except redis.RedisError as exc:
        # The beat lease remains authoritative. A transient Redis restart must
        # not take down the scheduler merely because this optional probe could
        # not establish its deduplication marker.
        print(f"Skipped startup learning recovery: {exc.__class__.__name__}.", flush=True)
        return
    if not should_queue:
        return
    try:
        celery_app.send_task(
            "app.tasks.jobs.retry_failed_strategy_learning_scopes_job",
            queue="learning",
            expires=15 * 60,
        )
        print("Queued startup learning recovery check.", flush=True)
    except Exception:
        try:
            client.delete(STARTUP_LEARNING_RECOVERY_KEY)
        except redis.RedisError:
            pass
        print("Skipped startup learning recovery: queue unavailable.", flush=True)


def _acquire_lease(client: redis.Redis, owner: str) -> None:
    """Wait through deployment overlap instead of abandoning the scheduler."""
    while True:
        try:
            if client.set(LEASE_KEY, owner, nx=True, ex=LEASE_SECONDS):
                return
            print("Another Celery beat scheduler holds the lease; waiting.", flush=True)
        except redis.RedisError as exc:
            print(
                f"Celery beat lease coordination unavailable; retrying: {exc.__class__.__name__}.",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(2)


def _stop_child(child: subprocess.Popen) -> None:
    """Stop beat without allowing shutdown cleanup to kill the wrapper."""
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # The wrapper must remain alive so its next loop can reacquire the
            # lease after a transient broker or scheduler failure.
            pass


def main() -> int:
    client = redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=2, socket_timeout=2)
    owner = secrets.token_hex(16)
    _acquire_lease(client, owner)

    if _startup_refresh_enabled():
        _queue_startup_refresh(client)
    else:
        print("Skipped startup market-data refresh; scheduled jobs remain enabled.", flush=True)
    _queue_startup_learning_recovery(client)

    schedule_file = os.environ.get(
        "CELERY_BEAT_SCHEDULE_FILE",
        f"/tmp/frozen-stock-celerybeat-{owner}",
    )

    def spawn_beat() -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.tasks.celery_app:celery_app",
                "beat",
                "--loglevel=INFO",
                f"--schedule={schedule_file}",
            ]
        )

    child = spawn_beat()
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
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
        while not stopping:
            while child.poll() is None and not stopping:
                time.sleep(LEASE_SECONDS / 3)
                try:
                    refreshed = refresh(keys=[LEASE_KEY], args=[owner, LEASE_SECONDS])
                except redis.RedisError as exc:
                    _stop_child(child)
                    print(
                        f"Celery beat lease coordination is unavailable; retrying: {exc.__class__.__name__}.",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(2)
                    _acquire_lease(client, owner)
                    child = spawn_beat()
                    continue
                if not refreshed:
                    child.terminate()
                    print("Celery beat lease was lost.", file=sys.stderr, flush=True)
                    return 1
            if stopping:
                break
            print(
                f"Celery beat exited with code {child.returncode}; restarting.",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(2)
            child = spawn_beat()
        return 0
    finally:
        # Do not release leadership until the old scheduler has stopped.
        _stop_child(child)
        try:
            release(keys=[LEASE_KEY], args=[owner])
        except redis.RedisError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
