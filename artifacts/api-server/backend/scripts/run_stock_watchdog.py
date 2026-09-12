"""Independent fail-closed watchdog for the stock paper recovery state."""
from __future__ import annotations

import logging
import time

from app.db.session import SessionLocal
from app.services.stock_recovery import run_stock_watchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s stock-watchdog %(message)s")


def main() -> None:
    while True:
        db = SessionLocal()
        try:
            result = run_stock_watchdog(db)
            logging.info("watchdog status=%s reasons=%s", result["status"], result["reasons"])
        except Exception:
            db.rollback()
            logging.exception("watchdog evaluation failed; leaving the paper path fail-closed")
        finally:
            db.close()
        time.sleep(30)


if __name__ == "__main__":
    main()