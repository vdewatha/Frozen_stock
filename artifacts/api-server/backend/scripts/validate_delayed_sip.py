"""Read actual delayed market data twice into disposable storage; no broker orders."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models
from app.db.base import Base
from app.models import IntradayBar
from app.services.delayed_sip_research import collect_delayed_sip, delayed_sip_status
from app.services.intraday_data import feed_status


def main():
    now = datetime.now(timezone.utc)
    reads = []
    with tempfile.TemporaryDirectory(prefix="sip-probe-") as directory:
        engine = create_engine("sqlite:///" + str(Path(directory) / "research.sqlite"))
        try:
            Base.metadata.create_all(engine)
            for _ in range(2):
                with Session(engine) as db:
                    collection = collect_delayed_sip(db, now=now)
                    db.commit()
                    reads.append({"collection": collection, "persisted_rows": db.query(IntradayBar).count()})
            with Session(engine) as db:
                status = delayed_sip_status(db, now=now)
                execution_blocked = all(feed_status(db, symbol, now=now)["status"] != "ready" for symbol in ("AAPL", "MSFT", "QQQ", "SPY"))
        finally:
            engine.dispose()
    passed = all(row["collection"]["status"] == "observed" for row in reads)
    passed = passed and reads[0]["persisted_rows"] == reads[1]["persisted_rows"] and execution_blocked
    print(json.dumps({"status": "passed" if passed else "blocked", "observed_at": now.isoformat(),
                      "reads": reads, "symbols": status["symbols"], "execution_feed_still_blocked": execution_blocked,
                      "broker_orders": 0, "application_database_modified": False,
                      "real_time_sip_entitlement_proven": False}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
