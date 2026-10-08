"""Persist symbol/strategy research worker publication state."""
from alembic import op
import sqlalchemy as sa


revision = "0056_learning_worker_dispatch"
down_revision = "0055_intraday_verify"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
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
    op.create_index(
        "ix_stock_learning_worker_dispatches_batch_id",
        "stock_learning_worker_dispatches",
        ["batch_id"],
    )
    op.create_index(
        "ix_stock_learning_worker_dispatches_symbol",
        "stock_learning_worker_dispatches",
        ["symbol"],
    )
    op.create_index(
        "ix_stock_learning_worker_dispatches_strategy_slug",
        "stock_learning_worker_dispatches",
        ["strategy_slug"],
    )
    op.create_index(
        "ix_stock_learning_worker_dispatches_status",
        "stock_learning_worker_dispatches",
        ["status"],
    )


def downgrade():
    raise RuntimeError("0056 is forward-only; restore a verified backup to roll back")
