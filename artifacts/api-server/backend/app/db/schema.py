"""Read-only schema checks. Never create, stamp or upgrade at application startup."""
from pathlib import Path

from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.db.base import Base
from app.models import models, stock_paper  # noqa: F401


def migration_config() -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return config


def assert_schema_current(
    engine,
    *,
    require_migration_head: bool = True,
    check_drift: bool = True,
) -> None:
    """Fail closed on structural drift.

    Replit Publish owns managed production schema application, so its database
    can be structurally current while retaining an older Alembic revision
    marker. Local and standalone jobs still require the exact migration head.
    """
    expected = set(ScriptDirectory.from_config(migration_config()).get_heads())
    with engine.connect() as connection:
        actual = set(MigrationContext.configure(connection).get_current_heads())
        if require_migration_head and actual != expected:
            raise RuntimeError("Database migration required: run alembic upgrade head before startup")
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        # Avoid dependency sorting here. The paper schema contains intentional
        # cycles between audit tables, and sorting emits warnings while adding
        # no value to a presence/column check.
        for table in Base.metadata.tables.values():
            if table.name not in tables:
                raise RuntimeError(f"Database schema missing table: {table.name}")
            columns = {c["name"] for c in inspector.get_columns(table.name)}
            if missing := set(table.columns.keys()) - columns:
                raise RuntimeError(f"Database schema missing columns: {table.name}: {sorted(missing)}")
        # Ignore server defaults: historical migrations use DB defaults while
        # several ORM fields intentionally use application-side defaults.
        if check_drift:
            drift = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            if drift:
                raise RuntimeError("Database schema differs from ORM metadata; inspect with alembic check")


def assert_database_reachable(engine) -> None:
    """Run the bounded production boot probe after the migration process exits.

    Hosted startup already runs ``alembic upgrade head`` as a supervised child.
    Repeating a full ORM reflection here can take minutes on managed Postgres
    and prevents the API health endpoint from becoming ready. The migration
    exit code is the structural check; this probe verifies the application can
    open and use its database connection before serving requests.
    """
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


if __name__ == "__main__":
    from app.db.session import engine
    assert_schema_current(engine)
