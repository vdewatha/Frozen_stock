from datetime import datetime, timezone
from uuid import uuid4

import pytest
from alembic import command
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
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


def test_upgrade_accepts_publish_preapplied_activity_columns(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "preapplied-activity.sqlite"))
    config = migration_config()
    config.set_main_option("sqlalchemy.url", str(engine.url))
    try:
        command.upgrade(config, "0048_online_research")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE stock_paper_accounts "
                "ADD COLUMN activity_contract VARCHAR(32) NOT NULL DEFAULT 'legacy-v1'"
            )
            connection.exec_driver_sql(
                "ALTER TABLE stock_paper_accounts ADD COLUMN activity_baseline JSON"
            )
            connection.exec_driver_sql(
                "ALTER TABLE stock_paper_accounts "
                "ADD COLUMN cash_policy VARCHAR(48) NOT NULL DEFAULT 'exact-v1'"
            )
            connection.exec_driver_sql(
                "ALTER TABLE stock_paper_broker_activities "
                "ADD COLUMN normalized_payload JSON"
            )

        command.upgrade(config, "head")
        command.upgrade(config, "head")
        account_columns = {column["name"] for column in inspect(engine).get_columns("stock_paper_accounts")}
        activity_columns = {
            column["name"]
            for column in inspect(engine).get_columns("stock_paper_broker_activities")
        }
        assert {"activity_contract", "activity_baseline", "cash_policy"}.issubset(account_columns)
        assert "normalized_payload" in activity_columns
    finally:
        engine.dispose()


def test_upgrade_accepts_publish_preapplied_model_prediction_identity(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "preapplied-prediction-identity.sqlite"))
    config = migration_config()
    config.set_main_option("sqlalchemy.url", str(engine.url))
    try:
        command.upgrade(config, "0052_paper_research_qual")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_model_prediction_identity "
                "ON model_predictions(symbol, prediction_date, horizon_days, source)"
            )

        command.upgrade(config, "head")
        command.upgrade(config, "head")
        indexes = {item["name"] for item in inspect(engine).get_indexes("model_predictions")}
        constraints = {
            item.get("name")
            for item in inspect(engine).get_unique_constraints("model_predictions")
        }
        assert "uq_model_prediction_identity" in indexes | constraints
    finally:
        engine.dispose()


def test_account_transition_accepts_publish_missing_legacy_constraint(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "preapplied-account-transition.sqlite"))
    config = migration_config()
    config.set_main_option("sqlalchemy.url", str(engine.url))
    try:
        command.upgrade(config, "0053_model_prediction_identity")
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            with operations.batch_alter_table("stock_paper_accounts", recreate="auto") as batch:
                batch.drop_constraint("uq_stock_paper_account_broker", type_="unique")

        command.upgrade(config, "0054_paper_account_transition")
        command.upgrade(config, "0054_paper_account_transition")
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("stock_paper_accounts")}
        indexes = {item["name"] for item in inspector.get_indexes("stock_paper_accounts")}
        constraints = {
            item.get("name")
            for item in inspector.get_unique_constraints("stock_paper_accounts")
        }
        assert {"archived_at", "archive_reason"}.issubset(columns)
        assert "uq_stock_paper_account_broker" not in indexes | constraints
        assert "ix_stock_paper_accounts_archived_at" in indexes
    finally:
        engine.dispose()
