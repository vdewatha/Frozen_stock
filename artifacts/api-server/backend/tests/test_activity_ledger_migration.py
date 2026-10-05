from datetime import datetime, timezone
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db.schema import migration_config


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_upgrade_preserves_legacy_rows_and_allows_date_only_v2(tmp_path, dialect):
    schema = None
    owner = None
    if dialect == "postgresql":
        url = make_url(settings.database_url)
        if url.get_backend_name() != "postgresql":
            pytest.skip("Requires isolated PostgreSQL validation stack")
        owner = create_engine(url)
        schema = "activity_migration_" + uuid4().hex
        with owner.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    else:
        url = make_url("sqlite:///" + str(tmp_path / "upgrade.sqlite"))
    engine = create_engine(url)
    config = migration_config()
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    try:
        command.upgrade(config, "0048_online_research")
        metadata = MetaData()
        accounts = Table("stock_paper_accounts", metadata, autoload_with=engine)
        activities = Table("stock_paper_broker_activities", metadata, autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(accounts.insert().values(id=1, broker="alpaca_paper", broker_account_id="fixture",
                currency="USD", cash=1000, buying_power=1000, equity=1000, status="halted", costs_known=False,
                reconciliation_required=True, raw_payload={"preserve": "account"}))
            connection.execute(activities.insert().values(id=1, account_id=1, broker_activity_id="legacy", activity_type="CSD",
                occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc), raw_payload={"preserve": "activity"}))
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        metadata = MetaData()
        accounts = Table("stock_paper_accounts", metadata, autoload_with=engine)
        activities = Table("stock_paper_broker_activities", metadata, autoload_with=engine)
        with engine.begin() as connection:
            account = connection.execute(select(accounts)).mappings().one()
            for name in ("stock_paper_research_qualifications", "stock_paper_research_authorizations"):
                evidence = Table(name, metadata, autoload_with=engine)
                assert connection.execute(select(evidence)).first() is None
            activity = connection.execute(select(activities)).mappings().one()
            assert account["activity_contract"] == "legacy-v1"
            assert account["cash_policy"] == "exact-v1"
            assert account["activity_baseline"] is None and not account["accounting_verified"]
            assert account["raw_payload"] == {"preserve": "account"}
            assert activity["raw_payload"] == {"preserve": "activity"}
            assert activity["occurred_at"] is not None and activity["normalized_payload"] is None
            connection.execute(activities.insert().values(id=2, account_id=1, broker_activity_id="dated", activity_type="CSD",
                occurred_at=None, raw_payload={"date": "2026-01-01"}, normalized_payload={"effective_date": "2026-01-01"}))
        columns = {row["name"]: row for row in inspect(engine).get_columns("stock_paper_broker_activities")}
        assert columns["occurred_at"]["nullable"]
    finally:
        engine.dispose()
        if schema:
            with owner.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            owner.dispose()
