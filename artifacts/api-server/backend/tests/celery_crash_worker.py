"""Small real-worker harness used by the intraday lease crash test."""

from __future__ import annotations

import os
import time

import redis

from app.tasks import jobs
from app.tasks.celery_app import celery_app


def _test_ingest_intraday(db) -> dict:
    client = redis.Redis.from_url(os.environ["REDIS_URL"])
    phase_key = os.environ["INTRADAY_TEST_PHASE_KEY"]

    if os.environ.get("INTRADAY_TEST_BLOCK") == "1":
        client.set(phase_key, "started", ex=60)
        release_key = os.environ["INTRADAY_TEST_RELEASE_KEY"]
        while not client.exists(release_key):
            time.sleep(0.05)
    else:
        client.set(phase_key, "resumed", ex=60)

    return {
        "status": "complete",
        "repair": "bounded",
        "worker_phase": "interrupted" if os.environ.get("INTRADAY_TEST_BLOCK") == "1" else "replacement",
    }


def main() -> None:
    jobs.INTRADAY_JOB_LOCK_TTL_SECONDS = int(os.environ["INTRADAY_TEST_LOCK_TTL"])
    jobs.ingest_intraday = _test_ingest_intraday
    celery_app.worker_main(
        [
            "worker",
            "--pool=solo",
            "--concurrency=1",
            "--queues=intraday_market_data",
            "--loglevel=WARNING",
            "--hostname=intraday-crash-test@%h",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
        ]
    )


if __name__ == "__main__":
    main()