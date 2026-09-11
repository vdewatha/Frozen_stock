"""Remove interim registry foreign keys and normalize renamed holdout index."""
from alembic import op
import sqlalchemy as sa


# alembic_version.version_num is VARCHAR(32).
revision = "0012_stock_schema_fix"
down_revision = "0011_stock_training_repair"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("stock_holdout_reservations")}
    old = "ix_stock_holdout_reservations_horizon_bars"
    new = "ix_stock_holdout_reservations_horizon_days"
    if old in indexes:
        op.drop_index(old, table_name="stock_holdout_reservations")
    if new not in indexes:
        op.create_index(new, "stock_holdout_reservations", ["horizon_days"])

    # The interim tables pointed these columns at crypto research records.
    # 0011 preserved the rows by inserting explicit legacy registry entries;
    # remove that stale authority edge while retaining the new stock edge.
    for table, column in (
        ("stock_training_jobs", "result_run_id"),
        ("stock_paper_model_bindings", "model_run_id"),
    ):
        for fk in sa.inspect(bind).get_foreign_keys(table):
            if fk["referred_table"] == "research_model_runs" and fk["constrained_columns"] == [column]:
                op.drop_constraint(fk["name"], table, type_="foreignkey")


def downgrade():
    raise RuntimeError("0012 is forward-only; restore a verified backup to roll back")