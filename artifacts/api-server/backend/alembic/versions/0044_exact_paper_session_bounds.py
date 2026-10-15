"""Persist exact one-session paper-run bounds."""
from alembic import op
import sqlalchemy as sa


revision = "0044_exact_paper_session_bounds"
down_revision = "0043_paper_run_approval"
branch_labels = None
depends_on = None


def _add_column_if_missing(bind, table_name: str, column: sa.Column) -> None:
    columns = {item["name"] for item in sa.inspect(bind).get_columns(table_name)}
    if column.name not in columns:
        op.add_column(table_name, column)


def _create_index_if_missing(bind, name: str, table_name: str, columns: list[str]) -> None:
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
    if name not in indexes:
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    bind = op.get_bind()
    _add_column_if_missing(
        bind,
        "stock_paper_run_approvals",
        sa.Column("loss_limits", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    _add_column_if_missing(
        bind,
        "stock_paper_run_approvals",
        sa.Column("stop_authority", sa.String(length=64), nullable=False, server_default="operator_and_system"),
    )
    _add_column_if_missing(
        bind,
        "stock_paper_run_approvals",
        sa.Column("pending_order_treatment", sa.String(length=32), nullable=False, server_default="cancel"),
    )
    _add_column_if_missing(
        bind,
        "stock_paper_run_approvals",
        sa.Column("remaining_position_policy", sa.String(length=32), nullable=False, server_default="hold"),
    )
    _add_column_if_missing(
        bind,
        "stock_paper_orders",
        sa.Column("trial_id", sa.String(length=36), nullable=True),
    )
    _create_index_if_missing(bind, "ix_stock_paper_orders_trial_id", "stock_paper_orders", ["trial_id"])


def downgrade() -> None:
    raise RuntimeError("0044 is forward-only; restore a verified backup to roll back")
