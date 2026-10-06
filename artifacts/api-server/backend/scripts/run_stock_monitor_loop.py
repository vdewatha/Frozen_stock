"""Run the stock monitor independently from Celery beat.

This process is supervised by the production gateway. A failed evaluation is
logged and leaves the existing watchdog state untouched, which preserves the
fail-closed recovery behavior.
"""
from __future__ import annotations

import logging
import os
import time

from app.tasks.jobs import stock_monitoring_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s stock-monitor-loop %(message)s")
logger = logging.getLogger("stock-monitor-loop")


def run_once() -> dict:
    # Reuse the normal job wrapper so Redis and PostgreSQL leases prevent
    # duplicate evaluations when autoscale starts more than one instance.
    return stock_monitoring_job()


def main() -> None:
    interval = max(30, int(os.environ.get("PAPER_MONITOR_INTERVAL_SECONDS", "60")))
    while True:
        try:
            result = run_once()
            logger.info("status=%s snapshot_id=%s", result.get("status"), result.get("snapshot_id"))
        except Exception as exc:  # pragma: no cover - exercised by process supervision
            logger.exception("monitor evaluation failed; recovery remains fail-closed: %s", type(exc).__name__)
        time.sleep(interval)


if __name__ == "__main__":
    main()
