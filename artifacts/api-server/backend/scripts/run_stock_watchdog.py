"""Independent fail-closed watchdog for the stock paper recovery state."""
from __future__ import annotations

import logging
import time

from app.db.session import SessionLocal
from app.services.stock_recovery import run_stock_watchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s stock-watchdog %(message)s")


def run_once() -> dict:
    """Evaluate one watchdog cycle and keep exceptions fail-closed."""
    db = SessionLocal()
    try:
        result = run_stock_watchdog(db)
        logging.info("watchdog status=%s reasons=%s", result["status"], result["reasons"])
        return result
    except Exception:
        db.rollback()
        logging.exception("watchdog evaluation failed; leaving the paper path fail-closed")
        return {
            "status": "unknown",
            "reasons": ["watchdog_evaluation_failed"],
            "fail_closed": True,
        }
    finally:
        db.close()


def main() -> None:
    while True:
        run_once()
        time.sleep(30)


if __name__ == "__main__":
    main()