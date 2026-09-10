"""Export verified completed Kraken candles for offline training, never orders."""
import argparse
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
import csv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL"):
        parser.error("Set DATABASE_URL explicitly")
    from app.db.session import engine, SessionLocal
    from app.db.schema import assert_schema_current
    from app.services.crypto_collection import load_closed_history
    assert_schema_current(engine)
    try:
        with SessionLocal() as db:
            rows = load_closed_history(db, as_of=datetime.now(timezone.utc), minimum=300)
            with args.output.open("x", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("date", "close", "volume"))
                writer.writerows((r.opened_at, str(r.close), str(r.volume)) for r in rows)
        print(f"Exported {len(rows)} verified completed hourly candles; research only.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
