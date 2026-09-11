"""Ensure repaired legacy registry rows are append-only as well."""
from alembic import op
import sqlalchemy as sa


revision = "0013_stock_registry_immutable"
down_revision = "0012_stock_schema_fix"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        exists = bind.execute(sa.text(
            "SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass('stock_model_registry') "
            "AND tgname = 'stock_model_registry_immutable' AND NOT tgisinternal"
        )).scalar()
        if not exists:
            # The function is present on both the fresh 0010 and interim
            # installation paths; CREATE OR REPLACE makes recovery robust if
            # an earlier partial deployment omitted it.
            op.execute(
                "CREATE OR REPLACE FUNCTION stock_training_immutable_row() RETURNS trigger AS "
                "$$ BEGIN RAISE EXCEPTION 'stock training audit rows are immutable'; END; $$ LANGUAGE plpgsql"
            )
            op.execute(
                "CREATE TRIGGER stock_model_registry_immutable BEFORE UPDATE OR DELETE "
                "ON stock_model_registry FOR EACH ROW EXECUTE FUNCTION stock_training_immutable_row()"
            )
    elif bind.dialect.name == "sqlite":
        exists = bind.execute(sa.text(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='stock_model_registry_immutable_update'"
        )).scalar()
        if not exists:
            op.execute(
                "CREATE TRIGGER stock_model_registry_immutable_update BEFORE UPDATE ON stock_model_registry "
                "BEGIN SELECT RAISE(ABORT, 'stock_model_registry is immutable'); END"
            )
            op.execute(
                "CREATE TRIGGER stock_model_registry_immutable_delete BEFORE DELETE ON stock_model_registry "
                "BEGIN SELECT RAISE(ABORT, 'stock_model_registry is immutable'); END"
            )


def downgrade():
    raise RuntimeError("0013 is forward-only; restore a verified backup to roll back")