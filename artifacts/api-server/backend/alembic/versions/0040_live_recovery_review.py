"""Add durable live accounting-review fields."""
from alembic import op
import sqlalchemy as sa


revision = "0040_live_recovery_review"
down_revision = "0039_isolated_live_broker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("live_broker_accounts")}
    additions = (
        ("accounting_review_required", sa.Boolean()),
        ("accounting_reviewed_at", sa.DateTime(timezone=True)),
        ("accounting_reviewed_by", sa.String(length=128)),
        ("accounting_review_reason", sa.Text()),
        ("accounting_review_digest", sa.String(length=64)),
    )
    for name, column_type in additions:
        if name not in columns:
            op.add_column(
                "live_broker_accounts",
                sa.Column(name, column_type, nullable=False, server_default=sa.false())
                if name == "accounting_review_required"
                else sa.Column(name, column_type, nullable=True),
            )


def downgrade() -> None:
    raise RuntimeError("0040 is forward-only; restore a verified backup to roll back")