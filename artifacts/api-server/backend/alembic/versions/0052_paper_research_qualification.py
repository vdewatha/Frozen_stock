"""Separate paper-research qualification from legacy complete-fee admission."""
from alembic import op
import sqlalchemy as sa

revision = "0052_paper_research_qual"
down_revision = "0051_paper_cash_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("stock_paper_research_qualifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("stock_paper_accounts.id"), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("report_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("reviewed_by", sa.String(128), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_stock_paper_research_qualifications_account_id", "stock_paper_research_qualifications", ["account_id"])
    op.create_table("stock_paper_research_authorizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("qualification_id", sa.Integer(), sa.ForeignKey("stock_paper_research_qualifications.id"), nullable=False),
        sa.Column("authorized_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("authorization_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("paper_only", sa.Boolean(), nullable=False),
        sa.Column("live_authorized", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("paper_only = true", name="ck_research_authorization_paper_only"),
        sa.CheckConstraint("live_authorized = false", name="ck_research_authorization_live_disabled"))
    op.create_index("ix_stock_paper_research_authorizations_qualification_id", "stock_paper_research_authorizations", ["qualification_id"])


def downgrade():
    raise RuntimeError("0052 is forward-only; restore a verified backup to roll back")
