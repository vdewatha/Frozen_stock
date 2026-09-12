"""Durable governed stock learning-cycle evidence."""
from alembic import op
import sqlalchemy as sa


revision = "0026_stock_learning_cycle"
down_revision = ("0025_readiness_immutable", "0025_stock_paper_recovery")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_learning_cycles",
        sa.Column("cycle_id", sa.String(length=64), primary_key=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="blocked"),
        sa.Column("stage", sa.String(length=48), nullable=False, server_default="preflight"),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("symbols", sa.JSON(), nullable=False),
        sa.Column("cutoff_date", sa.Date(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False, server_default="42"),
        sa.Column("snapshot_id", sa.String(length=64), sa.ForeignKey("stock_dataset_snapshots.snapshot_id")),
        sa.Column("training_job_id", sa.String(length=36), sa.ForeignKey("stock_training_jobs.id")),
        sa.Column("model_run_id", sa.String(length=64), sa.ForeignKey("stock_model_registry.run_id")),
        sa.Column("trial_id", sa.String(length=36)),
        sa.Column("active_binding_id", sa.Integer()),
        sa.Column("monitor_snapshot_id", sa.Integer()),
        sa.Column("recovery_event_id", sa.Integer()),
        sa.Column("gates", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("last_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("request_sha256", name="uq_stock_learning_cycle_request"),
        sa.CheckConstraint(
            "status IN ('blocked', 'deferred', 'queued', 'running', "
            "'awaiting_forward_evidence', 'operator_review', 'complete', "
            "'demoted', 'rolled_back', 'failed')",
            name="ck_stock_learning_cycle_status",
        ),
    )
    op.create_index("ix_stock_learning_cycles_status", "stock_learning_cycles", ["status"])
    op.create_index("ix_stock_learning_cycles_stage", "stock_learning_cycles", ["stage"])
    op.create_index("ix_stock_learning_cycles_created_at", "stock_learning_cycles", ["created_at"])

    op.create_table(
        "stock_learning_cycle_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cycle_id", sa.String(length=64), sa.ForeignKey("stock_learning_cycles.cycle_id"), nullable=False),
        sa.Column("stage", sa.String(length=48), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("decision_sha256", sa.String(length=64), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("decision_sha256", name="uq_stock_learning_cycle_event_digest"),
        sa.CheckConstraint(
            "decision IN ('pass', 'fail', 'unknown', 'blocked', 'deferred', 'complete')",
            name="ck_stock_learning_cycle_event_decision",
        ),
    )
    op.create_index("ix_stock_learning_cycle_events_cycle_id", "stock_learning_cycle_events", ["cycle_id"])
    op.create_index("ix_stock_learning_cycle_events_stage", "stock_learning_cycle_events", ["stage"])
    op.create_index("ix_stock_learning_cycle_events_created_at", "stock_learning_cycle_events", ["created_at"])

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION stock_learning_cycle_event_immutable_row() "
            "RETURNS trigger AS $$ BEGIN RAISE EXCEPTION "
            "'stock_learning_cycle_events is immutable'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER stock_learning_cycle_events_immutable "
            "BEFORE UPDATE OR DELETE ON stock_learning_cycle_events "
            "FOR EACH ROW EXECUTE FUNCTION stock_learning_cycle_event_immutable_row()"
        )
    elif bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER stock_learning_cycle_events_immutable_update "
            "BEFORE UPDATE ON stock_learning_cycle_events BEGIN SELECT RAISE(ABORT, "
            "'stock_learning_cycle_events is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER stock_learning_cycle_events_immutable_delete "
            "BEFORE DELETE ON stock_learning_cycle_events BEGIN SELECT RAISE(ABORT, "
            "'stock_learning_cycle_events is immutable'); END"
        )


def downgrade() -> None:
    raise RuntimeError("0026 is forward-only; restore a verified backup to roll back")