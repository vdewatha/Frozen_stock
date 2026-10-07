"""Preserve archived stock-paper ledgers across broker-account transitions."""
from alembic import op
import sqlalchemy as sa


revision = "0054_paper_account_transition"
down_revision = "0053_model_prediction_identity"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("stock_paper_accounts", recreate="auto") as batch_op:
        batch_op.drop_constraint("uq_stock_paper_account_broker", type_="unique")
        batch_op.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("archive_reason", sa.Text(), nullable=True))
        batch_op.create_index("ix_stock_paper_accounts_archived_at", ["archived_at"])


def downgrade():
    raise RuntimeError("0054 is forward-only; restore a verified backup to roll back")
