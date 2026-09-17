"""Persist immutable agent comparison report snapshots."""
from alembic import op
import sqlalchemy as sa


revision = "0046_agent_research_reports"
down_revision = "0045_agent_research_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_research_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("report_id", sa.String(length=36), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("eligible_for_trading", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "eligible_for_trading = false",
            name="ck_agent_research_report_ineligible",
        ),
        sa.UniqueConstraint("report_id", name="uq_agent_research_report_id"),
        sa.UniqueConstraint("content_sha256", name="uq_agent_research_report_content"),
    )
    op.create_index(
        "ix_agent_research_reports_content_sha256",
        "agent_research_reports",
        ["content_sha256"],
    )
    op.create_index(
        "ix_agent_research_reports_created_at",
        "agent_research_reports",
        ["created_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("""
            CREATE FUNCTION reject_agent_research_report_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'agent research reports are immutable';
            END;
            $$ LANGUAGE plpgsql
        """))
        op.execute(sa.text("""
            CREATE TRIGGER agent_research_reports_immutable
            BEFORE UPDATE OR DELETE ON agent_research_reports
            FOR EACH ROW EXECUTE FUNCTION reject_agent_research_report_mutation()
        """))
    elif op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(sa.text(f"""
                CREATE TRIGGER agent_research_reports_immutable_{operation.lower()}
                BEFORE {operation} ON agent_research_reports
                BEGIN
                  SELECT RAISE(ABORT, 'agent research reports are immutable');
                END
            """))


def downgrade() -> None:
    raise RuntimeError("0046 is forward-only; restore a verified backup to roll back")