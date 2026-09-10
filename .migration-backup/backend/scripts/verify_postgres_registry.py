"""Isolated integration test against a disposable localhost PostgreSQL server.

Creates and drops only its uniquely named test database, never an existing one.
Requires a passwordless disposable server; never point this at a production port.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import command
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.db.schema import assert_schema_current, migration_config
from app.models import ResearchModelRun
from app.services.research_registry import register_research_run
from app.services.research_training import train_research_run
from app.models.execution import PaperExecutionAccount
from app.services.paper_execution import create_account, execution_transaction, set_kill_switch, PaperExecutionError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Invalid localhost port")
    name = "research_test_" + uuid4().hex
    admin = create_engine(f"postgresql+psycopg://postgres@127.0.0.1:{args.port}/postgres", isolation_level="AUTOCOMMIT")
    engine = None
    created = False
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
            created = True
        engine = create_engine(f"postgresql+psycopg://postgres@127.0.0.1:{args.port}/{name}")
        config = migration_config()
        config.set_main_option("sqlalchemy.url", str(engine.url))
        command.upgrade(config, "0003_journal_scanner")
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert_schema_current(engine)
        def initialize(_):
            try:
                with Session(engine, autoflush=False) as db, execution_transaction(db):
                    create_account(db, "10000.12345678")
                return True
            except PaperExecutionError:
                return False
        with ThreadPoolExecutor(max_workers=2) as workers:
            assert sorted(workers.map(initialize, range(2))) == [False, True]
        with Session(engine, autoflush=False) as db, execution_transaction(db):
            set_kill_switch(db, False)
            set_kill_switch(db, True)
        with Session(engine) as db:
            account = db.get(PaperExecutionAccount, 1)
            assert str(account.cash) == "10000.12345678"
            assert account.kill_switch is True
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            x = np.arange(400)
            prices = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=400), "close": 100 + np.sin(x) * 4, "volume": x + 1000})
            manifest = train_research_run(prices, root, symbol="TEST", source="integration-fixture")
            path = root / manifest["run_id"]
            with Session(engine) as db:
                register_research_run(db, path)
                db.rollback()
            with Session(engine) as db:
                assert db.scalar(select(func.count()).select_from(ResearchModelRun)) == 0
            def register(_):
                with Session(engine) as db, db.begin():
                    return register_research_run(db, path).id
            with ThreadPoolExecutor(max_workers=2) as workers:
                ids = list(workers.map(register, range(2)))
            assert ids[0] == ids[1]
            with Session(engine) as db:
                assert db.scalar(select(func.count()).select_from(ResearchModelRun)) == 1
        print("PASS: PostgreSQL migrations/schema parity, registry rollback/concurrency, paper account concurrent creation, exact amounts and kill-switch persistence")
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


if __name__ == "__main__":
    main()
