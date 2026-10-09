"""Match scalp research timestamps to the timezone-aware ORM definition."""
from alembic import op
import sqlalchemy as sa


revision = "0060_scalp_research_timezone"
down_revision = "0059_scalp_research_runs"
branch_labels = None
depends_on = None


def upgrade():
    for column in ("started_at", "completed_at", "created_at"):
        op.alter_column(
            "scalp_research_runs",
            column,
            existing_type=sa.DateTime(timezone=False),
            type_=sa.DateTime(timezone=True),
            existing_nullable=column != "created_at",
            postgresql_using=f"{column} AT TIME ZONE 'UTC'",
        )


def downgrade():
    raise RuntimeError("Scalp research timezone migration is forward-only.")
