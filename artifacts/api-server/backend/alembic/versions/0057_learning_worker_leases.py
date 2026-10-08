"""Add leases for durable learning worker dispatches."""
from alembic import op
import sqlalchemy as sa


revision = "0057_learning_worker_leases"
down_revision = "0056_learning_worker_dispatch"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stock_learning_worker_dispatches", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("stock_learning_worker_dispatches", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_stock_learning_worker_dispatches_lease_expires_at",
        "stock_learning_worker_dispatches",
        ["lease_expires_at"],
    )


def downgrade():
    raise RuntimeError("0057 is forward-only; restore a verified backup to roll back")
