"""Read-only schema checks. Never create, stamp or upgrade at application startup."""
from pathlib import Path

from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.db.base import Base
from app.models import models, stock_paper  # noqa: F401


def migration_config() -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return config


def assert_schema_current(engine) -> None:
    expected = set(ScriptDirectory.from_config(migration_config()).get_heads())
    with engine.connect() as connection:
        actual = set(MigrationContext.configure(connection).get_current_heads())
        if actual != expected:
            raise RuntimeError("Database migration required: run alembic upgrade head before startup")
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                raise RuntimeError(f"Database schema missing table: {table.name}")
            columns = {c["name"] for c in inspector.get_columns(table.name)}
            if missing := set(table.columns.keys()) - columns:
                raise RuntimeError(f"Database schema missing columns: {table.name}: {sorted(missing)}")
        # Ignore server defaults: historical migrations use DB defaults while
        # several ORM fields intentionally use application-side defaults.
        drift = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        if drift:
            raise RuntimeError("Database schema differs from ORM metadata; inspect with alembic check")


if __name__ == "__main__":
    from app.db.session import engine
    assert_schema_current(engine)
