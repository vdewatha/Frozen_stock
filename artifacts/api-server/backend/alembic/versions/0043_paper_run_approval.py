"""Add immutable paper-run handoff approvals."""
from alembic import op
import sqlalchemy as sa


revision = "0043_paper_run_approval"
down_revision = "0042_intraday_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_paper_run_approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(length=64),
            sa.ForeignKey("stock_learning_cycles.cycle_id"),
            nullable=False,
        ),
        sa.Column("environment", sa.String(length=16), nullable=False, server_default="paper"),
        sa.Column("execution_provider", sa.String(length=64), nullable=False),
        sa.Column("provider_switch", sa.JSON()),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("exposure_limits", sa.JSON(), nullable=False),
        sa.Column("duration_sessions", sa.Integer(), nullable=False),
        sa.Column("schedule", sa.JSON(), nullable=False),
        sa.Column("stop_conditions", sa.JSON(), nullable=False),
        sa.Column("approving_actors", sa.JSON(), nullable=False),
        sa.Column("approval_sha256", sa.String(length=64), nullable=False),
        sa.Column("paper_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_authorized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("environment = 'paper'", name="ck_stock_paper_approval_environment"),
        sa.CheckConstraint("paper_only = true", name="ck_stock_paper_approval_paper_only"),
        sa.CheckConstraint("live_authorized = false", name="ck_stock_paper_approval_live_disabled"),
        sa.UniqueConstraint("approval_sha256", name="uq_stock_paper_run_approval_digest"),
    )
    op.create_index(
        "ix_stock_paper_run_approvals_cycle_id",
        "stock_paper_run_approvals",
        ["cycle_id"],
    )
    op.create_index(
        "ix_stock_paper_run_approvals_created_at",
        "stock_paper_run_approvals",
        ["created_at"],
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_stock_paper_run_approval_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'stock paper run approvals are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        ))
        op.execute(sa.text(
            """
            CREATE TRIGGER stock_paper_run_approvals_immutable
            BEFORE UPDATE OR DELETE ON stock_paper_run_approvals
            FOR EACH ROW EXECUTE FUNCTION reject_stock_paper_run_approval_mutation()
            """
        ))
    elif bind.dialect.name == "sqlite":
        op.execute(sa.text(
            """
            CREATE TRIGGER stock_paper_run_approvals_immutable_update
            BEFORE UPDATE ON stock_paper_run_approvals
            BEGIN
              SELECT RAISE(ABORT, 'stock paper run approvals are immutable');
            END
            """
        ))
        op.execute(sa.text(
            """
            CREATE TRIGGER stock_paper_run_approvals_immutable_delete
            BEFORE DELETE ON stock_paper_run_approvals
            BEGIN
              SELECT RAISE(ABORT, 'stock paper run approvals are immutable');
            END
            """
        ))


def downgrade() -> None:
    raise RuntimeError("0043 is forward-only; restore a verified backup to roll back")