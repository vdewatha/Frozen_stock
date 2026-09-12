"""Finalize the tamper-evident audit digest column."""
from alembic import op
import sqlalchemy as sa

revision = "0028_audit_not_null"
down_revision = "0027_audit_event_chain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("audit_logs") as batch:
        batch.alter_column("event_sha256", existing_type=sa.String(length=64), nullable=False)


def downgrade() -> None:
    raise RuntimeError("0028 is forward-only; restore a verified backup to roll back")