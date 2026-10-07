"""Track authenticated refreshes without rewriting research observation time."""
from alembic import op
import sqlalchemy as sa


revision = "0055_intraday_verify"
down_revision = "0054_paper_account_transition"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("intraday_bars", recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    raise RuntimeError("0055 is forward-only; restore a verified backup to roll back")
