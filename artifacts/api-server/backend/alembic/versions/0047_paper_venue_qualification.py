"""Add account-specific paper venue qualification and activation records."""
from alembic import op
import sqlalchemy as sa


revision = "0047_paper_venue_qualification"
down_revision = "0046_agent_research_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_paper_venue_qualifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("account_id_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("qualification_version", sa.String(length=32), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("report_sha256", sa.String(length=64), nullable=False),
        sa.Column("reviewed_by", sa.String(length=128), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("report_sha256", name="uq_stock_paper_venue_qualification_report"),
    )
    op.create_index(
        "ix_stock_paper_venue_qualifications_provider",
        "stock_paper_venue_qualifications",
        ["provider"],
    )
    op.create_index(
        "ix_stock_paper_venue_qualifications_status",
        "stock_paper_venue_qualifications",
        ["status"],
    )
    op.create_table(
        "stock_paper_venue_authorizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("qualification_id", sa.Integer(), sa.ForeignKey("stock_paper_venue_qualifications.id"), nullable=False),
        sa.Column("authorized_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("authorization_sha256", sa.String(length=64), nullable=False),
        sa.Column("paper_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_authorized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("authorization_sha256", name="uq_stock_paper_venue_authorization"),
        sa.CheckConstraint("paper_only = true", name="ck_stock_paper_venue_authorization_paper_only"),
        sa.CheckConstraint("live_authorized = false", name="ck_stock_paper_venue_authorization_live_disabled"),
    )
    op.create_index(
        "ix_stock_paper_venue_authorizations_provider",
        "stock_paper_venue_authorizations",
        ["provider"],
    )
    op.create_index(
        "ix_stock_paper_venue_authorizations_qualification_id",
        "stock_paper_venue_authorizations",
        ["qualification_id"],
    )
    op.create_index(
        "ix_stock_paper_venue_authorizations_created_at",
        "stock_paper_venue_authorizations",
        ["created_at"],
    )


def downgrade() -> None:
    raise RuntimeError("0047 is forward-only; restore a verified backup to roll back")