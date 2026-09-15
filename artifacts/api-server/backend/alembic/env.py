from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, inspect, pool, text

from app.core.config import settings
from app.db.base import Base
from app.models import models  # noqa: F401

config = context.config
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _preserve_legacy_interim_head(connection) -> None:
    """Keep repaired stock-only namespaces at their documented compatibility head."""
    tables = set(inspect(connection).get_table_names())
    if "stock_dataset_snapshots" not in tables:
        return
    is_legacy = connection.execute(text(
        "SELECT 1 FROM stock_dataset_snapshots "
        "WHERE CAST(metadata_json AS TEXT) LIKE '%legacy_interim_schema%' LIMIT 1"
    )).scalar()
    if is_legacy:
        connection.execute(text(
            "UPDATE alembic_version "
            "SET version_num = '0030_stock_accounting_review'"
        ))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()
            _preserve_legacy_interim_head(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
