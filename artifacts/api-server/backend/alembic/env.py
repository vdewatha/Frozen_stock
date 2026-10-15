from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from app.core.config import settings
from app.db.base import Base
from app.models import models  # noqa: F401

config = context.config
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Replit can briefly run more than one gateway replica during a publish or
# restart.  Serialize PostgreSQL migrations so schema-existence checks in
# legacy repair migrations cannot race with another migrator.
MIGRATION_LOCK_KEY = 8_731_004_300


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
        lock_acquired = False
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_lock(:lock_key)"), {"lock_key": MIGRATION_LOCK_KEY})
            lock_acquired = True
        try:
            context.configure(connection=connection, target_metadata=target_metadata)

            with context.begin_transaction():
                context.run_migrations()
        finally:
            if lock_acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": MIGRATION_LOCK_KEY},
                )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
