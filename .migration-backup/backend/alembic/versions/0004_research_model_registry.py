"""Immutable experimental research registration."""
from alembic import op
import sqlalchemy as sa

revision = "0004_research_model_registry"
down_revision = "0003_journal_scanner"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "research_model_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False, unique=True),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("eligible_for_trading", sa.Boolean(), nullable=False),
        sa.Column("training_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status = 'experimental'", name="ck_research_run_experimental"),
        sa.CheckConstraint("eligible_for_trading = false", name="ck_research_run_ineligible"),
    )


def downgrade():
    raise RuntimeError("0004 is forward-only; restore a verified backup to roll back")
