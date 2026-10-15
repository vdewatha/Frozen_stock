"""Versioned immutable session-evidence readiness reports."""
from alembic import op
import sqlalchemy as sa


revision = "0031_session_evidence_reports"
down_revision = "0030_stock_accounting_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Use database-native atomic guards here.  A publish can briefly have
        # more than one process running the same migration, so an inspector
        # check followed by ALTER TABLE can still race even when migrations
        # are serialized in the normal path.
        op.execute(sa.text(
            "ALTER TABLE stock_paper_promotion_readiness_reports "
            "ADD COLUMN IF NOT EXISTS report_version INTEGER"
        ))
        op.execute(sa.text(
            "ALTER TABLE stock_paper_promotion_readiness_reports "
            "ADD COLUMN IF NOT EXISTS as_of TIMESTAMPTZ"
        ))
        # Nullable preserves old, immutable report rows exactly as they were.
        op.execute(sa.text(
            "ALTER TABLE stock_paper_promotion_readiness_reports "
            "ADD COLUMN IF NOT EXISTS evidence JSONB"
        ))
        op.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_stock_readiness_report_trial_version "
            "ON stock_paper_promotion_readiness_reports (trial_id, report_version)"
        ))
    else:
        inspector = sa.inspect(bind)
        columns = {column["name"] for column in inspector.get_columns("stock_paper_promotion_readiness_reports")}
        for name, column_type in (
            ("report_version", sa.Integer()),
            ("as_of", sa.DateTime(timezone=True)),
        ):
            if name not in columns:
                op.add_column(
                    "stock_paper_promotion_readiness_reports",
                    sa.Column(name, column_type),
                )
        # Nullable preserves old, immutable report rows exactly as they were.
        if "evidence" not in columns:
            op.add_column(
                "stock_paper_promotion_readiness_reports",
                sa.Column("evidence", sa.JSON()),
            )
        op.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_stock_readiness_report_trial_version "
            "ON stock_paper_promotion_readiness_reports (trial_id, report_version)"
        ))


def downgrade() -> None:
    raise RuntimeError("0031 is forward-only; restore a verified backup to roll back")
