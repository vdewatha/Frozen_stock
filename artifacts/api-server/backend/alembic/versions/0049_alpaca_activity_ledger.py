"""Explicit, non-qualifying Alpaca cash/activity reconciliation contract."""
from alembic import op
import sqlalchemy as sa

revision = "0049_alpaca_activity_ledger"
down_revision = "0048_online_research"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stock_paper_accounts", sa.Column("activity_contract", sa.String(32), nullable=False, server_default="legacy-v1"))
    op.add_column("stock_paper_accounts", sa.Column("activity_baseline", sa.JSON(), nullable=True))
    with op.batch_alter_table("stock_paper_broker_activities") as batch:
        batch.alter_column("occurred_at", existing_type=sa.DateTime(timezone=True), nullable=True)
        batch.add_column(sa.Column("normalized_payload", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("0049 is forward-only; restore a verified backup to roll back")
