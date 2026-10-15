"""Persist immutable agent comparison report snapshots."""
from alembic import op
import sqlalchemy as sa


revision = "0046_agent_research_reports"
down_revision = "0045_agent_research_runs"
branch_labels = None
depends_on = None


def _create_table_if_missing(bind, table_name: str, *elements) -> None:
    if not sa.inspect(bind).has_table(table_name):
        table = sa.Table(table_name, sa.MetaData(), *elements)
        bind.execute(sa.schema.CreateTable(table, if_not_exists=True))


def _create_index_if_missing(bind, name: str, table_name: str, columns: list[str]) -> None:
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}
    if name not in indexes:
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    bind = op.get_bind()
    _create_table_if_missing(
        bind,
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
    _create_index_if_missing(
        bind,
        "ix_agent_research_reports_content_sha256",
        "agent_research_reports",
        ["content_sha256"],
    )
    _create_index_if_missing(
        bind,
        "ix_agent_research_reports_created_at",
        "agent_research_reports",
        ["created_at"],
    )
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("""
            CREATE OR REPLACE FUNCTION reject_agent_research_report_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'agent research reports are immutable';
            END;
            $$ LANGUAGE plpgsql
        """))
        op.execute(sa.text("""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'agent_research_reports_immutable'
                  AND tgrelid = 'agent_research_reports'::regclass
              ) THEN
                CREATE TRIGGER agent_research_reports_immutable
                BEFORE UPDATE OR DELETE ON agent_research_reports
                FOR EACH ROW EXECUTE FUNCTION reject_agent_research_report_mutation();
              END IF;
            END
            $$;
        """))
    elif op.get_bind().dialect.name == "sqlite":
        trigger_names = {
            row[0]
            for row in bind.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'agent_research_reports'"
            ))
        }
        for operation in ("UPDATE", "DELETE"):
            trigger_name = f"agent_research_reports_immutable_{operation.lower()}"
            if trigger_name not in trigger_names:
                op.execute(sa.text(f"""
                    CREATE TRIGGER {trigger_name}
                    BEFORE {operation} ON agent_research_reports
                    BEGIN
                      SELECT RAISE(ABORT, 'agent research reports are immutable');
                    END
                """))


def downgrade() -> None:
    raise RuntimeError("0046 is forward-only; restore a verified backup to roll back")
