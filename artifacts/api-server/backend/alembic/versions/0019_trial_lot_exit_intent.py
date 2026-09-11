"""Durable post-close trial lot exit intent."""
from alembic import op
import sqlalchemy as sa

revision = "0019_trial_lot_exit_intent"
down_revision = "0018_stock_trial_lots"
branch_labels = None
depends_on = None

def upgrade() -> None:
    with op.batch_alter_table("stock_paper_trial_lots") as batch:
        batch.add_column(sa.Column("exit_reason", sa.String(24)))
        batch.add_column(sa.Column("exit_decided_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("exit_reference_price", sa.Numeric(20, 8)))

def downgrade() -> None:
    with op.batch_alter_table("stock_paper_trial_lots") as batch:
        batch.drop_column("exit_reference_price")
        batch.drop_column("exit_decided_at")
        batch.drop_column("exit_reason")