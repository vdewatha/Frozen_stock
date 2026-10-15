"""Persist symbol/strategy research worker publication state."""
from alembic import op
import sqlalchemy as sa


revision = "0056_learning_worker_dispatch"
down_revision = "0055_intraday_verify"
branch_labels = None
depends_on = None


def _create_table_if_missing(bind, table_name: str, *elements) -> None:
    if not sa.inspect(bind).has_table(table_name):
        metadata = sa.MetaData()
        for element in elements:
            foreign_keys = getattr(element, "foreign_keys", ())
            for foreign_key in foreign_keys:
                target_table, target_column = foreign_key.target_fullname.rsplit(".", 1)
                if target_table not in metadata.tables:
                    sa.Table(target_table, metadata, sa.Column(target_column, sa.Integer()))
        table = sa.Table(table_name, metadata, *elements)
        bind.execute(sa.schema.CreateTable(table, if_not_exists=True))


def _create_index_if_missing(bind, name: str, table_name: str, columns: list[str]) -> None:
    if name not in {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}:
        op.create_index(name, table_name, columns)


def upgrade():
    bind = op.get_bind()
    _create_table_if_missing(
        bind,
        "stock_learning_worker_dispatches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("strategy_slug", sa.String(length=64), nullable=False),
        sa.Column("max_candidates", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="planned"),
        sa.Column("task_id", sa.String(length=100), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "symbol", "strategy_slug", name="uq_stock_learning_worker_scope"),
    )
    _create_index_if_missing(
        bind,
        "ix_stock_learning_worker_dispatches_batch_id",
        "stock_learning_worker_dispatches",
        ["batch_id"],
    )
    _create_index_if_missing(
        bind,
        "ix_stock_learning_worker_dispatches_symbol",
        "stock_learning_worker_dispatches",
        ["symbol"],
    )
    _create_index_if_missing(
        bind,
        "ix_stock_learning_worker_dispatches_strategy_slug",
        "stock_learning_worker_dispatches",
        ["strategy_slug"],
    )
    _create_index_if_missing(
        bind,
        "ix_stock_learning_worker_dispatches_status",
        "stock_learning_worker_dispatches",
        ["status"],
    )


def downgrade():
    raise RuntimeError("0056 is forward-only; restore a verified backup to roll back")
