"""Add leases for durable learning worker dispatches."""
from alembic import op
import sqlalchemy as sa


revision = "0057_learning_worker_leases"
down_revision = "0056_learning_worker_dispatch"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Multiple published workers can start together. PostgreSQL's atomic
        # IF NOT EXISTS clauses prevent concurrent migration attempts from
        # racing between inspection and DDL execution.
        op.execute(
            sa.text(
                "ALTER TABLE stock_learning_worker_dispatches "
                "ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP WITH TIME ZONE"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE stock_learning_worker_dispatches "
                "ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP WITH TIME ZONE"
            )
        )
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_stock_learning_worker_dispatches_lease_expires_at "
                "ON stock_learning_worker_dispatches (lease_expires_at)"
            )
        )
        return
    inspector = sa.inspect(bind)
    columns = {
        column["name"]
        for column in inspector.get_columns("stock_learning_worker_dispatches")
    }
    if "claimed_at" not in columns:
        op.add_column(
            "stock_learning_worker_dispatches",
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "lease_expires_at" not in columns:
        op.add_column(
            "stock_learning_worker_dispatches",
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "ix_stock_learning_worker_dispatches_lease_expires_at" not in {
        index["name"] for index in sa.inspect(bind).get_indexes("stock_learning_worker_dispatches")
    }:
        op.create_index(
            "ix_stock_learning_worker_dispatches_lease_expires_at",
            "stock_learning_worker_dispatches",
            ["lease_expires_at"],
        )


def downgrade():
    raise RuntimeError("0057 is forward-only; restore a verified backup to roll back")
