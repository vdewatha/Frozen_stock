"""Add the operator pause for scheduled paper learning decisions."""
from alembic import op
import sqlalchemy as sa


revision = "0035_learning_schedule_control"
down_revision = "0034_scheduled_trial_handoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "stock_learning_schedule_control" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "stock_learning_schedule_control",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("pause_reason", sa.Text()),
            sa.Column("updated_by", sa.String(length=128), nullable=False, server_default="system"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "id = 1",
                name="ck_stock_learning_schedule_control_singleton",
            ),
        )
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
    raise RuntimeError("0035 is forward-only; restore a verified backup to roll back")