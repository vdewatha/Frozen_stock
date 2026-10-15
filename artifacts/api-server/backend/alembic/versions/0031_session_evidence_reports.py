"""Versioned immutable session-evidence readiness reports."""
from alembic import op
import sqlalchemy as sa


revision = "0031_session_evidence_reports"
down_revision = "0030_stock_accounting_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
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
    # PostgreSQL's inspector does not consistently expose an index that was
    # created by an existing ORM index/constraint.  Use native idempotency so
    # a publish-time schema pre-creation cannot block the migration.
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_stock_readiness_report_trial_version "
            "ON stock_paper_promotion_readiness_reports (trial_id, report_version)"
        ))
    else:
        op.execute(sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_stock_readiness_report_trial_version "
            "ON stock_paper_promotion_readiness_reports (trial_id, report_version)"
        ))


def downgrade() -> None:
    raise RuntimeError("0031 is forward-only; restore a verified backup to roll back")
