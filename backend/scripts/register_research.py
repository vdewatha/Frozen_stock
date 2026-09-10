"""Register an existing bundle. DATABASE_URL must explicitly select a migrated DB."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description="Register experimental research; never authorizes trading")
    parser.add_argument("--run", required=True, type=Path)
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL"):
        parser.error("Set DATABASE_URL explicitly; this command never migrates a database")
    from app.db.session import engine, SessionLocal
    from app.db.schema import assert_schema_current
    from app.services.research_registry import register_research_run
    assert_schema_current(engine)
    with SessionLocal.begin() as db:
        record = register_research_run(db, args.run)
        print(f"Registered experimental run {record.run_id}; eligible_for_trading=false")


if __name__ == "__main__":
    main()
