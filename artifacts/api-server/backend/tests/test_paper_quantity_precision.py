from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.schema import migration_config
from app.models.stock_paper import StockPaperAccount, StockPaperOrder
from app.services.stock_paper_ledger import _upsert_orders


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_quantity_upgrade_and_repeat_import(tmp_path, dialect):
    owner = None
    schema = None
    if dialect == "postgresql":
        url = make_url(settings.database_url)
        if url.get_backend_name() != "postgresql":
            pytest.skip("Requires isolated PostgreSQL validation stack")
        owner = create_engine(url)
        schema = "quantity_" + uuid4().hex
        with owner.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    else:
        url = make_url("sqlite:///" + str(tmp_path / "quantity.sqlite"))
    engine = create_engine(url)
    config = migration_config()
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    try:
        command.upgrade(config, "0049_alpaca_activity_ledger")
        metadata = MetaData()
        accounts = Table("stock_paper_accounts", metadata, autoload_with=engine)
        orders = Table("stock_paper_orders", metadata, autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(accounts.insert().values(id=1, broker="alpaca_paper", broker_account_id="fixture",
                currency="USD", cash=1000, buying_power=1000, equity=1000, status="halted", costs_known=False,
                reconciliation_required=True, raw_payload={"preserve": True}))
            connection.execute(orders.insert().values(id=-1, account_id=1, client_order_id="legacy",
                symbol="SPY", side="buy", quantity=Decimal("0.01306039"), order_type="market",
                time_in_force="day", reserved_cash=0, status="filled", source="broker_import", uncertain_submission=False,
                raw_payload={"qty": "0.013060394"}))
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        for table, names in {
            "stock_paper_positions": ["quantity"], "stock_paper_orders": ["quantity"],
            "stock_paper_fills": ["quantity"], "stock_paper_trial_lots": ["quantity", "exited_quantity"],
        }.items():
            columns = {c["name"]: c for c in inspect(engine).get_columns(table)}
            for name in names:
                assert columns[name]["type"].scale == 9
                assert columns[name]["type"].precision == 21
        with Session(engine) as db:
            legacy = db.get(StockPaperOrder, -1)
            assert legacy.quantity == Decimal("0.01306039")
            assert legacy.raw_payload == {"qty": "0.013060394"}
        raw = {"id": "new-order", "client_order_id": "new-client", "symbol": "SPY", "side": "sell",
               "qty": "0.013060394", "filled_qty": "0.013060394", "type": "market",
               "time_in_force": "day", "status": "filled"}
        for _ in range(2):
            with Session(engine) as db:
                _upsert_orders(db, db.get(StockPaperAccount, 1), [raw])
                db.commit()
            with Session(engine) as db:
                order = db.scalar(select(StockPaperOrder).where(StockPaperOrder.client_order_id == "new-client"))
                assert order.quantity == Decimal(raw["qty"])
        # Exercise the numeric type's smallest fraction and retained integer range.
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE quantity_probe (value NUMERIC(21,9))"))
            for value in ("0.000000001", "0.013060394", "999999999999.123456789"):
                if dialect == "sqlite" and value.startswith("999"):
                    continue  # SQLite has no native fixed-precision numeric storage.
                connection.execute(text("DELETE FROM quantity_probe"))
                connection.execute(text("INSERT INTO quantity_probe VALUES (:value)"), {"value": value})
                actual = connection.execute(text("SELECT value FROM quantity_probe")).scalar_one()
                assert Decimal(str(actual)) == Decimal(value)
    finally:
        engine.dispose()
        if schema:
            with owner.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            owner.dispose()
