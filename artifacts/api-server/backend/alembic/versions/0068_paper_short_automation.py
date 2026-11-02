"""Explicit standing automation authority for the isolated paper pilot."""
from alembic import op
import sqlalchemy as sa

revision = "0068_paper_short_automation"
down_revision = "0067_paper_short_pilot"
branch_labels = None
depends_on = None


def upgrade():
    if "automated" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("paper_short_pilot")}:
        op.add_column("paper_short_pilot", sa.Column("automated", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("paper_short_pilot") as batch:
        batch.drop_column("automated")
