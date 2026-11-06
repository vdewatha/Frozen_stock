"""Run the stock monitor independently from Celery beat.

This process is supervised by the production gateway. A failed evaluation is
logged and leaves the existing watchdog state untouched, which preserves the
fail-closed recovery behavior.
"""
from __future__ import annotations

import logging
import os
import time

from app.db.session import SessionLocal, limit_lock_waits, release_previous_boot_transactions
from app.services.stock_monitoring import run_stock_monitoring
from app.services.stock_learning_cycle import sync_cycle_observability

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s stock-monitor-loop %(message)s")
logger = logging.getLogger("stock-monitor-loop")


def run_once() -> dict:
    # This process is already supervised as the singleton monitor in the
    # direct-loop deployment mode. Calling the Celery task wrapper here would
    # add a Redis lease dependency and can suppress the heartbeat when the
    # queue worker still holds the same lease.
    db = SessionLocal()
    try:
        result = run_stock_monitoring(db, source="direct_monitor_loop")
        sync_cycle_observability(
            db,
            monitor_snapshot_id=result.get("snapshot_id"),
            recovery_event_id=result.get("recovery_event_id"),
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


LOCK_WAIT_SECONDS = 20


def release_stale_sessions() -> None:
    """End a replaced deploy's idle transactions before evaluating."""
    try:
        release_previous_boot_transactions()
    except Exception:
        logger.exception("previous-deploy session cleanup failed; continuing")


def main() -> None:
    interval = max(30, int(os.environ.get("PAPER_MONITOR_INTERVAL_SECONDS", "60")))
    # A blocked lock wait retries next cycle instead of stopping the heartbeat.
    limit_lock_waits(LOCK_WAIT_SECONDS)
    while True:
        release_stale_sessions()
        try:
            result = run_once()
            logger.info("status=%s snapshot_id=%s", result.get("status"), result.get("snapshot_id"))
        except Exception as exc:  # pragma: no cover - exercised by process supervision
            logger.exception("monitor evaluation failed; recovery remains fail-closed: %s", type(exc).__name__)
        time.sleep(interval)


if __name__ == "__main__":
    main()
