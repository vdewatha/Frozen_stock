"""Versioned immutable session-evidence readiness reports."""
from alembic import op
import sqlalchemy as sa


revision = "0031_session_evidence_reports"
down_revision = "0030_stock_accounting_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stock_paper_promotion_readiness_reports",
        sa.Column("report_version", sa.Integer()),
    )
    op.add_column(
        "stock_paper_promotion_readiness_reports",
        sa.Column("as_of", sa.DateTime(timezone=True)),
    )
    # Nullable preserves old, immutable report rows exactly as they were.
    op.add_column(
        "stock_paper_promotion_readiness_reports",
        sa.Column("evidence", sa.JSON()),
    )
    op.create_index(
        "uq_stock_readiness_report_trial_version",
        "stock_paper_promotion_readiness_reports",
        ["trial_id", "report_version"],
        unique=True,
    )


def downgrade() -> None:
    raise RuntimeError("0031 is forward-only; restore a verified backup to roll back")