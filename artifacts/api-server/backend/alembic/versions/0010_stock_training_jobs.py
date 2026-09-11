"""Durable immutable stock training jobs, registry, holdout reservations."""
from alembic import op
import sqlalchemy as sa

revision = "0010_stock_training_jobs"
down_revision = "0009_intraday_alpaca"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stock_dataset_snapshots",
        sa.Column("snapshot_id", sa.String(64), primary_key=True),
        sa.Column("dataset_sha256", sa.String(64), nullable=False, index=True),
        sa.Column("cutoff_date", sa.Date(), nullable=False),
        sa.Column("universe", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("feature_config_id", sa.String(128), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "stock_holdout_reservations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("universe_key", sa.String(64), nullable=False, index=True),
        sa.Column("universe", sa.JSON(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False, index=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("stock_dataset_snapshots.snapshot_id"), nullable=False),
        sa.Column("reserved_by_job_id", sa.String(36), unique=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("snapshot_id", "horizon_days", name="uq_stock_holdout_snapshot_horizon"),
    )
    op.create_table(
        "stock_model_registry",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("stock_dataset_snapshots.snapshot_id"), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("training_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "stock_training_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dedupe_key", sa.String(64), nullable=False, index=True),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("requested_by", sa.String(32), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("stock_dataset_snapshots.snapshot_id"), nullable=False),
        sa.Column("holdout_reservation_id", sa.Integer(), sa.ForeignKey("stock_holdout_reservations.id")),
        sa.Column("celery_task_id", sa.String(64), unique=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queue_error", sa.Text()),
        sa.Column("failure_code", sa.String(96)),
        sa.Column("failure_detail", sa.Text()),
        sa.Column("result_run_id", sa.String(64), sa.ForeignKey("stock_model_registry.run_id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("recovery_attempted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("dedupe_key", name="uq_stock_training_job_dedupe"),
        sa.CheckConstraint("status IN ('queued', 'running', 'cancel_requested', 'cancelled', 'succeeded', 'failed', 'deferred')", name="ck_stock_training_job_status"),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 3", name="ck_stock_training_job_attempts"),
        sa.CheckConstraint("delivery_attempts >= 0 AND delivery_attempts <= 3", name="ck_stock_training_job_deliveries"),
    )
    op.create_table(
        "stock_paper_model_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_run_id", sa.String(64), sa.ForeignKey("stock_model_registry.run_id"), nullable=False),
        sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("stock_dataset_snapshots.snapshot_id"), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("paper_only", sa.Boolean(), nullable=False),
        sa.Column("live_authorized", sa.Boolean(), nullable=False),
        sa.Column("bound_by", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("paper_only = true", name="ck_stock_binding_paper_only"),
        sa.CheckConstraint("live_authorized = false", name="ck_stock_binding_live_disabled"),
        sa.UniqueConstraint("binding_sha256", name="uq_stock_binding_digest"),
    )
    immutable = ("stock_dataset_snapshots", "stock_holdout_reservations", "stock_model_registry", "stock_paper_model_bindings")
    if op.get_bind().dialect.name == "sqlite":
        for table in immutable:
            op.execute(f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END")
            op.execute(f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END")
    else:
        op.execute("CREATE FUNCTION stock_training_immutable_row() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'stock training audit rows are immutable'; END; $$ LANGUAGE plpgsql")
        for table in immutable:
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION stock_training_immutable_row()")


def downgrade():
    raise RuntimeError("0010 is forward-only; restore a verified backup to roll back")