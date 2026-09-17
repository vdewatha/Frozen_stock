"""Persist bounded TradingAgents shadow research runs."""
from alembic import op
import sqlalchemy as sa


revision = "0045_agent_research_runs"
down_revision = "0044_exact_paper_session_bounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_research_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("eligible_for_trading", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("framework_version", sa.String(length=32), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("evaluation", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'unavailable')",
            name="ck_agent_research_run_status",
        ),
        sa.CheckConstraint("eligible_for_trading = false", name="ck_agent_research_run_ineligible"),
        sa.UniqueConstraint("run_id", name="uq_agent_research_run_run_id"),
        sa.UniqueConstraint("dedupe_key", name="uq_agent_research_run_dedupe"),
    )
    op.create_index("ix_agent_research_runs_status", "agent_research_runs", ["status"])
    op.create_index("ix_agent_research_runs_symbol", "agent_research_runs", ["symbol"])
    op.create_index("ix_agent_research_runs_created_at", "agent_research_runs", ["created_at"])


def downgrade() -> None:
    raise RuntimeError("0045 is forward-only; restore a verified backup to roll back")