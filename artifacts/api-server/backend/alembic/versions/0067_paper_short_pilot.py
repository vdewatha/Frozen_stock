"""Isolated development paper short pilot; no changes to long/live ledgers."""
from alembic import op
import sqlalchemy as sa

revision = "0067_paper_short_pilot"
down_revision = "0066_live_mandate"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("paper_short_pilot"):
        op.create_table(
            "paper_short_pilot",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_sha256", sa.String(64), nullable=False),
            sa.Column("symbols", sa.JSON(), nullable=False),
            sa.Column("max_order_notional", sa.Numeric(20, 8), nullable=False),
            sa.Column("max_gross_notional", sa.Numeric(20, 8), nullable=False),
            sa.Column("baseline_equity", sa.Numeric(20, 8), nullable=False),
            sa.Column("authorized_by", sa.String(128), nullable=False),
            sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.CheckConstraint("id = 1", name="ck_short_pilot_singleton"),
        )
    if not sa.inspect(bind).has_table("paper_short_orders"):
        op.create_table(
            "paper_short_orders",
            sa.Column("client_order_id", sa.String(64), primary_key=True),
            sa.Column("account_sha256", sa.String(64), nullable=False),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("intent", sa.String(16), nullable=False),
            sa.Column("quantity", sa.Numeric(21, 9), nullable=False),
            sa.Column("limit_price", sa.Numeric(20, 8), nullable=False),
            sa.Column("filled_quantity", sa.Numeric(21, 9), nullable=False),
            sa.Column("filled_avg_price", sa.Numeric(20, 8)),
            sa.Column("broker_order_id", sa.String(96)),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("active_slot", sa.Integer()),
            sa.Column("actor", sa.String(128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True)),
            sa.Column("broker_evidence", sa.JSON()),
            sa.UniqueConstraint("active_slot", name="uq_short_order_active_slot"),
            sa.UniqueConstraint("broker_order_id", name="uq_short_order_broker_id"),
            sa.CheckConstraint("quantity > 0 AND limit_price > 0", name="ck_short_order_positive"),
            sa.CheckConstraint("filled_quantity >= 0 AND filled_quantity <= quantity", name="ck_short_order_filled"),
            sa.CheckConstraint("intent IN ('open_short', 'cover_short')", name="ck_short_order_intent"),
            sa.CheckConstraint("active_slot IS NULL OR active_slot = 1", name="ck_short_order_slot"),
        )


def downgrade():
    op.drop_table("paper_short_orders")
    op.drop_table("paper_short_pilot")
