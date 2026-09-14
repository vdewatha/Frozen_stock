"""Ensure the scheduled-learning pause lookup index exists."""
from alembic import op
import sqlalchemy as sa


revision = "0036_learning_schedule_index"
down_revision = "0035_learning_schedule_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "stock_learning_schedule_control" not in sa.inspect(bind).get_table_names():
        return
    indexes = {
        item["name"]
        for item in sa.inspect(bind).get_indexes("stock_learning_schedule_control")
    }
    if "ix_stock_learning_schedule_control_paused" not in indexes:
        op.create_index(
            "ix_stock_learning_schedule_control_paused",
            "stock_learning_schedule_control",
            ["paused"],
        )


def downgrade() -> None:
    raise RuntimeError("0036 is forward-only; restore a verified backup to roll back")