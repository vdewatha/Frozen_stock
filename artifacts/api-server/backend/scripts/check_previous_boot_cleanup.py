"""Check on a real PostgreSQL that a new deploy ends a previous deploy's idle transaction.

Opens one session tagged as an old boot, leaves it idle inside a transaction,
runs ``release_previous_boot_transactions`` as a new boot and reports whether
the old session was ended. It reads no table rows and writes nothing.

  PYTHONPATH=. python scripts/check_previous_boot_cleanup.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import release_previous_boot_transactions  # noqa: E402


def main() -> int:
    if not settings.database_url.startswith("postgresql"):
        print("SKIP: needs PostgreSQL")
        return 0
    old = create_engine(settings.database_url, connect_args={"application_name": "frozen-paper:check-old-boot"})
    new = create_engine(settings.database_url, connect_args={"application_name": "frozen-paper:check-new-boot"})
    stale = old.connect()
    stale.begin()
    stale.execute(text("SELECT 1"))
    time.sleep(3)
    released = release_previous_boot_transactions(new, {"PAPER_BOOT_ID": "check-new-boot"}, idle_seconds=1)
    print("released pids:", released)
    try:
        stale.execute(text("SELECT 1"))
    except Exception as exc:
        print("OK: the old boot's idle transaction was ended:", type(exc).__name__)
        return 0
    print("FAIL: the old boot's idle transaction is still open")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
