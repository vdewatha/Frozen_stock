"""Observed provider exits without inventing model orders."""
from alembic import op
import sqlalchemy as sa

revision = "0008_external_paper_fills"
down_revision = "0007_paper_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("paper_external_fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entry_intent_id", sa.Integer(), sa.ForeignKey("paper_order_intents.id"), nullable=False),
        sa.Column("provider_order_id", sa.String(128), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("cost", sa.BigInteger(), nullable=False),
        sa.Column("fee", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))


def downgrade():
    raise RuntimeError("forward-only paper audit migration; restore a verified backup")
